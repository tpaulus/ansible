#!/usr/bin/python3
"""Read SMART health without treating smartctl's health exit bits as read errors."""

import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import time


def atomic_write(path, text):
    path = Path(path)
    pending = path.with_name(path.name + ".new")
    try:
        with pending.open("w") as output:
            os.chmod(pending, 0o644)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def parse_smart(data, returncode, serial):
    if returncode < 0 or returncode & 7:
        raise ValueError(f"smartctl command/device/read failure: {returncode}")
    if data["serial_number"].strip() != serial:
        raise ValueError("SMART disk serial does not match expected stable identity")
    passed = data["smart_status"]["passed"]
    if not isinstance(passed, bool):
        raise ValueError("SMART health not reported")
    values = {"health_passed": int(passed), "exit_status": returncode,
              "health_failed": int(not passed or bool(returncode & 24)),
              "history_warning": int(bool(returncode & 224))}
    temperature = data.get("temperature", {}).get("current")
    if isinstance(temperature, (int, float)) and math.isfinite(temperature):
        values["temperature_celsius"] = temperature
    nvme = data.get("nvme_smart_health_information_log", {})
    for field in (
            "critical_warning", "available_spare", "available_spare_threshold",
            "percentage_used", "data_units_read", "data_units_written",
            "power_on_hours", "unsafe_shutdowns", "media_errors", "num_err_log_entries"):
        raw = nvme.get(field)
        if isinstance(raw, (int, float)) and math.isfinite(raw):
            values[f"nvme_{field}"] = raw
    attributes = []
    for attribute in data.get("ata_smart_attributes", {}).get("table", []):
        for field, raw in (("raw", attribute.get("raw", {}).get("value")),
                           ("normalized", attribute.get("value")),
                           ("threshold", attribute.get("thresh"))):
            if isinstance(raw, (int, float)) and math.isfinite(raw):
                attributes.append((int(attribute["id"]), field, raw))
    return values, attributes


def read_smart(path, device_type=None):
    command = ["smartctl", "-j", "-a"]
    if device_type:
        command.extend(["-d", device_type])
    command.append(path)
    result = subprocess.run(command, text=True,
                            capture_output=True, timeout=20)
    return json.loads(result.stdout), result.returncode


def collect(config, read=read_smart, clock=time.time):
    metrics = []
    failed = False
    for disk in config["disks"]:
        labels = f'disk="{disk["id"]}",role="{disk["role"]}"'
        values = {"collector_success": 0}
        try:
            observed = (read(disk["path"], disk["device_type"])
                        if "device_type" in disk else read(disk["path"]))
            health, attributes = parse_smart(*observed, disk["serial"])
            values.update(health, collector_success=1, observed_timestamp_seconds=int(clock()))
            for attribute, field, value in attributes:
                metrics.append(
                    f'nas_smart_attribute_{field}{{{labels},attribute_id="{attribute}"}} {value}\n')
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            logging.exception("Cannot read SMART for %s", disk["id"])
            failed = True
        metrics.extend(f"nas_smart_{key}{{{labels}}} {value}\n"
                       for key, value in values.items())
    atomic_write(Path(config["metrics_dir"]) / "nas_smart.prom", "".join(metrics))
    return int(failed)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with open(sys.argv[1]) as source:
        sys.exit(collect(json.load(source)))
