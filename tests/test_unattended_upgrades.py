from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parents[1]


class UnattendedUpgradeTests(unittest.TestCase):
    def render(self, distribution, groups=()):
        templates = Environment(
            loader=FileSystemLoader(ROOT / "roles/unattended_upgrades/templates"),
            undefined=StrictUndefined,
        )
        templates.filters["comment"] = lambda value: f"// {value}"
        templates.filters["ternary"] = lambda value, yes, no: yes if value else no
        return templates.get_template("50unattended-upgrades.j2").render(
            ansible_managed="Managed by Ansible",
            ansible_facts={"distribution": distribution},
            group_names=groups,
        )

    def allowed(self, config, distribution, codename, origin, release):
        config = config.replace("${distro_id}", distribution).replace(
            "${distro_codename}", codename
        )
        patterns = re.search(
            r"Unattended-Upgrade::Origins-Pattern\s*\{(.*?)\};",
            config,
            re.DOTALL,
        ).group(1)
        metadata = {"origin": origin, "codename": release}
        return any(
            all(metadata[key] == value for key, value in (
                field.split("=", 1) for field in pattern.split(",")
            ))
            for pattern in re.findall(r'"([^"]+)";', patterns)
        )

    def test_debian_release_and_security_updates_survive_suite_changes(self):
        config = self.render("Debian")
        # Bookworm's suites change from stable to oldstable; codenames do not.
        for release in ("bookworm", "bookworm-security", "bookworm-updates"):
            with self.subTest(release=release):
                self.assertTrue(
                    self.allowed(config, "Debian", "bookworm", "Debian", release)
                )
        for origin, release in (
            ("Debian", "trixie"),
            ("Debian", "bookworm-backports"),
            ("Debian", "bookworm-proposed-updates"),
            ("ThirdParty", "bookworm"),
        ):
            with self.subTest(origin=origin, release=release):
                self.assertFalse(
                    self.allowed(config, "Debian", "bookworm", origin, release)
                )

    def test_ubuntu_updates_and_esm_remain_allowed(self):
        config = self.render("Ubuntu")
        for origin, release in (
            ("Ubuntu", "noble"),
            ("Ubuntu", "noble-security"),
            ("Ubuntu", "noble-updates"),
            ("UbuntuESMApps", "noble-apps-security"),
            ("UbuntuESM", "noble-infra-security"),
        ):
            with self.subTest(origin=origin, release=release):
                self.assertTrue(
                    self.allowed(config, "Ubuntu", "noble", origin, release)
                )
        self.assertFalse(
            self.allowed(config, "Ubuntu", "noble", "Ubuntu", "noble-backports")
        )

    def test_kernel_flavours_and_non_kernel_updates_are_not_filtered(self):
        for distribution in ("Debian", "Ubuntu"):
            config = self.render(distribution)
            for option in ("Package-Whitelist", "Package-Blacklist"):
                block = re.search(
                    rf"Unattended-Upgrade::{option}\s*\{{(.*?)\}};",
                    config,
                    re.DOTALL,
                )
                patterns = re.findall(r'"([^"]+)";', block.group(1)) if block else []
                for package in (
                    "linux-image-amd64",
                    "linux-image-arm64",
                    "linux-image-6.1.0-42-amd64",
                    "linux-generic-hwe-24.04",
                    "linux-image-6.8.0-90-generic",
                    "openssl",
                ):
                    with self.subTest(distribution=distribution, package=package):
                        matches = any(re.match(pattern, package) for pattern in patterns)
                        if option == "Package-Whitelist":
                            self.assertTrue(not patterns or matches)
                        else:
                            self.assertFalse(matches)

    def test_only_non_k3s_hosts_reboot_automatically(self):
        for groups, expected in (((), "true"), (("tags_k3s",), "false")):
            with self.subTest(groups=groups):
                self.assertIn(
                    f'Unattended-Upgrade::Automatic-Reboot "{expected}";',
                    self.render("Debian", groups),
                )

    @unittest.skipUnless(shutil.which("apt-config"), "apt-config requires Linux/APT")
    def test_apt_accepts_rendered_configuration(self):
        for distribution in ("Debian", "Ubuntu"):
            with self.subTest(distribution=distribution):
                with tempfile.NamedTemporaryFile(mode="w", suffix=".conf") as config:
                    config.write(self.render(distribution))
                    config.flush()
                    result = subprocess.run(
                        ["apt-config", "-c", config.name, "dump"],
                        text=True,
                        capture_output=True,
                        timeout=10,
                    )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Unattended-Upgrade::Origins-Pattern", result.stdout)


if __name__ == "__main__":
    unittest.main()
