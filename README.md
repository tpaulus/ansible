# Ansible

Home-lab provisioning and maintenance. NetBox supplies production inventory
and the versioned `nas` share context; Ansible owns service configuration,
application versions and maintenance policy. Secrets stay in Ansible Vault,
unlocked through the 1Password CLI.

## Layout

| Path | Purpose |
| --- | --- |
| `inventories/production/netbox.yml` | Dynamic production inventory |
| `inventories/production/group_vars/` | Configuration and encrypted vaults |
| `inventories/ci/hosts.yml` | Synthetic inventory for local validation |
| `playbooks/site.yml` | Common, storage and K3s provisioning |
| `playbooks/provision/` | Focused provisioning entry points |
| `playbooks/operations/` | Explicit maintenance and migration operations |
| `playbooks/decommission/` | Destructive operations, never included by `site.yml` |
| `roles/` | Role-local defaults, tasks, handlers and templates |
| `.ansible/` | Downloaded Galaxy dependencies, ignored by Git |

## Controller setup

Run from the repository root. On macOS, use the standard Homebrew commands;
no repository-specific virtual environment or CLI wrapper is needed:

```sh
brew install ansible ansible-lint
"$(brew --prefix ansible)/libexec/bin/python" -m pip install jsonschema
```

The `ansible.utils.validate` JSON Schema engine needs `jsonschema` in
Ansible's Python. Homebrew has no standalone formula for that library, so
the command above installs it into Homebrew Ansible's existing environment,
not the system Python. Repeat that command after upgrading or reinstalling
the `ansible` formula. Homebrew's Ansible already includes `pynetbox` and
`pytz` for the NetBox inventory.

For other installations, use Python 3.13 or newer and install
`requirements-dev.txt`, plus `pynetbox` and `pytz`, into the Python environment
that runs Ansible.

Install the repository's Galaxy dependencies:

```sh
ansible-galaxy role install -r requirements.yml --roles-path .ansible/roles --force
ansible-galaxy collection install -r requirements.yml --collections-path .ansible/collections
```

Production inventory needs an authenticated 1Password CLI. Network provisioning
needs Go on the controller to build Unbound exporter. On macOS, install the
additional provisioning tools with `brew install go gnu-tar unzip`.

```sh
export NETBOX_TOKEN="$(op read 'op://Infra/Ansible/NETBOX_TOKEN')"
ansible-inventory --graph
ansible-playbook playbooks/site.yml --limit HOSTNAME \
  --vault-id 'op://Infra/Ansible/password@vault-1p-client.sh' --check
```

Use the same Vault argument for production commands below, including inventory
listing commands. Check mode is a preview; first-time installs and upstream
roles can need prerequisites from a real run. Avoid `--diff` for secret-bearing
configuration. Clear `NETBOX_TOKEN` after use.

## Provisioning

```sh
ansible-playbook playbooks/site.yml --list-hosts
ansible-playbook playbooks/site.yml --list-tags
ansible-playbook playbooks/provision/common.yml --limit HOSTNAME --tags motd
ansible-playbook playbooks/provision/k3s.yml
ansible-playbook playbooks/provision/network.yml --limit FIREWALL
ansible-playbook playbooks/provision/storage.yml --limit STORAGE
```

Network provisioning is deliberately separate from `site.yml`. Tags include
`base`, `monitoring`, `nut`, `motd`, `unattended_upgrades`, `storage`, `backups`,
`k3s` and `network`. Storage scopes also include `accounts`, `shares`,
`maintenance`, `logging`, `apps`, `jellyfin`, `prometheus` and `webdav`.
`--tags shares` reconciles storage and share roots without redeploying apps or
rotating passwords. `--tags logging` can bootstrap Alloy before pool adoption.

Keep environment settings in inventory, reusable settings in role defaults
with the role's prefix, and secrets in encrypted variable files.
Use FQCNs, modules rather than shell where practical, configuration validation
and handlers for restarts. Prefer a direct playbook over custom validation
frameworks or approval paperwork.

- Pin `node_exporter_version` in production `all.yml`; its textfile directory
  is shared with backup and maintenance collectors.
- K3s uses the upstream role's HA coordination. Do not add `serial: 1` or
  limit cluster bootstrap to one controller. Use its documented upgrade
  workflow and set `k3s_release_version` for a deliberate version change.
- K3s backup clients resolve the NetBox NFS export with purpose `k3s-etcd`.
  Reconcile storage separately when changing clients.
- NUT uses `nut_mode: primary`, `secondary` or `shutdown_only`. The existing
  `nut_primary` tag selects the primary. Timed shutdown is opt-in through
  `nut_timed_shutdown_enabled`; test shutdown ordering on actual hardware.
- Unbound exporter builds a pinned tag on the controller and rebuilds only
  when the installed version differs.

## Storage

Debian 13 amd64 runs OpenZFS, Samba/NFS, smartd/Sanoid, rootless Podman
applications and native Alloy. Never provision the old TrueNAS appliance
as a Debian host. OS installation and pool adoption are separate operations.

The pinned `alvistack.podman` role installs Podman 6.1.3 from its signed
Debian 13 repository; the rootful API remains masked. Our local role retains
the dedicated rootless accounts and per-user API services, and
only container-runtime packages may come from that repository. The Podman
package is held against unattended upgrades. Exporters use digest-pinned
v2 images for Podman 6, with both versions owned by storage `versions.yml`.

NetBox's Local Config Context Data owns the `nas` object, validated against
`roles/config/files/schema.json` (schema version 2). Each `nas.datasets` entry
owns its optional `smb` and `nfs` declarations; there are no separate share maps
or repeated dataset references. NFS clients can use networks or inventory groups.
Repository variables own `storage_pools`, `accounts_users`, `accounts_groups`,
applications, backup sources, disk identities and retention.

Declare only deviations from these defaults:

| Scope | Defaults |
| --- | --- |
| Dataset | `path`: approved pool mountpoint plus dataset-relative name; `owner`: root; `group`: owner; `mode`: `"2770"`; `create`: false |
| Private child | `kind`: dataset; owner/group: child username; `mode`: `"0700"`; `create`: false |
| SMB | name: dataset basename; all `valid_users` writable except `read_users`; browseable and shadow copies enabled; ABE and Time Machine disabled; no host restriction |
| NFS | writable; root squash; `security`: sys; `nas.nfs_protocols`: `[3, 4]` |

Missing `smb`/`nfs` means no share/export, not an unrestricted one. `valid_users`
and NFS `clients` remain mandatory. Set `write_users: []` for read-only SMB;
an explicit write list limits writers. `%U` paths still require declared private
children for every permitted user. Dataset creation always requires `create: true`.
ZFS properties are unmanaged unless explicitly declared, not reset to defaults.

```yaml
nas:
  schema_version: 2
  datasets:
    docs:
      dataset: tank/Docs
      group: nas-users
      smb:
        valid_users: [tom, mel]
    time_machine:
      dataset: tank/TimeMachine
      mode: "0711"
      private_children:
        tom: {}
        mel: {}
      smb:
        valid_users: [tom, mel]
        path_suffix: "%U"
        time_machine: true
    k3s_etcd:
      dataset: tank/Server Backups/k3s-etcd
      owner: nobody
      group: operator
      mode: "0755"
      nfs:
        purpose: k3s-etcd
        clients:
          - inventory_group: tags_k3s
```

Schema v1 is rejected rather than silently interpreted with new defaults.
When migrating, nest each old share under its dataset, remove its `dataset`
reference, and omit only values matching the defaults above. Preserve explicit
read-only policies, host restrictions, ownership and creation approvals.

### macOS SMB and Time Machine

All shares load `fruit` and `streams_xattr` so the first connection always
negotiates Apple's AAPL extensions. `fruit:copyfile = yes` enables macOS
server-side whole-file copies including metadata; these requests block the
client until complete and are not a promise of ZFS reflinks or cross-share copies.
SMB multichannel remains enabled. Samba's default `fruit:zero_file_id = yes`
avoids exposing inode numbers that macOS could mistake for stable catalog IDs.
Shadow copies wrap the fruit/stream modules so previous-version paths are
resolved before metadata access.

Only Time Machine shares advertise FULLSYNC and Avahi backup discovery.
`fruit:time machine = yes` also enforces durable handles and disables kernel
oplocks, kernel share modes and POSIX locking for those shares; do not disable
these locks globally on mixed SMB/NFS data. Keep backup data in dedicated
private child datasets. Set explicit ZFS quotas on the backing datasets when
capacity limits are needed; the current context does not impose arbitrary caps.
We do not use `fruit:time machine max size`: its sparsebundle-only estimate is
not a filesystem quota. Before rollout, reconnect Mac clients and exercise a
Finder duplicate, a Time Machine backup/restore, and previous-version access.

The existing `fruit:metadata = stream` and `fruit:resource = stream` storage
formats are preserved to avoid hiding resource forks during this configuration
change. Samba warns that resource streams with `streams_xattr` are constrained
by xattr size limits. AppleDouble sidecars avoid that limit but use an older
format; `fruit:resource = xattr` is not a portable Linux replacement merely
because the filesystem is ZFS. Any format change requires a separate metadata
inventory, migration and restore check. See the
[Samba fruit manual](https://www.samba.org/samba/docs/4.22/man-html/vfs_fruit.8.html).

Normal provisioning never creates/imports pools, upgrades ZFS features,
recursively changes share permissions or initializes Restic repositories.
Pool GUID checks and a small shell mount check prevent writing to the boot
filesystem when a dataset is missing. Private datasets remain root-owned or
owned by their declared application account.

### One-time prefix migration

Before provisioning an existing installation with these names, run:

```sh
ansible-playbook playbooks/operations/storage/migrate-prefixes.yml --limit STORAGE
```

This stops old `nas-*` timers/services and application Quadlets, removes their
managed configuration/helpers, then provisions and starts the replacements.
Expect a service interruption. Run it without tag filtering and let it finish;
if interrupted, rerun it. It does not delete datasets, backups, application
homes or maintenance history.

Roles, variables and tags now use `apps_*`, `storage_*`, `backup_*`, etc.,
rather than `nas_apps_*`, `nas_storage_*`, `nas_backup_*`. Application units
are `jellyfin.service`, `prometheus.service` and `webdav.service`.
Existing Unix accounts (`nas-jellyfin`, etc.), data paths, Vault variable names,
the NetBox `nas` key, exported metric names and Loki's `nas-journal` job label
are unchanged.
Update private extra-vars files and wrappers to use the new variable/tag names.

### Explicit storage operations

```sh
ansible-playbook playbooks/operations/storage/validate-context.yml --limit STORAGE
ansible-playbook playbooks/operations/storage/discover.yml --limit STORAGE \
  -e discovery_output=/absolute/private/path/discovery.json
ansible-playbook playbooks/operations/storage/reset-smb-passwords.yml --limit STORAGE \
  -e password_confirm=STORAGE
```

Discovery is read-only and omits secret environment values; it is not a backup.
Edit the NetBox share context directly using the schema. Keep discovery output,
configuration archives, credentials and application backups outside Git.

`prepare-host.yml`, `adopt-pool.yml` and `activate-pool.yml` handle OS cutover
and deliberate pool imports. Their explicit host/pool/backup confirmations
remain because these operations can affect data. Inspect their required
variables before running them; preserve a bootable rollback disk and console
access. Never force an import while another host can access the pool.

`convert-permissions.yml` requires stopped writers, a verified off-host metadata
archive and a selected `permission_roots` list or exhaustive
`permission_manifest`. Native recursive tools do not follow symlinks or cross
unselected dataset boundaries. Routine provisioning never converts legacy
NFSv4 ACLs or recursively remaps ownership.

### Applications and backups

`apps.yml` defines `apps_definitions` and `apps_accounts`; `versions.yml` pins
images and Alloy. Each application has a dedicated rootless account and
lingering user manager. Generated Quadlet services are started, not enabled
as ordinary installed services. Containers log to journald.

```sh
appctl ls
sudo appctl status jellyfin
sudo appctl restart prometheus
sudo appctl logs webdav --follow
```

`import-app-state.yml` restores offline state using copy or protected ZFS clones;
normal application runs never import databases. It requires
`apps_import_confirm: "IMPORT HOSTNAME"`, stopped-source/verified-backup
confirmations, and either a staging root/dataset or explicit clone mappings.
Never use the TrueNAS Docker graph as runtime storage. Restore state before
starting apps and preserve existing library paths and numeric ownership.
SMB password resets do not rotate WebDAV credentials.

Prometheus's original imported configuration is archived once and included
in application captures; routine configuration updates do not replace it.

`backup_sources` selects file datasets and app-consistent captures.
`capture-app NAME` stops the application, snapshots its private state, resumes
it, and copies a completed generation for Restic. Capacity checks protect the
pool reserve. `/usr/local/sbin/backup-to-b2` is the stable backup entry point.
Restic and cloud sync have separate timers and process locks.
Initialize repositories and unlock stale locks deliberately.

`backup_pruning_approved` and `maintenance_pruning_approved` control deletion
of backups/snapshots. `backup_cloud_sync_deletion_approved` switches the
encrypted cloud mirror from dry-run comparison to real synchronization.
Verify restores and review deletion plans before enabling them.

Sanoid uses repository retention policy; OpenZFS's packaged cron handles scrubs.
smartd logs to the journal. SMART/scrub collectors publish atomic node-exporter
textfiles, retaining successful scrub history across later scans. Disk policy
uses by-id paths and serial numbers, not `/dev/sdX`.
Alloy uses bounded persistent journals and a boot-filesystem OTLP queue.
Central alerts remain in the separate K3s repository.

## Maintenance and decommissioning

Maintenance requires an explicit `--limit` and processes hosts one at a time:

```sh
ansible-playbook playbooks/operations/compute/install-updates.yml --limit HOSTNAME
ansible-playbook playbooks/operations/compute/cleanup-docker.yml --limit HOSTNAME
ansible-playbook playbooks/operations/storage/unlock-restic-backends.yml --limit STORAGE
```

Docker cleanup removes stopped containers and all unused images, not volumes.
For Debian 12 to 13 upgrades, run `dist-upgrade-pre.yml`, reboot if required,
review third-party repositories, then run `dist-upgrade-exec.yml` with
`dist_upgrade_third_party_reviewed=true`. Reboot into Debian 13 before
`dist-upgrade-post.yml`. Only official Debian source files are rewritten.

```sh
ansible-playbook playbooks/decommission/k3s.yml --limit NODE \
  -e k3s_decommission_confirm=NODE \
  -e k3s_decommission_control_host=OTHER_CONTROL_PLANE
```

Decommissioning requires a different healthy control plane and adequate etcd
quorum. It drains/stops the node and removes membership, without erasing data.

## Validation

```sh
ANSIBLE_INVENTORY=inventories/ci/hosts.yml ansible-lint --offline
bash scripts/check-syntax.sh
"$(brew --prefix ansible)/libexec/bin/python" -m unittest discover -s tests
```

CI runs these checks with pinned dependencies (use `python` for the filter tests
outside Homebrew). Focused filter/template tests cover share defaults and access
policy; no playbook/script unit-test suite is maintained. Do not suppress lint rules. Syntax and lint checks do not
replace checking mounts, client access, application health, backup restores
and reboot behavior on the actual hosts.
