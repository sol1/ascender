"""Turn NetBox event rules into inventory syncs, without drowning in them.

NetBox fires one webhook per changed object, not per request. A single bulk
edit of twelve devices through one API call produced twelve POSTs of about
6 KB each when measured, so an estate-wide change produces hundreds. Launching
a sync per webhook would queue hundreds of inventory updates, and nothing in
the scheduler would collapse them: the only existing guard, should_update_again
against update_cache_timeout, applies to the update-on-launch dependency path
and not to a directly launched update.

So a webhook never launches anything and never carries data into the inventory.
It marks the source dirty, and a periodic pass launches at most one sync per
source once the burst has settled, and only when that source has no update
already in flight.

Nothing in the payload is used beyond its object type. Feeding a single
changed object to the importer would delete every host it did not mention, the
same full-reconcile constraint the incremental sync works around.
"""

import logging

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ascender.main.constants import ACTIVE_STATES
from ascender.main.netbox import sync as netbox_sync

logger = logging.getLogger('ascender.main.netbox')

#: How long after the last webhook of a burst to wait before syncing. Bounded
#: below by the period of the task that drains the queue, so the real latency
#: is this plus up to one tick.
DEFAULT_DEBOUNCE_SECONDS = 15

#: Key under InventorySource.source_sync_state holding the pending burst.
STATE_KEY = 'webhook'


def is_watched(object_type, config):
    """Whether a change to this NetBox object type can alter the inventory.

    The same watch list the incremental gate uses, so the two cannot disagree
    about what matters.
    """
    if config.get('changelog_watch_all'):
        return True
    return object_type in netbox_sync.WATCHED_OBJECT_TYPES


def debounce_seconds(config):
    try:
        value = int(config.get('webhook_debounce_seconds', DEFAULT_DEBOUNCE_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_DEBOUNCE_SECONDS
    return max(0, value)


def record_event(source, object_type, change_ids=None):
    """Fold one webhook into the source's pending burst.

    Webhooks from a bulk edit arrive concurrently, so the whole read, merge and
    write happens under the source's row lock. Without it, two deliveries
    landing together would each write a burst built from the state they read
    before the other, and one of them would be lost.
    """
    from ascender.main.models.inventory import InventorySource

    now = timezone.now()
    highest = max(change_ids) if change_ids else None

    with transaction.atomic():
        # of=('self',) locks the inventory source row alone. InventorySource
        # inherits from UnifiedJobTemplate, so a plain select_for_update spans
        # an outer join and PostgreSQL refuses to lock that.
        locked = InventorySource.objects.select_for_update(of=('self',)).filter(pk=source.pk).first()
        if locked is None:
            return None
        state = dict(locked.source_sync_state or {})
        pending = dict(state.get(STATE_KEY) or {})

        pending['dirty_since'] = pending.get('dirty_since') or now.isoformat()
        pending['last_event_at'] = now.isoformat()
        pending['events'] = int(pending.get('events') or 0) + 1
        if highest is not None:
            pending['change_id'] = max(highest, pending.get('change_id') or 0)
        pending.setdefault('object_types', [])
        if object_type and object_type not in pending['object_types']:
            pending['object_types'] = sorted(set(pending['object_types']) | {object_type})

        state[STATE_KEY] = pending
        InventorySource.objects.filter(pk=locked.pk).update(source_sync_state=state)

    return pending


def _settled(pending, config, now):
    """Whether the burst has been quiet for long enough to act on."""
    last = parse_datetime(pending.get('last_event_at') or '')
    if last is None:
        # Unreadable state should still sync rather than wedge the source.
        return True
    return (now - last).total_seconds() >= debounce_seconds(config)


def clear_pending(source_pk):
    """Drop the burst, once a sync for it has been launched."""
    from ascender.main.models.inventory import InventorySource

    with transaction.atomic():
        locked = InventorySource.objects.select_for_update(of=('self',)).filter(pk=source_pk).first()
        if locked is None:
            return
        state = dict(locked.source_sync_state or {})
        state.pop(STATE_KEY, None)
        InventorySource.objects.filter(pk=locked.pk).update(source_sync_state=state)


def launch_settled_syncs():
    """Start one sync for each source whose webhook burst has settled.

    A source with an update already pending or running is left dirty and
    reconsidered next pass, rather than queueing a second update behind the
    first. That update may have started before the change landed, so the burst
    is not cleared until a sync is actually launched for it.
    """
    from ascender.main.models.inventory import InventorySource

    now = timezone.now()
    launched = []
    candidates = InventorySource.objects.filter(source='netbox', source_sync_state__has_key=STATE_KEY)

    for source in candidates.iterator():
        pending = (source.source_sync_state or {}).get(STATE_KEY) or {}
        config = source.source_vars_dict or {}
        if not _settled(pending, config, now):
            continue

        if source.inventory_updates.filter(status__in=ACTIVE_STATES).exists():
            logger.debug('NetBox webhook sync for inventory source %s deferred, an update is already in flight', source.pk)
            continue

        try:
            update = source.create_inventory_update(_eager_fields={'launch_type': 'webhook'})
            started = update.signal_start()
        except Exception:
            logger.exception('Could not start a webhook triggered sync for inventory source %s', source.pk)
            continue

        if not started:
            # Leave the burst in place; whatever blocked it may clear.
            logger.warning('Webhook triggered sync for inventory source %s could not start', source.pk)
            update.delete()
            continue

        clear_pending(source.pk)
        logger.info(
            'Started %s from %s NetBox webhook(s) on inventory source %s',
            update.log_format,
            pending.get('events'),
            source.pk,
        )
        launched.append(update)

    return launched
