import argparse
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agents import ADAPTERS, adapter_agents, find_cli, resolve_clis  # noqa: E402


class AgentsCliDetectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="remotecode-agents-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _stub(self, name):
        path = self.dir / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def test_find_cli_prefers_explicit_path(self):
        stub = self._stub("my-codex")
        args = argparse.Namespace(codex_path=stub, claude_path=None)
        self.assertEqual(find_cli("codex", args), str(stub.resolve()))

    def test_find_cli_uses_path(self):
        self._stub("codex")
        old = os.environ.get("PATH")
        try:
            os.environ["PATH"] = str(self.dir)
            self.assertEqual(find_cli("codex", argparse.Namespace(codex_path=None, claude_path=None)),
                             str((self.dir / "codex").resolve()))
        finally:
            os.environ["PATH"] = old

    def test_find_cli_returns_none_when_absent(self):
        empty = self.dir / "empty"
        empty.mkdir()
        old = os.environ.get("PATH")
        try:
            os.environ["PATH"] = str(empty)
            self.assertIsNone(find_cli("codex", argparse.Namespace(codex_path=None, claude_path=None)))
        finally:
            os.environ["PATH"] = old

    def test_resolve_clis(self):
        stub = self._stub("codex")
        empty = self.dir / "empty"
        empty.mkdir()
        old = os.environ.get("PATH")
        try:
            os.environ["PATH"] = str(empty)
            resolved = resolve_clis(argparse.Namespace(codex_path=stub, claude_path=None))
        finally:
            os.environ["PATH"] = old
        self.assertEqual(resolved["codex"], str(stub.resolve()))
        self.assertIsNone(resolved["claude"])


class AdapterAgentsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="remotecode-agents-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _adapter(self, key):
        pkgdir = self.root / "agents/node_modules" / ADAPTERS[key]
        pkgdir.mkdir(parents=True)
        (pkgdir / "index.js").write_text("#!/usr/bin/env node\n")
        (pkgdir / "package.json").write_text('{"name": "%s", "version": "1.0.0", "bin": {"%s": "index.js"}}' % (ADAPTERS[key], key))
        return pkgdir

    def test_env_wiring_points_adapters_at_resolved_clis(self):
        for key in ADAPTERS:
            self._adapter(key)
        resolved = {"codex": "/opt/codex", "claude": None}
        agents = adapter_agents(self.root, "/usr/bin/node", {"PATH": "/a:/b"}, resolved)
        codex = agents["Codex (local ACP)"]
        self.assertEqual(codex["command"], "/usr/bin/node")
        self.assertEqual(codex["env"]["CODEX_PATH"], "/opt/codex")
        claude = agents["Claude (local ACP)"]
        self.assertNotIn("CLAUDE_CODE_EXECUTABLE", claude["env"])
        self.assertEqual(claude["env"]["PATH"], "/a:/b")


if __name__ == "__main__":
    unittest.main()
