#!/usr/bin/python3
"""Report live pool health and retain full scrub results independently of current scans."""

import datetime
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def atomic_write(path, text, mode=0o644):
    path = Path(path)
    pending = path.with_name(path.name + ".new")
    try:
        with pending.open("w") as output:
            os.chmod(pending, mode)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def timestamp(value, history=False):
    pattern = "%Y-%m-%d.%H:%M:%S" if history else "%a %b %d %H:%M:%S %Y"
    return int(datetime.datetime.strptime(value.strip(), pattern)
               .replace(tzinfo=datetime.timezone.utc).timestamp())


def parse_pool_health(text):
    state = re.search(r"^\s*state:\s*(\S+)\s*$", text, re.MULTILINE)
    if not state or state[1] not in {
            "ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "SUSPENDED", "REMOVED"}:
        raise ValueError("missing or unknown zpool health state")
    return int(state[1] == "ONLINE")


def parse_status(text):
    match = re.search(r"^\s*scan:\s*(.+)$", text, re.MULTILINE)
    if not match:
        raise ValueError("missing zpool scan status")
    scan = match[1]
    result = {"result": None, "running": 0, "progress": 0}
    completed = re.fullmatch(
        r"scrub repaired \S+ in .+ with (\d+) errors on (.+)", scan)
    if completed:
        errors = int(completed[1])
        result["result"] = (timestamp(completed[2]), errors == 0, errors)
    elif re.match(r"scrub (in progress|paused)", scan):
        result["running"] = 1
        progress = re.search(r"(\d+(?:\.\d+)?)% done", text)
        if progress:
            result["progress"] = float(progress[1]) / 100
    elif scan.startswith("scrub canceled on "):
        result["result"] = (timestamp(scan.removeprefix("scrub canceled on ")), False, 0)
    elif scan == "none requested" or re.match(
            r"(resilver|rebuild|error scrub|scrub (?:of|errors))", scan):
        pass
    else:
        raise ValueError(f"unrecognized scan state: {scan}")
    return result


def parse_history(text):
    # Only full scans (func=1, mintxg=0) qualify. An intervening setup,
    # cancellation or aborted scan breaks the association with a completion.
    full = False
    results = []
    for line in text.splitlines():
        record = re.match(
            r"^(\d{4}-\d\d-\d\d\.\d\d:\d\d:\d\d)\s+\[txg:\d+\]\s+(.+)$", line)
        if not record:
            continue
        event = record[2]
        if event.startswith("scan setup "):
            full = bool(re.search(r"\bfunc=1 mintxg=0\b", event))
        elif event.startswith("scan aborted") or re.match(r"scan cancell?ed\b", event):
            if full:
                results.append((timestamp(record[1], True), False, 0))
            full = False
        elif event.startswith("scan done "):
            errors = re.search(r"\berrors=(\d+)\b", event)
            if full and errors:
                count = int(errors[1])
                results.append((timestamp(record[1], True), count == 0, count))
            full = False
    return results


def update_state(state, results, now):
    combined = {}
    for completed, success, errors in results:
        previous_success, previous_errors = combined.get(completed, (True, 0))
        combined[completed] = (previous_success and success, max(previous_errors, errors))
    for completed, (success, errors) in sorted(combined.items()):
        if completed > now + 300 or completed <= 0:
            raise ValueError("invalid/future scan completion")
        if success:
            state["last_success"] = max(state["last_success"], completed)
        if completed >= state["last_result_time"]:
            state.update(last_result_time=completed, last_scan_failed=int(not success),
                         last_errors=errors)
    return state


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          timeout=60, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}).stdout


def collect(config, command=run, clock=time.time):
    metrics = []
    failed = False
    for pool in config["pools"]:
        now = int(clock())
        state = dict(guid=str(pool["guid"]), last_success=0, last_result_time=0,
                     last_scan_failed=0, last_errors=0, observed=0)
        state_path = Path(config["state_dir"]) / f"scrub-{pool['guid']}.json"
        success = 0
        healthy = 0
        scan = {"running": 0, "progress": 0}
        try:
            if state_path.exists():
                stored = json.loads(state_path.read_text())
                if stored["guid"] != state["guid"]:
                    raise ValueError("persisted pool GUID mismatch")
                for key in state.keys() - {"guid"}:
                    if not isinstance(stored[key], int) or not 0 <= stored[key] <= now + 300:
                        raise ValueError("invalid persisted scrub state")
                state = stored
            guid = command("zpool", "get", "-H", "-o", "value", "guid", pool["name"]).strip()
            if guid != str(pool["guid"]):
                raise ValueError("live pool GUID mismatch")
            status = command("zpool", "status", "-p", pool["name"])
            healthy = parse_pool_health(status)
            scan = parse_status(status)
            results = parse_history(command("zpool", "history", "-il", pool["name"]))
            # Status alone cannot distinguish a full scrub from `scrub -C`
            # (since a saved txg). Only paired history proves full coverage.
            if scan["result"] and not scan["result"][1]:
                results.append(scan["result"])
            update_state(state, results, now)
            state["observed"] = now
            atomic_write(state_path, json.dumps(state) + "\n", 0o600)
            success = 1
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            logging.exception("Cannot collect scrub history for %s", pool["name"])
            failed = True
        labels = f'pool="{pool["name"]}",guid="{pool["guid"]}"'
        metrics.append(f"nas_zpool_healthy{{{labels}}} {healthy}\n")
        values = {
            "collector_success": success,
            "observed_timestamp_seconds": state["observed"],
            "last_success_timestamp_seconds": state["last_success"],
            "last_result_timestamp_seconds": state["last_result_time"],
            "last_scan_failed": state["last_scan_failed"],
            "last_scan_errors": state["last_errors"],
            "running": scan["running"],
            "progress_ratio": scan["progress"],
        }
        metrics.extend(f"nas_scrub_{key}{{{labels}}} {value}\n"
                       for key, value in values.items())
    atomic_write(Path(config["metrics_dir"]) / "nas_scrub.prom", "".join(metrics))
    return int(failed)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with open(sys.argv[1]) as source:
        sys.exit(collect(json.load(source)))
