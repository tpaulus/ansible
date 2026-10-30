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
ansible-playbook playbooks/operations/applier.yml
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

The MOTD records its render time in UTC as `Last Ansible apply`; this updates
whenever the `motd` role runs, not after every unrelated playbook or as proof
that an entire run succeeded. After applying on actual hosts, check the SSH
login banner: `Booted` should immediately follow the hostname, NetBox tags
should appear on one line below `Role` (and be omitted for untagged nodes),
and the apply timestamp should match the MOTD run.

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

### Swap tuning

The `base` role sets `vm.swappiness=1` immediately and persistently on **all
inventory hosts**, including compute, storage, K3s and firewall hosts. This
requires Linux and strongly favors reclaiming file-backed pages over swapping
anonymous memory, reducing routine SSD swap writes while keeping existing swap
available as an emergency buffer. It does not guarantee zero swap I/O or
prevent out-of-memory kills. Active swap, `/etc/fstab`, systemd units, swap
files and partitions are left untouched; hosts without swap remain without it.

Apply through `playbooks/site.yml` or `playbooks/provision/common.yml` with
`--tags base` (and the production Vault argument above). No hosts are changed
until the playbook is applied. Existing swapped-out pages are not forcibly
brought back into RAM.

On actual hosts, confirm `sysctl vm.swappiness` reports `1` and
`swapon --show` lists the same configured swap areas as before. Repeat after
a planned reboot to verify persistence. Observe `vmstat 1` under representative
load for swap-in/swap-out activity (`si`/`so`), rather than expecting existing
swap usage in `free -h` to drop immediately. Confirm a second `--tags base`
run leaves swap tuning unchanged.

### Reboot monitoring

Unattended upgrades automatically reboot non-K3s compute hosts at 04:00 when an
update requires it. Storage hosts do not reboot automatically: after a kernel
update, check `dkms status` and confirm the ZFS module for the updated kernel
is available with `modinfo -k <updated-kernel-version> zfs` before rebooting
manually. K3s nodes leave reboot coordination to Kured. Firewall hosts are not
included in this update or monitoring policy.

The `monitoring` tag installs `reboot-metrics.timer` on non-K3s compute and storage
hosts. Every minute (starting 15 seconds after boot), it atomically publishes the
`node_reboot_required` gauge to the shared node-exporter textfile directory:
`1` when `/run/reboot-required` exists and `0` otherwise. Collector failures are
reported in `reboot-metrics.service`'s journal and fail the service; the existing
textfile is preserved until the next successful collection. Node exporter's
`node_textfile_mtime_seconds` exposes the last successful publication time.
K3s nodes are skipped, leaving their reboot telemetry to Kured.

The central Prometheus rule belongs in `tpaulus/kube-config`, alongside Kured's
alerts: `node_reboot_required == 1` with `for: 24h`. This means 24 hours of
continuously observed reboot-required status, not a scheduled reboot or an
estimate based on marker file modification time. Deploy the monitoring role and
the central alert rule together; this repository does not manage central alerts.

### Ansible run monitoring

The final play of `site.yml` runs the `last_run_metrics` role on every inventory
host, but publishes the metric only when a Node Exporter service is running. It
atomically writes `ansible_last_run_timestamp_seconds`, the controller's Unix
time at completion, to `ansible_last_run.prom` in the shared node-exporter
textfile directory. The play is tagged `always`, so tag-limited `site.yml` runs
count, but only hosts that completed every earlier play are stamped. Check mode,
hosts without a running Node Exporter, failed or unreachable hosts, and the
focused `playbooks/provision/` entry points or other playbooks leave the
previous value in place.

`AnsibleRunStale` in `tpaulus/kube-config`'s `_alerts/ansible-runner.yaml`
fires when a host's timestamp is more than seven days old. A host that has
never published the metric does not alert. After deploying, confirm on a host
that `/var/lib/node_exporter/ansible_last_run.prom` exists and that
`curl -s localhost:9100/metrics | grep ansible_last_run` reports a recent time.

## Storage

Debian 13 amd64 runs OpenZFS, Samba/NFS, smartd/Sanoid, rootless Podman
applications and native Alloy. OS installation and pool adoption are separate
operations.

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
Set `nas.datasets.<key>.properties.quota` for a dataset-wide cap, or
`nas.datasets.<key>.private_children.<username>.properties.quota` for an
individual private child dataset (including Time Machine destinations).
Both accept a string containing a positive integer followed by `M`, `G` or `T`,
or the explicit string `"none"` to remove a cap. Omit `quota` to preserve the
existing ZFS setting; no default cap or automatic quota removal is applied.
`refquota` is supported in the same property maps and limits only the dataset's
referenced data, excluding snapshots and descendants. Preserve that distinction
when importing existing limits; do not translate a `refquota` into a `quota`.
Private children default to `kind: dataset`; `kind: directory` cannot declare
ZFS properties because it shares its parent's filesystem. Child quotas are
independent declarations, not copies of the parent's quota; a parent quota
still limits the combined usage of its descendants.
Storage runs (including check mode) report existing ZFS filesystems in approved
pools that are not declared as share roots, application datasets or app-state
snapshot staging datasets. Removed private children appear in this report; their datasets
and data are left untouched. Undeclared descendants are reported individually,
even beneath a declared parent. Ordinary directories and snapshots are not included.

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

### File-service boot dependencies

The shares role writes persistent `.mount` units for declared dataset roots,
ordered after `zfs-mount.service`. ZFS still imports approved pools from its
cache and mounts their datasets first. The unit files let systemd resolve
SMB/NFS `BindsTo` dependencies before those mounts exist during cold boot;
mount detection alone is too late to rescue discarded service start jobs.
Directory-only private children use their backing dataset's mount unit.
Startup guards still check every expected dataset, and file services remain
bound to the dataset mounts so an unmount stops them.

Provisioning runs `systemd-analyze verify --man=no smbd.service nfs-server.service`
on the host before starting services. After applying, repeat that command and
check `systemctl cat mnt-tank-Mel.mount` on WoodlandPark. During a maintenance
window, reboot and confirm `systemctl is-active zfs-mount.service
shares-ready.service smbd.service nfs-server.service` reports four active
units without a manual start or another Ansible run. Confirm SMB client access
and reads through an existing NFS client mount; `active (exited)` is normal for
the oneshot NFS service.

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

### Explicit storage operations

```sh
ansible-playbook playbooks/operations/storage/validate-context.yml --limit STORAGE
ansible-playbook playbooks/operations/storage/reset-smb-passwords.yml --limit STORAGE \
  -e password_confirm=STORAGE
```

Edit the NetBox share context directly using the schema. Keep configuration
archives, credentials and application backups outside Git.

`prepare-host.yml`, `adopt-pool.yml` and `activate-pool.yml` handle OS cutover
and deliberate pool imports. Their explicit host/pool/backup confirmations
remain because these operations can affect data. Inspect their required
variables before running them; preserve a bootable rollback disk and console
access. Never force an import while another host can access the pool.

### Storage host OS reinstall

The owner-facing process is three steps: export the pool, reinstall Debian,
then apply Ansible. Replace `NAS_HOST` with the exact inventory hostname.

1. **Export the pool and shut down.** Backups are assumed verified and clients
   disconnected. The play waits for known storage jobs, stops writers, cleanly
   exports the inventory-selected pool, and powers off; do not force an export
   if it reports a blocker.

   ```sh
   ansible-playbook playbooks/operations/storage/export-pool.yml --limit NAS_HOST
   ```

2. **Reinstall Debian.** Install with ext4 on the OS disk only. Leave every
   pool device and dataset untouched. Configure network access as usual.

3. **Apply the Ansible configuration.** Once the host is reachable, this
   operation first creates the existing `applier` account through the
   installer login, using the one public key offered by your unlocked 1Password
   SSH agent. It closes that connection, reconnects as `applier`, changes
   the inventory SSH administrator from installer UID/GID 1000 to its
   inventory identity, then reapplies the standard applier SSH key. Close any
   separate administrator session first. If only the administrator's systemd
   user manager remains, recovery terminates that session; it refuses to
   proceed if a shell or other administrator process is still running. It then
   reconnects as the administrator, imports the pool identified by inventory,
   and runs normal `site.yml` provisioning. It restores cache-only pool import
   on future boots; recovery does not maintain a second copy of the site's role
   list.

   ```sh
   ansible-playbook playbooks/operations/storage/recover-reinstall.yml --limit NAS_HOST
   ```

Pool identity comes from the host's storage inventory; no GUID needs to be
copied into the command. The recovery operation sets up
`zfs-import-cache.service`; scan-based imports remain masked. Backups are
assumed, and recovery is run only after the old host is offline.

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

The Podman role orders only the dedicated application user managers after the
host's `network-online.target`, covering application containers and their Podman
exporters without delaying unrelated login sessions. Debian's enabled
`networking.service` orders itself before that target; for running NetworkManager
or systemd-networkd, the role enables the corresponding wait-online service.
Provisioning fails if none of these readiness providers is found. A dependency
on `network-online.target` inside a user Quadlet alone cannot order it against
the system manager. Keeping the user managers behind the host target also avoids
starting Podman's short-lived user network wait before a slow boot has finished.
This is boot-time interface readiness, not a guarantee that DNS or a remote
service is reachable, and it does not stop containers on later network outages.

Pasta-backed application Quadlets additionally run `podman-network-guard` before
each start. It requires an IPv4 default route and a successful local route lookup
with a source address; it sends no network traffic. An active online target can
precede DHCP completion, allowing pasta to retain an IPv6-only namespace even
after the host gains IPv4. A failed guard logs the missing prerequisite and the
existing `Restart=on-failure` policy retries after 30 seconds instead. Host and
bridge networking are unchanged; both address families remain enabled for pasta.

Apply with `playbooks/provision/storage.yml --limit WoodlandPark --tags apps`
(and the production Vault argument above). Existing user managers are not
restarted just to apply boot ordering. On the host, check
`systemctl cat user@3100.service` for the network drop-in and inspect
`systemctl list-dependencies network-online.target` for the enabled readiness
provider. After a planned reboot, compare
`journalctl -b -o short-monotonic -u network-online.target -u user@3100.service`
with `sudo appctl logs jellyfin`, confirm all containers and exporters start
without manual intervention, and verify Jellyfin playback and client discovery.
Repeat with delayed network configuration to exercise the original boot race;
check the wait-online service's journal for timeouts rather than treating an
active target alone as proof of connectivity.
Check `curl --fail http://127.0.0.1:30013/health` on the host as well as the
container's health: an internal health check can pass with broken port forwarding.
After a delayed-IPv4 boot, confirm the guard retries, Jellyfin eventually returns
HTTP 200 through the published host port, and the container has IPv4 routes.

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
Restore state before starting apps and preserve existing library paths and
numeric ownership.
If a staged copy import fails partway through, its target dataset may contain
incomplete state. Verify the target dataset, destroy only that dataset with
`zfs destroy TARGET_DATASET`, then rerun the import; do not destroy the source
dataset or backup.
SMB password resets do not rotate WebDAV credentials.

Prometheus's original imported configuration is archived once and included
in app snapshots; routine configuration updates do not replace it.

`apps_snapshots` and each application's `snapshot_targets` select private state.
Application-owned datasets that must remain outside container mounts can be
declared beside that application's settings as `retained_datasets`, with explicit
`dataset`, `path`, and optional `properties.quota` / `properties.refquota`.
Provisioning verifies these existing datasets and reconciles only declared
limits; it never creates them or changes their ownership. Prometheus declares
`tank/apps/prometheus-data` here with its existing 512 GiB quota and refquota.
The running `/data` mount uses `tank/apps/prometheus-runtime-data`, a clone of
that retained source's migration snapshot. ZFS clones do not inherit the source's
quota limits; the runtime mount explicitly declares the same 512 GiB quota and
refquota in `apps_definitions.prometheus.mounts`. Provisioning reconciles those
limits on the active clone as well as preserving the retained source's limits.

`app-snapshots NAME` stops the application, takes atomic ZFS snapshots, resumes
it, and stages a completed generation for Restic. Jellyfin configuration/state
and Prometheus configuration are included; Prometheus metric data is not.
WebDAV's private state/configuration is no longer included in Restic backups;
Svalbard documents are backed up separately.

Local app snapshots and staged generations expire after seven days
(`apps_snapshot_retention_days`). Cleanup runs before snapshots and independently
each day through `app-snapshots-prune.timer`, serialized with backups. It removes
only recognized helper-owned generations and snapshots, including legacy captures
and abandoned staging generations older than the recovery window. Other ZFS
snapshots, datasets and application data are untouched. Legacy Prometheus metric
snapshots/staging copies age out under the same policy. Existing `tank/app-backups`
dataset paths remain unchanged to avoid a storage migration; new generations use
`snapshots/`, with policy in `/etc/apps/app-snapshots.d/`.
Upgrade with a full storage provisioning run (or `--tags apps,backups`) so every
new policy is installed before the backup role retires the legacy commands.

`backup_sources` selects file datasets and app snapshots. The scheduled backup
runs at most two repositories concurrently, publishes per-repository success,
failure and duration metrics as each finishes, and does not impose a fixed
two-hour timeout on a Restic backup. Restic's stuck-request timeout still bounds
individual stalled backend requests. Prometheus configuration remains backed up;
its large metric data is excluded. WebDAV state and documents are not included
in Restic backups.
`/usr/local/sbin/backup-to-b2` remains the stable backup entry point.
After deploying a backup-script change to the NAS, interrupt an active
`restic.service` during a planned window and confirm no
`.b2_backup_*.prom.*` worker temp files remain in the textfile metrics directory.
App repositories retain a seven-day window (`forget --keep-within 7d`);
file repositories retain their existing 7-daily/5-weekly/7-monthly policy.
Backup and retention group by host, not the changing generation path.
Restic's window is relative to the latest backup, preserving recovery points
through an outage rather than deleting the last usable backup.
Weekly `restic-check.timer` checks repository metadata and reads a rotating
quarter of each repository's stored data, advancing only on success. The
metadata check shares the backup lock; the longer data scan does not hold that
external lock and relies on Restic's repository locking. Checks publish
completion/failure metrics. Scheduled checks do not replace periodic restore
rehearsals. Initialize repositories and unlock stale locks deliberately.
`b2_check_expected` independently lists every enabled repository so central
alerts can detect checks that never ran or stopped publishing, not only explicit
`b2_check_failed` results. Central Restic integrity rules live in the K3s repository.
After deploying a lock change, verify on the NAS during a planned window that a
backup can run while a long integrity data scan is active, then confirm both
`b2_backup_completed` and `b2_check_completed` advance and the check subset state
advances only after success.

Cloud sync derives filters and runtime mount checks from the same
`backup_cloud_sync_source_keys` list, verifying every selected child dataset
as well as its parent before invoking rclone. It uses deferred deletion and
`backup_cloud_sync_max_delete` (default 100) to bound deletions per run; exceeding
the ceiling is a failure requiring review, not an automatic unlimited retry.
Cloud sync is a mirror, not versioned backup; confirm B2 recovery/version
retention separately. Restic and cloud sync retain independent timers/locks.

`backup_pruning_approved` and `maintenance_pruning_approved` control deletion
of backups/snapshots. `backup_cloud_sync_deletion_approved` switches the
encrypted cloud mirror from dry-run comparison to real synchronization.
Production Restic pruning is enabled; other deployments should verify restores
and review deletion plans before enabling it. Sanoid pruning remains separately
gated and does not control the seven-day app-snapshot policy.

Sanoid uses repository retention policy; OpenZFS's packaged cron handles scrubs.
At each maintenance playbook run, physical disks are discovered with `lsblk`,
SMART transports with `smartctl --scan-open`, and allocation roles from the live
pool topology. Stable by-id paths and probed serials feed smartd, the collectors,
and their independent expected-disk metrics from the same discovered inventory.
Disks omitted by the SMART scan are probed with automatic detection, then SAT
and supported USB NVMe bridge transports; the first successful identification
supplies the transport. No per-disk inventory or transport overrides are needed.
Discovery fails explicitly rather than silently omitting an unidentifiable disk.
smartd logs to the journal and schedules daily short tests and staggered monthly
extended tests, avoiding days 8-14 for the packaged second-Sunday scrub.
New disks enter monitoring and test schedules on the next maintenance playbook
run; hotplug alone does not refresh the configuration.
Check actual device/USB-bridge test support on
the NAS; do not interpret successful scheduling as a completed disk test.
SMART/scrub collectors publish atomic node-exporter
textfiles, retaining successful scrub history across later scans. Disk policy
uses by-id paths and serial numbers, not `/dev/sdX`.
Alloy uses bounded persistent journals and a boot-filesystem OTLP queue. After
deploying to the NAS, verify `systemctl is-active alloy.service`, run
`alloy validate --stability.level=public-preview /etc/alloy/config.alloy`, and
confirm only the configured Prometheus sources can reach Alloy's `:12345` endpoint.
Central alerts remain in the separate K3s repository.

## Maintenance and decommissioning

Maintenance requires an explicit `--limit` and processes hosts one at a time:

```sh
ansible-playbook playbooks/operations/compute/install-updates.yml --limit HOSTNAME
ansible-playbook playbooks/operations/storage/unlock-restic-backends.yml --limit STORAGE
```

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
```

CI runs these checks with pinned dependencies. Do not add unit tests or
test-running CI steps; use the existing lint and syntax checks instead.
Do not suppress lint rules. Syntax and lint checks do not
replace checking mounts, client access, application health, backup restores
and reboot behavior on the actual hosts.
