"""Exercise mirror failover, per-source recovery and the trust boundary."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "scripts"))
import common
import setup
import transfer

URL = "https://github.com/docker/compose/releases/download/v5.6.1/docker-compose-linux-x86_64"


class MirrorTests(unittest.TestCase):
    def test_cn_source_order_custom_priority_deduplication_and_official_fallback(self):
        with patch.dict(os.environ, REMOTECODE_CN="1"):
            sources = common.download_sources(URL)
            self.assertEqual([u.split("/")[2] for u in sources],
                             ["ghfast.top", "ghproxy.net", "gh-proxy.com", "github.com"])
            custom = "https://custom.example/{url}"
            self.assertEqual(common.download_sources(URL, custom), [custom.replace("{url}", URL), *sources])
            self.assertEqual(common.download_sources(URL, "https://ghfast.top/{url}"), sources)
        with patch.dict(os.environ, REMOTECODE_CN="0"):
            self.assertEqual(common.download_sources(URL), [URL])

    def test_automatic_mirrors_never_receive_api_private_repo_or_credential_urls(self):
        urls = ["https://api.github.com/repos/docker/compose/releases/tags/v5.6.1",
                URL.replace("docker/compose", "private/project"), URL + "?token=SECRET",
                URL + "#SECRET", URL.replace("https://", "https://user:SECRET@"),
                "https://update.code.visualstudio.com/api/update/server-linux-x64-web/stable/latest"]
        with patch.dict(os.environ, REMOTECODE_CN="1"):
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(common.download_sources(url), [url])

    def test_stream_failure_and_hash_mismatch_switch_sources_without_installing_bad_bytes(self):
        data = b"abcdef"
        digest = hashlib.sha256(data).hexdigest()
        for corrupt in (False, True):
            calls = []

            @contextmanager
            def source(url, headers=None):
                calls.append(url)
                if url == "https://first.example/asset":
                    if not corrupt:
                        raise urllib.error.HTTPError(url, 404, "missing", {}, None)
                    payload = b"xxxxxx"
                else:
                    self.assertEqual(url, "https://second.example/asset")
                    payload = data
                yield common.HTTPResult(io.BytesIO(payload), 200, {"Content-Length": "6"})

            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "asset"
                with patch.object(common, "download_sources", return_value=["https://first.example/asset", "https://second.example/asset"]), patch.object(common, "request", source), patch.object(common.time, "sleep"):
                    common.download(URL, target, digest)
                self.assertEqual(target.read_bytes(), data)
                self.assertEqual(calls, ["https://first.example/asset", "https://second.example/asset"])

    def test_failed_mirror_cycle_preserves_each_prefix_for_the_next_run(self):
        data = b"abcdef"
        digest = hashlib.sha256(data).hexdigest()
        sources = ["https://first.example/asset", "https://second.example/asset"]
        counts = {}

        @contextmanager
        def interrupted(url, headers=None):
            counts[url] = counts.get(url, 0) + 1
            if counts[url] > 1:
                raise OSError("offline")
            payload = b"abc" if url == sources[0] else b"ab"
            yield common.HTTPResult(io.BytesIO(payload), 200, {"Content-Length": "6"})

        @contextmanager
        def resumed(url, headers=None):
            self.assertEqual(url, sources[0])
            self.assertEqual(headers["Range"], "bytes=3-")
            yield common.HTTPResult(io.BytesIO(b"def"), 206, {"Content-Length": "3", "Content-Range": "bytes 3-5/6"})

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "asset"
            with patch.object(common, "download_sources", return_value=sources), patch.object(common.time, "sleep"):
                with patch.object(common, "request", interrupted), self.assertRaises(RuntimeError):
                    common.download(URL, target, digest)
                with patch.object(common, "request", resumed):
                    common.download(URL, target, digest)
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(list(Path(folder).glob("*.part*")), [])

    def test_segmented_404_and_ignored_range_switch_to_the_next_source(self):
        sources = ["https://first.example/asset", "https://second.example/asset", "https://third.example/asset"]
        data = b"abcdef"
        calls = []

        @contextmanager
        def source(url, headers=None):
            calls.append(url)
            if url == sources[0]:
                raise urllib.error.HTTPError(url, 404, "missing", {}, None)
            if url == sources[1]:
                yield common.HTTPResult(io.BytesIO(data), 200, {"Content-Length": "6"})
            else:
                start, end = map(int, headers["Range"][6:].split("-"))
                yield common.HTTPResult(io.BytesIO(data[start:end + 1]), 206,
                                        {"Content-Length": str(end - start + 1), "Content-Range": f"bytes {start}-{end}/6"})

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "asset"
            with patch.object(transfer, "download_sources", return_value=sources), patch.object(transfer, "request", source):
                transfer.segmented_download(URL, target, hashlib.sha256(data).hexdigest(), workers=1)
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(calls, [sources[0], sources[1], sources[2], sources[2]])

    def test_bad_segmented_hash_is_cleared_before_trying_another_mirror(self):
        data = b"abcdef"
        sources = ["https://bad.example/asset", "https://good.example/asset"]

        @contextmanager
        def source(url, headers=None):
            start, end = map(int, headers["Range"][6:].split("-"))
            payload = b"xxxxxx" if url == sources[0] else data
            yield common.HTTPResult(io.BytesIO(payload[start:end + 1]), 206,
                                    {"Content-Length": str(end - start + 1), "Content-Range": f"bytes {start}-{end}/6"})

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "asset"
            with patch.object(transfer, "download_sources", return_value=sources), patch.object(transfer, "request", source):
                transfer.segmented_download(URL, target, hashlib.sha256(data).hexdigest(), workers=1)
            self.assertEqual(target.read_bytes(), data)

    def test_segment_403_switches_source_without_repeating_the_same_failed_request(self):
        data = b"abcdef"
        sources = ["https://first.example/asset", "https://second.example/asset"]
        calls = []

        @contextmanager
        def source(url, headers=None):
            calls.append((url, headers["Range"]))
            start, end = map(int, headers["Range"][6:].split("-"))
            if url == sources[0] and end > 0:
                raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)
            yield common.HTTPResult(io.BytesIO(data[start:end + 1]), 206,
                                    {"Content-Length": str(end - start + 1), "Content-Range": f"bytes {start}-{end}/6"})

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "asset"
            with patch.object(transfer, "download_sources", return_value=sources), patch.object(transfer, "request", source), patch.object(transfer.time, "sleep") as sleep:
                transfer.segmented_download(URL, target, hashlib.sha256(data).hexdigest(), workers=1)
                sleep.assert_not_called()
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(sum(url == sources[0] for url, _ in calls), 2)

    def test_failure_log_redacts_url_credentials_and_exception_details(self):
        output = io.StringIO()

        @contextmanager
        def source(url, headers=None):
            raise OSError("SECRET: " + url)
            yield

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, REMOTECODE_CN="0"), redirect_stdout(output), redirect_stderr(output):
            target = Path(folder) / "asset"
            with patch.object(common, "request", source), patch.object(common.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    common.download(URL, target, "0" * 64, "https://custom.example/{url}?token=SECRET")
            with patch.object(transfer, "request", source):
                with self.assertRaises(RuntimeError) as exc:
                    transfer.segmented_download(URL, target, "0" * 64, "https://custom.example/{url}?token=SECRET")
        self.assertNotIn("SECRET", output.getvalue())
        self.assertNotIn("SECRET", str(exc.exception))
        self.assertIn("custom.example", output.getvalue())

    @unittest.skipIf(os.name == "nt", "POSIX bootstrap")
    def test_cn_is_exported_before_shell_bootstrap_downloads(self):
        command = r'''
set -euo pipefail
source "$1/scripts/bootstrap-posix.sh"
RC_PACKAGE=$1
rc_python() { RC_PYTHON=python3; return 0; }
rc_bootstrap local --help --cn
[ "$REMOTECODE_CN" = 1 ]
'''
        result = subprocess.run(["bash", "-c", command, "test", str(PACKAGE)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_setup_cn_reaches_python_downloads_and_children(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            argv = ["setup.py", "cloud", "status", "--cn", "--state", str(root / "state"), "--cache", str(root / "cache")]
            with patch.dict(os.environ, REMOTECODE_CN="0"), patch.object(sys, "argv", argv), patch.object(setup, "cloud_setup") as deploy:
                setup.main()
                deploy.assert_called_once()
                self.assertEqual(os.environ["REMOTECODE_CN"], "1")


if __name__ == "__main__":
    unittest.main()
