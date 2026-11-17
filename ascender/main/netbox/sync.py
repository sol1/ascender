"""Turn NetBox objects into an Ansible inventory structure.

The output is the same shape `ansible-inventory --list` produces, because that
is what inventory_import.Command.perform_update already consumes:

    {
        "_meta": {"hostvars": {"<host>": {...}}},
        "<group>": {"hosts": [...], "vars": {...}, "children": [...]},
    }

Keeping that contract means the native path reuses all of the existing host
and group reconciliation, including overwrite handling and host filters.

This module stays a pure transform: it takes NetBox API objects in and returns
a dict. Everything that talks to NetBox lives in client.py, and everything that
decides what to ask for lives in the management command.
"""

import logging
import re

logger = logging.getLogger('ascender.main.netbox')

#: NetBox object fields copied onto every host as-is.
BASE_FIELDS = ('id', 'name', 'display', 'status', 'description', 'comments', 'url')

#: Hardware identifiers, present on devices only.
HARDWARE_FIELDS = ('serial', 'asset_tag', 'position')

#: Virtual machine sizing, present on VMs only.
VM_FIELDS = ('vcpus', 'memory', 'disk')

#: Site attributes a device's own payload never carries. They arrive through
#: the site lookup instead, and become host vars rather than groups: grouping
#: by time zone inflates the group count without helping anyone target a play.
SITE_FIELDS = ('time_zone', 'facility')

#: Related objects turned into both a hostvar and a group. The value is the
#: hostvar prefix; the group name is built from it too.
GROUPABLE = (
    ('site', 'site'),
    ('location', 'location'),
    ('rack', 'rack'),
    ('tenant', 'tenant'),
    ('platform', 'platform'),
    ('cluster', 'cluster'),
    ('device_type', 'device_type'),
)

#: Nested trees resolved from the site lookup. Each is (lookup key, group and
#: hostvar prefix).
SITE_HIERARCHIES = (('regions', 'region'), ('site_groups', 'site_group'))

#: Every field this transform reads, for the API's `fields=` parameter. NetBox
#: silently drops a name an endpoint does not have, so one superset serves both
#: devices and virtual machines. Asking for these by name is worth doing: the
#: full device serializer is roughly twice this, most of the difference being
#: `*_count` aggregates that cost a subquery each and that nothing here reads.
API_FIELDS = (
    BASE_FIELDS
    + HARDWARE_FIELDS
    + VM_FIELDS
    + tuple(key for key, _ in GROUPABLE)
    + (
        'role',
        'tags',
        'custom_fields',
        'local_context_data',
        'oob_ip',
        'primary_ip',
        'primary_ip4',
        'primary_ip6',
    )
)

#: Requested separately because rendering it is the expensive half of a device
#: list, so it is only asked for when something is going to read it.
CONFIG_CONTEXT_FIELD = 'config_context'

_SAFE = re.compile(r'[^A-Za-z0-9_]+')


def api_fields(config):
    """The `fields=` value for a device or virtual machine list request.

    Returns None when the whole object is wanted, which is the one case where
    trimming the response would lose something the caller asked for.
    """
    if config.get('include_raw'):
        return None
    fields = set(API_FIELDS)
    if config.get('config_context', True):
        fields.add(CONFIG_CONTEXT_FIELD)
    return ','.join(sorted(fields))


def group_name(*parts):
    """Build an Ansible-safe group name.

    Ansible group names allow letters, digits and underscores, so anything else
    collapses to an underscore. A leading digit is prefixed because a group may
    not start with one.
    """
    raw = '_'.join(str(p) for p in parts if p not in (None, ''))
    name = _SAFE.sub('_', raw).strip('_').lower()
    if not name:
        return None
    if name[0].isdigit():
        name = '_' + name
    return name


def _nested_name(value):
    """Human-readable name of a nested NetBox object.

    NetBox is not uniform here: most objects have `name`, device types carry
    `model` instead, and choice fields such as status are {value, label}.
    """
    if isinstance(value, dict):
        for key in ('name', 'model', 'label', 'display', 'slug', 'value'):
            if value.get(key):
                return value[key]
        return None
    return value


def _nested_slug(value):
    """Stable machine-readable key, which is what groups are built from."""
    if isinstance(value, dict):
        for key in ('slug', 'value', 'name', 'model', 'display'):
            if value.get(key):
                return value[key]
        return None
    return value


def _manufacturer(obj):
    """NetBox nests the manufacturer inside device_type rather than exposing
    it on the device, so dig it out for grouping and host vars."""
    device_type = obj.get('device_type')
    if isinstance(device_type, dict):
        return device_type.get('manufacturer')
    return None


def _ip_only(cidr):
    """`10.0.0.5/24` -> `10.0.0.5`. NetBox always stores the mask."""
    if not cidr:
        return None
    return str(cidr).split('/')[0]


def _address_of(value):
    """Pull a bare address out of a nested NetBox IP object."""
    if isinstance(value, dict):
        return _ip_only(value.get('address'))
    return None


def _ancestry(start_id, lookup):
    """Slugs from a node up to the root of its tree, nearest first.

    NetBox nests regions and site groups, and a device sees neither, so the
    chain is walked here from the lookup tables. The seen set guards against a
    cycle that should not exist but would otherwise hang the sync.
    """
    chain = []
    seen = set()
    node_id = start_id
    while node_id is not None and node_id not in seen:
        seen.add(node_id)
        node = lookup.get(node_id)
        if not node:
            break
        slug = node.get('slug') or node.get('name')
        if slug:
            chain.append(slug)
        parent = node.get('parent')
        node_id = parent.get('id') if isinstance(parent, dict) else None
    return chain


def build_site_lookup(sites=None, regions=None, site_groups=None):
    """Resolve what a device's nested site representation leaves out.

    A device carries only the brief form of its site: id, url, display, name,
    slug and description. Region, site group, time zone and facility are not in
    it at any page size, so grouping by region off the device payload alone
    silently produces nothing. One small list request per object type fills
    them in for the whole estate.
    """
    region_lookup = {r['id']: r for r in regions or [] if 'id' in r}
    group_lookup = {g['id']: g for g in site_groups or [] if 'id' in g}

    lookup = {}
    for site in sites or []:
        if 'id' not in site:
            continue
        region = site.get('region')
        group = site.get('group')
        lookup[site['id']] = {
            'slug': site.get('slug') or site.get('name'),
            'regions': _ancestry(region.get('id'), region_lookup) if isinstance(region, dict) else [],
            'site_groups': _ancestry(group.get('id'), group_lookup) if isinstance(group, dict) else [],
            'time_zone': site.get('time_zone') or None,
            'facility': site.get('facility') or None,
        }
    return lookup


def _site_extras(obj, sites):
    """The site lookup entry for this object, or an empty one."""
    if not sites:
        return {}
    site = obj.get('site')
    if not isinstance(site, dict):
        return {}
    return sites.get(site.get('id')) or {}


def host_name_for(obj, config):
    """Which NetBox field names the host.

    Defaults to the object name. `use_fqdn` prefers the primary IP's DNS name
    when NetBox has one, which is usually what connects.

    `display` is not a fallback. NetBox allows a device to have no name and
    renders one from its hardware instead, as `Arista DCS-7050SX3-48YC8
    (2013)`. That is a label for the NetBox UI, not an address: it names the
    model and the primary key, and it changes if the device is re-typed. An
    object with no name is skipped by the caller rather than imported under
    one of those.
    """
    if config.get('use_fqdn'):
        for key in ('primary_ip', 'primary_ip4', 'primary_ip6'):
            ip = obj.get(key)
            if isinstance(ip, dict) and ip.get('dns_name'):
                return ip['dns_name']
    return obj.get('name')


def ansible_host_for(obj):
    """The address Ansible should connect to, from the object's own fields.

    Primary IP wins, v4 before v6, then nothing so Ansible falls back to
    resolving the inventory hostname. Config context and the out-of-band
    address are applied by the caller, which owns the precedence.
    """
    for key in ('primary_ip4', 'primary_ip', 'primary_ip6'):
        addr = _address_of(obj.get(key))
        if addr:
            return addr
    return None


def build_hostvars(obj, kind, config, sites=None):
    """Flatten one NetBox device or VM into Ansible host variables."""
    hv = {'netbox_object_type': kind}

    # Devices and virtual machines are separate tables with separate primary
    # key sequences, so their ids overlap: device 5 and VM 5 both exist. The
    # id alone is therefore unusable as the inventory instance id, and this
    # qualified form is what NETBOX_INSTANCE_ID_VAR points at. It is stable
    # across renames, which is the point of an instance id.
    if obj.get('id') is not None:
        hv['netbox_instance_id'] = '{}:{}'.format(kind, obj['id'])
    for field in BASE_FIELDS:
        if field in obj:
            value = obj[field]
            hv['netbox_{}'.format(field)] = _nested_slug(value) if field == 'status' else value

    for key, prefix in GROUPABLE:
        if key in obj and obj[key]:
            hv['netbox_{}'.format(prefix)] = _nested_name(obj[key])

    role = obj.get('role')
    if role:
        hv['netbox_role'] = _nested_name(role)

    manufacturer = _manufacturer(obj)
    if manufacturer:
        hv['netbox_manufacturer'] = _nested_name(manufacturer)

    tags = [t.get('slug') or t.get('name') for t in obj.get('tags') or [] if isinstance(t, dict)]
    if tags:
        hv['netbox_tags'] = tags

    extras = _site_extras(obj, sites)
    for key, prefix in SITE_HIERARCHIES:
        if extras.get(key):
            hv['netbox_{}'.format(key)] = extras[key]
    for field in SITE_FIELDS:
        if extras.get(field):
            hv['netbox_site_{}'.format(field)] = extras[field]

    # Custom fields are the main extension point operators use, so surface them
    # both namespaced and, optionally, at the top level.
    custom = obj.get('custom_fields') or {}
    if custom:
        hv['netbox_custom_fields'] = custom
        if config.get('flatten_custom_fields'):
            for key, value in custom.items():
                hv.setdefault(key, value)

    # Config context is NetBox's own mechanism for shipping config data, and is
    # the closest thing it has to host vars, so it lands at the top level
    # unless that is turned off.
    context = obj.get('config_context')
    if isinstance(context, dict) and context:
        hv['netbox_config_context'] = context
        if config.get('merge_config_context', True):
            for key, value in context.items():
                hv.setdefault(key, value)

    # NetBox merges local context data into the rendered config context at the
    # highest precedence, so these values have already arrived flattened above.
    # The raw blob is kept because the merged form cannot answer which values
    # were set on this host and which it inherited from a site or a role, and
    # because a write-back has to read it before it can merge into it.
    local = obj.get('local_context_data')
    if isinstance(local, dict) and local:
        hv['netbox_local_context_data'] = local

    for field in HARDWARE_FIELDS + VM_FIELDS:
        if obj.get(field) not in (None, ''):
            hv['netbox_{}'.format(field)] = obj[field]

    # Out-of-band address: the iDRAC, iLO or BMC. NetBox models it as a
    # first-class field on the device, and it is what a Redfish or racadm play
    # needs to target, so it is worth surfacing rather than leaving the caller
    # to dig through interfaces.
    oob = _address_of(obj.get('oob_ip'))
    if oob:
        hv['netbox_oob_ip'] = oob

    v6 = _address_of(obj.get('primary_ip6'))
    if v6:
        hv['netbox_primary_ip6'] = v6

    # Connection address, in order:
    #   1. ansible_host from config context, when merging is on
    #   2. the out-of-band address, when the source asks for it
    #   3. primary IPv4, then any primary, then IPv6
    #
    # Both steps below use setdefault, so an ansible_host merged from config
    # context wins. That merge has already resolved NetBox's own
    # precedence, weight ordering followed by the local override, which makes
    # it the most specific answer NetBox has rather than a site-wide default.
    # It also keeps ansible_host consistent with ansible_user, ansible_port and
    # every other ansible_* key, all of which already arrive this way because
    # nothing else here assigns them.
    if oob and config.get('oob_ip_as_primary_ip'):
        hv.setdefault('ansible_host', oob)

    addr = ansible_host_for(obj)
    if addr:
        hv.setdefault('ansible_host', addr)

    if config.get('include_raw'):
        hv['netbox_raw'] = obj

    return hv


def groups_for(obj, kind, config, sites=None):
    """Every group this object belongs to, as (group, parent_or_None)."""
    groups = []

    def add(name, parent=None):
        if name:
            groups.append((name, parent))

    add(group_name(kind))  # "devices" / "virtual_machines"

    for key, prefix in GROUPABLE:
        value = obj.get(key)
        if value:
            add(group_name(prefix, _nested_slug(value)), group_name(prefix + 's'))

    role = obj.get('role')
    if role:
        add(group_name('role', _nested_slug(role)), group_name('roles'))

    manufacturer = _manufacturer(obj)
    if manufacturer:
        add(group_name('manufacturer', _nested_slug(manufacturer)), group_name('manufacturers'))

    status = _nested_slug(obj.get('status'))
    if status and config.get('group_by_status', True):
        add(group_name('status', status), group_name('statuses'))

    if config.get('group_by_tag', True):
        for tag in obj.get('tags') or []:
            add(group_name('tag', _nested_slug(tag)), group_name('tags'))

    # Regions and site groups sit above the host and nest within themselves, so
    # they are chained parent to parent with the site hanging off the innermost
    # one. The host joins each level, and build_inventory then clears the hosts
    # from any group that gained a child, leaving a tree rather than a pile.
    extras = _site_extras(obj, sites)
    site = obj.get('site')
    site_group = group_name('site', _nested_slug(site)) if isinstance(site, dict) else None
    for key, prefix in SITE_HIERARCHIES:
        names = [group_name(prefix, slug) for slug in extras.get(key) or []]
        names = [name for name in names if name]
        for index, name in enumerate(names):
            parent = names[index + 1] if index + 1 < len(names) else group_name(prefix + 's')
            add(name, parent)
        if names and site_group:
            groups.append((site_group, names[0]))

    return groups


def build_inventory(devices=None, virtual_machines=None, config=None, sites=None):
    """Assemble the full inventory structure.

    `devices` and `virtual_machines` are iterables of NetBox API objects.
    `sites` is the lookup from build_site_lookup, keyed by site id.
    """
    config = dict(config or {})
    inventory = {'_meta': {'hostvars': {}}}
    all_hosts = []
    parents = {}

    def ensure(group):
        return inventory.setdefault(group, {'hosts': [], 'vars': {}, 'children': []})

    for kind, objects in (('devices', devices or []), ('virtual_machines', virtual_machines or [])):
        for obj in objects:
            name = host_name_for(obj, config)
            if not name:
                logger.warning('Skipping unnamed NetBox %s id=%s, which Ansible has no way to address', kind, obj.get('id'))
                continue
            if name in inventory['_meta']['hostvars']:
                logger.warning('Duplicate host name %r from NetBox %s id=%s, keeping the first', name, kind, obj.get('id'))
                continue

            inventory['_meta']['hostvars'][name] = build_hostvars(obj, kind, config, sites=sites)
            all_hosts.append(name)

            for group, parent in groups_for(obj, kind, config, sites=sites):
                ensure(group)['hosts'].append(name)
                if parent:
                    parents.setdefault(parent, set()).add(group)

    for parent, children in parents.items():
        entry = ensure(parent)
        for child in sorted(children):
            if child != parent and child not in entry['children']:
                entry['children'].append(child)
            # A grouping parent holds its children, not the hosts directly.
        entry['hosts'] = []

    # De-duplicate while keeping a stable order, which keeps the job output and
    # any diffing of it readable.
    for group, entry in inventory.items():
        if group == '_meta':
            continue
        entry['hosts'] = sorted(set(entry['hosts']))

    inventory['all'] = {'hosts': sorted(set(all_hosts)), 'vars': {}, 'children': []}
    return inventory
