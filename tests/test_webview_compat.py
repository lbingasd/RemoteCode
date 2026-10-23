"""Compatibility patch identity, recovery and refusal of unrelated vendor edits."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import webview_compat as compat


class WebviewCompatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = Path(self.temp.name)
        self.bundle = self.server / compat.BUNDLE
        self.template = self.server / compat.TEMPLATE
        self.bundle.parent.mkdir(parents=True)
        self.original = (compat.START + ';this.root=document.createElement("div")}}' + compat.END + '{};').encode()
        self.html = b'<script type="module" src="{{BASE}}/out/vs/code/browser/workbench/workbench.js"></script>'
        self.bundle.write_bytes(self.original)
        self.template.write_bytes(self.html)
        guard = patch.object(compat, "ORIGINAL_SHA256", hashlib.sha256(self.original).hexdigest())
        guard.start()
        self.addCleanup(guard.stop)

    def test_apply_is_repeatable_keeps_original_and_restores(self):
        report = compat.apply_server(self.server)
        patched = self.bundle.read_bytes()
        self.assertIn(compat.MARKER.encode(), patched)
        self.assertIn(compat.QUERY.encode(), self.template.read_bytes())
        self.assertEqual(compat.apply_server(self.server), report)
        self.assertEqual(self.bundle.read_bytes(), patched)
        self.assertEqual(self.bundle.with_suffix('.js.remotecode-original').read_bytes(), self.original)
        compat.restore_server(self.server)
        self.assertEqual(self.bundle.read_bytes(), self.original)
        self.assertEqual(self.template.read_bytes(), self.html)
        compat.apply_server(self.server)
        self.assertEqual(self.bundle.read_bytes(), patched)

    def test_unknown_version_is_rejected_before_any_write(self):
        self.bundle.write_bytes(self.original + b'changed')
        with self.assertRaises(ValueError):
            compat.apply_server(self.server)
        self.assertFalse(self.bundle.with_suffix('.js.remotecode-original').exists())
        self.assertEqual(self.template.read_bytes(), self.html)

    def test_windows_build_is_supported_without_replacing_original_build(self):
        with patch.object(compat, 'ORIGINAL_SHA256', '0' * 64), \
             patch.object(compat, 'WINDOWS_SHA256', hashlib.sha256(self.original).hexdigest()):
            self.assertIn(compat.MARKER.encode(), compat.patched_bundle(self.original))
            with self.assertRaises(ValueError):
                compat.patched_bundle(self.original + b'changed')

    def test_later_edits_are_preserved_on_apply_and_restore(self):
        compat.apply_server(self.server)
        later = self.bundle.read_bytes() + b'other patch'
        self.bundle.write_bytes(later)
        for action in (compat.apply_server, compat.restore_server):
            with self.assertRaises(ValueError):
                action(self.server)
        self.assertEqual(self.bundle.read_bytes(), later)

    def test_interrupted_apply_resumes_without_repatching_bundle(self):
        compat.apply_server(self.server)
        patched = self.bundle.read_bytes()
        self.template.write_bytes(self.html)
        compat.apply_server(self.server)
        self.assertEqual(self.bundle.read_bytes(), patched)
        self.assertIn(compat.QUERY.encode(), self.template.read_bytes())

    def test_apply_and_restore_refuse_later_template_changes(self):
        compat.apply_server(self.server)
        self.template.write_bytes(self.template.read_bytes() + b'other edit')
        patched = self.bundle.read_bytes()
        for action in (compat.apply_server, compat.restore_server):
            with self.assertRaises(ValueError):
                action(self.server)
        self.assertEqual(self.bundle.read_bytes(), patched)

    def test_bad_entrypoint_is_rejected_before_bundle_changes(self):
        self.template.write_bytes(b'<html>unknown entrypoint</html>')
        with self.assertRaises(ValueError):
            compat.apply_server(self.server)
        self.assertEqual(self.bundle.read_bytes(), self.original)
        self.assertFalse(self.bundle.with_suffix('.js.remotecode-original').exists())


if __name__ == '__main__':
    unittest.main()
