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

from django.core.management.base import BaseCommand, CommandError

from ascender.main.models.inventory import InventoryUpdate
from ascender.main.netbox.client import NetBoxClient, NetBoxError
from ascender.main.netbox import sync as netbox_sync


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

        try:
            version = client.require_supported_version()
            self.stdout.write('NetBox version {}'.format(version or 'unknown'))

            devices = self._fetch(client, '/api/dcim/devices/', 'devices', config)
            vms = self._fetch(client, '/api/virtualization/virtual-machines/', 'virtual machines', config)
        except NetBoxError as exc:
            # A clean one-line failure in the job log beats a traceback.
            raise CommandError(str(exc))

        inventory = netbox_sync.build_inventory(devices=devices, virtual_machines=vms, config=config)

        host_count = len(inventory['_meta']['hostvars'])
        group_count = len([k for k in inventory if k != '_meta'])
        self.stdout.write('Built {} host(s) in {} group(s)'.format(host_count, group_count))

        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, 'w') as handle:
            json.dump(inventory, handle)

        self.stdout.write('Wrote inventory to {} in {:.1f}s'.format(output, time.time() - started))

    def _fetch(self, client, path, label, config):
        """Pull one object type, applying any filters from source_vars."""
        params = {}
        # `filters` in source_vars passes straight through as NetBox query
        # parameters, so an operator can scope a source to a site or tag
        # without this needing to know about every NetBox filter.
        filters = config.get('filters')
        if isinstance(filters, dict):
            params.update(filters)

        if config.get('exclude_offline', False):
            params.setdefault('status', 'active')

        objects = list(client.paginate(path, params=params or None))
        self.stdout.write('Fetched {} {}'.format(len(objects), label))
        return objects
