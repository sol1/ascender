import type { SummaryFieldRef } from 'types/api';
import React from 'react';
import { useLingui } from '@lingui/react/macro';
import { Link } from 'react-router';

const ansibleDocUrls = {
  ec2: 'https://docs.ansible.com/ansible/latest/collections/amazon/aws/aws_ec2_inventory.html',
  azure_rm:
    'https://docs.ansible.com/ansible/latest/collections/azure/azcollection/azure_rm_inventory.html',
  ascender:
    'https://docs.ansible.com/ansible/latest/collections/awx/awx/tower_inventory.html',
  gce: 'https://docs.ansible.com/ansible/latest/collections/google/cloud/gcp_compute_inventory.html',
  openstack:
    'https://docs.ansible.com/ansible/latest/collections/openstack/cloud/openstack_inventory.html',
  satellite6:
    'https://docs.ansible.com/ansible/latest/collections/theforeman/foreman/foreman_inventory.html',
  // NetBox is a native integration rather than an Ansible inventory plugin, so
  // this points at NetBox's own API reference. Its source variables are
  // documented by netboxSourceVars below, not by any collection.
  netbox: 'https://netboxlabs.com/docs/netbox/integrations/rest-api/',
  vmware:
    'https://docs.ansible.com/ansible/latest/collections/community/vmware/vmware_vm_inventory_inventory.html',
  constructed:
    'https://docs.ansible.com/ansible/latest/collections/ansible/builtin/constructed_inventory.html',
  terraform:
    'https://github.com/ansible-collections/cloud.terraform/blob/stable-statefile-inventory/plugins/inventory/terraform_state.py',
};

function useInventoryHelpTextStrings() {
  const { t } = useLingui();
  return {
    labels: t`Optional labels that describe this inventory,
          such as 'dev' or 'test'. Labels can be used to group and filter
          inventories and completed jobs.`,
    variables: () => {
      const jsonExample = `
      {
        "somevar": "somevalue"
        "somepassword": "Magic"
      }
    `;
      const yamlExample = `
      ---
      somevar: somevalue
      somepassword: magic
    `;

      return (
        <>
          {t`Variables must be in JSON or YAML syntax. Use the radio button to toggle between the two.`}
          <br />
          <br />
          {t`JSON:`}
          <pre>{jsonExample}</pre>
          <br />
          {t`YAML:`}
          <pre>{yamlExample}</pre>
          <br />
          {t`View JSON examples at`}{' '}
          <a
            href="http://www.json.org"
            target="_blank"
            rel="noopener noreferrer"
          >
            {t`www.json.org`}
          </a>
          <br />
          {t`View YAML examples at`}{' '}
          <a
            href="http://docs.ansible.com/YAMLSyntax.html"
            target="_blank"
            rel="noopener noreferrer"
          >
            {t`docs.ansible.com`}
          </a>
        </>
      );
    },
    subFormVerbosityFields: t`Control the level of output Ansible will produce for inventory source update jobs.`,
    vmwarePlugin: t`Select the Ansible collection providing the inventory plugin used to sync from vCenter. The community.vmware collection is deprecated in favor of the newer vmware.vmware collection. The selection is applied via the "plugin" key in the source variables; when the key is absent, the default collection is used.`,
    subFormOptions: {
      overwrite: (
        <>
          {t`If checked, any hosts and groups that were previously present on the external source but are now removed will be removed from the inventory. Hosts and groups that were not managed by the inventory source will be promoted to the next manually created group or if there is no manually created group to promote them into, they will be left in the "all" default group for the inventory.`}
          <br />
          <br />
          {t`When not checked, local child hosts and groups not found on the external source will remain untouched by the inventory update process.`}
        </>
      ),
      overwriteVariables: (
        <>
          {t`If checked, all variables for child groups and hosts will be removed and replaced by those found on the external source.`}
          <br />
          <br />
          {t`Inventory variables this source added on an earlier sync are removed once the source no longer provides them, unless they were edited since or another source of this inventory still provides them.`}
          <br />
          <br />
          {t`When not checked, a merge will be performed, combining local variables with those found on the external source.`}
        </>
      ),
      // The project the inventory source syncs from, absent until one is
      // picked, which is what the paragraph below is conditional on.
      updateOnLaunch: ({ value }: { value?: SummaryFieldRef | null }) => (
        <>
          <div>
            {t`Each time a job runs using this inventory, refresh the inventory from the selected source before executing job tasks.`}
          </div>
          <br />
          {value && (
            <div>
              {t`If you want the Inventory Source to update on launch, click on Update on Launch, and also go to `}
              <Link to={`/projects/${value.id}/details`}> {value.name} </Link>
              {t`and click on Update Revision on Launch.`}
            </div>
          )}
        </>
      ),
      updateOnProjectUpdate: ({
        value,
      }: {
        value?: SummaryFieldRef | null;
      }) => (
        <>
          <div>
            {t`After every project update where the SCM revision changes, refresh the inventory from the selected source before executing job tasks. This is intended for static content, like the Ansible inventory .ini file format.`}
          </div>
          <br />
          {value && (
            <div>
              {t`If you want the Inventory Source to update on launch, click on Update on Launch, and also go to `}
              <Link to={`/projects/${value.id}/details`}> {value.name} </Link>
              {t`and click on Update Revision on Launch`}
            </div>
          )}
        </>
      ),
      cachedTimeOut: t`Time in seconds to consider an inventory sync to be current. During job runs and callbacks the task system will evaluate the timestamp of the latest sync. If it is older than Cache Timeout, it is not considered current, and a new inventory sync will be performed.`,
    },
    enabledVariableField: t`Retrieve the enabled state from the given dict of host variables. The enabled variable may be specified using dot notation, e.g: 'foo.bar'`,
    sourceControlBranch: t`Branch to use on inventory sync. Project default used if blank. Only allowed if project allow_override field is set to true.`,
    enabledValue: t`This field is ignored unless an Enabled Variable is set. If the enabled variable matches this value, the host will be enabled on import.`,
    hostFilter: t`Regular expression where only matching host names will be imported. The filter is applied as a post-processing step after any inventory plugin filters are applied.`,
    netboxSourceVars: () => (
      <>
        {t`Variables that configure what this source pulls from NetBox and how it
        is shaped. NetBox is a native integration, so these are Ascender's own
        options rather than an Ansible inventory plugin's.`}
        <br />
        <br />
        <strong>{t`Scope`}</strong>
        <br />
        {t`filters: NetBox query filters applied to both devices and virtual machines.
        device_filters and vm_filters add to it for one object type only, which is
        needed because the two endpoints do not accept the same filters. Filter
        names are checked against the NetBox API schema before anything is fetched,
        and the sync fails on one NetBox would ignore.`}
        <br />
        {t`exclude_offline: fetch only objects whose status is active. Off by default,
        because non-active objects are imported disabled rather than dropped.`}
        <br />
        <br />
        <strong>{t`Host names and addresses`}</strong>
        <br />
        {t`use_fqdn: name each host after its primary IP's DNS name when NetBox has one.`}
        <br />
        {t`oob_ip_as_primary_ip: connect to the out-of-band address, the iDRAC, iLO or
        BMC, instead of the primary IP.`}
        <br />
        {t`ansible_host is resolved in this order: a value merged from config context,
        then the out-of-band address when oob_ip_as_primary_ip is set, then primary
        IPv4, any primary IP, and IPv6.`}
        <br />
        <br />
        <strong>{t`Host variables`}</strong>
        <br />
        {t`config_context: fetch NetBox config context at all. On by default. Turning it
        off makes the fetch noticeably cheaper on a large NetBox, because rendering
        config context is the expensive part of listing devices.`}
        <br />
        {t`merge_config_context: also place each config context key at the top level as a
        host variable. On by default. The raw mapping is always available as
        netbox_config_context, and anything set on the object itself as
        netbox_local_context_data.`}
        <br />
        {t`flatten_custom_fields: place custom fields at the top level as well as in
        netbox_custom_fields.`}
        <br />
        {t`include_raw: attach the whole NetBox object as netbox_raw. Useful for working
        out what is available, expensive for a routine sync.`}
        <br />
        <br />
        <strong>{t`Incremental sync`}</strong>
        <br />
        {t`incremental: ask NetBox's changelog whether anything relevant has changed
        before fetching, and skip the fetch when nothing has. Off by default.`}
        <br />
        {t`The inventory importer performs a full reconcile, so this skips whole runs
        rather than applying deltas. The watch list covers every NetBox object type
        that can change this inventory, including config contexts, tags, sites and
        IP addresses, none of which record a change against the device they affect.`}
        <br />
        {t`changelog_watch_all: fetch on any change at all rather than trusting the
        watch list. Skips far less often, and cannot miss anything the changelog
        recorded.`}
        <br />
        {t`incremental_max_skips: force a full fetch after this many skipped runs in a
        row, 24 by default. This bounds how stale the inventory can get from a change
        the changelog never recorded, such as a direct database write or a restore.`}
        <br />
        {t`A full fetch also happens whenever the position is missing or has been
        pruned, the NetBox URL changed, or the previous run did not succeed.`}
        <br />
        <br />
        <strong>{t`Groups`}</strong>
        <br />
        {t`group_by_status and group_by_tag: both on by default. Groups for site, region,
        site group, location, rack, tenant, platform, cluster, device type, role and
        manufacturer are always built.`}
        <br />
        <br />
      </>
    ),
    sourceVars: (docsBaseUrl: string, source: string) => {
      const docsUrl = `${docsBaseUrl}/userguide/inventories.html#inventory-plugins`;
      let sourceType = '';
      if (source && source !== 'scm') {
        const type =
          ansibleDocUrls[source as keyof typeof ansibleDocUrls].split(/[/,.]/);
        sourceType = type[type.length - 2] as string;
      }
      return (
        <>
          {t`Variables used to configure the inventory source. For a detailed description of how to configure this plugin, see`}{' '}
          <a
            href={docsBaseUrl ? docsUrl : ''}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t`Inventory Plugins`}
          </a>{' '}
          {t`in the documentation and the`}{' '}
          <a
            href={ansibleDocUrls[source as keyof typeof ansibleDocUrls]}
            target="_blank"
            rel="noopener noreferrer"
          >
            {sourceType}
          </a>{' '}
          {t`plugin configuration guide.`}
          <br />
          <br />
        </>
      );
    },
    constructedInventorySourceVars: () => {
      const yamlExample = `
      ---
      plugin: constructed
      strict: true
      use_vars_plugins: true
    `;
      return (
        <>
          {t`Variables used to configure the constructed inventory plugin. For a detailed description of how to configure this plugin, see`}{' '}
          <a
            href={ansibleDocUrls.constructed}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t`constructed inventory`}
          </a>{' '}
          {t`plugin configuration guide.`}
          <br />
          <br />
          <hr />
          <br />
          {t`Variables must be in JSON or YAML syntax. Use the radio button to toggle between the two.`}
          <br />
          <br />
          {t`YAML:`}
          <pre>{yamlExample}</pre>
        </>
      );
    },
    sourcePath: t`The inventory file to be synced by this source. You can select from the dropdown or enter a file within the input.`,
    preventInstanceGroupFallback: t`If enabled, the inventory will prevent adding any organization instance groups to the list of preferred instances groups to run associated job templates on. Note: If this setting is enabled and you provided an empty list, the global instance groups will be applied.`,
    allowDeletesWhileInUse: t`If enabled, hosts can be deleted from this inventory while jobs are running against it. By default those deletions are rejected until the running jobs finish.`,
    allowJobsWhileSyncing: t`If enabled, jobs and ad hoc commands against this inventory start while it is being synced, using the hosts it had before the sync. By default they wait for the sync to finish. With update on launch, a job still waits for a sync started for it, and for a running one when that source has never synced successfully.`,
    enabledOptions: (
      <>
        <p>
          {t`Prevent Instance Group Fallback: If enabled, the inventory will prevent adding any organization instance groups to the list of preferred instances groups to run associated job templates on.`}
        </p>
        <p>
          {t`Allow Deletes While In Use: If enabled, hosts can be deleted from this inventory while jobs are running against it.`}
        </p>
        <p>
          {t`Allow Jobs While Syncing: If enabled, jobs against this inventory do not wait for a sync of it to finish.`}
        </p>
      </>
    ),
  };
}

export default useInventoryHelpTextStrings;
