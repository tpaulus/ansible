#!/usr/bin/python3
"""One-time, explicitly approved migration of stopped state into new ZFS clones."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys


PROPERTY = "org.whitestar:nas-app-migration"


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def exists(dataset):
    result = subprocess.run(
        ["zfs", "list", "-H", "-o", "name", dataset], capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise ValueError("Cannot inspect migration dataset")
    return result.returncode == 0


def get(dataset, key):
    return command("zfs", "get", "-H", "-o", "value", key, dataset)


def validate(config, mount):
    if (config.get("confirm") != "clone-stopped-app-state"
            or config.get("sources_stopped") is not True
            or config.get("backups_verified") is not True):
        raise ValueError("Explicit stopped-source and recoverable-backup approval required")
    if not re.fullmatch(r"[a-z0-9-]+", config["migration"]):
        raise ValueError("Invalid migration identifier")
    source, target = mount["source_dataset"], mount["dataset"]
    if (source.split("/")[0] != target.split("/")[0]
            or source == target or target.startswith(source + "/")
            or not target.startswith(source.split("/")[0] + "/apps/")):
        raise ValueError("Migration needs independent runtime state on the same approved pool")
    for key in ("source_path", "path", "mountpoint"):
        if not Path(mount[key]).is_absolute() or ".." in Path(mount[key]).parts:
            raise ValueError("Migration paths must be canonical absolute paths")
    if mount["path"] == mount["source_path"]:
        raise ValueError("Migration must not modify its source")
    if not re.fullmatch(r"[a-z0-9-]+", mount["import_name"]):
        raise ValueError("Invalid import identity")
    if min(int(mount["uid"]), int(mount["gid"])) <= 0:
        raise ValueError("Runtime state must use a dedicated non-root identity")


def required_state(root, mount):
    for name in mount.get("required_files", []):
        path = root / name
        if not path.exists() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Missing or escaping required application state: {name}")


def remap_clone(root, uid, gid):
    """Use native physical recursion only on the independently verified clone."""
    command("chown", "-hR", "-P", "--", f"{uid}:{gid}", str(root))
    command("chmod", "-R", "--", "u+rwX,go-rwx", str(root))


def migrate(config, mount):
    validate(config, mount)
    source, target = mount["source_dataset"], mount["dataset"]
    root = Path(mount["path"])
    snapshot = source + "@" + config["migration"]
    command(config["mount_guard"], mount["source_path"], source)
    required_state(Path(mount["source_path"]), mount)
    if not exists(snapshot):
        if exists(target):
            raise ValueError("Refusing existing target without this migration snapshot")
        command("zfs", "snapshot", "-o", PROPERTY + "=" + config["migration"], snapshot)
    if get(snapshot, PROPERTY) != config["migration"]:
        raise ValueError("Snapshot was not created by this migration")
    holds = command("zfs", "holds", "-H", snapshot)
    if not any(line.split("\t")[1] == config["migration"] for line in holds.splitlines()):
        command("zfs", "hold", config["migration"], snapshot)
    if not exists(target):
        if root.exists() and (root.is_symlink() or not root.is_dir() or any(root.iterdir())):
            raise ValueError("Refusing to overlay an existing target path")
        command(
            "zfs", "clone", "-o", "mountpoint=" + mount["mountpoint"],
            "-o", "canmount=on", "-o", "readonly=off", "-o", "acltype=posixacl",
            "-o", "aclmode=discard", "-o", "aclinherit=discard",
            "-o", PROPERTY + "=" + config["migration"], snapshot, target,
        )
    if get(target, "origin") != snapshot or get(target, PROPERTY) != config["migration"]:
        raise ValueError("Refusing unrelated target: migration clone origin does not match")
    if get(target, "mountpoint") != mount["path"] or get(target, "readonly") != "off":
        raise ValueError("Migration clone mountpoint/readonly identity differs")
    command(config["mount_guard"], str(root), target)
    required_state(root, mount)
    marker = root / ".nas-app-imported"
    if marker.exists():
        if marker.is_symlink() or marker.read_text() != mount["import_name"] + "\n":
            raise ValueError("Unexpected import marker")
        print("Already restored " + mount["import_name"])
        return
    if command("findmnt", "-R", "-l", "-n", "-o", "TARGET", "--target", str(root)) != str(root):
        raise ValueError("Unexpected child filesystem in application clone")
    command("setfacl", "--recursive", "--physical", "--remove-all", "--remove-default", "--", str(root))
    remap_clone(root, int(mount["uid"]), int(mount["gid"]))
    required_state(root, mount)
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(mount["import_name"] + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chown(marker, int(mount["uid"]), int(mount["gid"]))
    print("Restored protected clone " + mount["import_name"])


if __name__ == "__main__":
    try:
        configuration = json.load(sys.stdin)
        for entry in configuration["mounts"]:
            migrate(configuration, entry)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
