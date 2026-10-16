import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parents[1]


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.bin = self.path / "bin"
        self.bin.mkdir()
        self.log = self.path / "commands"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "COMMAND_LOG": str(self.log),
            "MAX_RETRIES": "2",
            "BASE_RETRY_DELAY": "0",
        }
        self.templates = Environment(
            loader=FileSystemLoader(ROOT),
            undefined=StrictUndefined,
        )
        self.templates.filters["quote"] = shlex.quote
        self.templates.filters["comment"] = lambda value: f"# {value}"

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return path

    def render(self, template, **variables):
        text = self.templates.get_template(template).render(
            ansible_managed="Managed by Ansible",
            **variables,
        )
        path = self.path / Path(template).stem
        path.write_text(text)
        subprocess.run(["bash", "-n", path], check=True, capture_output=True)
        return path

    def backup(self, behavior):
        restic = self.command(
            "restic",
            'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n' + behavior,
        )
        self.command("timeout", 'shift 2\nexec "$@"')
        return self.render(
            "roles/truenas_backup/templates/backup-script.sh.j2",
            truenas_backup_restic_bin=str(restic),
            offsite_restic_password="secret with '$ and \" quotes",
            b2_account_id="fixture",
            b2_account_key="secret",
            offsite_b2_bucket_name="fixture",
            b2_s3_endpoint="example.invalid",
            truenas_backup_shares=["Test Dataset"],
            truenas_backup_metrics_dir=str(self.path),
        )

    def run_script(self, path):
        return subprocess.run(
            ["bash", path],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_backup_preserves_dataset_arguments_and_runs_retention(self):
        script = self.backup(
            'case "$*" in *backup*)\n'
            '  [ "$RESTIC_PASSWORD" = "secret with \'\\$ and \\" quotes" ] || exit 9\n'
            '  [ "$7" = "/mnt/tank/Test Dataset" ] || exit 10\n'
            "esac\nexit 0"
        )
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("forget --keep-daily 7", self.log.read_text())
        self.assertIn("b2_backup_completed", (self.path / "b2_backup.prom").read_text())

    def test_failed_backup_retries_without_pruning_unlocking_or_false_success(self):
        script = self.backup('case "$*" in *backup*) exit 3 ;; esac\nexit 0')
        result = self.run_script(script)
        self.assertNotEqual(result.returncode, 0)
        commands = self.log.read_text()
        self.assertEqual(commands.count("backup --cleanup-cache"), 2)
        self.assertNotIn("forget", commands)
        self.assertNotIn("unlock", commands)
        self.assertNotIn("self-update", commands)
        metrics = (self.path / "b2_backup.prom").read_text()
        self.assertIn("b2_backup_failed", metrics)
        self.assertNotIn("b2_backup_completed", metrics)

    def test_unavailable_repository_fails_without_automatic_initialization(self):
        result = self.run_script(self.backup("exit 1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.log.read_text().strip(), "snapshots")
        self.assertIn("initialize it explicitly", result.stderr)
        self.assertIn("b2_backup_failed", (self.path / "b2_backup.prom").read_text())

    def test_uncordon_failure_keeps_marker(self):
        marker = self.path / "drained"
        marker.touch()
        self.command("kubectl", 'case "$1" in get) exit 0 ;; *) exit 1 ;; esac')
        script = self.render(
            "roles/k3s_uncordon/templates/uncordon-node.sh.j2",
            k3s_uncordon_marker=str(marker),
        )
        self.assertNotEqual(self.run_script(script).returncode, 0)
        self.assertTrue(marker.exists())

    def test_uncordon_success_removes_marker(self):
        marker = self.path / "drained"
        marker.touch()
        self.command("kubectl", "exit 0")
        script = self.render(
            "roles/k3s_uncordon/templates/uncordon-node.sh.j2",
            k3s_uncordon_marker=str(marker),
        )
        self.assertEqual(self.run_script(script).returncode, 0)
        self.assertFalse(marker.exists())

    def test_etcd_backup_unmounts_after_rsync_failure(self):
        mount = self.path / "mount"
        mount.mkdir()
        self.command("mktemp", f"printf '%s\\n' {shlex.quote(str(mount))}")
        self.command("mount", "exit 0")
        self.command("mountpoint", "exit 0")
        self.command("rsync", "exit 23")
        self.command("umount", 'printf "unmounted\\n" >> "$COMMAND_LOG"')
        self.command("rmdir", "exit 0")
        script = self.render(
            "roles/k3s_backup/templates/backup_etcd.sh.j2",
            k3s_backup_server="example.invalid",
            k3s_backup_share="/Server Backups/k3s",
            k3s_backup_source="/source",
            k3s_backup_metrics_dir=str(self.path),
            k3s_backup_retention_days=14,
            ansible_facts={"hostname": "fixture"},
        )
        self.assertEqual(self.run_script(script).returncode, 23)
        self.assertIn("unmounted", self.log.read_text())
        self.assertFalse((self.path / "k3s_etcd_backup.prom").exists())

    def test_ups_shutdown_timer_dispatches_privileged_script(self):
        script = self.render(
            "roles/nut/templates/upssched-cmd.j2",
            ansible_facts={"os_family": "Debian"},
            nut_timed_shutdown_enabled=True,
        )
        # Redirect the absolute privileged command to a harmless test double.
        text = script.read_text().replace("/usr/bin/sudo", str(self.bin / "sudo"))
        script.write_text(text)
        self.command("sudo", 'printf "%s\\n" "$*" >> "$COMMAND_LOG"')
        result = subprocess.run(
            ["sh", script, "SHUTDOWN"], env=self.env, capture_output=True
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.log.read_text().strip(), "-n /sbin/ups-shutdown")

    def test_ups_timer_is_inert_unless_explicitly_enabled(self):
        script = self.render(
            "roles/nut/templates/upssched-cmd.j2",
            ansible_facts={"os_family": "Debian"},
            nut_timed_shutdown_enabled=False,
        )
        self.command("logger", 'printf "%s\\n" "$*" >> "$COMMAND_LOG"')
        result = subprocess.run(
            ["sh", script, "SHUTDOWN"], env=self.env, capture_output=True
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Timed shutdown is disabled", self.log.read_text())

    def test_decommission_refuses_unconfirmed_target_before_commands(self):
        result = subprocess.run(
            [
                sys.executable, "-m", "ansible.cli.playbook",
                "-i", "inventories/ci/hosts.yml",
                "playbooks/decommission/k3s.yml",
                "--check", "--limit", "ci_compute",
                "-e", '{"ansible_become": false}',
                "-e", "ansible_connection=local",
                "-e", f"ansible_python_interpreter={sys.executable}",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("k3s_decommission_confirm", result.stdout)
        self.assertNotIn("TASK [Cordon target node]", result.stdout)

    def test_motd_tag_does_not_run_unrelated_upstream_validation(self):
        result = subprocess.run(
            [
                sys.executable, "-m", "ansible.cli.playbook",
                "-i", "inventories/ci/hosts.yml", "playbooks/site.yml",
                "--check", "--tags", "motd", "--limit", "ci_compute",
                "-e", '{"ansible_become": false}',
                "-e", "ansible_connection=local",
                "-e", f"ansible_python_interpreter={sys.executable}",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("motd : Install MOTD", result.stdout)
        self.assertNotIn("validate_argument_spec", result.stdout)


if __name__ == "__main__":
    unittest.main()
