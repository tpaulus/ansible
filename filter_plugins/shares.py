"""Resolve validated NetBox share declarations without executing stored expressions."""

import ipaddress
import posixpath
import re

from ansible.errors import AnsibleFilterError


def _fail(message):
    raise AnsibleFilterError(f"Invalid NAS share context: {message}")


def _plain(value):
    if isinstance(value, str):
        if any(token in value for token in ("{{", "{%", "{#", "\n", "\r", "\0")):
            _fail("executable expressions and control characters are prohibited")
    elif isinstance(value, dict):
        for key, item in value.items():
            _plain(key)
            _plain(item)
    elif isinstance(value, list):
        for item in value:
            _plain(item)


def _materialize(value):
    if isinstance(value, dict):
        return {_materialize(key): _materialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_materialize(item) for item in value]
    return value


def _network(value):
    if value == "*":
        return value
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        _fail(f"client {value!r} must be an IP/network or explicit '*'")


def zfs_mountpoint(path, dataset, pools):
    pool = dataset.split("/", 1)[0]
    if pool not in pools:
        _fail("dataset mountpoint references an unapproved pool")
    root = pools[pool]["mountpoint"]
    if path != posixpath.normpath(path) or not (path == root or path.startswith(root + "/")):
        _fail("dataset mountpoint escapes the approved pool root")
    altroot = pools[pool].get("altroot", "").rstrip("/")
    if not altroot:
        return path
    if not altroot.startswith("/") or altroot != posixpath.normpath(altroot) or not path.startswith(altroot + "/"):
        _fail("invalid alternate import root for dataset mountpoint")
    return path[len(altroot):]


def resolve_shares(context, accounts, groups, inventory_hosts, inventory_groups, pools):
    _plain(context)
    if context.get("schema_version") != 2:
        _fail("expected schema_version 2 with shares nested under datasets")
    for name, policy in pools.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", name):
            _fail(f"invalid approved pool name {name!r}")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", str(policy.get("guid", ""))):
            _fail(f"missing/invalid approved pool GUID for {name}")
        mountpoint = policy.get("mountpoint", "")
        if not mountpoint.startswith("/mnt/") or mountpoint != posixpath.normpath(mountpoint):
            _fail(f"invalid approved mountpoint for {name}")
    user_ids, numeric_groups = set(), set()
    for name, gid in groups.items():
        if not isinstance(gid, int) or isinstance(gid, bool) or gid < 0 or gid in numeric_groups:
            _fail(f"invalid or duplicate repository GID for {name}")
        numeric_groups.add(gid)
    for name, identity in accounts.items():
        uid = identity.get("uid")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 1 or uid in user_ids or identity.get("group") not in groups:
            _fail(f"invalid/duplicate UID or primary group for {name}")
        if not set(identity.get("groups", [])).issubset(groups):
            _fail(f"unmanaged supplementary group for {name}")
        user_ids.add(uid)
    datasets = _materialize(context["datasets"])
    roots = []
    identities = dict(accounts)
    identities.setdefault("root", {"uid": 0, "group": "root"})
    group_ids = dict(groups)
    group_ids.setdefault("root", 0)
    seen_datasets, seen_paths = set(), set()
    smb_declarations, nfs_declarations = {}, {}
    for key, dataset in list(datasets.items()):
        pool, _, relative_name = dataset["dataset"].partition("/")
        if pool not in pools:
            _fail(f"unapproved pool for {key}")
        dataset.setdefault("path", posixpath.join(pools[pool]["mountpoint"], relative_name))
        dataset.setdefault("owner", "root")
        dataset.setdefault("group", dataset["owner"])
        dataset.setdefault("mode", "2770")
        dataset.setdefault("create", False)
        if "smb" in dataset:
            smb_declarations[key] = dataset.pop("smb")
        if "nfs" in dataset:
            nfs_declarations[key] = dataset.pop("nfs")
        for username, child in dataset.pop("private_children", {}).items():
            child = {"kind": "dataset", "group": username, "mode": "0700", "create": False, **child}
            child_key = f"{key}__{username}"
            if child_key in datasets:
                _fail(f"duplicate private child key {child_key}")
            datasets[child_key] = {
                "dataset": dataset["dataset"] + ("/" + username if child["kind"] == "dataset" else ""),
                "path": dataset["path"] + "/" + username,
                "owner": username, "group": child["group"], "mode": child["mode"],
                "create": child["create"], "kind": child["kind"],
                "parent_key": key,
            }
        dataset["kind"] = "dataset"
    for key, dataset in datasets.items():
        name, path = dataset["dataset"], dataset["path"]
        pool = name.split("/", 1)[0]
        if pool not in pools or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", pool):
            _fail(f"unapproved pool for {key}")
        root = pools[pool]["mountpoint"]
        if path != posixpath.normpath(path) or not path.startswith(root + "/"):
            _fail(f"path for {key} escapes approved mountpoint")
        if any(part in (".", "..", "") for part in name.split("/")):
            _fail(f"unsafe dataset name for {key}")
        if dataset["owner"] not in identities or dataset["group"] not in group_ids:
            _fail(f"unknown owner/group for {key}")
        if dataset["kind"] == "dataset" and name in seen_datasets:
            _fail(f"duplicate backing dataset {name}")
        if path in seen_paths:
            _fail(f"duplicate backing path {path}")
        if int(dataset["mode"], 8) & 0o002:
            _fail(f"world-writable root is not allowed for {key}")
        if dataset["kind"] == "dataset":
            seen_datasets.add(name)
        seen_paths.add(path)
        dataset["key"] = key
        roots.append(dataset)
    smb = []
    names = set()
    for key, declaration in smb_declarations.items():
        dataset = datasets[key]
        share = {
            "name": posixpath.basename(dataset["dataset"]),
            "read_users": [], "browseable": True, "abe": False,
            "hosts_allow": [], "time_machine": False, "shadow_copy": True,
            **declaration, "dataset": key,
        }
        share.setdefault("write_users", [user for user in share["valid_users"] if user not in share["read_users"]])
        if share["name"].casefold() in names:
            _fail("duplicate SMB name")
        names.add(share["name"].casefold())
        users = set(share["valid_users"])
        for username in users | set(share["read_users"]) | set(share["write_users"]):
            if username not in accounts or not accounts[username].get("smb", False):
                _fail(f"SMB account {username} is not repository-managed and SMB-enabled")
        if not set(share["read_users"] + share["write_users"]).issubset(users):
            _fail(f"read/write users must be valid users for {key}")
        if set(share["read_users"]) & set(share["write_users"]):
            _fail(f"conflicting read/write user policy for {key}")
        for address in share["hosts_allow"]:
            _network(address)
        share["path"] = dataset["path"]
        if share.get("path_suffix"):
            if any(f"{share['dataset']}__{username}" not in datasets for username in users):
                _fail(f"private SMB share {key} lacks declared children")
            share["path"] += "/%U"
        share.update(key=key, backing_dataset=dataset["dataset"])
        smb.append(share)
    nfs, purposes, export_paths = [], set(), set()
    for key, declaration in nfs_declarations.items():
        export = {"read_only": False, "squash": "root", "security": "sys", **declaration, "dataset": key}
        dataset = datasets[key]
        clients = []
        for selector in export["clients"]:
            if "network" in selector:
                clients.append(_network(selector["network"]))
            else:
                selected = inventory_groups.get(selector["inventory_group"], [])
                if not selected:
                    _fail(f"empty client group for {key}")
                for hostname in selected:
                    address = inventory_hosts[hostname].get("ansible_host")
                    try:
                        clients.append(str(ipaddress.ip_interface(address).ip))
                    except (ValueError, TypeError):
                        _fail(f"missing/invalid client IP for {hostname}")
        if not clients:
            _fail(f"empty clients for {key}")
        if dataset["path"] in export_paths:
            _fail("declare all clients of a backing path in one NFS export")
        export_paths.add(dataset["path"])
        if export.get("purpose"):
            if export["purpose"] in purposes:
                _fail("duplicate NFS purpose")
            purposes.add(export["purpose"])
        if export["squash"] == "all":
            user, group = export["anonymous_user"], export["anonymous_group"]
            if user not in identities or group not in group_ids:
                _fail(f"unknown anonymous identity for {key}")
            export.update(anonuid=identities[user]["uid"], anongid=group_ids[group])
        export.update(key=key, path=dataset["path"], backing_dataset=dataset["dataset"], clients=sorted(set(clients)))
        nfs.append(export)
    return {
        "datasets": datasets, "roots": sorted(roots, key=lambda item: item["path"].count("/")),
        "smb": smb, "nfs": nfs, "protocols": context.get("nfs_protocols", [3, 4]),
        "mounts": [{"path": item["path"], "dataset": item["dataset"]} for item in roots],
    }


def exports_path(value):
    return value.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")


def nfs_consumer(inventory_hosts, storage_hosts, inventory_groups, purpose, client):
    matches = []
    for hostname in storage_hosts:
        host = inventory_hosts[hostname]
        context = host.get("nas")
        if not context:
            _fail(f"missing NetBox share context for storage host {hostname}")
        resolved = resolve_shares(
            context, host["accounts_users"], host["accounts_groups"],
            inventory_hosts, inventory_groups, host["storage_pools"],
        )
        for export in resolved["nfs"]:
            if export.get("purpose") == purpose:
                try:
                    server = str(ipaddress.ip_interface(host["ansible_host"]).ip)
                    address = ipaddress.ip_interface(inventory_hosts[client]["ansible_host"]).ip
                except (KeyError, ValueError, TypeError):
                    _fail("storage and client hosts require explicit primary IP addresses")
                if not any(network == "*" or address in ipaddress.ip_network(network) for network in export["clients"]):
                    _fail(f"{client} is not permitted by the {purpose} export")
                matches.append({"server": server, "path": export["path"], "host": hostname})
    if len(matches) != 1:
        _fail(f"expected exactly one NFS export with purpose {purpose!r}")
    return matches[0]


def permission_plan(manifest, resolved, accounts, groups):
    _plain(manifest)
    if not isinstance(manifest, dict) or set(manifest) != {"datasets", "entries"}:
        _fail("permission manifest requires datasets and entries")
    if not manifest["datasets"] or not manifest["entries"]:
        _fail("empty permission conversion is prohibited")
    result = {"datasets": {}, "entries": []}
    identities = {"root": {"uid": 0}, **accounts}
    group_ids = {"root": 0, **groups}
    for key in manifest["datasets"]:
        if key not in resolved["datasets"] or resolved["datasets"][key]["kind"] != "dataset":
            _fail(f"permission dataset {key} is not declared")
        root = resolved["datasets"][key]
        result["datasets"][key] = {"path": root["path"], "dataset": root["dataset"]}
    for entry in manifest["entries"]:
        if set(entry) != {"key", "relative_path", "owner", "group", "mode"}:
            _fail("permission entries require key, relative_path, owner, group and mode only")
        if entry["key"] not in result["datasets"]:
            _fail("permission entry references an unselected dataset")
        if entry["owner"] not in identities or entry["group"] not in group_ids:
            _fail("permission entry references an unmanaged identity")
        if not re.fullmatch(r"[0123][0-7]{3}", entry["mode"]) or int(entry["mode"], 8) & 0o002:
            _fail("unsafe conversion mode")
        result["entries"].append({
            "key": entry["key"], "relative_path": entry["relative_path"], "mode": entry["mode"],
            "uid": identities[entry["owner"]]["uid"], "gid": group_ids[entry["group"]],
        })
    return result


def recursive_permission_plan(keys, resolved, accounts, groups):
    if not isinstance(keys, list) or not keys or len(set(keys)) != len(keys):
        _fail("select unique declared permission roots")
    identities = {"root": {"uid": 0}, **accounts}
    group_ids = {"root": 0, **groups}
    roots = resolved["datasets"]
    result = []
    for key in keys:
        if key not in roots:
            _fail(f"permission root {key} is not declared")
        root = roots[key]
        mode = root["mode"]
        if not re.fullmatch(r"[0123][0-7]{3}", mode) or int(mode, 8) & 0o002:
            _fail("unsafe conversion mode")
        result.append({
            "path": root["path"], "dataset": root["dataset"],
            "uid": identities[root["owner"]]["uid"], "gid": group_ids[root["group"]],
            "mode": mode,
            "exclude": [
                child["path"] for child in roots.values()
                if child["path"].startswith(root["path"] + "/")
            ],
        })
    return result


class FilterModule:
    def filters(self):
        return {
            "resolve_shares": resolve_shares, "exports_path": exports_path,
            "nfs_consumer": nfs_consumer,
            "permission_plan": permission_plan,
            "recursive_permission_plan": recursive_permission_plan,
            "zfs_mountpoint": zfs_mountpoint,
        }
