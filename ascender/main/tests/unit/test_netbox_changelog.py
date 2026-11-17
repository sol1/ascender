"""Unit tests for the incremental sync gate.

Every case here is about one asymmetry: a needless fetch costs time, while a
needless skip leaves the inventory wrong and the job green. So the tests assert
that each uncertainty resolves towards fetching.
"""

import pytest

from django.core.cache import cache

from ascender.main.netbox import changelog
from ascender.main.netbox import sync


def change(change_id, object_type='dcim.device'):
    return {'id': change_id, 'changed_object_type': object_type}


class FakeClient:
    """A changelog and an object type list, served the way NetBox serves them."""

    def __init__(self, changes=None, object_types=None, base_url='https://netbox.example/'):
        self.base_url = base_url
        self.version = '4.7.5'
        self.changes = list(changes or [])
        self.object_types = object_types
        self.requests = []

    def get(self, path, params=None):
        params = dict(params or {})
        self.requests.append((path, params))
        assert path == changelog.CHANGELOG_PATH, path
        rows = self.changes
        if params.get('id__gt') is not None:
            rows = [r for r in rows if r['id'] > params['id__gt']]
        wanted = params.get('changed_object_type')
        if wanted:
            rows = [r for r in rows if r['changed_object_type'] in wanted]
        rows = sorted(rows, key=lambda r: r['id'], reverse=params.get('ordering') == '-id')
        return {'count': len(rows), 'results': rows[: params.get('limit', 50)]}

    def paginate(self, path, params=None):
        assert path == changelog.OBJECT_TYPES_PATH, path
        names = sync.WATCHED_OBJECT_TYPES if self.object_types is None else self.object_types
        for name in names:
            app_label, model = name.split('.')
            yield {'app_label': app_label, 'model': model}


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


# -- the watch list ---------------------------------------------------------


def test_every_groupable_key_names_an_object_type():
    """Adding a grouping without an object type would make an incremental sync
    blind to changes in it, so this fails the build instead of the inventory."""
    assert set(sync.GROUPABLE_OBJECT_TYPES) == {key for key, _ in sync.GROUPABLE}


def test_every_site_hierarchy_names_an_object_type():
    assert set(sync.SITE_HIERARCHY_OBJECT_TYPES) == {key for key, _ in sync.SITE_HIERARCHIES}


def test_the_watch_list_covers_what_changes_an_inventory_from_outside_the_host():
    """These never appear in a device's own changelog entries, which is the
    trap an incremental sync falls into."""
    for object_type in ('extras.configcontext', 'extras.tag', 'extras.customfield', 'ipam.ipaddress', 'dcim.site', 'dcim.region'):
        assert object_type in sync.WATCHED_OBJECT_TYPES


def test_watch_all_drops_the_filter():
    assert changelog.watch_types({'changelog_watch_all': True}) is None
    assert changelog.watch_types({}) == list(sync.WATCHED_OBJECT_TYPES)


# -- skipping ---------------------------------------------------------------


def test_skips_when_nothing_changed_at_all():
    client = FakeClient(changes=[change(10)])
    decision = changelog.decide(client, {'changelog_id': 10, 'netbox_url': client.base_url}, {})
    assert decision.fetch is False
    assert decision.watermark == 10


def test_skips_when_the_changes_do_not_touch_the_inventory():
    client = FakeClient(changes=[change(10), change(11, 'circuits.circuit')])
    decision = changelog.decide(client, {'changelog_id': 10, 'netbox_url': client.base_url}, {})
    assert decision.fetch is False
    # The position still moves, so the same irrelevant change is not reconsidered.
    assert decision.watermark == 11


# -- fetching ---------------------------------------------------------------


def test_fetches_on_a_relevant_change():
    client = FakeClient(changes=[change(10), change(11, 'extras.configcontext')])
    decision = changelog.decide(client, {'changelog_id': 10, 'netbox_url': client.base_url}, {})
    assert decision.fetch is True
    assert decision.watermark == 11


def test_a_config_context_edit_is_not_missed():
    """It records one entry against the config context and none against the
    devices whose host vars just changed."""
    client = FakeClient(changes=[change(10), change(11, 'extras.configcontext')])
    assert changelog.decide(client, {'changelog_id': 10, 'netbox_url': client.base_url}, {}).fetch is True


def test_fetches_without_a_recorded_position():
    client = FakeClient(changes=[change(10)])
    decision = changelog.decide(client, {}, {})
    assert decision.fetch is True
    assert decision.watermark == 10


def test_fetches_when_the_changelog_is_empty():
    decision = changelog.decide(FakeClient(changes=[]), {'changelog_id': 10}, {})
    assert decision.fetch is True
    assert decision.watermark is None


def test_fetches_after_a_failed_run():
    """Its position was recorded before its import ran, so trusting it would
    skip past work that never landed."""
    client = FakeClient(changes=[change(10)])
    decision = changelog.decide(client, {'changelog_id': 10, 'netbox_url': client.base_url}, {}, previous_failed=True)
    assert decision.fetch is True


def test_fetches_when_the_netbox_url_changed():
    client = FakeClient(changes=[change(10)])
    decision = changelog.decide(client, {'changelog_id': 10, 'netbox_url': 'https://other.example/'}, {})
    assert decision.fetch is True


def test_fetches_when_the_position_has_been_pruned():
    """Retention defaults to 90 days, so a quiet source's position can outlive
    the entries it would need to read."""
    client = FakeClient(changes=[change(500), change(501)])
    decision = changelog.decide(client, {'changelog_id': 10, 'netbox_url': client.base_url}, {})
    assert decision.fetch is True
    assert 'pruned' in decision.reason


def test_an_adjacent_oldest_entry_is_not_a_gap():
    client = FakeClient(changes=[change(11, 'dcim.site')])
    decision = changelog.decide(client, {'changelog_id': 10, 'netbox_url': client.base_url}, {})
    assert decision.fetch is True
    assert 'pruned' not in decision.reason


def test_fetches_at_the_skip_ceiling():
    client = FakeClient(changes=[change(10)])
    state = {'changelog_id': 10, 'netbox_url': client.base_url, 'skips': changelog.DEFAULT_MAX_SKIPS}
    decision = changelog.decide(client, state, {})
    assert decision.fetch is True
    assert 'ceiling' in decision.reason


def test_the_ceiling_is_configurable():
    client = FakeClient(changes=[change(10)])
    state = {'changelog_id': 10, 'netbox_url': client.base_url, 'skips': 3}
    assert changelog.decide(client, state, {'incremental_max_skips': 3}).fetch is True
    assert changelog.decide(client, state, {'incremental_max_skips': 4}).fetch is False


def test_a_nonsense_ceiling_falls_back_to_the_default():
    client = FakeClient(changes=[change(10)])
    state = {'changelog_id': 10, 'netbox_url': client.base_url, 'skips': 1}
    assert changelog.decide(client, state, {'incremental_max_skips': 'often'}).fetch is False


def test_fetches_when_a_watched_object_type_is_unknown_to_this_netbox():
    """A content type NetBox does not have is filtered silently, returning no
    changes, so the watch list would quietly stop covering it."""
    known = [t for t in sync.WATCHED_OBJECT_TYPES if t != 'extras.configcontext']
    client = FakeClient(changes=[change(10), change(11, 'circuits.circuit')], object_types=known)
    decision = changelog.decide(client, {'changelog_id': 10, 'netbox_url': client.base_url}, {})
    assert decision.fetch is True
    assert 'extras.configcontext' in decision.reason


# -- bookkeeping ------------------------------------------------------------


def test_a_skip_extends_the_streak_and_a_fetch_resets_it():
    client = FakeClient()
    assert changelog.next_state(client, changelog.Decision(False, '', 10), 3)['skips'] == 4
    assert changelog.next_state(client, changelog.Decision(True, '', 10), 3)['skips'] == 0


def test_the_state_records_the_url_it_came_from():
    client = FakeClient()
    assert changelog.next_state(client, changelog.Decision(True, '', 10), 0)['netbox_url'] == client.base_url


def test_the_count_query_asks_for_one_row_and_filters_by_type():
    client = FakeClient(changes=[change(11, 'dcim.site')])
    changelog.changes_since(client, 10, ['dcim.site', 'dcim.device'])
    path, params = client.requests[-1]
    assert params['limit'] == 1
    assert params['id__gt'] == 10
    assert params['changed_object_type'] == ['dcim.site', 'dcim.device']
