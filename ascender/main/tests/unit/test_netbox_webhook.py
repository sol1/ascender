"""Unit tests for the webhook coalescing rules.

The measured behaviour these exist to contain: one bulk PATCH of twelve
devices through a single NetBox API call produced twelve webhooks. The rules
here are what stop that becoming twelve inventory updates.
"""

import pytest

from django.utils import timezone

from ascender.main.netbox import sync
from ascender.main.netbox import webhook


def burst(seconds_ago, **extra):
    moment = (timezone.now() - timezone.timedelta(seconds=seconds_ago)).isoformat()
    pending = {'dirty_since': moment, 'last_event_at': moment, 'events': 1}
    pending.update(extra)
    return pending


# -- which events matter ----------------------------------------------------


def test_watched_types_are_the_ones_the_incremental_gate_uses():
    """One list, so the push and poll paths cannot disagree about what counts."""
    for object_type in sync.WATCHED_OBJECT_TYPES:
        assert webhook.is_watched(object_type, {})


def test_an_unrelated_object_type_is_ignored():
    assert webhook.is_watched('ipam.prefix', {}) is False
    assert webhook.is_watched('circuits.circuit', {}) is False


def test_watch_all_accepts_anything():
    assert webhook.is_watched('ipam.prefix', {'changelog_watch_all': True}) is True


# -- the debounce window ----------------------------------------------------


def test_the_default_window():
    assert webhook.debounce_seconds({}) == webhook.DEFAULT_DEBOUNCE_SECONDS


def test_the_window_is_configurable():
    assert webhook.debounce_seconds({'webhook_debounce_seconds': 45}) == 45


def test_a_nonsense_window_falls_back_to_the_default():
    assert webhook.debounce_seconds({'webhook_debounce_seconds': 'soon'}) == webhook.DEFAULT_DEBOUNCE_SECONDS


def test_a_negative_window_is_treated_as_none():
    assert webhook.debounce_seconds({'webhook_debounce_seconds': -5}) == 0


@pytest.mark.parametrize(
    'age, settled',
    [
        (0, False),
        (webhook.DEFAULT_DEBOUNCE_SECONDS - 1, False),
        (webhook.DEFAULT_DEBOUNCE_SECONDS, True),
        (600, True),
    ],
)
def test_a_burst_is_acted_on_only_once_it_goes_quiet(age, settled):
    assert webhook._settled(burst(age), {}, timezone.now()) is settled


def test_a_later_webhook_pushes_the_window_out():
    """A long bulk edit keeps extending the burst rather than syncing partway
    through it."""
    now = timezone.now()
    pending = burst(600)
    pending['last_event_at'] = now.isoformat()
    assert webhook._settled(pending, {}, now) is False


def test_unreadable_state_syncs_rather_than_wedging_the_source():
    assert webhook._settled({'last_event_at': 'not a time'}, {}, timezone.now()) is True
    assert webhook._settled({}, {}, timezone.now()) is True
