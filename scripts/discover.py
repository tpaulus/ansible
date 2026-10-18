#!/usr/bin/env python3
"""Read-only NAS evidence, deliberately excluding credentials and app environments."""

import json
import os
import subprocess
import sys


def command(*args):
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def discover():
    evidence = {
        "hostname": command("hostname", "-f").strip(),
        "pool_status": command("zpool", "status", "-P"),
        "pool_properties": command("zpool", "get", "-Hp", "all", "tank"),
        "dataset_properties": command(
            "zfs", "get", "-Hpr", "-t", "filesystem", "-o", "name,property,value,source",
            "mountpoint,mounted,canmount,encryption,encryptionroot,keystatus,"
            "acltype,aclmode,aclinherit,xattr,atime,recordsize,compression",
            "tank",
        ),
        "network": command("ip", "-j", "address", "show"),
        "routes": command("ip", "-j", "route", "show"),
        "pci": command("lspci", "-nn"),
        "nfs_exports": command("exportfs", "-v"),
        "smb_config": command("testparm", "-s"),
        "samba_sid": command("net", "getlocalsid").strip(),
        "applications": [],
        "roots": [],
        "accounts": {},
        "shares": {},
        "share_root_acls": {},
    }
    smb = json.loads(command("midclt", "call", "sharing.smb.query"))
    nfs = json.loads(command("midclt", "call", "sharing.nfs.query"))
    evidence["shares"]["smb"] = [
        {key: share.get(key) for key in (
            "name", "path", "enabled", "purpose", "browsable",
            "readonly", "access_based_share_enumeration",
        )}
        for share in smb
    ]
    evidence["shares"]["nfs"] = [
        {key: share.get(key) for key in (
            "path", "enabled", "hosts", "networks", "mapall_user", "mapall_group",
            "maproot_user", "maproot_group", "ro", "security",
        )}
        for share in nfs
    ]
    for path in sorted({share["path"].split("/%U", 1)[0] for share in smb + nfs}):
        source = command("findmnt", "-n", "-o", "SOURCE", "--target", path).strip()
        acltype = command("zfs", "get", "-H", "-o", "value", "acltype", source).strip()
        tool = "nfs4xdr_getfacl" if acltype == "nfsv4" else "getfacl"
        evidence["share_root_acls"][path] = {
            "acltype": acltype, "acl": command(tool, "-n", path),
        }
    identifiers = command("docker", "ps", "-aq").split()
    if identifiers:
        for container in json.loads(command("docker", "inspect", *identifiers)):
            config = container["Config"]
            evidence["applications"].append({
                "name": container["Name"].lstrip("/"),
                "image": config["Image"],
                "image_id": container["Image"],
                "user": config["User"],
                "environment_keys": sorted(item.split("=", 1)[0] for item in config.get("Env", [])),
                "mounts": [
                    {key: mount.get(key) for key in ("Type", "Source", "Destination", "RW")}
                    for mount in container["Mounts"]
                ],
                "network_mode": container["HostConfig"]["NetworkMode"],
                "ports": container["HostConfig"]["PortBindings"],
                "devices": container["HostConfig"]["Devices"],
                "group_add": container["HostConfig"]["GroupAdd"],
                "restart_policy": container["HostConfig"]["RestartPolicy"],
                "running": container["State"]["Running"],
            })
    for username in ("tom", "mel", "longhorn", "uprotect", "databasus", "media"):
        account = command("getent", "passwd", username).strip().split(":")
        evidence["accounts"][username] = {
            "uid": int(account[2]), "gid": int(account[3]),
            "supplementary_gids": [int(group) for group in command("id", "-G", username).split()],
            "home": account[5], "shell": account[6],
        }
    paths = command("zfs", "list", "-Hr", "-t", "filesystem", "-o", "mountpoint", "tank").splitlines()
    for path in paths:
        if not path.startswith("/mnt/") or not os.path.isdir(path):
            continue
        stat = os.stat(path)
        evidence["roots"].append({
            "path": path, "uid": stat.st_uid, "gid": stat.st_gid,
            "mode": f"{stat.st_mode & 0o7777:04o}",
            "xattr_names": sorted(os.listxattr(path)),
        })
    return evidence


if __name__ == "__main__":
    try:
        print(json.dumps(discover(), indent=2))
    except (RuntimeError, OSError, ValueError) as error:
        print(f"NAS discovery failed: {error}", file=sys.stderr)
        sys.exit(1)
