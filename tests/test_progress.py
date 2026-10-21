"""Progress must appear before blocking work, flush and stop without leaking secrets."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import transfer


class ProgressTests(unittest.TestCase):
    def test_waiting_activity_emits_heartbeat_and_stops_its_thread(self):
        messages = []
        tick = threading.Event()

        def record(message):
            messages.append(message)
            if "仍在处理中" in message:
                tick.set()

        with patch.object(common, "progress", side_effect=record):
            with common.activity("等待网络", interval=.01):
                self.assertTrue(tick.wait(2), "no heartbeat while blocked")
        self.assertIn("：开始", messages[0])
        self.assertIn("：完成", messages[-1])
        self.assertFalse(any(t.name == "RemoteCodeProgress" for t in threading.enumerate()))

    def test_failed_activity_reports_failure_without_exception_contents(self):
        output = io.StringIO()
        with redirect_stderr(output):
            with self.assertRaises(RuntimeError):
                with common.activity("下载资源", interval=.01):
                    raise RuntimeError("SECRET_EXCEPTION_DETAIL")
        self.assertIn("：未完成", output.getvalue())
        self.assertNotIn("：完成", output.getvalue())
        self.assertNotIn("SECRET_EXCEPTION_DETAIL", output.getvalue())

    def test_cache_progress_does_not_print_url_or_download_unnecessarily(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "asset.zip"
            target.write_bytes(b"verified")
            digest = hashlib.sha256(b"verified").hexdigest()
            output = io.StringIO()
            with redirect_stderr(output), patch.object(common, "request") as fetch:
                common.download("https://official.example/file?token=SECRET_TOKEN", target, digest)
                transfer.segmented_download("https://official.example/file?token=SECRET_TOKEN", target, digest)
                fetch.assert_not_called()
            self.assertIn("缓存校验通过", output.getvalue())
            self.assertNotIn("SECRET_TOKEN", output.getvalue())
            self.assertNotIn("https://", output.getvalue())

    def test_download_announces_work_before_the_first_network_probe(self):
        messages = []

        @contextmanager
        def unavailable(_url, _headers=None):
            self.assertTrue(any("：开始" in m for m in messages))
            raise OSError("offline")
            yield  # make a context manager without contacting any network

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(common, "progress", side_effect=messages.append), patch.object(transfer, "request", unavailable), redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    transfer.segmented_download("https://official.example/file", Path(folder) / "asset", "0" * 64)
        self.assertIn("：未完成", messages[-1])


if __name__ == "__main__":
    unittest.main()
