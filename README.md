# Ansible

Server configuration and explicit maintenance workflows. Production inventory
comes from NetBox; secrets remain encrypted with Ansible Vault and unlocked
through the 1Password CLI.

## Layout

| Path | Purpose |
| --- | --- |
| `inventories/production/netbox.yml` | Dynamic NetBox inventory |
| `inventories/production/group_vars/` | Environment settings and encrypted vaults |
| `inventories/ci/hosts.yml` | Non-routable fixture hosts, no production credentials |
| `playbooks/site.yml` | Common, storage, and K3s provisioning |
| `playbooks/provision/` | Focused provisioning entry points |
| `playbooks/operations/` | Explicit maintenance operations |
| `playbooks/decommission/` | Destructive workflows, never imported by `site.yml` |
| `roles/` | Cohesive reusable configuration with role-local templates |
| `.ansible/` | Ignored, downloaded Galaxy dependencies |

The former `provision/`, `ops/`, `network/`, `decom/`, and root inventory paths
have moved. Network provisioning remains separate from `site.yml`, as it was
before the restructure. Docker and cAdvisor are optional roles and are not
implicitly enabled on existing compute hosts.
Use `playbooks/provision/containers.yml` to opt in to Docker and cAdvisor.

## Controller setup

Use Python 3.13 or newer and run commands from the repository root so
`ansible.cfg` is loaded.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
ansible-galaxy install -r requirements.yml --roles-path .ansible/roles --force
```

Production NetBox inventory additionally needs `pynetbox` and `pytz` installed in
the controller environment. Install the 1Password CLI (`op`) and authenticate
before decrypting production variables. On macOS, `gnu-tar` and `unzip` may be
needed for upstream roles. Network provisioning also requires Go on the
controller to cross-compile Unbound exporter.

```sh
python -m pip install pynetbox pytz
export NETBOX_TOKEN="$(op read 'op://Infra/Ansible/NETBOX_TOKEN')"
ansible-inventory --graph
```

Pass the vault client explicitly for production playbooks:

```sh
ansible-playbook playbooks/site.yml \
  --vault-id 'op://Infra/Ansible/password@vault-1p-client.sh' \
  --limit HOSTNAME --check
```

Remove `--check` only after reviewing the intended targets. Check mode is a
preview, not a substitute for validation: upstream roles, first-time binary
installs, and controller-side builds may need prerequisites that do not exist
until a real run. Do not use `--diff` on secret-bearing templates; those tasks
are also protected with `no_log`.

Existing Fish wrappers can remain unchanged if they only inject `NETBOX_TOKEN`
and the vault ID. Update any hard-coded inventory or playbook paths. Clear
`NETBOX_TOKEN` after the session with `unset NETBOX_TOKEN` (Fish: `set -e NETBOX_TOKEN`).

## Provisioning and variables

```sh
ansible-playbook playbooks/site.yml --list-hosts
ansible-playbook playbooks/site.yml --list-tags
ansible-playbook playbooks/provision/common.yml --limit HOSTNAME --tags motd
ansible-playbook playbooks/provision/k3s.yml --list-tasks
ansible-playbook playbooks/provision/network.yml --limit FIREWALL
```

Supply the vault ID for commands that load encrypted production variables,
including listing commands. Provisioning tags include `base`, `monitoring`,
`nut`, `motd`, `unattended_upgrades`, `storage`, `backups`, `k3s`, and `network`.

Keep environment-specific values in production group variables or NetBox
configuration context. Keep reusable, operator-overridable settings in role
`defaults/main.yml`, using a role prefix. Keep secret values in `vault.yaml`
and expose them through non-secret aliases in the neighboring variable file.

Important settings:

- `node_exporter_version` is pinned in production `all.yml`; its textfile
  directory is shared with backup scripts.
- `k3s_server` and registry credentials are in `tags_k3s` variables. The upstream
  K3s role retains its cluster-wide coordination; do not add `serial: 1` or
  limit a bootstrap run to one controller. Use the role's documented upgrade
  workflow for an existing HA cluster. Set `k3s_release_version` explicitly to
  the approved cluster version before provisioning; otherwise the upstream
  role retains its existing stable-channel behavior.
- `k3s_backup_server`, `k3s_backup_share`, and `k3s_backup_share_name` describe the
  existing TrueNAS NFS export. A playbook limit must include the storage host
  when updating share permissions.
- `nut_mode` selects `primary`, `secondary`, or `shutdown_only`. Primary and
  secondary configuration now targets `racks_Compute`; TrueNAS receives only
  its shutdown script because the appliance manages NUT itself. Storage hosts
  are excluded from the generic secondary play.
- Compute-rack NUT credentials initially use a byte-for-byte copy of the
  existing encrypted MDF vault. Edit the Compute vault if those credentials
  differ; no decryption or secret rotation occurred during migration.
- NUT uses OS-specific service names and restricts monitor credentials to root
  and the NUT group. **Timed power-loss shutdown is opt-in** through
  `nut_timed_shutdown_enabled: true`. The scheduler can now dispatch the existing
  360-second primary / 240-second secondary timers; by default it logs that the
  timer is disabled, while upsmon retains critical-battery shutdown behavior.
  A narrow sudo rule permits only `/sbin/ups-shutdown`. Validate UPS coordination
  and shutdown ordering on a maintenance host before enabling timed shutdown.
- `truenas_backup_shares` is the single dataset list for backup and unlock
  operations. Restic is pinned and installed during provisioning, not updated
  during a backup. Existing externally scheduled `/usr/local/sbin/backup-to-b2`
  invocations continue to work; the role does not create an additional schedule.
  Initialize a new repository explicitly with Restic before running backups.
  Backup failures return nonzero, and automatic unlocks are no longer attempted.
- `cadvisor_version` and its architecture-specific checksums must be updated
  together. Unbound exporter builds a pinned source tag in a unique temporary
  directory, records the installed version, and rebuilds only when needed.
- Docker uses a scoped signed DEB822 repository and validates `daemon.json`
  before restarting. Debian-family compute hosts and TrueNAS SCALE are the
  supported Linux platforms; FreeBSD-specific NUT and firewall paths remain
  guarded.

## Maintenance and decommissioning

Maintenance requires an explicit `--limit`; hosts are processed one at a time.
Failures stop the workflow instead of being suppressed. Apply the same vault
arguments used for provisioning.

```sh
ansible-playbook playbooks/operations/compute/install-updates.yml --limit HOSTNAME
ansible-playbook playbooks/operations/compute/cleanup-docker.yml --limit HOSTNAME
ansible-playbook playbooks/operations/storage/unlock-restic-backends.yml --limit STORAGE
```

Docker pruning uses the Docker API and requires the collection's `requests`
dependency on the managed host. It removes stopped containers and **all**
unused images, not just dangling images; volumes are untouched.

For Debian 12 to 13 upgrades: verify backups, run `dist-upgrade-pre.yml`, reboot
if instructed, and review/disable incompatible third-party repositories.
Only the main `sources.list`, `debian.list`, and `debian.sources` are rewritten
automatically; third-party repositories require an explicit compatibility
review rather than a blind distribution-name replacement.

```sh
ansible-playbook playbooks/operations/compute/dist-upgrade-pre.yml --limit HOSTNAME
ansible-playbook playbooks/operations/compute/dist-upgrade-exec.yml --limit HOSTNAME \
  -e dist_upgrade_third_party_reviewed=true
# Reboot into Debian 13 before the post-upgrade phase.
ansible-playbook playbooks/operations/compute/dist-upgrade-post.yml --limit HOSTNAME
```

Decommissioning requires exactly one target and a different healthy control
plane. Ensure the remaining etcd membership can maintain quorum. The playbook
cordons/drains the node, disables K3s, stops containers, deletes the Kubernetes
node, and verifies removal. It does not erase data or uninstall binaries.

```sh
ansible-playbook playbooks/decommission/k3s.yml --limit NODE \
  -e k3s_decommission_confirm=NODE \
  -e k3s_decommission_control_host=OTHER_CONTROL_PLANE
```

## Validation and contributing

```sh
ANSIBLE_INVENTORY=inventories/ci/hosts.yml ansible-lint --offline
bash scripts/check-syntax.sh
python -m unittest discover -s tests -v
```

CI uses the same pinned tools and Galaxy requirements. The syntax script
discovers every playbook below `playbooks/`, so new entry points are included
automatically. Fixture inventory validation never contacts NetBox or requires
vault credentials. It checks structure, not live service health or OS upgrades.

Add focused roles only when behavior is cohesive; keep orchestration and host
selection in playbooks. Use FQCNs, modules over shell commands, handlers for
configuration-driven restarts, explicit platform guards, and accurate change
and failure conditions. Add regression tests for changed behavior; run the
commands above and review `--list-hosts`, `--list-tags`, and `--list-tasks` before
merging. Provision a limited maintenance host before expanding a rollout.
