"""Unit tests for NetBox filter validation.

The behaviour being guarded is NetBox's: it answers HTTP 200 and returns every
object when a query parameter is not one of its filters. A source whose filters
are misspelled therefore syncs the whole estate and reports success, so these
tests are mostly about making sure the sync stops instead.
"""

import pytest

from django.core.cache import cache

from ascender.main.netbox import filters as netbox_filters
from ascender.main.netbox.client import NetBoxError

DEVICE_PARAMS = ['name', 'site', 'status', 'rack_id', 'cluster_id', 'cluster_group', 'tag']
VM_PARAMS = ['name', 'site', 'status', 'cluster', 'cluster_id']

SCHEMA = {
    'paths': {
        netbox_filters.DEVICE_PATH: {'get': {'parameters': [{'name': n} for n in DEVICE_PARAMS]}},
        netbox_filters.VM_PATH: {'get': {'parameters': [{'name': n} for n in VM_PARAMS]}},
    }
}


class FakeClient:
    """Just enough NetBoxClient for the validator: a cache key and a GET."""

    def __init__(self, schema=SCHEMA, error=None):
        self.base_url = 'https://netbox.example/'
        self.version = '4.7.5'
        self.schema = schema
        self.error = error
        self.calls = 0

    def get(self, path, params=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.schema


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def test_a_real_filter_passes():
    netbox_filters.validate(FakeClient(), netbox_filters.DEVICE_PATH, {'site': 'sydney-hq', 'status': 'active'}, 'device')


def test_no_filters_means_no_schema_fetch():
    client = FakeClient()
    netbox_filters.validate(client, netbox_filters.DEVICE_PATH, {}, 'device')
    assert client.calls == 0


def test_an_unknown_filter_stops_the_sync():
    with pytest.raises(NetBoxError) as exc:
        netbox_filters.validate(FakeClient(), netbox_filters.DEVICE_PATH, {'totally_made_up': 'yes'}, 'device')
    assert 'totally_made_up' in str(exc.value)


def test_a_near_miss_is_suggested():
    """`rack` is the natural guess and `rack_id` is the real one."""
    with pytest.raises(NetBoxError) as exc:
        netbox_filters.validate(FakeClient(), netbox_filters.DEVICE_PATH, {'rack': 'SYD-R02'}, 'device')
    assert "did you mean 'rack_id'" in str(exc.value)


def test_the_two_endpoints_have_different_filters():
    """`cluster` is a virtual machine filter and not a device one, which is why
    a single shared filter set cannot be correct for both."""
    netbox_filters.validate(FakeClient(), netbox_filters.VM_PATH, {'cluster': 'syd-vmw-prod'}, 'virtual machine')
    with pytest.raises(NetBoxError):
        netbox_filters.validate(FakeClient(), netbox_filters.DEVICE_PATH, {'cluster': 'syd-vmw-prod'}, 'device')


def test_custom_field_filters_are_taken_on_trust():
    """They are generated per deployment and never appear in the schema."""
    netbox_filters.validate(FakeClient(), netbox_filters.DEVICE_PATH, {'cf_backup_tier': 'gold'}, 'device')


def test_the_schema_is_fetched_once_per_version():
    client = FakeClient()
    netbox_filters.allowed_filter_names(client, netbox_filters.DEVICE_PATH)
    netbox_filters.allowed_filter_names(client, netbox_filters.DEVICE_PATH)
    assert client.calls == 1


def test_a_sub_path_deployment_still_matches():
    schema = {'paths': {'/netbox' + netbox_filters.DEVICE_PATH: {'get': {'parameters': [{'name': 'site'}]}}}}
    netbox_filters.validate(FakeClient(schema=schema), netbox_filters.DEVICE_PATH, {'site': 'sydney-hq'}, 'device')


def test_an_unreadable_schema_stops_the_sync_rather_than_guessing():
    client = FakeClient(error=NetBoxError('boom'))
    with pytest.raises(NetBoxError) as exc:
        netbox_filters.validate(client, netbox_filters.DEVICE_PATH, {'site': 'sydney-hq'}, 'device')
    assert 'widening' in str(exc.value)


def test_an_empty_schema_stops_the_sync_too():
    with pytest.raises(NetBoxError):
        netbox_filters.validate(FakeClient(schema={'paths': {}}), netbox_filters.DEVICE_PATH, {'site': 'x'}, 'device')
