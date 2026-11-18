# NetBox Inventory

Ascender can build an inventory directly from the NetBox REST API. Devices and
virtual machines become hosts, the objects they relate to become groups, and
NetBox config context becomes host variables.

This source talks to NetBox from the control plane. It does not run
`ansible-inventory`, it does not use the `netbox.netbox` collection, and it
does not need an execution environment. Nothing has to be added to an EE for
it to work.

NetBox 4.6 is the minimum. The sync reads the version on connect and stops
with a clear error on anything older.

## Setting up a source

Create a NetBox credential first. It has three fields:

 - `host` - base URL of the NetBox instance, for example
   `https://netbox.example.org`. No trailing `/api`.
 - `token` - API token with read access to the objects you want. Leave it
   blank only if the instance allows anonymous reads.
 - `verify_ssl` - defaults to on.

Then add an inventory source with `source` set to `netbox` and that credential
selected. Nothing else is required. The remaining fields are the ones every
cloud source has: verbosity, host filter, enabled variable and value, the
overwrite and update-on-launch options, and source variables.

The token only has to read. These are every endpoint the sync touches:

```
/api/status/                          version check
/api/schema/                          filter name validation
/api/dcim/devices/                    hosts
/api/virtualization/virtual-machines/ hosts
/api/dcim/sites/                      site attributes a device does not carry
/api/dcim/regions/                    region tree
/api/dcim/site-groups/                site group tree
/api/core/object-changes/             incremental sync only
/api/core/object-types/               incremental sync only
```

Tenants, platforms, racks, tags, custom fields, config context and IP
addresses all arrive nested in the device and virtual machine payloads, so
none of them costs a request of its own.

## Outbound proxy

The fetch runs on the control plane, so it leaves by the control plane's
egress path rather than an execution node's. Where that path goes through a
proxy, set it in **Settings > Jobs > Extra Environment Variables**
(`ASCENDER_TASK_ENV`), which already applies to inventory updates:

```json
{
  "HTTPS_PROXY": "http://proxy.example.net:8080",
  "NO_PROXY": "netbox.internal.example.net"
}
```

The client reads these from its process environment, so `HTTP_PROXY`,
`HTTPS_PROXY` and `NO_PROXY` behave as they do anywhere else. `NO_PROXY` is
the one to reach for when NetBox is internal and the proxy exists only for
egress to the internet.

This setting is global rather than per source. A deployment that needs one
NetBox proxied and another not can separate them with `NO_PROXY`.

## What you get

Every device and virtual machine becomes a host, named after the NetBox
object. Hosts are matched on `netbox_instance_id`, which is the object type
and the NetBox primary key, so renaming a device in NetBox updates the
existing host rather than orphaning it and creating a second one.

Objects whose status is not active are imported **disabled** rather than
dropped, which is what the `ec2` and `vmware` sources do. They stay visible in
the inventory, they keep their group memberships, and no play targets them.

### Host variables

Everything NetBox knows about the object arrives under a `netbox_` prefix:

 - `netbox_object_type`, `netbox_instance_id`, `netbox_id`, `netbox_name`,
   `netbox_display`, `netbox_url`
 - `netbox_status`, `netbox_description`, `netbox_comments`
 - `netbox_site`, `netbox_location`, `netbox_rack`, `netbox_tenant`,
   `netbox_platform`, `netbox_cluster`, `netbox_device_type`, `netbox_role`,
   `netbox_manufacturer`
 - `netbox_regions` and `netbox_site_groups`, each resolved up the tree so a
   host in a child region is also in its parents
 - `netbox_site_time_zone` and `netbox_site_facility`
 - `netbox_serial`, `netbox_asset_tag`, `netbox_position` on devices
 - `netbox_vcpus`, `netbox_memory`, `netbox_disk` on virtual machines
 - `netbox_tags`, `netbox_custom_fields`
 - `netbox_oob_ip` and `netbox_primary_ip6`. The primary IPv4 address is not
   repeated under its own key, because it is already `ansible_host`.
 - `netbox_config_context` and `netbox_local_context_data`

A device carries only the brief form of its site, which stops at the slug. The
region, site group, time zone and facility come from three extra list requests
covering the whole estate, not one request per host.

### Groups

Each related object produces a group, and the groups of one kind sit under a
parent group named after that kind:

| Parent | Members |
|---|---|
| `sites` | `site_<slug>` |
| `regions` | `region_<slug>` |
| `site_groups` | `site_group_<slug>` |
| `locations` | `location_<slug>` |
| `racks` | `rack_<name>` |
| `tenants` | `tenant_<slug>` |
| `platforms` | `platform_<slug>` |
| `clusters` | `cluster_<name>` |
| `device_types` | `device_type_<model>` |
| `roles` | `role_<slug>` |
| `manufacturers` | `manufacturer_<slug>` |
| `statuses` | `status_<value>` |
| `tags` | `tag_<slug>` |

Everything is also in `devices` or `virtual_machines`. Group names are
lowercased, and anything outside letters, digits and underscores collapses to
an underscore, so a rack called `DC1-R01` produces `rack_dc1_r01`. A name that
would start with a digit gets a leading underscore, because Ansible group
names may not.

Status and tag grouping can be turned off with `group_by_status` and
`group_by_tag` if they inflate the group count more than they help.

## Config context

Config context is the closest thing NetBox has to host variables, so it is
merged into the host variables by default. NetBox renders it per object,
applying weight order and then the object's own local context data, and the
result arrives already resolved.

Three keys are involved:

 - `netbox_config_context` is the rendered result, kept whole.
 - `netbox_local_context_data` is what was set on the object itself, kept
   separate so a play can tell what an object declared from what it inherited.
 - The rendered context is also merged flat into the host variables, so a
   play reads `cis_profile` rather than `netbox_config_context.cis_profile`.

Set `merge_config_context: false` to keep the flat merge from happening and
leave `netbox_config_context` as the only copy. Set `config_context: false` to
stop requesting it at all, which is worth doing on a large estate that does
not use it: rendering config context is the expensive part of listing devices
in NetBox.

> The `netbox.netbox` inventory plugin tries to skip this with
> `?exclude=config_context`. That parameter stopped working in NetBox 4.6.
> NetBox returns 200 and renders the context anyway. This source uses
> `?fields=`, which still works, and leaves `config_context` out of the field
> list when it is not wanted.

## Choosing the connection address

`ansible_host` is set from the first of these that exists:

 1. `ansible_host` from config context, when config context is being merged.
 2. The out-of-band address, when `oob_ip_as_primary_ip` is on.
 3. The primary IPv4 address, then any primary address, then IPv6.

Config context comes first because NetBox has already resolved its own
precedence by the time the value arrives, which makes it the most specific
answer NetBox holds rather than a site-wide default. It also keeps
`ansible_host` consistent with `ansible_user`, `ansible_port` and every other
`ansible_*` key, all of which arrive the same way.

The out-of-band address is always available as `netbox_oob_ip` whether or not
it is used for the connection. For iDRAC, iLO and BMC work this is usually the
address a play needs.

Set `use_fqdn: true` to name hosts after the NetBox primary IP's DNS name
where one exists.

## Filtering

Filters are passed to NetBox as query parameters, so anything the NetBox API
accepts works, including custom field lookups with the `cf_` prefix:

```yaml
filters:
  tag: ansible-managed
device_filters:
  rack_id: 12
  cf_owner_team: platform
vm_filters:
  cluster: dc1-vmw-prod
```

`filters` applies to both endpoints. `device_filters` and `vm_filters` apply to
one each and override the shared set. Devices and virtual machines do not take
the same filters: NetBox 4.7 publishes 369 for one and 224 for the other, so
`cluster` on a device request is an error and `rack_id` on a virtual machine
request is one too.

Filter names are checked against the NetBox API schema before the sync runs,
and an unrecognised name fails the job with a suggestion:

```
Unknown device filter 'rack' (did you mean 'rack_id'?)
```

This matters because NetBox ignores a filter name it does not recognise and
returns **every** object. Without the check, a typo silently widens the sync to
the whole estate and the job still reports success.

`exclude_offline: true` adds `status=active`, which drops non-active objects
instead of importing them disabled.

## Source variables

All of these are optional and go in the source variables field.

| Variable | Default | Effect |
|---|---|---|
| `filters` | none | Query parameters for both endpoints |
| `device_filters` | none | Query parameters for devices only |
| `vm_filters` | none | Query parameters for virtual machines only |
| `exclude_offline` | `false` | Drop non-active objects rather than importing them disabled |
| `config_context` | `true` | Request config context from NetBox |
| `merge_config_context` | `true` | Merge the rendered context flat into host variables |
| `flatten_custom_fields` | `false` | Also merge custom fields flat into host variables |
| `group_by_status` | `true` | Create `status_*` groups |
| `group_by_tag` | `true` | Create `tag_*` groups |
| `oob_ip_as_primary_ip` | `false` | Prefer the out-of-band address for `ansible_host` |
| `use_fqdn` | `false` | Name hosts by DNS name where the primary IP has one |
| `include_raw` | `false` | Keep the whole NetBox object as `netbox_raw` |
| `incremental` | `false` | Skip a sync when nothing relevant changed |
| `incremental_max_skips` | `24` | Force a full sync after this many skips |
| `changelog_watch_all` | `false` | Treat any change as relevant, not just the watched types |
| `webhook_debounce_seconds` | `15` | Quiet period before a webhook triggers a sync |

## Incremental sync

Off by default. With `incremental: true`, each run first asks
`/api/core/object-changes/` whether anything has changed since the last run.
If nothing relevant has, the sync stops without fetching the estate and the
inventory is left as it is.

A skipped run takes about a tenth of a second, and that figure does not move
with the size of the estate. Skipping the 2023 host estate below costs the
same 0.1s as skipping a dozen:

```
Incremental check: no NetBox changes at all since position 2162
Skipped the fetch, the inventory already matches NetBox (1 skipped run(s) in a row).
Checked in 0.1s
```

Seventeen object types are watched, covering everything that can change a host
or a group: devices, virtual machines, the groupable objects, regions, site
groups, roles, manufacturers, config contexts, tags, custom fields and IP
addresses. `changelog_watch_all: true` widens this to any change at all, for
anyone who would rather not maintain that judgement.

Three things force a full sync regardless:

 - `incremental_max_skips` consecutive skips, 24 by default.
 - The previous run failed.
 - The recorded changelog position is older than NetBox still holds.
   `CHANGELOG_RETENTION` defaults to 90 days, and a position that has aged out
   cannot be reasoned about.

The position is stored on the inventory source, so turning the option on and
off is safe at any time.

## Webhooks

A NetBox event rule can trigger a sync, so a change reaches the inventory in
seconds rather than at the next schedule.

Each inventory source has its own receiver and key:

```
/api/v2/inventory_sources/<id>/netbox/       the URL NetBox posts to
/api/v2/inventory_sources/<id>/webhook_key/  the shared secret, GET to read, POST to rotate
```

Point a NetBox webhook at the first, set its secret to the value of the
second, and attach it to an event rule for the object types you care about.
Signatures are verified as HMAC-SHA512 over the request body, which is what
NetBox sends in `X-Hook-Signature`. A request with no valid signature is
rejected.

NetBox delivers webhooks through its RQ worker. If no worker is running, the
event rule fires and nothing arrives.

A webhook does not carry inventory data and nothing in the payload is used
beyond the object type. It marks the source as needing a sync. A background
pass then launches at most one sync per source once the burst has settled, and
only when that source has no update already running.

The debounce matters. A bulk edit or a scripted import in NetBox can fire
hundreds of webhooks in a few seconds, and one sync per webhook would flatten
the control plane. `webhook_debounce_seconds` is the quiet period a source
waits for before it syncs, 15 seconds by default. Raise it if NetBox changes
arrive in long bursts.

## Why this runs on the control plane

`BaseTask` already had two dispatch paths, one running through receptor into
an execution environment and one running in process. System jobs used the
second. A `runs_on_control_plane` property generalises that check, and the
NetBox source answers true.

The far end is unchanged. The sync writes the same
`artifacts/<id>/output.json` that `ansible-inventory --list` would, in the same
place, and `post_run_hook` hands it to the same importer. Host matching,
group reconciliation, `overwrite`, host filters and `enabled_var` are the
existing code paths, not reimplementations.

This is also why a cheap changed-since check is worth having. If every check
cost a container launch there would be no point making one.

### What the control plane position means

The fetch happens in the Ascender process, so the request leaves from the
control plane's network position rather than from an execution environment.
Anyone who can create a credential and an inventory source can therefore cause
an HTTP request to any address the control plane can reach, and see part of
the response in the job output.

Every other inventory source reaches its API from inside an execution
environment, so this is new. It is the part of the design to argue about, and
it is written down here rather than left to be found. An operator who wants a
boundary has the control plane's own egress rules, and control over who holds
`add` on inventories and credentials.

What the client does not do is widen it any further. The `next` link on a
paginated list and a redirect `Location` are both chosen by whatever answered
the previous request, so following either as given would let that host move
the request somewhere nobody configured. Pagination keeps the path and query
and takes the origin from the credential, and a redirect is refused with the
address it pointed at in the job output.

## Scale

Measured against the development stack on a laptop, with a NetBox holding
2012 devices and 11 virtual machines:

```
Fetched 2012 devices
Fetched 11 virtual machines
Built 2023 host(s) in 61 group(s)
Wrote inventory to .../output.json in 2.6s
Inventory import completed in 3.9s
```

9.7 seconds end to end on a steady-state run, of which 2.6 seconds is NetBox
and the rest is the inventory importer every source shares. The first run,
which creates all 2023 hosts rather than updating them, takes 14.3 seconds.

Config context is the expensive half of the NetBox side. Turning it off with
`config_context: false` on an estate that does not use it is the largest
saving available.

Reproduce it with `tools/docker-compose/netbox-seed.py --scale 2000`.

## Development environment

`make docker-compose NETBOX=true` brings up NetBox alongside the development
stack, reachable at <http://localhost:8000> and at `http://netbox:8080` from
inside the compose network. Set `NETBOX_PORT` to use a different host port.

`tools/docker-compose/netbox-seed.py` fills it with an estate: two region
trees, three sites, racks, tenants, clusters, twelve devices and eleven virtual
machines, out-of-band addresses on the hardware, four config contexts that
overlap so the merge order is visible, and three objects left offline so the
disabled import can be seen. It uses the standard library only and is safe to
re-run.

```console
$ tools/docker-compose/netbox-seed.py
NetBox 4.7.5 at http://localhost:8000
...
12 devices and 11 virtual machines in NetBox.
```

The development NetBox is created with `admin` / `admin` and a fixed API
token. Both are in
`tools/docker-compose/ansible/roles/sources/defaults/main.yml`. They are
development values and this instance holds nothing worth protecting.

`--scale N` adds N plain devices on top, for timing a sync against a large
estate.

## Troubleshooting

**`NetBox 4.5.9 is too old for this integration, which needs 4.6 or newer.`**
Earlier releases have REST performance problems, and holding the line at 4.6
keeps compatibility shims for renamed fields and moved endpoints out of the
client.

**`NetBox rejected the credentials for GET /api/status/ (HTTP 403).`** - the
token is wrong, expired, or has no read access. A 401 or 403 is not retried,
because nothing about retrying it would change the answer.

**`Could not reach NetBox at ... after 3 attempts`** - a connection or DNS
failure, with the underlying error on the same line. Connection errors and
HTTP 5xx are retried three times with a backoff, so this means all three
failed.

**`Unknown device filter '<name>'`** - the filter is not in the NetBox API
schema. The message suggests the closest match. The schema is fetched once per
NetBox version and cached.

**`Could not read the NetBox API schema`** - the token cannot reach
`/api/schema/`. The sync stops rather than running with filters NetBox might
ignore.

**A sync reports success but nothing changed** - check whether `incremental`
is on. A skipped run says so in its output, and says how many it has skipped
in a row.

**Hosts arrive disabled** - their NetBox status is not active. This is the
default behaviour. Use `exclude_offline: true` to drop them instead.
