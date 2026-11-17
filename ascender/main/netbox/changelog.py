"""Decide whether a NetBox sync has anything to fetch.

The inventory importer performs a full reconcile: with overwrite set it deletes
every host and group in the database that is absent from the payload. A partial
payload built from changelog deltas would therefore delete everything that did
not change, so this module does not build deltas. It answers one question
before the expensive work starts, which is whether anything relevant has
changed at all, and lets the run skip the device and virtual machine fetch when
the answer is no.

NetBox's object-change ids are a plain auto-increment sequence, so the position
of the last sync is a single integer. Ids are used rather than timestamps
because a change committed at T but not visible until after a query for T would
be lost permanently from a time window, and never from an id watermark.

Every uncertainty resolves towards fetching. A missing position, a pruned
changelog, an unrecognised object type, a failed previous run and the skip
ceiling all produce a full fetch, because a needless fetch costs time while a
needless skip leaves the inventory wrong and the job green.
"""

import logging

from django.core.cache import cache

from ascender.main.netbox import sync as netbox_sync

logger = logging.getLogger('ascender.main.netbox')

CHANGELOG_PATH = '/api/core/object-changes/'
OBJECT_TYPES_PATH = '/api/core/object-types/'

#: Full fetches forced after this many consecutive skips, bounding how stale an
#: inventory can get from a change the changelog never recorded: a direct
#: database write, a bulk load outside the request path, or a restore.
DEFAULT_MAX_SKIPS = 24

#: Object types change only with the NetBox version, which is in the key.
CACHE_TIMEOUT = 60 * 60 * 24


class Decision:
    """Whether to fetch, why, and the position to record afterwards."""

    def __init__(self, fetch, reason, watermark=None):
        self.fetch = fetch
        self.reason = reason
        self.watermark = watermark

    def __repr__(self):
        return '<Decision fetch={} watermark={} {!r}>'.format(self.fetch, self.watermark, self.reason)


def _edge_change_id(client, ordering):
    """The first change id in one direction, or None when there are none."""
    payload = client.get(CHANGELOG_PATH, params={'limit': 1, 'ordering': ordering, 'fields': 'id'})
    results = payload.get('results') or []
    return results[0].get('id') if results else None


def latest_change_id(client):
    return _edge_change_id(client, '-id')


def oldest_change_id(client):
    return _edge_change_id(client, 'id')


def known_object_types(client):
    """Every object type this NetBox has, as `app_label.model`."""
    key = 'netbox-object-types:{}:{}'.format(client.base_url, client.version or 'unknown')
    cached = cache.get(key)
    if cached is not None:
        return set(cached)
    types = {
        '{}.{}'.format(t.get('app_label'), t.get('model'))
        for t in client.paginate(OBJECT_TYPES_PATH, params={'fields': 'app_label,model'})
        if t.get('app_label') and t.get('model')
    }
    if types:
        cache.set(key, sorted(types), CACHE_TIMEOUT)
    return types


def watch_types(config):
    """The object types to watch, or None to watch everything.

    `changelog_watch_all` is the escape hatch for an operator who would rather
    fetch on any change at all than trust a curated list.
    """
    if config.get('changelog_watch_all'):
        return None
    return list(netbox_sync.WATCHED_OBJECT_TYPES)


def changes_since(client, watermark, object_types=None):
    """How many changes above `watermark` touch the watched object types.

    Only the count is read. NetBox returns it alongside the first page, so one
    request with a page size of one answers the question.
    """
    params = {'id__gt': watermark, 'limit': 1, 'fields': 'id'}
    if object_types:
        # requests repeats the parameter for each value, which is how NetBox
        # takes a multiple-choice filter.
        params['changed_object_type'] = list(object_types)
    return client.get(CHANGELOG_PATH, params=params).get('count') or 0


def decide(client, state, config, previous_failed=False):
    """Whether this run should fetch from NetBox, and the position to record.

    `state` is the bookkeeping the last run of this source left behind.
    """
    watermark = state.get('changelog_id')
    skips = int(state.get('skips') or 0)
    try:
        max_skips = int(config.get('incremental_max_skips', DEFAULT_MAX_SKIPS))
    except (TypeError, ValueError):
        max_skips = DEFAULT_MAX_SKIPS

    # Read the position before fetching anything, never after. A change landing
    # while the fetch runs then has an id above this watermark and is picked up
    # by the next run, where reading afterwards would swallow it.
    latest = latest_change_id(client)
    if latest is None:
        return Decision(True, 'the NetBox changelog is empty, so it cannot say what changed', None)
    if previous_failed:
        return Decision(True, 'the previous sync of this source did not succeed', latest)
    if watermark is None:
        return Decision(True, 'this source has no recorded changelog position yet', latest)
    if state.get('netbox_url') and state['netbox_url'] != client.base_url:
        return Decision(True, 'the NetBox URL changed since the last sync', latest)
    if max_skips and skips >= max_skips:
        return Decision(True, 'the ceiling of {} skipped runs in a row was reached'.format(max_skips), latest)

    oldest = oldest_change_id(client)
    # Ids are not gapless, because a rolled back transaction still consumes one,
    # so this can report a gap that was never a real change. That costs one
    # unnecessary fetch, which is the direction to be wrong in.
    if oldest is None or oldest > watermark + 1:
        return Decision(True, 'changelog entries after position {} have been pruned, so the gap cannot be read'.format(watermark), latest)

    if latest <= watermark:
        return Decision(False, 'no NetBox changes at all since position {}'.format(watermark), watermark)

    types = watch_types(config)
    if types:
        unknown = sorted(set(types) - known_object_types(client))
        if unknown:
            return Decision(True, 'this NetBox has no object type {}, so the watch list cannot be trusted'.format(', '.join(unknown)), latest)

    count = changes_since(client, watermark, types)
    if count == 0:
        return Decision(False, 'none of the {} NetBox changes since position {} affect this inventory'.format(latest - watermark, watermark), latest)
    return Decision(True, '{} NetBox change(s) since position {} affect this inventory'.format(count, watermark), latest)


def next_state(client, decision, previous_skips):
    """The bookkeeping to store after this run.

    A skipped run carries the streak forward so the ceiling can be reached; a
    run that fetched resets it.
    """
    return {
        'changelog_id': decision.watermark,
        'skips': 0 if decision.fetch else int(previous_skips or 0) + 1,
        'netbox_url': client.base_url,
    }
