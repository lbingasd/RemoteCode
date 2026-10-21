"""Targeted regressions for bootstrapping before Python 3.12 is available."""
import ast
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "scripts"))
spec = importlib.util.spec_from_file_location("bootstrap_python", PACKAGE / "scripts/bootstrap-python.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def archive(path, entries):
    with tarfile.open(path, "w:gz") as target:
        for name, kind, content in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = 0o6755  # no setuid/setgid permissions may survive extraction
            if kind == tarfile.REGTYPE:
                info.size = len(content)
                target.addfile(info, io.BytesIO(content))
            else:
                info.linkname = content
                target.addfile(info)


@unittest.skipIf(os.name == "nt", "Linux private runtime bootstrap")
class PythonBootstrapTests(unittest.TestCase):
    def test_bootstrap_and_downloader_remain_parseable_on_python38(self):
        for name in ("bootstrap-python.py", "common.py", "transfer.py"):
            path = PACKAGE / "scripts" / name
            ast.parse(path.read_text(), filename=str(path), feature_version=(3, 8))

    def test_safe_files_links_and_parent_relative_terminfo_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = root / "python.tar.gz"
            archive(fixture, [("python/bin/python3.12", tarfile.REGTYPE, b"fixture"),
                              ("python/bin/python3", tarfile.SYMTYPE, "python3.12"),
                              ("python/share/terminfo/a/real", tarfile.REGTYPE, b"data"),
                              ("python/share/terminfo/b/alias", tarfile.SYMTYPE, "../a/real")])
            bootstrap.extract_runtime(fixture, root / "out")
            binary = root / "out/python/bin/python3"
            self.assertEqual(binary.read_bytes(), b"fixture")
            self.assertEqual(binary.resolve().stat().st_mode & 0o7777, 0o755)
            self.assertEqual((root / "out/python/share/terminfo/b/alias").read_bytes(), b"data")

    def test_traversal_special_files_links_and_symlink_parent_are_rejected(self):
        cases = [[("../outside", tarfile.REGTYPE, b"bad")],
                 [("/python/bin/bad", tarfile.REGTYPE, b"bad")],
                 [("python/bin/python3", tarfile.SYMTYPE, "../../../outside")],
                 [("python/bin/device", tarfile.FIFOTYPE, "")],
                 [("python/bin/hard", tarfile.LNKTYPE, "/outside")],
                 [("python/link", tarfile.SYMTYPE, "bin"), ("python/link/bad", tarfile.REGTYPE, b"bad")]]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index, entries in enumerate(cases):
                with self.subTest(index=index):
                    fixture = root / f"bad-{index}.tar.gz"
                    archive(fixture, entries)
                    with self.assertRaises(ValueError):
                        bootstrap.extract_runtime(fixture, root / f"out-{index}")
            self.assertFalse((root / "outside").exists())

    def test_failed_probe_preserves_download_and_does_not_publish_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / "cache"
            cache.mkdir()
            fixture = cache / "runtime.tar.gz"
            archive(fixture, [("python/bin/python3", tarfile.REGTYPE, b"fixture")])
            item = {"name": fixture.name, "url": "https://official.example/runtime.tar.gz", "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest()}
            prefix = root / "managed/python"
            with patch.object(bootstrap, "segmented_download"), patch.object(bootstrap, "probe", side_effect=ValueError("cannot execute")):
                with self.assertRaises(ValueError):
                    bootstrap.install(prefix, cache, item)
            self.assertTrue(fixture.is_file())
            self.assertFalse(prefix.exists())
            self.assertFalse(list(prefix.parent.glob(".python-install-*")))

    def test_completed_bootstrap_is_reused_without_download_or_file_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / "cache"
            cache.mkdir()
            fixture = cache / "runtime.tar.gz"
            archive(fixture, [("python/bin/python3", tarfile.REGTYPE, b"fixture")])
            item = {"name": fixture.name, "url": "https://official.example/runtime.tar.gz", "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest()}
            prefix = root / "managed/python"
            with patch.object(bootstrap, "segmented_download") as fetch, patch.object(bootstrap, "probe"):
                binary = bootstrap.install(prefix, cache, item)
                inode = binary.stat().st_ino
                self.assertEqual(bootstrap.install(prefix, cache, item).stat().st_ino, inode)
                fetch.assert_called_once()

    def test_old_system_python_uses_private_bootstrap_and_forwards_mirror_cache(self):
        with tempfile.TemporaryDirectory(prefix="bootstrap with space ") as folder:
            root = Path(folder)
            env = dict(os.environ, RC_TEST_DONE=str(root / "done"), RC_TEST_ARGS=str(root / "args"), RC_TEST_PACKAGE=str(PACKAGE), RC_TEST_CACHE=str(root / "cache"))
            command = r'''
set -euo pipefail
source "$RC_TEST_PACKAGE/scripts/bootstrap-posix.sh"
RC_PACKAGE=$RC_TEST_PACKAGE
RC_DOWNLOAD_CACHE=$RC_TEST_CACHE
RC_DOWNLOAD_MIRROR='https://trusted.example/{url}'
rc_python() { if [ -f "$RC_TEST_DONE" ]; then RC_PYTHON="$RC_PACKAGE/.bootstrap/python/bin/python3"; return 0; fi; return 1; }
python3() {
    if [ "$1" = -c ]; then return 0; fi
    printf '%s\n' "$@" >> "$RC_TEST_ARGS"
    touch "$RC_TEST_DONE"
}
rc_ensure_linux_python
rc_ensure_linux_python
'''
            result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "args").read_text().splitlines(), [str(PACKAGE / "scripts/bootstrap-python.py"), "--prefix", str(PACKAGE / ".bootstrap/python"), "--cache", str(root / "cache/bootstrap"), "--mirror", "https://trusted.example/{url}"])

    def test_private_python_is_found_before_old_system_python(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder)
            binary = package / ".bootstrap/python/bin/python3"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            env = dict(os.environ, REMOTECODE_PYTHON="", RC_TEST_PACKAGE=str(package), RC_TEST_SOURCE=str(PACKAGE / "scripts/bootstrap-posix.sh"))
            result = subprocess.run(["bash", "-c", 'set -euo pipefail; source "$RC_TEST_SOURCE"; RC_PACKAGE=$RC_TEST_PACKAGE; rc_python; printf "%s" "$RC_PYTHON"'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, str(binary))


if __name__ == "__main__":
    unittest.main()
