#!/usr/bin/python3
"""Map the Debian root filesystem and approved ZFS pool leaves to physical disks."""
import json
import os
import re
import subprocess
import sys


def run(arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True)


def flatten(devices):
    result = []
    for device in devices:
        result.append(device)
        result.extend(flatten(device.get("children", [])))
    return result


def resolve_device(source):
    if source.startswith("/dev/"):
        return os.path.realpath(source)
    if source.startswith("UUID="):
        return os.path.realpath(run(["blkid", "-U", source.removeprefix("UUID=")]).stdout.strip())
    if source.startswith("PARTUUID="):
        return os.path.realpath(run(["blkid", "-t", source, "-o", "device"]).stdout.splitlines()[0])
    raise ValueError(f"Root filesystem is not backed by an identifiable block device: {source}")


def block_ancestry(device):
    output = run([
        "lsblk", "--json", "--paths", "--inverse", "--output",
        "PATH,TYPE,MAJ:MIN,SERIAL,MODEL,WWN", device,
    ])
    records = flatten(json.loads(output.stdout)["blockdevices"])
    physical = [record for record in records if record["type"] == "disk"]
    if not physical:
        raise ValueError(f"No physical disk ancestry could be identified for {device}")
    return records, physical


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9_.-]+", sys.argv[1]):
        raise ValueError("Supply exactly one approved pool name")
    pool = sys.argv[1]
    root_source = run(["findmnt", "--noheadings", "--output", "SOURCE", "--target", "/"]).stdout.strip()
    root_device = resolve_device(root_source)
    root_records, root_disks = block_ancestry(root_device)
    status = run(["zpool", "status", "-P", "-L", pool]).stdout
    vdev_paths = sorted(set(re.findall(r"/dev/\S+", status)))
    if not vdev_paths:
        raise ValueError(f"No physical vdev paths found for pool {pool}")

    vdevs = []
    pool_disks = {}
    for path in vdev_paths:
        resolved = os.path.realpath(path)
        records, physical = block_ancestry(resolved)
        vdevs.append({"path": path, "resolved_path": resolved, "devices": records})
        for disk in physical:
            pool_disks[disk["maj:min"]] = disk

    root_disk_ids = {disk["maj:min"] for disk in root_disks}
    overlaps = sorted(root_disk_ids & set(pool_disks))
    result = {
        "root_source": root_source,
        "root_device": root_device,
        "root_ancestry": root_records,
        "root_disks": root_disks,
        "pool": pool,
        "pool_vdevs": vdevs,
        "pool_disks": list(pool_disks.values()),
        "overlapping_disk_major_minor": overlaps,
    }
    print(json.dumps(result, sort_keys=True))
    if overlaps:
        raise ValueError(f"OS root disk overlaps ZFS pool vdevs: {', '.join(overlaps)}")


if __name__ == "__main__":
    try:
        main()
    except (IndexError, KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Cannot safely identify reinstall and pool disks: {error}", file=sys.stderr)
        sys.exit(1)
