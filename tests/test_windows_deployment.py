"""Native Windows regressions; no real services, tasks or credentials are changed."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import local
import qq_bot


@unittest.skipUnless(os.name == 'nt', 'requires native Windows PowerShell and NTFS')
class WindowsDeploymentTests(unittest.TestCase):
    def test_default_cache_initialization_in_windows_powershell(self):
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy',
                                 'Bypass', '-File', str(PACKAGE / 'deploy-local.ps1'), '-Help'],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'-Bundle', result.stdout)

    def test_acl_preserves_existing_file_execution_and_repeat_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'runtime'
            nested = root / 'server' / 'out'
            nested.mkdir(parents=True)
            script = nested / 'probe.ps1'
            script.write_text('Write-Output "ACL_OK"', encoding='utf-8')
            for _ in range(2):
                local.restrict_windows(root)
                self.assertIn('ACL_OK', script.read_text(encoding='utf-8'))
                result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy',
                                         'Bypass', '-File', str(script)], capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b'ACL_OK', result.stdout)
                later = nested / 'new.txt'
                later.write_text('created after ACL repair', encoding='utf-8')
                self.assertEqual(later.read_text(encoding='utf-8'), 'created after ACL repair')

    def test_qq_runner_keeps_native_stderr_and_exit_status(self):
        for code in (0, 7):
            with self.subTest(exit_code=code), tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / 'runtime with spaces'
                root.mkdir()
                probe = root / 'probe.py'
                probe.write_text('import sys\nprint("QQ_STARTUP", file=sys.stderr)\n'
                                 'print("QQ_RUNNING")\nsys.exit(' + str(code) + ')\n', encoding='utf-8')
                qq_bot.write(root / 'local.json', {'id': 'windows-regression'})
                qq_bot.write(root / 'qq/deployment.json', {'command': [sys.executable, str(probe)]})
                # Generate the production runner, but never register a scheduled task.
                with patch.object(qq_bot.subprocess, 'run'):
                    qq_bot.service(root, 'install')
                result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy',
                                         'Bypass', '-File', str(root / 'qq/run-qq.ps1')],
                                        capture_output=True, timeout=30)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn('QQ_STARTUP', (root / 'logs/qq.err.log').read_text(encoding='utf-16'))
                self.assertIn('QQ_RUNNING', (root / 'logs/qq.out.log').read_text(encoding='utf-16'))


if __name__ == '__main__':
    unittest.main()
