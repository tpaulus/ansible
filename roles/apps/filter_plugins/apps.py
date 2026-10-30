"""Resolve repository application mounts without modifying NetBox share policy."""
import copy
import posixpath
import re

from ansible.errors import AnsibleFilterError


def absolute_path(value):
    return (isinstance(value, str) and value.startswith("/")
            and posixpath.normpath(value) == value
            and not any(char in value for char in '\n\r\x00":%\\'))


def select_apps(configured, run_tags):
    application_tags = set(run_tags) & set(configured)
    if application_tags and not set(run_tags) & {"all", "apps"}:
        return [name for name in configured if name in application_tags]
    return configured


def snapshot_config(app, snapshots, mount_guard, retention_days):
    try:
        target = copy.deepcopy(snapshots[app["name"]])
        if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days < 1:
            raise ValueError("App-snapshot retention must be a positive number of days")
        if not absolute_path(target["path"]) or not target["path"].startswith("/mnt/"):
            raise ValueError("App-snapshot staging must be an explicit /mnt path")
        if not re.fullmatch(r"[A-Za-z0-9_./ -]+", target["dataset"]):
            raise ValueError("App-snapshot staging needs an explicit dataset")
        selected = app["snapshot_targets"]
        sources = [mount for mount in app["mounts"] if mount["target"] in selected]
        if not selected or len(sources) != len(selected):
            raise ValueError("Explicit private state snapshot targets are required")
        if any("dataset_key" in mount or not mount.get("require_import") for mount in sources):
            raise ValueError("App snapshots must not include media, document roots or disposable caches")
        if len({mount["dataset"].split("/")[0] for mount in sources}) != 1:
            raise ValueError("Atomic stopped snapshots require a single source pool")
        for retired in target.get("retired_datasets", []):
            if (not absolute_path(retired["path"]) or not retired["path"].startswith("/mnt/")
                    or not re.fullmatch(r"[A-Za-z0-9_./ -]+", retired["dataset"])
                    or ".." in retired["dataset"].split("/")
                    or retired["dataset"] == target["dataset"]):
                raise ValueError("Retired snapshot sources require explicit independent datasets")
        home = app["identity"]["home"]
        files = [
            f"{home}/.config/containers/systemd/{app['name']}.container",
            f"{home}/.config/containers/containers.conf",
            f"{home}/.config/containers/storage.conf",
        ]
        private_files = app.get("snapshot_private_files", [])
        if (not isinstance(private_files, list)
                or any(not isinstance(filename, str) or not filename
                       or filename.startswith("/")
                       or posixpath.normpath(filename) != filename
                       or ".." in filename.split("/")
                       or any(char in filename for char in "\n\r\x00")
                       for filename in private_files)):
            raise ValueError("Snapshot private files must be safe app-home-relative paths")
        files.extend(f"{home}/{filename}" for filename in private_files)
        return {
            "name": app["name"], "identity": app["identity"], "image": app["image"],
            "snapshot": target, "sources": sources, "private_files": files,
            "rsync_excludes": app.get("rsync_excludes", []),
            "retention_days": retention_days,
            "mount_guard": mount_guard, "readiness_url": app["readiness_url"],
            "readiness_status": app.get("readiness_status", 200),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise AnsibleFilterError(f"Invalid app-snapshot configuration: {error}") from error


def resolve_apps(apps, resolved, accounts, selected):
    try:
        if not apps or not selected or len(set(selected)) != len(selected):
            raise ValueError("Explicit application definitions and selection are required")
        account_map = {account["name"]: account for account in accounts}
        result = []
        for name in selected:
            if not isinstance(name, str) or name not in apps:
                raise ValueError(f"Unknown application: {name}")
            app = copy.deepcopy(apps[name])
            app["name"] = name
            app["identity"] = account_map[app["account"]]
            if any(not isinstance(app[key], int) or isinstance(app[key], bool) or app[key] <= 0
                   for key in ("container_uid", "container_gid")):
                raise ValueError("Explicit nonroot container UID/GID mappings are required")
            if not re.fullmatch(r"[\w./-]+:[\w.+-]+@sha256:[a-f0-9]{64}", app["image"]):
                raise ValueError(f"{name} requires a tag and SHA256 digest")
            repository = app["image"].split(":", 1)[0]
            app["runtime_image"] = app["image"]
            if "." not in repository.split("/")[0]:
                prefix = "docker.io/" if "/" in repository else "docker.io/library/"
                app["runtime_image"] = prefix + app["image"]
            if app["network"] not in ("pasta", "host"):
                raise ValueError("Unreviewed container network")
            if app["network"] == "host" and app.get("ports"):
                raise ValueError("Host networking cannot publish additional ports")
            if not app.get("mounts") or not app.get("health_cmd"):
                raise ValueError("Persistent mounts and explicit health checks are required")
            for retained in app.get("retained_datasets", []):
                if (not absolute_path(retained["path"])
                        or not retained["path"].startswith("/mnt/")
                        or not re.fullmatch(r"[A-Za-z0-9_./ -]+", retained["dataset"])
                        or ".." in retained["dataset"].split("/")):
                    raise ValueError("Retained app datasets require explicit dataset identities and paths")
                properties = retained.get("properties", {})
                if (not isinstance(properties, dict)
                        or set(properties) - {"quota", "refquota"}
                        or any(not isinstance(value, str)
                               or not re.fullmatch(r"none|[1-9][0-9]*[MGT]", value)
                               for value in properties.values())):
                    raise ValueError("Retained app datasets support only explicit quota/refquota limits")
            destinations = set()
            for mount in app["mounts"]:
                properties = mount.get("properties", {})
                if (not isinstance(properties, dict)
                        or set(properties) - {"quota", "refquota"}
                        or any(not isinstance(value, str)
                               or not re.fullmatch(r"none|[1-9][0-9]*[MGT]", value)
                               for value in properties.values())
                        or (properties and ("dataset_key" in mount or not mount.get("create")))):
                    raise ValueError("Only managed application datasets may declare quota/refquota limits")
                if "dataset_key" in mount:
                    dataset = resolved["datasets"][mount["dataset_key"]]
                    if any(key in mount for key in (
                        "path", "dataset", "create", "create_directory",
                        "require_import", "owner", "group", "mode",
                    )):
                        raise ValueError("A share reference cannot override storage policy")
                    suffix = mount.get("relative_path", "")
                    if suffix and (suffix.startswith("/") or ".." in suffix.split("/")):
                        raise ValueError("Invalid share-relative path")
                    mount["path"] = posixpath.join(dataset["path"], suffix).rstrip("/")
                    mount["dataset"] = dataset["dataset"]
                    mount["create"] = False
                elif mount.get("create") and any(
                    dataset["dataset"] == mount["dataset"]
                    for dataset in resolved.get("datasets", {}).values()
                ):
                    raise ValueError("Repository app creation must not manage a NetBox dataset")
                if (not absolute_path(mount["path"]) or not mount["path"].startswith("/mnt/")
                        or not absolute_path(mount["target"])
                        or not re.fullmatch(r"[A-Za-z0-9_./ -]+", mount["dataset"])
                        or ".." in mount["dataset"].split("/")):
                    raise ValueError("Invalid dataset/bind path")
                if mount["target"] in destinations or mount["access"] not in ("ro", "rw"):
                    raise ValueError("Duplicate destination or invalid mount access")
                destinations.add(mount["target"])
                for required in mount.get("required_files", []):
                    if required.startswith("/") or ".." in required.split("/"):
                        raise ValueError("Invalid required state file")
                if (mount.get("require_import")
                        and not re.fullmatch(r"[a-z0-9-]+", mount.get("import_name", ""))):
                    raise ValueError("Imported state requires a staged backup name")
            if name == "prometheus":
                if not app.get("configuration", {}).get("scrape_configs"):
                    raise ValueError("Prometheus requires explicit nonempty scrape configuration")
                if app.get("rule_files", []) != app["configuration"].get("rule_files", []):
                    raise ValueError("Prometheus configuration/rule validation lists differ")
            result.append(app)
        if len({app["account"] for app in result}) != len(result):
            raise ValueError("Each application requires its own account")
        return result
    except (KeyError, TypeError, ValueError) as error:
        raise AnsibleFilterError(f"Invalid NAS application configuration: {error}") from error


class FilterModule:
    def filters(self):
        return {
            "resolve_apps": resolve_apps,
            "select_apps": select_apps,
            "snapshot_config": snapshot_config,
            "readonly_mount_args": lambda mounts: [
                arg for mount in mounts
                for arg in ("--volume", f"{mount['path']}:{mount['target']}:ro")
            ],
        }
