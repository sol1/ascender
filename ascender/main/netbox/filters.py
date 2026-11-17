"""Check inventory source filters against the NetBox API schema before use.

NetBox silently ignores a query parameter it does not recognise. A request for
`?rack=SYD-R02` on an endpoint whose filter is actually `rack_id` returns HTTP
200 and every device in the estate, so a typo in a source's filters widens its
scope instead of narrowing it and the job still goes green. That is the failure
mode worth spending a request to avoid: an inventory source that quietly syncs
more than it was told to is worse than one that stops and says why.

The OpenAPI schema is the only place NetBox publishes the real filter names.
It is large, around 15 MB on NetBox 4.7, so the extracted names are cached
against the NetBox version they came from and the fetch only happens for a
source that actually sets filters.
"""

import difflib
import logging

from django.core.cache import cache

from ascender.main.netbox.client import NetBoxError

logger = logging.getLogger('ascender.main.netbox')

#: NetBox serves the schema as YAML by default; ask for JSON.
SCHEMA_PATH = '/api/schema/'
SCHEMA_PARAMS = {'format': 'json'}

#: Filter names only change with the NetBox version, which is part of the key.
CACHE_TIMEOUT = 60 * 60 * 24

#: Custom field filters are generated per deployment and are not in the schema,
#: so anything in that namespace is taken on trust.
CUSTOM_FIELD_PREFIX = 'cf_'

DEVICE_PATH = '/api/dcim/devices/'
VM_PATH = '/api/virtualization/virtual-machines/'


def _cache_key(client, path):
    return 'netbox-filters:{}:{}:{}'.format(client.base_url, client.version or 'unknown', path)


def _names_from_schema(schema, path):
    """Pull the GET query parameter names for one endpoint out of the schema.

    Paths are matched by suffix because a NetBox served under a sub-path
    prefixes every key in the document with it.
    """
    for key, spec in (schema.get('paths') or {}).items():
        if key == path or key.endswith(path):
            parameters = (spec.get('get') or {}).get('parameters') or []
            return {p['name'] for p in parameters if isinstance(p, dict) and p.get('name')}
    return set()


def allowed_filter_names(client, path):
    """Every filter name NetBox accepts on this endpoint."""
    key = _cache_key(client, path)
    cached = cache.get(key)
    if cached is not None:
        return set(cached)

    logger.debug('Fetching the NetBox API schema to validate filters for %s', path)
    schema = client.get(SCHEMA_PATH, params=SCHEMA_PARAMS)
    names = _names_from_schema(schema, path)
    if names:
        cache.set(key, sorted(names), CACHE_TIMEOUT)
    return names


def validate(client, path, filters, label):
    """Raise unless every filter name is one NetBox will actually apply.

    `label` names the object type for the error message, so an operator reading
    a failed job knows which half of the source to fix.
    """
    if not filters:
        return

    try:
        allowed = allowed_filter_names(client, path)
    except NetBoxError as exc:
        raise NetBoxError(
            'Could not read the NetBox API schema to check the {} filters: {} '
            'NetBox ignores a filter name it does not recognise and returns every object, '
            'so this sync stops rather than silently widening its own scope. '
            'Remove the filters if you meant to sync everything.'.format(label, exc)
        )

    if not allowed:
        raise NetBoxError(
            'The NetBox API schema lists no filters for {}, so the {} filters cannot be '
            'checked. NetBox ignores an unrecognised filter name and returns every object, '
            'so this sync stops rather than risk syncing more than it was asked to.'.format(path, label)
        )

    problems = []
    for name in sorted(filters):
        if name in allowed or name.startswith(CUSTOM_FIELD_PREFIX):
            continue
        close = difflib.get_close_matches(name, allowed, n=3, cutoff=0.6)
        problems.append('{!r}{}'.format(name, ' (did you mean {}?)'.format(' or '.join(repr(c) for c in close)) if close else ''))

    if problems:
        raise NetBoxError(
            'NetBox has no {} filter named {}. It would ignore it and return every object, '
            'so this sync stopped instead. Device and virtual machine filters are not the same '
            'set: put one that applies to only one of them in device_filters or vm_filters '
            'rather than in filters.'.format(label, '; '.join(problems))
        )
