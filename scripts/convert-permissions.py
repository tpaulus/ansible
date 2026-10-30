#!/usr/bin/env python3
"""Apply reviewed Unix permissions; native recursion never follows symlinks."""

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def prepare(plan, mount_guard_path):
    roots = plan["datasets"]
    entries = []
    expected = {key: set() for key in roots}
    for entry in plan["entries"]:
        key = entry["key"]
        if key not in roots:
            raise ValueError(f"unknown dataset key {key}")
        root = Path(roots[key]["path"])
        relative = entry["relative_path"]
        parts = Path(relative).parts
        if Path(relative).is_absolute() or ".." in parts or ".zfs" in parts:
            raise ValueError(f"unsafe relative path {relative}")
        path = root / relative
        if str(path) in expected[key]:
            raise ValueError(f"duplicate conversion path {path}")
        expected[key].add(str(path))
        if not isinstance(entry["uid"], int) or not isinstance(entry["gid"], int):
            raise ValueError("numeric reviewed UID/GID values are required")
        mode = int(entry["mode"], 8)
        if mode & ~0o3777 or mode & 0o002:
            raise ValueError(f"unsafe mode for {path}")
        if path.parent != root.parent and path.parent.resolve() != path.parent:
            raise ValueError(f"symlinked parent for {path}")
        metadata = path.lstat()
        if metadata.st_dev != root.stat().st_dev:
            raise ValueError(f"{path} crosses a dataset boundary")
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise ValueError(f"unsupported object type {path}")
        entries.append((path, entry, stat.S_ISLNK(metadata.st_mode)))
    for key, root in roots.items():
        subprocess.run(
            [mount_guard_path, root["path"], root["dataset"]],
            check=True,
        )
        descendants = subprocess.run(
            ["zfs", "list", "-Hr", "-t", "filesystem", "-o", "name", root["dataset"]],
            text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        selected = {item["dataset"] for item in roots.values()}
        if set(descendants) - selected:
            raise ValueError(f"{key}: select every descendant dataset before changing inherited ACL behavior")
        device = os.stat(root["path"]).st_dev
        actual = {str(Path(root["path"]))}
        def fail_walk(error):
            raise error
        for parent, directories, files in os.walk(root["path"], followlinks=False, onerror=fail_walk):
            directories[:] = [
                name for name in directories
                if name != ".zfs" and os.lstat(Path(parent) / name).st_dev == device
            ]
            for name in directories + files:
                actual.add(str(Path(parent) / name))
        if actual != expected[key]:
            raise ValueError(
                f"{key}: manifest is not exhaustive; missing={len(actual - expected[key])}, "
                f"unexpected={len(expected[key] - actual)}. Quiesce writers and regenerate/review it."
            )
    return entries


def apply(plan, entries):
    for dataset in plan["datasets"].values():
        subprocess.run(["zfs", "set", "acltype=off", dataset["dataset"]], check=True)
    for path, entry, symlink in entries:
        if path.is_symlink() != symlink or (path.parent.resolve() != path.parent):
            raise ValueError(f"{path} changed during conversion")
        os.chown(path, entry["uid"], entry["gid"], follow_symlinks=False)
        if not symlink:
            os.chmod(path, int(entry["mode"], 8), follow_symlinks=False)


def prepare_recursive(roots, mount_guard_path):
    if not isinstance(roots, list) or not roots:
        raise ValueError("select at least one permission root")
    selected = {root["dataset"] for root in roots}
    paths = {root["path"] for root in roots}
    for root in roots:
        path = Path(root["path"])
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise ValueError(f"unsafe permission root {path}")
        if any(not isinstance(root[key], int) or root[key] < 0 for key in ("uid", "gid")):
            raise ValueError("numeric reviewed UID/GID values are required")
        mode = int(root["mode"], 8)
        if mode & ~0o3777 or mode & 0o002:
            raise ValueError(f"unsafe mode for {path}")
        for excluded in root["exclude"]:
            if not excluded.startswith(str(path) + "/") or ".." in Path(excluded).parts:
                raise ValueError(f"unsafe exclusion {excluded}")
            if Path(excluded).exists() and excluded not in paths:
                raise ValueError(f"select existing child permission root {excluded}")
        subprocess.run(
            [mount_guard_path, str(path), root["dataset"]], check=True,
        )
        descendants = subprocess.run(
            ["zfs", "list", "-Hr", "-t", "filesystem", "-o", "name", root["dataset"]],
            text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        if set(descendants) - selected:
            raise ValueError("select every descendant dataset before changing inherited ACL behavior")


def native_find(root, action, directories_only=False):
    command = ["find", "-P", root["path"], "-xdev", "(",
               "-name", ".zfs"]
    for excluded in root["exclude"]:
        literal = excluded.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")
        command.extend(["-o", "-path", literal])
    # Prune child roots before processing them: -xdev alone still visits mountpoints.
    command.extend([")", "-prune", "-o", "(", "-type", "f", "-o", "-type", "d", "-o", "-type", "l", ")"])
    if directories_only:
        command.extend(["-type", "d"])
    command.extend(["-exec", *action, "{}", "+"])
    return command


def apply_recursive(roots):
    for dataset in sorted({root["dataset"] for root in roots}):
        subprocess.run(["zfs", "set", "acltype=off", "aclmode=passthrough", dataset], check=True)
    for root in roots:
        print(f"Applying native recursive permissions: {root['path']}", flush=True)
        subprocess.run(native_find(root, ["chown", "--no-dereference", "--",
                                         f"{root['uid']}:{root['gid']}"]), check=True)
        mode = int(root["mode"], 8)
        clauses = []
        for who, shift in (("u", 6), ("g", 3), ("o", 0)):
            bits = (mode >> shift) & 7
            permissions = ("r" if bits & 4 else "") + ("w" if bits & 2 else "") + ("X" if bits & 1 else "")
            clauses.append(f"{who}={permissions}")
        # find excludes symlinks for chmod; ownership above changes links themselves.
        command = native_find(root, ["chmod", ",".join(clauses), "--"])
        command[command.index("-exec"):command.index("-exec")] = ["!", "-type", "l"]
        subprocess.run(command, check=True)
        subprocess.run(native_find(root, ["chmod", "g+s" if mode & 0o2000 else "g-s", "--"],
                                   directories_only=True), check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--mount-guard", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--recursive", action="store_true")
    arguments = parser.parse_args()
    plan = json.loads(arguments.manifest.read_text())
    if arguments.apply:
        if arguments.recursive:
            prepare_recursive(plan, arguments.mount_guard)
            apply_recursive(plan)
        else:
            entries = prepare(plan, arguments.mount_guard)
            apply(plan, entries)
    else:
        if arguments.recursive:
            prepare_recursive(plan, arguments.mount_guard)
        else:
            entries = prepare(plan, arguments.mount_guard)
    count = len(plan) if arguments.recursive else len(entries)
    print(f"{'Applied' if arguments.apply else 'Validated'} {count} reviewed {'roots' if arguments.recursive else 'paths'}; ACL/xattr values were not deleted.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"Permission conversion failed: {error}", file=sys.stderr)
        sys.exit(1)
