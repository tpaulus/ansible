#!/usr/bin/python3
"""Wait without a deadline for NAS storage writers and their exclusive locks."""
import errno
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time


UNITS = (
    "restic.service",
    "restic-check.service",
    "cloud-sync.service",
    "sanoid.service",
    "sanoid-prune.service",
    "app-snapshots-prune.service",
    "maintenance-metrics.service",
    "apt-daily.service",
    "apt-daily-upgrade.service",
)
PROCESS_MARKERS = (
    "/usr/local/sbin/backup-to-b2",
    "/usr/local/sbin/check-restic",
    "/usr/local/sbin/cloud-sync",
    "/usr/local/sbin/app-snapshots",
    "sanoid --cron",
    "/usr/bin/restic",
    "/usr/sbin/restic",
)
LOCKS = (
    "/run/lock/restic.lock",
    "/run/lock/cloud-sync.lock",
)


def unit_state(unit):
    result = subprocess.run(
        ["systemctl", "show", "--no-pager", "--property=LoadState,ActiveState,Result", unit],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Cannot inspect {unit}: {result.stderr.strip()}")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def active_processes():
    active = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for marker in PROCESS_MARKERS:
            if marker in command:
                active.append(f"{marker} (pid {entry.name})")
                break
    return active


def locked_files():
    active = []
    paths = [Path(filename) for filename in LOCKS]
    paths.extend(Path("/run/lock").glob("app-snapshots-*.lock"))
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open("r") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
        except FileNotFoundError:
            continue
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                active.append(str(path))
            else:
                raise
    return active


def pool_scan(pool):
    imported = subprocess.run(
        ["zpool", "list", "-H", "-o", "name", pool],
        capture_output=True, text=True, check=False,
    )
    if imported.returncode == 1:
        return []
    if imported.returncode:
        raise RuntimeError(f"Cannot inspect imported pool {pool}: {imported.stderr.strip()}")
    result = subprocess.run(
        ["zpool", "status", pool], capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Cannot inspect {pool} scan state: {result.stderr.strip()}")
    lines = [line.strip() for line in result.stdout.splitlines()]
    return [line for line in lines if "in progress" in line.lower()]


def main(pool):
    print("Waiting indefinitely for scheduled storage work to become idle.", flush=True)
    inactive_failures = set()
    while True:
        busy = []
        for unit in UNITS:
            state = unit_state(unit)
            if state.get("LoadState") == "not-found":
                continue
            active_state = state.get("ActiveState")
            if active_state in ("active", "activating", "reloading", "deactivating"):
                busy.append(unit)
            elif active_state == "failed" or state.get("Result") not in ("success", "none"):
                failure = f"{unit}: {state}"
                if failure not in inactive_failures:
                    print(
                        "Inactive service failure (not an active writer): " + failure,
                        file=sys.stderr,
                        flush=True,
                    )
                    inactive_failures.add(failure)
        busy.extend(active_processes())
        busy.extend(locked_files())
        busy.extend(pool_scan(pool))
        if not busy:
            print("All known storage jobs, locks and pool scans are idle.", flush=True)
            return
        print("Still active: " + "; ".join(sorted(set(busy))), flush=True)
        time.sleep(15)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("usage: wait-storage-idle.py POOL")
        main(sys.argv[1])
    except (OSError, RuntimeError, ValueError, KeyboardInterrupt) as error:
        print(f"Storage idle wait stopped: {error}", file=sys.stderr)
        sys.exit(1)
