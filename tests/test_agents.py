import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agents import ADAPTERS, adapter_agents, find_cli, resolve_clis  # noqa: E402
import agents


class AgentsCliDetectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="remotecode-agents-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        runner = patch('agents.subprocess.run')
        self.run = runner.start()
        self.addCleanup(runner.stop)

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
        with patch('agents.shutil.which', return_value=str(self.dir / 'codex')):
            self.assertEqual(find_cli("codex", argparse.Namespace(codex_path=None, claude_path=None)),
                             str((self.dir / "codex").resolve()))

    def test_find_cli_returns_none_when_absent(self):
        empty = self.dir / "empty"
        empty.mkdir()
        with patch('agents.shutil.which', return_value=None):
            self.assertIsNone(find_cli("codex", argparse.Namespace(codex_path=None, claude_path=None)))

    def test_resolve_clis(self):
        stub = self._stub("codex")
        claude = self._stub('claude')
        resolved = resolve_clis(argparse.Namespace(codex_path=stub, claude_path=claude))
        self.assertEqual(resolved["codex"], str(stub.resolve()))
        self.assertEqual(resolved["claude"], str(claude.resolve()))

    def test_resolve_reports_all_missing_clis(self):
        with patch('agents.shutil.which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'codex, claude'):
                resolve_clis(argparse.Namespace(codex_path=None, claude_path=None))

    def test_broken_cli_is_rejected_without_using_another_binary(self):
        with patch('agents.shutil.which', return_value=sys.executable) as lookup:
            self.assertIsNone(find_cli('codex', argparse.Namespace(codex_path=self.dir / 'missing')))
            lookup.assert_not_called()
        self.run.side_effect = OSError('cannot execute')
        with self.assertRaisesRegex(ValueError, 'codex, claude'):
            resolve_clis(argparse.Namespace(codex_path=Path(sys.executable), claude_path=Path(sys.executable)))


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
        resolved = {"codex": "/opt/codex", "claude": "/opt/claude"}
        agents = adapter_agents(self.root, "/usr/bin/node", {"PATH": "/a:/b"}, resolved)
        codex = agents["Codex (local ACP)"]
        self.assertEqual(codex["command"], "/usr/bin/node")
        self.assertEqual(codex["env"]["CODEX_PATH"], "/opt/codex")
        claude = agents["Claude (local ACP)"]
        self.assertEqual(claude["env"]["CLAUDE_CODE_EXECUTABLE"], '/opt/claude')
        self.assertEqual(claude["env"]["PATH"], "/a:/b")

    def test_install_missing_cli_fails_before_npm_or_settings_changes(self):
        (self.root / 'local.json').write_text('{}')
        with patch.object(sys, 'argv', ['agents.py', '--root', str(self.root), 'install']), \
             patch.object(agents, 'find_node', return_value=sys.executable), \
             patch.object(agents.subprocess, 'check_output', return_value='v22.0.0'), \
             patch.object(agents, 'find_cli', return_value=None), \
             patch.object(agents, 'npm_command') as npm:
            with self.assertRaisesRegex(ValueError, 'codex, claude'):
                agents.main()
            npm.assert_not_called()
        self.assertFalse((self.root / 'agents').exists())
        self.assertFalse((self.root / 'acp-settings.json').exists())

    def test_old_direct_cli_dependencies_are_removed_from_adapter_manifest(self):
        directory = self.root / 'agents'
        directory.mkdir()
        (directory / 'package.json').write_text('{"dependencies":{"@openai/codex":"1.0.0"}}')
        (directory / 'package-lock.json').write_text('{}')
        args = argparse.Namespace(offline=False, registry='https://registry.npmjs.org',
                                  codex_acp_version='1.0.0', claude_agent_acp_version='1.0.0', npm_cache=None)
        with patch.object(agents, 'run'):
            agents.ensure_lock(args, ['npm'], {}, directory, ADAPTERS)
        self.assertEqual(set(agents.read(directory / 'package.json')['dependencies']), set(ADAPTERS.values()))


if __name__ == "__main__":
    unittest.main()
