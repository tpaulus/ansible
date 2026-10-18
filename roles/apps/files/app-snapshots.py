#!/usr/bin/python3
"""Create app-consistent snapshots and expire only this helper's retained state."""
import datetime
import fcntl
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
    target = config["snapshot"]
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
            raise ValueError("App-snapshot source/staging must be independent real directories")
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
            raise ValueError("Each snapshot source must be its exact ZFS dataset mountpoint")
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
    raise ValueError("Application did not become ready after snapshot restart")


def rsync_arguments(source, destination, previous=None):
    arguments = [
        "rsync", "-aH", "--numeric-ids", "--one-file-system", "--sparse", "--fsync",
        "--chown=0:0", "--chmod=go-rwx", "--exclude=.zfs",
        "--exclude=transcodes", "--exclude=*.candidate",
    ]
    if previous is not None:
        arguments.append(f"--link-dest={previous}")
    return arguments + ["--", str(source) + "/", str(destination) + "/"]


def published_generation(config, filename):
    root = Path(config["snapshot"]["path"])
    marker = root / filename
    if not marker.exists():
        return None
    if marker.is_symlink():
        raise ValueError("Published app-snapshot manifest must not be a symlink")
    previous = json.loads(marker.read_text())
    path = Path(previous["path"])
    if (previous.get("completed") is not True
            or previous.get("dataset") != config["snapshot"]["dataset"]
            or path.parent not in (root / "snapshots", root / "captures")
            or path.resolve() != path or not path.is_dir()):
        raise ValueError("Previous published generation is invalid")
    return path


def previous_generation(config):
    return (published_generation(config, "snapshot.json")
            or published_generation(config, "capture.json"))


GENERATION = re.compile(r"^(\d{8}T\d{6}Z)-[a-f0-9]{32}$")


def generation_time(name):
    match = GENERATION.fullmatch(name)
    if not match:
        raise ValueError(f"Unrecognized app-snapshot generation: {name}")
    return datetime.datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def prune(config, now=None):
    target = config["snapshot"]
    root = Path(target["path"])
    days = config["retention_days"]
    if type(days) is not int or days < 1:
        raise ValueError("App-snapshot retention must be positive days")
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Snapshot root is missing or symlinked")
    info = root.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Snapshot root is not private to the running user")
    execute([config["mount_guard"], str(root), target["dataset"]])
    cutoff = (time.time() if now is None else now) - days * 86400
    published = {
        name: published_generation(config, name)
        for name in ("snapshot.json", "capture.json")
    }
    for directory in ("snapshots", "captures", "pending"):
        parent = root / directory
        if not parent.exists():
            continue
        if parent.resolve() != parent or not parent.is_dir():
            raise ValueError("Generation parent is not a real directory")
        for path in parent.iterdir():
            created = generation_time(path.name)
            if path.resolve() != path or not path.is_dir():
                raise ValueError("Snapshot generation is not a real directory")
            if created >= cutoff:
                continue
            execute([config["mount_guard"], str(path), target["dataset"]])
            # Refuse nested mounts before deleting a specific owned generation.
            for base, directories, _files in os.walk(path, followlinks=False):
                for entry in [Path(base)] + [Path(base) / name for name in directories]:
                    if not entry.is_symlink() and entry.stat().st_dev != root.stat().st_dev:
                        raise ValueError("Nested filesystem in snapshot generation")
            for filename, generation in published.items():
                if generation == path:
                    (root / filename).unlink()
            shutil.rmtree(path)
    datasets = config["sources"] + target.get("retired_datasets", [])
    for source in datasets:
        execute([config["mount_guard"], source["path"], source["dataset"]])
        listing = execute([
            "zfs", "list", "-Hp", "-t", "snapshot", "-o", "name,creation",
            "-d", "1", source["dataset"],
        ])
        for line in listing.stdout.splitlines():
            name, created = line.split("\t")
            dataset, suffix = name.split("@", 1)
            if dataset != source["dataset"]:
                continue
            for prefix in ("app-snapshot-", "nas-app-capture-"):
                if suffix.startswith(prefix):
                    generation_time(suffix.removeprefix(prefix))
                    if int(created) < cutoff:
                        execute(["zfs", "destroy", name])
                    break


def perform_snapshot(config):
    prune(config)
    preflight(config)
    root = Path(config["snapshot"]["path"])
    previous = previous_generation(config)
    snapshot_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    snapshot = "app-snapshot-" + snapshot_id
    pending = root / "pending" / snapshot_id
    pending.mkdir(parents=True, mode=0o700)
    metadata = pending / "metadata"
    metadata.mkdir(mode=0o700)
    state = service_state(config)
    if state.get("ActiveState") not in ("active", "inactive"):
        raise ValueError("Application is failed or transitioning; investigate before snapshot")
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
    if shutil.disk_usage(root).free < estimated_bytes + config["snapshot"]["minimum_free_bytes"]:
        raise ValueError("Insufficient staging capacity for changed files plus the reviewed reserve")
    for arguments in transfers:
        execute([config["mount_guard"], str(root), config["snapshot"]["dataset"]])
        execute(arguments)
    if shutil.disk_usage(root).free < config["snapshot"]["minimum_free_bytes"]:
        raise ValueError("App snapshot exhausted the reviewed reserve")
    execute([config["mount_guard"], str(root), config["snapshot"]["dataset"]])
    result = {
        "path": str(root / "snapshots" / snapshot_id),
        "dataset": config["snapshot"]["dataset"], "completed": True,
    }
    manifest = {
        **result, "app": config["name"], "image": config["image"],
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "snapshots": [f"{source['dataset']}@{snapshot}" for source in config["sources"]],
        "private_files": config["private_files"],
    }
    (metadata / "snapshot.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (metadata / "snapshot.json").chmod(0o600)
    (root / "snapshots").mkdir(exist_ok=True, mode=0o700)
    pending.rename(result["path"])
    publication = root / f".snapshot-{snapshot_id}.json"
    with publication.open("x") as handle:
        handle.write(json.dumps(result) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    publication.chmod(0o600)
    publication.replace(root / "snapshot.json")
    return result


def interrupted(signum, _frame):
    raise InterruptedError(f"App snapshot interrupted by signal {signum}")


def load_config(config_path):
    info = config_path.stat()
    if config_path.is_symlink() or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("App-snapshot configuration must be a restricted root-owned file")
    return json.loads(config_path.read_text())


def locked(config, operation):
    if config["name"] not in ("jellyfin", "prometheus", "webdav"):
        raise ValueError("Unknown snapshot application")
    with open(f"/run/lock/app-snapshots-{config['name']}.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return operation(config)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("jellyfin", "prometheus", "webdav", "prune-all"):
        raise ValueError("usage: app-snapshots jellyfin|prometheus|webdav|prune-all")
    if os.geteuid() != 0:
        raise ValueError("App snapshots require root")
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    directory = Path("/etc/apps/app-snapshots.d")
    if sys.argv[1] == "prune-all":
        if not directory.is_dir():
            raise ValueError("App-snapshot configuration directory is missing")
        failed = False
        for path in sorted(directory.glob("*.json")):
            try:
                locked(load_config(path), prune)
            except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
                print(f"App-snapshot cleanup failed for {path.name}: {error}", file=sys.stderr)
                failed = True
        return int(failed)
    config = load_config(directory / f"{sys.argv[1]}.json")
    result = locked(config, perform_snapshot)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"NAS app snapshot failed: {error}", file=sys.stderr)
        sys.exit(1)
