#!/usr/bin/python3
"""Discover physical disks, stable SMART identities and pool allocation roles."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def command(*arguments):
    result = subprocess.run(arguments, text=True, capture_output=True, check=False,
                            timeout=60, env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise ValueError(f"{arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout


def identify_disk(path, transport):
    candidates = [transport] if transport else [
        "auto", "sat", "sntasmedia", "sntjmicron", "sntrealtek",
    ]
    failures = []
    for candidate in candidates:
        probe = subprocess.run(["smartctl", "-j", "-i", "-d", candidate, path],
                               capture_output=True, text=True, timeout=30, check=False)
        # SMART health exit bits are not command/transport failures.
        if probe.returncode < 0 or probe.returncode & 7:
            failures.append(f"{candidate}: {probe.stdout.strip()} {probe.stderr.strip()}")
            continue
        identity = json.loads(probe.stdout)
        serial = identity.get("serial_number", "").strip()
        if not serial or not re.fullmatch(r"[A-Za-z0-9_.-]+", serial):
            failures.append(f"{candidate}: missing or unsupported SMART serial")
            continue
        return serial, candidate
    raise ValueError(f"SMART identification failed for {path}: {'; '.join(failures)}")


def discover(pools):
    blocks = json.loads(command("lsblk", "-J", "-p", "-o", "NAME,TYPE,MOUNTPOINTS"))
    physical = {}
    boot = set()

    def visit(node, parent=None):
        name = os.path.realpath(node["name"])
        disk = name if node["type"] == "disk" else parent
        if disk:
            physical[name] = disk
            if "/" in (node.get("mountpoints") or []):
                boot.add(disk)
        for child in node.get("children", []):
            visit(child, disk)

    for node in blocks["blockdevices"]:
        visit(node)
    disks = set(physical.values())
    if not disks:
        raise ValueError("No physical disks discovered")
    aliases = {}
    for path in sorted(Path("/dev/disk/by-id").iterdir()):
        if re.search(r"-part\d+$", path.name):
            continue
        target = os.path.realpath(path)
        if target in disks:
            aliases.setdefault(target, []).append(str(path))
    scan = json.loads(command("smartctl", "--scan-open", "-j"))
    transports = {os.path.realpath(item["name"]): item["type"]
                  for item in scan.get("devices", [])}
    roles = {disk: "boot" for disk in boot}
    for pool in pools:
        allocation = "data"
        for line in command("zpool", "status", "-P", pool).splitlines():
            fields = line.split()
            if not fields:
                continue
            if fields[0] in ("special", "logs", "cache", "spares", "dedup"):
                allocation = fields[0]
            elif fields[0].startswith("/dev/"):
                path = os.path.realpath(fields[0])
                if path not in physical:
                    raise ValueError(f"Cannot resolve pool member to physical disk: {fields[0]}")
                roles[physical[path]] = allocation
    result = []
    for disk in sorted(disks):
        candidates = aliases.get(disk, [])
        if not candidates:
            raise ValueError(f"No stable by-id path for physical disk: {disk}")
        stable = min(candidates, key=lambda path: (
            not Path(path).name.startswith(("ata-", "nvme-")), path))
        serial, transport = identify_disk(stable, transports.get(disk))
        result.append({"id": serial, "serial": serial, "path": stable,
                       "device_type": transport, "role": roles.get(disk, "other")})
    result.sort(key=lambda disk: disk["serial"])
    if len({disk["serial"] for disk in result}) != len(result):
        raise ValueError("Duplicate physical disk SMART serials")
    # Avoid Debian's second-Sunday scrub window (days 8-14).
    days = list(range(1, 8)) + list(range(15, 29))
    for index, disk in enumerate(result):
        short_hour = int(hashlib.sha256(disk["serial"].encode()).hexdigest()[:8], 16) % 24
        day = days[index % len(days)]
        hour = (2 + index // len(days)) % 24
        disk["selftests"] = f"(S/../.././{short_hour:02}|L/../{day:02}/./{hour:02})"
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(discover(json.loads(sys.argv[1]))))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Disk discovery failed: {error}", file=sys.stderr)
        sys.exit(1)
