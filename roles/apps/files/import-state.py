#!/usr/bin/python3
"""Import an offline staged copy."""
import json
import os
import subprocess
import sys
from pathlib import Path


def validate_import(source, target, mount):
    if not source.is_dir() or source.is_symlink() or not target.is_dir() or target.is_symlink():
        raise ValueError("Import requires real source/destination directories")
    if source.resolve() == target.resolve() or source.resolve().is_relative_to(target.resolve()):
        raise ValueError("Import source must be an independent backup")
    if target.resolve().is_relative_to(source.resolve()):
        raise ValueError("Import destination must not be inside its source")
    marker = target / ".nas-app-imported"
    if marker.exists():
        if marker.read_text().strip() != mount["import_name"]:
            raise ValueError("Import marker belongs to a different backup")
        return False
    if any(target.iterdir()):
        raise ValueError("Refusing to merge into nonempty state; retain it and choose a fresh dataset")
    for required in mount.get("required_files", []):
        path = source / required
        if not path.exists() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"Incomplete source state: {required}")
    return True


if __name__ == "__main__":
    try:
        config = json.load(sys.stdin)
        root = Path(config["source_root"])
        if (not root.is_absolute() or root.is_symlink()
                or not root.is_dir()):
            raise ValueError("Use a reviewed offline staging root")
        for app in config["apps"]:
            for mount in app["mounts"]:
                if not mount.get("require_import"):
                    continue
                source, target = root / mount["import_name"], Path(mount["path"])
                subprocess.run([
                    config["mount_guard"], str(source), config["source_dataset"],
                    str(target), mount["dataset"],
                ], check=True)
                if not validate_import(source, target, mount):
                    continue
                subprocess.run([
                    "rsync", "-aH", "--numeric-ids",
                    f"--chown={app['identity']['uid']}:{app['identity']['gid']}",
                    "--chmod=go-rwx", "--exclude=.nas-app-imported",
                    *[f"--exclude={pattern}" for pattern in app.get("rsync_excludes", [])],
                    "--",
                    str(source) + "/", str(target) + "/",
                ], check=True)
                marker = target / ".nas-app-imported"
                with marker.open("x") as handle:
                    handle.write(mount["import_name"] + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                marker.chmod(0o600)
                os.chown(marker, app["identity"]["uid"], app["identity"]["gid"])
                print(f"Imported {mount['import_name']}")
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
