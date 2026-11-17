"""Unit tests for the NetBox inventory transform.

sync.py takes NetBox API objects and returns the inventory structure, with no
Django and no network, so these are plain function tests against payloads
shaped like the ones NetBox 4.6+ actually returns.
"""

import pytest

from ascender.main.netbox import sync


def ip(address, dns_name=''):
    return {'id': 1, 'address': address, 'dns_name': dns_name, 'family': {'value': 4, 'label': 'IPv4'}}


def device(**overrides):
    """A device as NetBox serialises one, trimmed to the fields read here.

    Note what the nested site does and does not carry: NetBox gives a device
    the brief site representation, which stops at the slug. No region, no site
    group, no time zone. That omission is what build_site_lookup exists for.
    """
    obj = {
        'id': 7,
        'name': 'syd-db-01',
        'display': 'syd-db-01',
        'status': {'value': 'active', 'label': 'Active'},
        'site': {'id': 1, 'name': 'Sydney HQ', 'slug': 'sydney-hq'},
        'rack': {'id': 2, 'name': 'SYD-R02'},
        'role': {'id': 5, 'name': 'Database Server', 'slug': 'database-server'},
        'device_type': {
            'id': 2,
            'model': 'PowerEdge R650',
            'slug': 'poweredge-r650',
            'manufacturer': {'id': 1, 'name': 'Dell', 'slug': 'dell'},
        },
        'platform': {'id': 1, 'name': 'Rocky Linux 9', 'slug': 'rocky-linux-9'},
        'tags': [{'id': 3, 'name': 'PCI Scope', 'slug': 'pci-scope'}],
        'primary_ip4': ip('10.20.10.31/32', 'syd-db-01.sol1.net'),
        'oob_ip': ip('10.20.20.31/32', 'syd-db-01-drac.sol1.net'),
        'config_context': {},
        'local_context_data': None,
        'custom_fields': {},
        'serial': 'ABC123',
    }
    obj.update(overrides)
    return obj


SITES = [{'id': 1, 'name': 'Sydney HQ', 'slug': 'sydney-hq', 'region': {'id': 2}, 'group': {'id': 9}, 'time_zone': 'Australia/Sydney', 'facility': 'Level 3'}]
REGIONS = [{'id': 1, 'slug': 'apac', 'parent': None}, {'id': 2, 'slug': 'australia', 'parent': {'id': 1}}]
SITE_GROUPS = [{'id': 9, 'slug': 'corporate', 'parent': None}]


@pytest.fixture
def sites():
    return sync.build_site_lookup(sites=SITES, regions=REGIONS, site_groups=SITE_GROUPS)


# -- fields= ---------------------------------------------------------------


def test_api_fields_asks_only_for_what_the_transform_reads():
    fields = sync.api_fields({}).split(',')
    assert 'config_context' in fields
    assert 'local_context_data' in fields
    assert 'oob_ip' in fields
    # The `*_count` aggregates cost a subquery each and nothing here reads them.
    assert not [f for f in fields if f.endswith('_count')]


def test_api_fields_drops_config_context_when_it_is_turned_off():
    assert 'config_context' not in sync.api_fields({'config_context': False}).split(',')


def test_api_fields_is_none_when_the_whole_object_was_asked_for():
    assert sync.api_fields({'include_raw': True}) is None


# -- site lookup -----------------------------------------------------------


def test_site_lookup_walks_the_region_tree_nearest_first(sites):
    assert sites[1]['regions'] == ['australia', 'apac']
    assert sites[1]['site_groups'] == ['corporate']
    assert sites[1]['time_zone'] == 'Australia/Sydney'
    assert sites[1]['facility'] == 'Level 3'


def test_site_lookup_survives_a_cycle():
    regions = [{'id': 1, 'slug': 'a', 'parent': {'id': 2}}, {'id': 2, 'slug': 'b', 'parent': {'id': 1}}]
    lookup = sync.build_site_lookup(sites=[{'id': 1, 'slug': 's', 'region': {'id': 1}}], regions=regions)
    assert lookup[1]['regions'] == ['a', 'b']


def test_region_groups_chain_parent_to_child_with_the_site_underneath(sites):
    inventory = sync.build_inventory(devices=[device()], config={}, sites=sites)
    assert inventory['region_apac']['children'] == ['region_australia']
    assert inventory['region_australia']['children'] == ['site_sydney_hq']
    assert inventory['site_sydney_hq']['hosts'] == ['syd-db-01']
    assert inventory['regions']['children'] == ['region_apac']


def test_no_region_groups_without_the_lookup():
    """The device payload alone cannot produce them, which is the bug this fixes."""
    inventory = sync.build_inventory(devices=[device()], config={})
    assert not [g for g in inventory if g.startswith('region')]


def test_site_attributes_land_as_host_vars(sites):
    hv = sync.build_hostvars(device(), 'devices', {}, sites=sites)
    assert hv['netbox_regions'] == ['australia', 'apac']
    assert hv['netbox_site_groups'] == ['corporate']
    assert hv['netbox_site_time_zone'] == 'Australia/Sydney'
    assert hv['netbox_site_facility'] == 'Level 3'


# -- config context and local context --------------------------------------


def test_config_context_merges_to_the_top_level_by_default():
    hv = sync.build_hostvars(device(config_context={'cis_profile': 'level2'}), 'devices', {})
    assert hv['cis_profile'] == 'level2'
    assert hv['netbox_config_context'] == {'cis_profile': 'level2'}


def test_config_context_can_be_kept_namespaced():
    hv = sync.build_hostvars(device(config_context={'cis_profile': 'level2'}), 'devices', {'merge_config_context': False})
    assert 'cis_profile' not in hv
    assert hv['netbox_config_context'] == {'cis_profile': 'level2'}


def test_local_context_data_is_kept_separately():
    """NetBox has already merged it into config_context at the highest weight,
    so the raw blob is the only thing that says which values were set here."""
    local = {'cis_profile': 'level1'}
    hv = sync.build_hostvars(device(local_context_data=local, config_context={'cis_profile': 'level1'}), 'devices', {})
    assert hv['netbox_local_context_data'] == local
    assert hv['cis_profile'] == 'level1'


def test_no_local_context_var_when_there_is_none():
    assert 'netbox_local_context_data' not in sync.build_hostvars(device(), 'devices', {})


# -- ansible_host precedence -----------------------------------------------


def test_config_context_ansible_host_beats_the_primary_ip():
    hv = sync.build_hostvars(device(config_context={'ansible_host': '10.20.20.31'}), 'devices', {})
    assert hv['ansible_host'] == '10.20.20.31'


def test_primary_ip_is_used_when_config_context_says_nothing():
    assert sync.build_hostvars(device(), 'devices', {})['ansible_host'] == '10.20.10.31'


def test_oob_ip_as_primary_ip_beats_the_primary_ip():
    hv = sync.build_hostvars(device(), 'devices', {'oob_ip_as_primary_ip': True})
    assert hv['ansible_host'] == '10.20.20.31'


def test_config_context_still_beats_the_oob_address():
    hv = sync.build_hostvars(device(config_context={'ansible_host': '192.0.2.9'}), 'devices', {'oob_ip_as_primary_ip': True})
    assert hv['ansible_host'] == '192.0.2.9'


def test_merging_off_leaves_the_primary_ip_in_place():
    hv = sync.build_hostvars(device(config_context={'ansible_host': '192.0.2.9'}), 'devices', {'merge_config_context': False})
    assert hv['ansible_host'] == '10.20.10.31'


def test_no_ansible_host_without_an_address():
    assert 'ansible_host' not in sync.build_hostvars(device(primary_ip4=None, oob_ip=None), 'devices', {})


# -- identity and grouping -------------------------------------------------


def test_instance_id_is_qualified_because_device_and_vm_ids_collide():
    assert sync.build_hostvars(device(), 'devices', {})['netbox_instance_id'] == 'devices:7'
    assert sync.build_hostvars(device(), 'virtual_machines', {})['netbox_instance_id'] == 'virtual_machines:7'


def test_manufacturer_is_dug_out_of_the_device_type():
    hv = sync.build_hostvars(device(), 'devices', {})
    assert hv['netbox_manufacturer'] == 'Dell'
    assert ('manufacturer_dell', 'manufacturers') in sync.groups_for(device(), 'devices', {})


def test_a_rack_without_a_slug_falls_back_to_its_name():
    assert ('rack_syd_r02', 'racks') in sync.groups_for(device(), 'devices', {})


def test_group_names_are_made_ansible_safe():
    assert sync.group_name('site', 'melbourne dc!') == 'site_melbourne_dc'
    assert sync.group_name('10g') == '_10g'
    assert sync.group_name(None, '') is None


def test_an_unnamed_device_is_skipped_rather_than_named_after_its_display():
    """An unnamed device displays as its model and pk, which is not an address."""
    unnamed = {'id': 2013, 'name': None, 'display': 'Arista DCS-7050SX3-48YC8 (2013)', 'status': {'value': 'active'}}
    named = {'id': 8, 'name': 'dc1-db-01', 'display': 'dc1-db-01', 'status': {'value': 'active'}}
    inventory = sync.build_inventory(devices=[unnamed, named], virtual_machines=[], config={})
    assert list(inventory['_meta']['hostvars']) == ['dc1-db-01']
