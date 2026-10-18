#!/usr/bin/python3
"""Capture only reviewed app state from clean stopped ZFS snapshots."""
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import time
import uuid


def execute(arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True,
                          env={**os.environ, "LC_ALL": "C"})


def service(config, *arguments):
    identity = config["identity"]
    return execute([
        "runuser", "-u", identity["name"], "--", "env",
        f"HOME={identity['home']}", f"XDG_RUNTIME_DIR=/run/user/{identity['uid']}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{identity['uid']}/bus",
        "systemctl", "--user", *arguments, f"{config['name']}.service",
    ])


def service_state(config):
    result = service(config, "show", "--property=ActiveState,SubState,Result,ExecMainStatus")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def preflight(config):
    target = config["capture"]
    root = Path(target["path"])
    if (type(target.get("minimum_free_bytes")) is not int
            or target["minimum_free_bytes"] <= 0):
        raise ValueError("A positive staging free-byte reserve is required")
    if not root.is_dir() or root.resolve() != root or not str(root).startswith("/mnt/"):
        raise ValueError("Staging must be an existing, nonsymlinked /mnt directory")
    info = root.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Staging must be root-owned and inaccessible to other users (0700)")
    if not os.access(root, os.W_OK | os.X_OK) or os.statvfs(root).f_flag & os.ST_RDONLY:
        raise ValueError("Staging is not writable")
    arguments = [config["mount_guard"], str(root), target["dataset"]]
    if not config["sources"]:
        raise ValueError("Explicit persistent state sources are required")
    for source in config["sources"]:
        path = Path(source["path"])
        if (path.resolve() != path or not path.is_dir()
                or path == root or path.is_relative_to(root) or root.is_relative_to(path)):
            raise ValueError("Capture source/staging must be independent real directories")
        if source["dataset"] == target["dataset"]:
            raise ValueError("Staging cannot be inside a source dataset")
        arguments.extend([str(path), source["dataset"]])
        marker = path / ".nas-app-imported"
        if not marker.is_file() or marker.read_text().strip() != source["import_name"]:
            raise ValueError("Source app state has not passed explicit import")
        for required in source.get("required_files", []):
            candidate = path / required
            if not candidate.exists() or not candidate.resolve().is_relative_to(path):
                raise ValueError("Required source state is missing or escapes the source")
        mountpoint = execute(["zfs", "get", "-Hp", "-o", "value", "mountpoint", source["dataset"]])
        if mountpoint.stdout.strip() != str(path):
            raise ValueError("Each capture source must be its exact ZFS dataset mountpoint")
    execute(arguments)
    for filename in config["private_files"]:
        path = Path(filename)
        if (not path.is_file() or path.resolve() != path
                or not path.is_relative_to(config["identity"]["home"])):
            raise ValueError("Restricted deployment metadata is missing or symlinked")
    if shutil.disk_usage(root).free < target["minimum_free_bytes"]:
        raise ValueError("Staging free space is below the reviewed reserve")


def wait_ready(config):
    for attempt in range(24):
        try:
            with urllib.request.urlopen(config["readiness_url"], timeout=5) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except (urllib.error.URLError, TimeoutError):
            status = None
        if status == config.get("readiness_status", 200):
            return
        if attempt < 23:
            time.sleep(5)
    raise ValueError("Application did not become ready after capture restart")


def rsync_arguments(source, destination, previous=None):
    arguments = [
        "rsync", "-aH", "--numeric-ids", "--one-file-system", "--sparse", "--fsync",
        "--chown=0:0", "--chmod=go-rwx", "--exclude=.zfs",
        "--exclude=transcodes", "--exclude=*.candidate",
    ]
    if previous is not None:
        arguments.append(f"--link-dest={previous}")
    return arguments + ["--", str(source) + "/", str(destination) + "/"]


def previous_generation(config):
    root = Path(config["capture"]["path"])
    marker = root / "capture.json"
    if not marker.exists():
        return None
    if marker.is_symlink():
        raise ValueError("Published capture manifest must not be a symlink")
    previous = json.loads(marker.read_text())
    path = Path(previous["path"])
    if (previous.get("completed") is not True
            or previous.get("dataset") != config["capture"]["dataset"]
            or path.parent != root / "captures" or path.resolve() != path or not path.is_dir()):
        raise ValueError("Previous published generation is invalid")
    return path


def perform_capture(config):
    preflight(config)
    root = Path(config["capture"]["path"])
    previous = previous_generation(config)
    capture_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    snapshot = "nas-app-capture-" + capture_id
    pending = root / "pending" / capture_id
    pending.mkdir(parents=True, mode=0o700)
    metadata = pending / "metadata"
    metadata.mkdir(mode=0o700)
    state = service_state(config)
    if state.get("ActiveState") not in ("active", "inactive"):
        raise ValueError("Application is failed or transitioning; investigate before capture")
    resume = state["ActiveState"] == "active"
    try:
        if resume:
            service(config, "stop")
        stopped = service_state(config)
        if (stopped.get("ActiveState") != "inactive" or stopped.get("Result") != "success"
                or stopped.get("ExecMainStatus") != "0"):
            raise ValueError("Application did not stop cleanly; refusing a database snapshot")
        execute(["zfs", "snapshot"] + [
            f"{source['dataset']}@{snapshot}" for source in config["sources"]
        ])
        for index, filename in enumerate(config["private_files"]):
            destination = metadata / f"{index}-{Path(filename).name}"
            shutil.copyfile(filename, destination)
            destination.chmod(0o600)
    finally:
        if resume:
            service(config, "start")
            wait_ready(config)

    transfers = []
    estimated_bytes = 0
    for source in config["sources"]:
        snapshot_path = Path(source["path"]) / ".zfs" / "snapshot" / snapshot
        if not snapshot_path.is_dir() or snapshot_path.resolve() != snapshot_path:
            raise ValueError("Stopped source snapshot is unavailable; live data will not be copied")
        destination = pending / source["import_name"]
        old = previous / source["import_name"] if previous is not None else None
        arguments = rsync_arguments(snapshot_path, destination, old)
        dryrun = execute(arguments[:1] + ["--dry-run", "--stats"] + arguments[1:])
        match = re.search(r"^Total transferred file size: ([0-9,]+) bytes$", dryrun.stdout, re.MULTILINE)
        if match is None:
            raise ValueError("Cannot establish the copy capacity estimate")
        estimated_bytes += int(match[1].replace(",", ""))
        transfers.append(arguments)
    if shutil.disk_usage(root).free < estimated_bytes + config["capture"]["minimum_free_bytes"]:
        raise ValueError("Insufficient staging capacity for changed files plus the reviewed reserve")
    for arguments in transfers:
        execute([config["mount_guard"], str(root), config["capture"]["dataset"]])
        execute(arguments)
    if shutil.disk_usage(root).free < config["capture"]["minimum_free_bytes"]:
        raise ValueError("Capture exhausted the reviewed reserve")
    execute([config["mount_guard"], str(root), config["capture"]["dataset"]])
    result = {
        "path": str(root / "captures" / capture_id),
        "dataset": config["capture"]["dataset"], "completed": True,
    }
    manifest = {
        **result, "app": config["name"], "image": config["image"],
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "snapshots": [f"{source['dataset']}@{snapshot}" for source in config["sources"]],
        "private_files": config["private_files"],
    }
    (metadata / "capture.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (metadata / "capture.json").chmod(0o600)
    (root / "captures").mkdir(exist_ok=True, mode=0o700)
    pending.rename(result["path"])
    publication = root / f".capture-{capture_id}.json"
    with publication.open("x") as handle:
        handle.write(json.dumps(result) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    publication.chmod(0o600)
    publication.replace(root / "capture.json")
    return result


def interrupted(signum, _frame):
    raise InterruptedError(f"Capture interrupted by signal {signum}")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("jellyfin", "prometheus", "webdav"):
        raise ValueError("usage: capture-app jellyfin|prometheus|webdav")
    if os.geteuid() != 0:
        raise ValueError("Application captures require root")
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    config_path = Path("/etc/apps/capture.d") / f"{sys.argv[1]}.json"
    info = config_path.stat()
    if config_path.is_symlink() or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Capture configuration must be a restricted root-owned file")
    config = json.loads(config_path.read_text())
    result = perform_capture(config)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"NAS application capture failed: {error}", file=sys.stderr)
        sys.exit(1)
