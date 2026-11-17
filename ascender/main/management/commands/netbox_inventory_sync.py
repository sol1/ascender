"""Fetch an inventory from NetBox and write it where the importer expects it.

Run from RunInventoryUpdate for sources in NATIVE_INVENTORY_SOURCES. It writes
the same output.json that `ansible-inventory --output` would, so the existing
post_run_hook imports the result with no special casing.

Printed output becomes the job's log, so it is written for an operator reading
a failed sync, not for a developer reading a traceback.
"""

import json
import os
import time

from django.db import transaction
from django.core.management.base import BaseCommand, CommandError

from ascender.main.constants import NATIVE_INVENTORY_UNCHANGED_MARKER
from ascender.main.models.inventory import InventorySource, InventoryUpdate
from ascender.main.netbox.client import NetBoxClient, NetBoxError
from ascender.main.netbox import changelog as netbox_changelog
from ascender.main.netbox import filters as netbox_filters
from ascender.main.netbox import sync as netbox_sync

#: Fields of a site that a device's own payload never carries.
SITE_LOOKUP_FIELDS = 'id,name,slug,region,group,time_zone,facility'

#: Regions and site groups are trees, so the parent is needed to walk them.
TREE_LOOKUP_FIELDS = 'id,name,slug,parent'


class Command(BaseCommand):
    help = 'Fetch inventory from NetBox for an inventory update and write it as JSON.'

    def add_arguments(self, parser):
        parser.add_argument('--inventory-update-id', dest='inventory_update_id', type=int, required=True)
        parser.add_argument('--output', dest='output', required=True, help='Path to write the inventory JSON to.')

    def handle(self, *args, **options):
        started = time.time()
        update_id = options['inventory_update_id']
        output = options['output']

        try:
            inventory_update = InventoryUpdate.objects.get(pk=update_id)
        except InventoryUpdate.DoesNotExist:
            raise CommandError('Inventory update {} does not exist.'.format(update_id))

        source = inventory_update.inventory_source
        credential = inventory_update.get_cloud_credential()
        if credential is None:
            raise CommandError('This NetBox inventory source has no credential attached. Add a NetBox credential and try again.')

        url = credential.get_input('host', default='')
        token = credential.get_input('token', default='')
        verify_ssl = credential.get_input('verify_ssl', default=True)
        if not url:
            raise CommandError('The NetBox credential has no URL set.')

        config = dict(source.source_vars_dict or {})

        self.stdout.write('Connecting to NetBox at {}'.format(url))
        client = NetBoxClient(url, token=token, verify_ssl=bool(verify_ssl))

        incremental = bool(config.get('incremental', False))
        decision = None

        try:
            version = client.require_supported_version()
            self.stdout.write('NetBox version {}'.format(version or 'unknown'))

            # Filters are checked before the incremental gate, not after. A
            # source whose filters are wrong would otherwise keep skipping and
            # reporting success, and only reveal the mistake whenever something
            # in NetBox happens to change. The schema behind the check is
            # cached per NetBox version, so this costs a set lookup.
            device_filters = self._filters_for(config, 'device_filters')
            vm_filters = self._filters_for(config, 'vm_filters')
            netbox_filters.validate(client, netbox_filters.DEVICE_PATH, device_filters, 'device')
            netbox_filters.validate(client, netbox_filters.VM_PATH, vm_filters, 'virtual machine')

            if incremental:
                decision = self._decide(client, source, config)
                if not decision.fetch:
                    self._skip(source, client, decision, output, started)
                    return

            sites = self._site_lookup(client)
            devices = self._fetch(client, netbox_filters.DEVICE_PATH, 'devices', config, device_filters)
            vms = self._fetch(client, netbox_filters.VM_PATH, 'virtual machines', config, vm_filters)
        except NetBoxError as exc:
            # A clean one-line failure in the job log beats a traceback.
            raise CommandError(str(exc))

        inventory = netbox_sync.build_inventory(devices=devices, virtual_machines=vms, config=config, sites=sites)

        host_count = len(inventory['_meta']['hostvars'])
        group_count = len([k for k in inventory if k != '_meta'])
        self.stdout.write('Built {} host(s) in {} group(s)'.format(host_count, group_count))

        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, 'w') as handle:
            json.dump(inventory, handle)

        if incremental:
            self._record(source, client, decision)

        self.stdout.write('Wrote inventory to {} in {:.1f}s'.format(output, time.time() - started))

    def _decide(self, client, source, config):
        """Ask the changelog whether this run has anything to fetch.

        A previous run that did not succeed forces a fetch: its position was
        recorded before its import ran, so trusting it would skip past work
        that never landed.
        """
        decision = netbox_changelog.decide(
            client,
            dict(source.source_sync_state or {}),
            config,
            previous_failed=bool(source.last_job_failed),
        )
        self.stdout.write('Incremental check: {}'.format(decision.reason))
        return decision

    def _skip(self, source, client, decision, output, started):
        """Record the skip and leave a marker where the importer looks.

        Writing nothing at all would reach the post-run hook as a missing
        inventory, which is indistinguishable from a fetch that failed to
        produce one, so the skip is stated rather than implied.
        """
        state = self._store(source, client, decision)
        directory = os.path.dirname(output)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, NATIVE_INVENTORY_UNCHANGED_MARKER), 'w') as handle:
            json.dump({'reason': decision.reason, 'changelog_id': decision.watermark, 'skips': state['skips']}, handle)

        self.stdout.write('Skipped the fetch, the inventory already matches NetBox ({} skipped run(s) in a row).'.format(state['skips']))
        self.stdout.write('Checked in {:.1f}s'.format(time.time() - started))

    def _record(self, source, client, decision):
        state = self._store(source, client, decision)
        self.stdout.write('Recorded changelog position {}'.format(state['changelog_id']))

    def _store(self, source, client, decision):
        """Persist the bookkeeping without going through the model's save().

        A direct update keeps this out of the activity stream and away from the
        name and organization handling in InventorySource.save(), matching how
        the importer writes managed_inventory_variables.

        Merged into whatever is in the column rather than written over it, and
        under the same row lock the webhook receiver takes. A webhook that
        lands while this run is fetching records a pending burst in this same
        column; replacing it wholesale would drop that burst, and the change
        behind it would then wait for a schedule or the next changelog poll
        instead of syncing once this run finishes.
        """
        with transaction.atomic():
            locked = InventorySource.objects.select_for_update(of=('self',)).filter(pk=source.pk).first()
            state = dict((locked.source_sync_state if locked is not None else source.source_sync_state) or {})
            state.update(netbox_changelog.next_state(client, decision, state.get('skips')))
            InventorySource.objects.filter(pk=source.pk).update(source_sync_state=state)
        return state

    def _filters_for(self, config, key):
        """Shared `filters`, then the per-endpoint overrides on top.

        Devices and virtual machines do not accept the same filters: NetBox 4.7
        publishes 369 for one and 224 for the other. Sending a filter to the
        endpoint that lacks it either fails the request outright or, worse, is
        ignored and returns everything, so each endpoint gets its own set and
        `filters` stays for the names that genuinely apply to both.
        """
        filters = {}
        for candidate in (config.get('filters'), config.get(key)):
            if isinstance(candidate, dict):
                filters.update(candidate)
        if config.get('exclude_offline', False):
            filters.setdefault('status', 'active')
        return filters

    def _site_lookup(self, client):
        """Fetch what a device's nested site representation leaves out.

        A device carries the brief form of its site only, which stops at the
        slug. Region, site group, time zone and facility come from these three
        small list requests instead, and they are cheap: one page each for the
        whole estate, however many hosts it holds.
        """
        sites = list(client.paginate('/api/dcim/sites/', params={'fields': SITE_LOOKUP_FIELDS}))
        regions = list(client.paginate('/api/dcim/regions/', params={'fields': TREE_LOOKUP_FIELDS}))
        site_groups = list(client.paginate('/api/dcim/site-groups/', params={'fields': TREE_LOOKUP_FIELDS}))
        self.stdout.write('Fetched {} site(s), {} region(s), {} site group(s)'.format(len(sites), len(regions), len(site_groups)))
        return netbox_sync.build_site_lookup(sites=sites, regions=regions, site_groups=site_groups)

    def _fetch(self, client, path, label, config, filters):
        """Pull one object type, asking only for the fields the transform reads."""
        params = dict(filters)
        fields = netbox_sync.api_fields(config)
        if fields:
            params['fields'] = fields
        if filters:
            self.stdout.write('Filtering {} by {}'.format(label, ', '.join('{}={}'.format(k, v) for k, v in sorted(filters.items()))))

        objects = list(client.paginate(path, params=params or None))
        self.stdout.write('Fetched {} {}'.format(len(objects), label))
        return objects
