"""Database behaviour of the NetBox webhook path.

What matters here is what happens to a burst of webhooks: that they fold into
one pending record, that one sync starts for the whole burst, and that a source
with an update already in flight is left alone rather than having a second
queued behind the first.
"""

import pytest

from django.utils import timezone

from ascender.main.models.inventory import InventorySource, InventoryUpdate
from ascender.main.netbox import webhook


@pytest.fixture
def netbox_source(inventory):
    return InventorySource.objects.create(name='netbox-src', inventory=inventory, source='netbox')


def settle(source, seconds_ago=600):
    """Backdate the pending burst so it reads as quiet."""
    moment = (timezone.now() - timezone.timedelta(seconds=seconds_ago)).isoformat()
    source.refresh_from_db()
    state = dict(source.source_sync_state or {})
    state[webhook.STATE_KEY]['last_event_at'] = moment
    InventorySource.objects.filter(pk=source.pk).update(source_sync_state=state)
    source.refresh_from_db()


@pytest.mark.django_db
def test_many_webhooks_fold_into_one_burst(netbox_source):
    for i in range(12):
        webhook.record_event(netbox_source, 'dcim.device', [400 + i])

    netbox_source.refresh_from_db()
    pending = netbox_source.source_sync_state[webhook.STATE_KEY]
    assert pending['events'] == 12
    assert pending['change_id'] == 411  # the highest seen, not the last
    assert pending['object_types'] == ['dcim.device']
    assert pending['dirty_since'] <= pending['last_event_at']


@pytest.mark.django_db
def test_the_burst_remembers_every_object_type(netbox_source):
    webhook.record_event(netbox_source, 'dcim.device', [1])
    webhook.record_event(netbox_source, 'extras.configcontext', [2])
    netbox_source.refresh_from_db()
    assert netbox_source.source_sync_state[webhook.STATE_KEY]['object_types'] == ['dcim.device', 'extras.configcontext']


@pytest.mark.django_db
def test_an_unsettled_burst_is_left_alone(netbox_source):
    webhook.record_event(netbox_source, 'dcim.device', [1])
    assert webhook.launch_settled_syncs() == []
    netbox_source.refresh_from_db()
    assert webhook.STATE_KEY in netbox_source.source_sync_state


@pytest.mark.django_db
def test_a_settled_burst_starts_one_sync(netbox_source):
    for i in range(30):
        webhook.record_event(netbox_source, 'dcim.device', [i])
    settle(netbox_source)

    launched = webhook.launch_settled_syncs()

    assert len(launched) == 1
    assert launched[0].launch_type == 'webhook'
    assert InventoryUpdate.objects.filter(inventory_source=netbox_source).count() == 1
    netbox_source.refresh_from_db()
    assert webhook.STATE_KEY not in netbox_source.source_sync_state


@pytest.mark.django_db
def test_nothing_is_queued_behind_a_sync_already_in_flight(netbox_source):
    """A second update would just wait on the first and then repeat its work."""
    netbox_source.create_inventory_update(_eager_fields={'launch_type': 'manual', 'status': 'running'})
    webhook.record_event(netbox_source, 'dcim.device', [1])
    settle(netbox_source)

    assert webhook.launch_settled_syncs() == []
    assert InventoryUpdate.objects.filter(inventory_source=netbox_source, launch_type='webhook').count() == 0


@pytest.mark.django_db
def test_the_burst_survives_being_deferred(netbox_source):
    """The in-flight update may have started before the change landed, so the
    burst is kept until a sync is actually launched for it."""
    running = netbox_source.create_inventory_update(_eager_fields={'launch_type': 'manual', 'status': 'running'})
    webhook.record_event(netbox_source, 'dcim.device', [1])
    settle(netbox_source)
    webhook.launch_settled_syncs()

    netbox_source.refresh_from_db()
    assert webhook.STATE_KEY in netbox_source.source_sync_state

    running.status = 'successful'
    running.save(update_fields=['status'])
    assert len(webhook.launch_settled_syncs()) == 1


@pytest.mark.django_db
def test_sources_of_other_types_are_not_touched(inventory):
    other = InventorySource.objects.create(name='ec2-src', inventory=inventory, source='ec2')
    webhook.record_event(other, 'dcim.device', [1])
    settle(other)
    assert webhook.launch_settled_syncs() == []


@pytest.mark.django_db
def test_clearing_leaves_the_rest_of_the_state_alone(netbox_source):
    """The incremental sync's changelog position lives in the same field."""
    InventorySource.objects.filter(pk=netbox_source.pk).update(source_sync_state={'changelog_id': 209, 'skips': 3})
    netbox_source.refresh_from_db()
    webhook.record_event(netbox_source, 'dcim.device', [1])
    webhook.clear_pending(netbox_source.pk)

    netbox_source.refresh_from_db()
    assert netbox_source.source_sync_state == {'changelog_id': 209, 'skips': 3}


@pytest.mark.django_db
def test_a_sync_writing_its_position_does_not_drop_a_pending_burst(netbox_source):
    """The mirror of test_clearing_leaves_the_rest_of_the_state_alone.

    A webhook that lands while a sync is fetching records a burst in the same
    column the sync writes its changelog position to. Writing the position
    over the top would drop the burst, and the change behind it would then
    wait for a schedule rather than syncing once this run finished.
    """
    from ascender.main.management.commands.netbox_inventory_sync import Command
    from ascender.main.netbox import changelog

    InventorySource.objects.filter(pk=netbox_source.pk).update(source_sync_state={'changelog_id': 209, 'skips': 3})
    netbox_source.refresh_from_db()

    # the webhook arrives mid-run
    webhook.record_event(netbox_source, 'dcim.device', [1])

    class FakeClient:
        base_url = 'https://netbox.example/'

    stored = Command()._store(netbox_source, FakeClient(), changelog.Decision(fetch=True, watermark=512, reason='changes'))

    netbox_source.refresh_from_db()
    assert stored['changelog_id'] == 512
    assert stored['skips'] == 0
    assert webhook.STATE_KEY in netbox_source.source_sync_state
    assert netbox_source.source_sync_state[webhook.STATE_KEY]['object_types'] == ['dcim.device']
