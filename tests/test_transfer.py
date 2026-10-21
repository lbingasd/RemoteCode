from contextlib import contextmanager
import hashlib
import gzip
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import cloud
import local
import transfer


class DownloadTests(unittest.TestCase):
    def test_http_gzip_decodes_representation_but_never_range(self):
        payload = b"PK-test-vsix-representation"
        encoded = gzip.compress(payload)
        response = common.HTTPResult(io.BytesIO(encoded), 200, {"Content-Encoding": "gzip", "Content-Length": str(len(encoded))})
        self.assertEqual(response.read(), payload)
        self.assertIsNone(response.headers.get("Content-Length"))
        with self.assertRaises(ValueError):
            common.HTTPResult(io.BytesIO(encoded), 206, {"Content-Encoding": "gzip"}, ranged=True)

    def test_normal_download_recovers_prefix_across_runs(self):
        data = b"abcdef"
        digest = hashlib.sha256(data).hexdigest()
        calls = []
        @contextmanager
        def interrupted(_url, headers=None):
            calls.append(headers)
            if len(calls) == 1:
                yield common.HTTPResult(io.BytesIO(b"abc"), 200, {"Content-Length": "6"})
            else:
                raise OSError("connection lost")
        @contextmanager
        def resumed(_url, headers=None):
            self.assertEqual(headers["Range"], "bytes=3-")
            yield common.HTTPResult(io.BytesIO(b"def"), 206, {"Content-Length": "3", "Content-Range": "bytes 3-5/6"})
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "asset"
            with patch.object(common, "request", interrupted), patch.object(common.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    common.download("https://official.example/file", target, digest)
            self.assertEqual(target.with_name("asset.part").read_bytes(), b"abc")
            with patch.object(common, "request", resumed):
                common.download("https://official.example/file", target, digest)
            self.assertEqual(target.read_bytes(), data)
            self.assertFalse(target.with_name("asset.part.json").exists())

    def test_ignored_range_restarts_instead_of_appending(self):
        data = b"abcdef"
        digest = hashlib.sha256(data).hexdigest()
        @contextmanager
        def source(_url, headers=None):
            self.assertEqual(headers["Range"], "bytes=3-")
            yield common.HTTPResult(io.BytesIO(data), 200, {"Content-Length": "6"})
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "asset"
            target.with_name("asset.part").write_bytes(b"abc")
            common.write(target.with_name("asset.part.json"), {"url": "https://official.example/file", "sha256": digest})
            with patch.object(common, "request", source):
                common.download("https://official.example/file", target, digest)
            self.assertEqual(target.read_bytes(), data)

    def test_segment_cache_survives_failure_and_detects_corruption(self):
        data = bytes(range(256)) * 600
        digest = hashlib.sha256(data).hexdigest()
        chunk_size = 65536
        calls = []
        broken = [True]
        @contextmanager
        def source(_url, headers=None):
            start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            calls.append(start)
            if broken[0] and start == chunk_size:
                raise OSError("disconnected segment")
            payload = data[start:end + 1]
            yield common.HTTPResult(io.BytesIO(payload), 206, {"Content-Length": str(len(payload)),
                "Content-Range": f"bytes {start}-{end}/{len(data)}", "ETag": '"test"'})
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "asset"
            with patch.object(transfer, "request", source), patch.object(transfer.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    transfer.segmented_download("https://official.example/file", target, digest, workers=1, chunk_size=chunk_size)
                parts = target.with_name("asset.chunks")
                self.assertTrue((parts / "000000.bin").is_file())
                # Same-size corruption of a saved segment must not be reused.
                (parts / "000000.bin").write_bytes(b"x" * chunk_size)
                broken[0] = False
                calls.clear()
                transfer.segmented_download("https://official.example/file", target, digest, workers=1, chunk_size=chunk_size)
                self.assertGreaterEqual(calls.count(0), 2)  # probe plus refetch
            self.assertEqual(target.read_bytes(), data)
            self.assertFalse(parts.exists())

    def test_segment_rejects_ignored_or_wrong_ranges(self):
        for status, value in ((200, "bytes 0-0/2"), (206, "bytes 1-1/2"), (206, "bytes 0-0/0")):
            response = common.HTTPResult(io.BytesIO(b"a"), status, {"Content-Range": value, "Content-Length": "1"})
            with self.assertRaises(ValueError):
                transfer.content_range(response, 0, 0)

    def test_segment_keeps_valid_prefix_when_a_subrange_is_cut(self):
        data = b"a" * 65536
        digest = hashlib.sha256(data).hexdigest()
        starts = []
        @contextmanager
        def source(_url, headers=None):
            start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            starts.append(start)
            payload = data[start:end + 1]
            if start == 0 and end > 0:
                payload = payload[:10000]
            yield common.HTTPResult(io.BytesIO(payload), 206, {"Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{len(data)}", "ETag": '"test"'})
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "asset"
            with patch.object(transfer, "request", source), patch.object(transfer.time, "sleep"):
                transfer.segmented_download("https://official.example/file", target, digest, workers=1, chunk_size=65536)
            self.assertIn(10000, starts)
            self.assertEqual(target.read_bytes(), data)


class InstallTests(unittest.TestCase):
    def test_failed_cloud_initialization_never_publishes_partial_state(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            archive = parent / "frp.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("frp/frps", "fixture")
            lock = parent / "lock.json"
            common.write(lock, {"platform": "linux-amd64", "frp_version": "0.71.1",
                               "files": {"frp": {"name": archive.name, "sha256": common.sha256(archive)}}})
            root = parent / "cloud"
            argv = ["cloud.py", "--state", str(root), "init", "--ip", "203.0.113.10",
                    "--lock", str(lock), "--cache", str(parent)]
            with patch.object(sys, "argv", argv), patch.object(cloud.subprocess, "check_output", return_value="linux/x86_64"), patch.object(cloud, "make_pki", side_effect=RuntimeError("issuer unavailable")):
                with self.assertRaises(RuntimeError):
                    cloud.main()
            self.assertFalse(root.exists())
            self.assertEqual(list(parent.glob(".remotecode-cloud-*")), [])

    def test_install_never_takes_over_nonempty_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "existing"
            root.mkdir()
            project = root / "project.txt"
            project.write_text("keep")
            with self.assertRaises(ValueError):
                local.install_runtime(root, Path("unused"), {}, Path("unused"), Path("unused"), {})
            self.assertEqual(project.read_text(), "keep")

    def test_bad_archive_rolls_back_entire_unpublished_install(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            bundle = parent / "bundle"
            bundle.mkdir()
            (bundle / "ca.pem").write_text("test CA")
            for name, entry in (("frp.zip", "frp/frpc"), ("web.zip", "wrong-layout/file")):
                with zipfile.ZipFile(parent / name, "w") as archive:
                    archive.writestr(entry, "test")
            lock = {"files": {"frp": {"name": "frp.zip"}, "vscode_web": {"name": "web.zip"}}}
            root = parent / "runtime"
            with patch.object(local.ssl, "create_default_context"):
                with self.assertRaises(ValueError):
                    local.install_runtime(root, bundle, lock, parent, parent / "project", {})
            self.assertFalse(root.exists())
            self.assertEqual(list(parent.glob(".remotecode-install-*")), [])

    def test_windows_task_quotes_trailing_backslashes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {"id": "pc1", "server_dir": str(root / "server"), "local_port": 18080,
                      "workspace": "D:\\My Projects\\"}
            local.write_windows_task(root, config)
            task = common.read(root / "windows-task.json")
            self.assertTrue(task["executable"].endswith("node.exe"))
            self.assertIn('"D:\\My Projects\\\\"', task["arguments"])
            self.assertNotIn("--without-connection-token", task["arguments"])


if __name__ == "__main__":
    unittest.main()
