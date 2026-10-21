"""Regressions for an existing Docker CLI without a Compose plugin."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "scripts"))
spec = importlib.util.spec_from_file_location("bootstrap_compose", PACKAGE / "scripts/bootstrap-compose.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@unittest.skipIf(os.name == "nt", "POSIX Compose plugin bootstrap")
class ComposeBootstrapTests(unittest.TestCase):
    def fixture(self, root):
        cache = root / "cache"
        cache.mkdir()
        binary = cache / "docker-compose-fixture"
        binary.write_text('#!/bin/sh\nprintf \'{"SchemaVersion":"0.1.0","Version":"v5.6.1"}\\n\'\n')
        item = {"name": binary.name, "url": "https://official.example/compose",
                "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
        return cache, binary, item

    def test_staged_verified_install_and_repeated_run_preserve_existing_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache, _, item = self.fixture(root)
            target_dir = root / "plugins"
            with patch.object(bootstrap, "segmented_download") as fetch:
                target = bootstrap.install(target_dir, cache, item, "5.6.1", "https://trusted.example/{url}")
                self.assertEqual(target.stat().st_mode & 0o777, 0o755)
                inode = target.stat().st_ino
                self.assertEqual(bootstrap.install(target_dir, cache, item, "5.6.1").stat().st_ino, inode)
                fetch.assert_called_once_with(item["url"], cache / item["name"], item["sha256"], "https://trusted.example/{url}")

    def test_tampered_download_and_failed_probe_never_publish_a_plugin(self):
        for bad_hash in (True, False):
            with self.subTest(bad_hash=bad_hash), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                cache, binary, item = self.fixture(root)
                if bad_hash:
                    binary.write_bytes(b"tampered")
                with patch.object(bootstrap, "segmented_download"), patch.object(bootstrap, "probe", side_effect=ValueError("bad metadata")):
                    with self.assertRaises(ValueError):
                        bootstrap.install(root / "plugins", cache, item, "5.6.1")
                self.assertTrue(binary.exists())
                self.assertFalse((root / "plugins/docker-compose").exists())
                self.assertFalse(list((root / "plugins").glob(".remotecode-compose-*")))

    def test_unknown_plugin_or_dangling_symlink_is_preserved_without_download(self):
        for link in (True, False):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                cache, _, item = self.fixture(root)
                directory = root / "plugins"
                directory.mkdir()
                target = directory / "docker-compose"
                if link:
                    target.symlink_to(root / "missing")
                else:
                    target.write_text("existing unknown plugin")
                with patch.object(bootstrap, "segmented_download") as fetch:
                    with self.assertRaisesRegex(ValueError, "未覆盖"):
                        bootstrap.install(directory, cache, item, "5.6.1")
                    fetch.assert_not_called()
                self.assertTrue(target.is_symlink() if link else target.read_text() == "existing unknown plugin")

    def test_range_failure_uses_verified_stream_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache, binary, item = self.fixture(root)
            with patch.object(bootstrap, "segmented_download", side_effect=RuntimeError("no ranges")), patch.object(bootstrap, "download") as fallback:
                bootstrap.install(root / "plugins", cache, item, "5.6.1", "https://trusted.example/{url}")
                fallback.assert_called_once_with(item["url"], binary, item["sha256"], "https://trusted.example/{url}")

    def test_metadata_version_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            _, binary, _ = self.fixture(Path(folder))
            binary.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "元数据/版本"):
                bootstrap.probe(binary, "0.0.0")

    def test_shell_installs_only_missing_plugin_then_reuses_it_without_apt(self):
        with tempfile.TemporaryDirectory(prefix="compose with spaces ") as folder:
            root = Path(folder)
            python = root / "python"
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$RC_TEST_ARGS"\ntouch "$RC_TEST_READY"\n')
            python.chmod(0o755)
            env = dict(os.environ, RC_TEST_SOURCE=str(PACKAGE / "scripts/bootstrap-posix.sh"),
                       RC_TEST_PACKAGE=str(PACKAGE), RC_TEST_CACHE=str(root / "cache"),
                       RC_TEST_PYTHON=str(python), RC_TEST_ARGS=str(root / "args"),
                       RC_TEST_READY=str(root / "ready"))
            command = r'''
set -euo pipefail
source "$RC_TEST_SOURCE"
RC_PACKAGE=$RC_TEST_PACKAGE
RC_PYTHON=$RC_TEST_PYTHON
RC_DOWNLOAD_CACHE=$RC_TEST_CACHE
RC_DOWNLOAD_MIRROR='https://trusted.example/{url}'
RC_CN=1
rc_as_root() { "$@"; }
apt-get() { exit 77; }
dpkg-query() { exit 77; }
systemctl() { :; }
docker() {
    case "$1" in
        compose) [ -f "$RC_TEST_READY" ] ;;
        info) return 0 ;;
        *) exit 77 ;;
    esac
}
rc_docker
rc_docker
'''
            result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "args").read_text().splitlines(),
                             ["-u", str(PACKAGE / "scripts/bootstrap-compose.py"), "--cache", str(root / "cache/bootstrap"),
                              "--mirror", "https://trusted.example/{url}"])

    def test_fixed_release_has_independent_architecture_hashes(self):
        lock = json.loads((PACKAGE / "examples/compose-plugin.lock.json").read_text())
        self.assertEqual(set(lock["files"]), {"linux-amd64", "linux-arm64"})
        for item in lock["files"].values():
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(item["url"].startswith("https://github.com/docker/compose/releases/download/v" + lock["version"] + "/"))


if __name__ == "__main__":
    unittest.main()
