#!/usr/bin/python3
"""Export read-only ZFS pool and dataset space accounting."""

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time


def run(*args):
    return subprocess.run(
        args, check=True, text=True, capture_output=True, timeout=60,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout


def labels(**values):
    return ",".join(f"{key}={json.dumps(str(value))}" for key, value in values.items())


def integer(value):
    result = int(value)
    if result < 0:
        raise ValueError("negative ZFS space or count")
    return result


def collect(config):
    metrics = []
    failed = False
    for pool in config["pools"]:
        identity = labels(pool=pool["name"], guid=pool["guid"])
        success = 0
        try:
            properties = {}
            for line in run(
                "zpool", "get", "-Hp", "-o", "property,value",
                "guid,size,allocated,free,fragmentation", pool["name"],
            ).splitlines():
                key, value = line.split("\t")
                properties[key] = value
            if properties["guid"] != str(pool["guid"]):
                raise ValueError("live pool GUID mismatch")
            collected = []
            for key in ("size", "allocated", "free"):
                collected.append(
                    f"nas_zpool_{key}_bytes{{{identity}}} {integer(properties[key])}\n"
                )
            if properties["fragmentation"] != "-":
                collected.append(
                    f"nas_zpool_fragmentation_percent{{{identity}}} "
                    f"{integer(properties['fragmentation'])}\n"
                )
            for line in run(
                "zfs", "list", "-Hp", "-r", "-t", "filesystem,volume",
                "-o", "name,used,available,usedbysnapshots,logicalused", pool["name"],
            ).splitlines():
                name, used, available, snapshots, logical = line.split("\t")
                if name != pool["name"] and not name.startswith(pool["name"] + "/"):
                    raise ValueError("dataset is outside expected pool")
                dataset_labels = labels(pool=pool["name"], guid=pool["guid"], dataset=name)
                for key, value in (
                    ("used", used), ("available", available),
                    ("snapshots_used", snapshots), ("logical_used", logical),
                ):
                    collected.append(
                        f"nas_zfs_dataset_{key}_bytes{{{dataset_labels}}} {integer(value)}\n"
                    )
            metrics.extend(collected)
            metrics.append(
                f"nas_storage_observed_timestamp_seconds{{{identity}}} {int(time.time())}\n"
            )
            success = 1
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            logging.exception("Cannot collect storage capacity for %s", pool["name"])
            failed = True
        metrics.append(f"nas_storage_collector_success{{{identity}}} {success}\n")

    path = Path(config["metrics_dir"]) / "nas_storage.prom"
    pending = path.with_name(path.name + ".new")
    try:
        with pending.open("w") as output:
            os.chmod(pending, 0o644)
            output.write("".join(metrics))
            output.flush()
            os.fsync(output.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
    return int(failed)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with open(sys.argv[1]) as source:
        sys.exit(collect(json.load(source)))
