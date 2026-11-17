"""Turn NetBox objects into an Ansible inventory structure.

The output is the same shape `ansible-inventory --list` produces, because that
is what inventory_import.Command.perform_update already consumes:

    {
        "_meta": {"hostvars": {"<host>": {...}}},
        "<group>": {"hosts": [...], "vars": {...}, "children": [...]},
    }

Keeping that contract means the native path reuses all of the existing host
and group reconciliation, including overwrite handling and host filters.
"""

import logging
import re

logger = logging.getLogger('ascender.main.netbox')

#: NetBox object fields copied onto every host as-is.
BASE_FIELDS = ('id', 'name', 'display', 'status', 'description', 'comments', 'url')

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

_SAFE = re.compile(r'[^A-Za-z0-9_]+')


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


def host_name_for(obj, config):
    """Which NetBox field names the host.

    Defaults to the object name. `use_fqdn` prefers the primary IP's DNS name
    when NetBox has one, which is usually what connects.
    """
    if config.get('use_fqdn'):
        for key in ('primary_ip', 'primary_ip4', 'primary_ip6'):
            ip = obj.get(key)
            if isinstance(ip, dict) and ip.get('dns_name'):
                return ip['dns_name']
    name = obj.get('name') or obj.get('display')
    return name


def ansible_host_for(obj):
    """The address Ansible should connect to.

    Primary IP wins, v4 before v6, then the DNS name, and otherwise nothing so
    Ansible falls back to resolving the inventory hostname.
    """
    for key in ('primary_ip4', 'primary_ip', 'primary_ip6'):
        ip = obj.get(key)
        if isinstance(ip, dict):
            addr = _ip_only(ip.get('address'))
            if addr:
                return addr
    return None


def build_hostvars(obj, kind, config):
    """Flatten one NetBox device or VM into Ansible host variables."""
    hv = {'netbox_object_type': kind}
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

    addr = ansible_host_for(obj)
    if addr:
        hv['ansible_host'] = addr

    if config.get('include_raw'):
        hv['netbox_raw'] = obj

    return hv


def groups_for(obj, kind, config):
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

    # A site's region and group are a level up from the host, so they become
    # parents of the site group rather than groups the host joins directly.
    site = obj.get('site')
    if isinstance(site, dict):
        site_group = group_name('site', _nested_slug(site))
        region = site.get('region')
        if isinstance(region, dict):
            add(group_name('region', _nested_slug(region)), None)
            groups.append((site_group, group_name('region', _nested_slug(region))))

    return groups


def build_inventory(devices=None, virtual_machines=None, config=None):
    """Assemble the full inventory structure.

    `devices` and `virtual_machines` are iterables of NetBox API objects.
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
                logger.warning('Skipping NetBox %s id=%s with no usable name', kind, obj.get('id'))
                continue
            if name in inventory['_meta']['hostvars']:
                logger.warning('Duplicate host name %r from NetBox %s id=%s, keeping the first', name, kind, obj.get('id'))
                continue

            inventory['_meta']['hostvars'][name] = build_hostvars(obj, kind, config)
            all_hosts.append(name)

            for group, parent in groups_for(obj, kind, config):
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
