"""Native supervisor recovery tests; never register tasks or touch a real IDE."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import local


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(.05)
    raise AssertionError('Condition did not become true')


def alive(pid):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    try:
        return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        api.CloseHandle(handle)


@unittest.skipUnless(os.name == 'nt', 'requires native Windows')
class WindowsIdeTests(unittest.TestCase):
    def test_supervisor_restarts_zero_and_failure_exits_and_cleans_process_tree(self):
        self.check_supervisor('ide')

    def test_qq_supervisor_restarts_zero_and_failure_exits_and_cleans_process_tree(self):
        self.check_supervisor('qq')

    def check_supervisor(self, component):
        with tempfile.TemporaryDirectory(prefix='ide recovery ') as folder:
            root = Path(folder)
            probe = root / 'probe.py'
            probe.write_text('''import ctypes,json,subprocess,sys,time
from pathlib import Path
root=Path(__file__).parent
p=root/'count'
n=int(p.read_text())+1 if p.exists() else 1
p.write_text(str(n))
print('FAKE_SENSITIVE_TOKEN',flush=True)
if n<3: sys.exit(7 if n==1 else 0)
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
(root/'probe.json').write_text(json.dumps({'descendant':child.pid,'console':ctypes.windll.kernel32.GetConsoleWindow()}))
time.sleep(60)
''', encoding='utf-8')
            runner = root / 'runner.py'
            runner.write_text('import sys\nfrom pathlib import Path\n'
                              + 'sys.path.insert(0,' + repr(str(PACKAGE / 'scripts')) + ')\n'
                              + 'from windows_supervisor import supervise\n'
                              + 'root=Path(__file__).parent\n'
                              + 'supervise(root,[sys.executable,str(root/"probe.py")],root,retry_delay=.1,component=' + repr(component) + ')\n', encoding='utf-8')
            parent = subprocess.Popen([sys.executable, str(runner)], creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                state_path = root / f'logs/{component}-supervisor.json'
                def running_after(count):
                    state = json.loads(state_path.read_text())
                    return state if state['phase'] == 'running' and state['attempts'] >= count else None
                first = wait_for(lambda: running_after(3))
                probe_state = wait_for(lambda: json.loads((root / 'probe.json').read_text()))
                self.assertEqual(probe_state['console'], 0)
                # A second trigger must not launch a second IDE.
                duplicate = subprocess.run([sys.executable, str(runner)], timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(duplicate.returncode, 0)
                self.assertEqual(json.loads(state_path.read_text())['child_pid'], first['child_pid'])
                os.kill(first['child_pid'], signal.SIGTERM)
                later = wait_for(lambda: running_after(4))
                self.assertNotEqual(first['child_pid'], later['child_pid'])
                wait_for(lambda: not alive(probe_state['descendant']))
                latest_probe = wait_for(lambda: (value if value['descendant'] != probe_state['descendant'] else None)
                                        if (value := json.loads((root / 'probe.json').read_text())) else None)
                parent.kill()
                parent.wait(timeout=5)
                wait_for(lambda: not alive(later['child_pid']) and not alive(latest_probe['descendant']))
                log = (root / f'logs/{component}-supervisor.log').read_text()
                self.assertIn('code=7', log)
                self.assertIn('code=0', log)
                self.assertNotIn('FAKE_SENSITIVE_TOKEN', log)
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)

    def test_manifest_uses_pythonw_and_quotes_paths_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='ide manifest ') as folder:
            root = Path(folder)
            config = {'server_dir': str(root / 'server'), 'workspace': str(root / 'workspace'),
                      'id': 'test', 'local_port': 18080}
            local.write_windows_task(root, config)
            manifest = json.loads((root / 'windows-supervisor-task.json').read_text())
            self.assertEqual(Path(manifest['executable']).name, 'pythonw.exe')
            self.assertEqual(manifest['arguments'], subprocess.list2cmdline([str(root / 'windows_ide.py'), '--root', str(root)]))

    def test_scheduler_recovery_trigger_is_indefinite_and_ignores_duplicate_starts(self):
        script = r'''$Ast=[Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$null,[ref]$null)
foreach($Name in @('Get-IdeTaskSettings','Get-IdeRecoveryTrigger','Resolve-TaskUserSid')) {
 $Function=$Ast.Find({param($Node) $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq $Name},$true)
 . ([scriptblock]::Create($Function.Extent.Text))
}
$Settings=Get-IdeTaskSettings
$Trigger=Get-IdeRecoveryTrigger
$Identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$ShortName=$Identity.Name.Split('\')[-1]
[pscustomobject]@{interval=$Trigger.Repetition.Interval;duration=$Trigger.Repetition.Duration;instances=[int]$Settings.MultipleInstances;limit=$Settings.ExecutionTimeLimit;hidden=$Settings.Hidden;sidMatched=((Resolve-TaskUserSid $ShortName) -eq $Identity.User.Value)} | ConvertTo-Json
'''
        with tempfile.TemporaryDirectory() as folder:
            probe = Path(folder) / 'check.ps1'
            probe.write_text(script, encoding='utf-8')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(probe),
                                     str(PACKAGE / 'scripts/local-windows.ps1')], capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            settings = json.loads(result.stdout)
            self.assertEqual(settings['interval'], 'PT1M')
            self.assertFalse(settings['duration'])
            self.assertEqual(settings['instances'], 2)
            self.assertEqual(settings['limit'], 'PT0S')
            self.assertTrue(settings['hidden'])
            self.assertTrue(settings['sidMatched'])
