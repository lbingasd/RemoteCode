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
            for name in ("brew", "apt-get", "sudo", "docker", "curl"):
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


if __name__ == "__main__":
    unittest.main()
