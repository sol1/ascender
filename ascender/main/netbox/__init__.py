"""Native NetBox integration.

Ascender talks to NetBox directly over its own API rather than shelling out to
the netbox.netbox Ansible collection. That removes the execution environment
from the inventory path entirely, and it means the integration can use parts of
NetBox the inventory plugin does not expose: the changelog for incremental
sync, event rules for push updates, and writes back to the source of truth.
"""
