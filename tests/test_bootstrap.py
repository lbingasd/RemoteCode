"""Exercise shell entrypoints while forbidding actual package-manager invocation."""
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PACKAGE = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "POSIX shell entrypoints")
class BootstrapTests(unittest.TestCase):
    def test_status_uninstall_offline_and_help_do_not_install_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            tools = base / "tools"
            tools.mkdir()
            marker = base / "forbidden-manager"
            for name in ("brew", "apt-get", "dnf", "yum", "zypper", "rpm", "sudo", "docker", "curl"):
                script = tools / name
                script.write_text('#!/bin/sh\nprintf forbidden > "$RC_FORBIDDEN"\nexit 77\n')
                script.chmod(0o755)
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ.get("PATH", ""),
                       REMOTECODE_PYTHON=sys.executable, RC_FORBIDDEN=str(marker))
            for action in ("status", "uninstall"):
                result = subprocess.run(["bash", str(PACKAGE / "deploy-local.sh"), action, "--root", str(base / "absent"), "--cache", str(base / "cache")], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            for entry in ("deploy-local.sh", "deploy-cloud.sh"):
                result = subprocess.run(["bash", str(PACKAGE / entry), "--help"], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(["bash", str(PACKAGE / "deploy-local.sh"), "install", "--offline", "--accept-license", "--root", str(base / "absent"), "--bundle", str(base / "no-bundle"), "--workspace", str(base / "workspace")], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists(), "non-install/offline operation invoked a package manager")
            if sys.platform == "darwin" and shutil.which("node") and shutil.which("git"):
                version = subprocess.check_output(["node", "--version"], text=True).strip()
                if int(version.lstrip("v").split(".")[0]) >= 22:
                    # Complete existing dependencies must skip Homebrew, even without --skip-deps.
                    command = 'set -euo pipefail; source "$1/scripts/bootstrap-posix.sh"; RC_PACKAGE=$1; rc_bootstrap local install --accept-license'
                    result = subprocess.run(["bash", "-c", command, "test", str(PACKAGE)], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(marker.exists(), "complete dependencies still invoked Homebrew")
                    # Execute the exact Node expression used by Windows, not a mirrored implementation.
                    windows = (PACKAGE / "deploy-local.ps1").read_text()
                    expression = re.search(r"-e '(process.exit[^']+)'", windows)[1]
                    self.assertEqual(subprocess.run(["node", "-e", expression], capture_output=True).returncode, 0)

    def test_license_and_invalid_action_fail_before_package_managers(self):
        for args in (("install",), ("invalid",)):
            result = subprocess.run(["bash", str(PACKAGE / "deploy-local.sh"), *args], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("RemoteCode:", result.stderr)
        result = subprocess.run(["bash", str(PACKAGE / "deploy-cloud.sh"), "install"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --ip", result.stderr)

    def test_os_detection_families(self):
        # rc_detect_os maps os-release ID/ID_LIKE to a family and package manager.
        # rhel's dnf-vs-yum choice depends on the host's PATH, so it is asserted loosely.
        cases = [
            ("ubuntu", "", "debian", "apt-get"),
            ("debian", "", "debian", "apt-get"),
            ("rhel", "", "rhel", None),
            ("centos", "", "rhel", None),
            ("rocky", "", "rhel", None),
            ("almalinux", "", "rhel", None),
            ("amzn", "centos rhel fedora", "rhel", None),
            ("openEuler", "", "rhel", None),
            ("alinux", "rhel fedora centos alinux", "rhel", None),
            ("anolis", "", "rhel", None),
            ("opensuse-leap", "", "suse", "zypper"),
            ("sles", "", "suse", "zypper"),
            ("myos", "rhel fedora", "rhel", None),
            ("myos", "debian", "debian", "apt-get"),
            ("gentoo", "", "other", ""),
        ]
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder) / "os-release"
            for os_id, os_like, family, pkg in cases:
                lines = [f'ID="{os_id}"']
                if os_like:
                    lines.append(f'ID_LIKE="{os_like}"')
                if os_id == "openEuler":
                    lines.append('PRETTY_NAME="openEuler 22.03 LTS"')
                fixture.write_text("\n".join(lines) + "\n")
                command = ('unset ID ID_LIKE PRETTY_NAME VERSION_ID VERSION_CODENAME; '
                           'set -u; source "$1/scripts/bootstrap-posix.sh"; '
                           'REMOTECODE_OS_RELEASE=$2 rc_detect_os; '
                           'printf "%s|%s|%s" "$RC_FAMILY" "$RC_PKG" "$RC_OS_ID"')
                result = subprocess.run(["bash", "-c", command, "test", str(PACKAGE), str(fixture)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                got_family, got_pkg, got_id = result.stdout.split("|")
                self.assertEqual(got_family, family, os_id)
                self.assertEqual(got_id, os_id.lower(), os_id)
                if pkg is not None:
                    self.assertEqual(got_pkg, pkg, os_id)
                elif family == "rhel":
                    self.assertIn(got_pkg, ("dnf", "yum"), os_id)

    def test_unsupported_family_fails_before_package_manager(self):
        # Drive the Linux branch on a non-Linux host via a fake uname, so the
        # unsupported-family gate is exercised without touching a package manager.
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            fixture = base / "os-release"
            fixture.write_text('ID="gentoo"\n')
            marker = base / "forbidden-manager"
            tools = base / "tools"
            tools.mkdir()
            fake_uname = tools / "uname"
            fake_uname.write_text('#!/bin/sh\n[ "$1" != "-s" ] || { echo Linux; exit 0; }\nexec /usr/bin/uname "$@"\n')
            fake_uname.chmod(0o755)
            for name in ("apt-get", "dnf", "yum", "zypper", "rpm"):
                script = tools / name
                script.write_text('#!/bin/sh\nprintf forbidden > "$RC_FORBIDDEN"\nexit 77\n')
                script.chmod(0o755)
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ.get("PATH", ""),
                       REMOTECODE_OS_RELEASE=str(fixture), RC_FORBIDDEN=str(marker))
            result = subprocess.run(["bash", str(PACKAGE / "deploy-local.sh"), "install", "--accept-license"],
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Auto dependencies support", result.stderr)
            self.assertFalse(marker.exists(), "unsupported family still invoked a package manager")


if __name__ == "__main__":
    unittest.main()
