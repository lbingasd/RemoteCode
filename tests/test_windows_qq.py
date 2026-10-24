"""Windows QQ lifecycle tests; task registration is intercepted, no QQ traffic."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.test_windows_ide import alive, wait_for

PACKAGE = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'requires native Windows')
class WindowsQqTests(unittest.TestCase):
    def test_actual_wrapper_is_console_free_and_preserves_configuration(self):
        with tempfile.TemporaryDirectory(prefix='qq recovery ') as folder:
            root = Path(folder)
            runtime = root / 'qq/runtime'
            runtime.mkdir(parents=True)
            for name in ('windows_qq.py', 'windows_supervisor.py', 'common.py'):
                shutil.copy2(PACKAGE / 'scripts' / name, runtime / name)
            config = root / 'qq/config.json'
            binding = root / 'qq/binding.json'
            config.write_text('{"app_secret":"FIXTURE_SECRET"}')
            binding.write_text('{"user_openid":"FIXTURE_OWNER"}')
            probe = root / 'probe.py'
            probe.write_text('import ctypes,json,time\nfrom pathlib import Path\n'
                             + 'Path(__file__).with_suffix(".json").write_text(json.dumps({"console":ctypes.windll.kernel32.GetConsoleWindow()}))\n'
                             + 'print("FIXTURE_SECRET",flush=True)\ntime.sleep(60)\n')
            (root / 'qq/deployment.json').write_text(json.dumps({'command': [sys.executable, str(probe)]}))
            pythonw = Path(sys.executable).with_name('pythonw.exe')
            process = subprocess.Popen([str(pythonw), str(runtime / 'windows_qq.py'), '--root', str(root)])
            try:
                state = wait_for(lambda: json.loads((root / 'logs/qq-supervisor.json').read_text()))
                result = wait_for(lambda: json.loads(probe.with_suffix('.json').read_text()))
                self.assertEqual(result['console'], 0)
                self.assertEqual(config.read_text(), '{"app_secret":"FIXTURE_SECRET"}')
                self.assertEqual(binding.read_text(), '{"user_openid":"FIXTURE_OWNER"}')
                self.assertNotIn('FIXTURE_SECRET', (root / 'logs/qq-supervisor.log').read_text())
                process.kill()
                process.wait(timeout=5)
                wait_for(lambda: not alive(state['child_pid']))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_task_migration_stop_remove_and_foreign_owner_protection(self):
        harness = r'''param($Target,$Root,$Pythonw,$Action,$Mode)
Import-Module ScheduledTasks -ErrorAction Stop
$global:Events=[Collections.Generic.List[string]]::new()
$Identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$Execute=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Arguments='-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $Root 'qq\run-qq.ps1')+'"'
$WorkingDirectory=$null
if($Mode -ne 'legacy') {
 $Execute=$Pythonw
 $Arguments='"'+(Join-Path $Root 'qq\runtime\windows_qq.py')+'" --root "'+$Root+'"'
 $WorkingDirectory=$Root
}
if($Mode -eq 'foreign') { $Execute='C:\unmanaged.exe' }
$User=$Identity.Name.Split('\')[-1]
if($Mode -eq 'other-user') { $User='S-1-5-18' }
$global:FakeTask=[pscustomobject]@{State='Running';Actions=@([pscustomobject]@{Execute=$Execute;Arguments=$Arguments;WorkingDirectory=$WorkingDirectory});Principal=[pscustomobject]@{UserId=$User};Enabled=$true;Settings=$null;Triggers=@()}
function global:Get-ScheduledTask {param($TaskName,$ErrorAction) $global:FakeTask}
function global:Disable-ScheduledTask {param($TaskName) $global:Events.Add('disable');$global:FakeTask.Enabled=$false}
function global:Stop-ScheduledTask {param($TaskName) $global:Events.Add('stop');$global:FakeTask.State='Ready'}
function global:Enable-ScheduledTask {param($TaskName) $global:Events.Add('enable');$global:FakeTask.Enabled=$true}
function global:Start-ScheduledTask {param($TaskName) $global:Events.Add('start');$global:FakeTask.State='Running'}
function global:Unregister-ScheduledTask {param($TaskName,[switch]$Confirm) $global:Events.Add('remove');$global:FakeTask=$null}
function global:Register-ScheduledTask {
 param($TaskName,$Action,$Trigger,$Principal,$Settings,[switch]$Force)
 $global:Events.Add('register')
 $global:FakeTask.Actions=@($Action)
 $global:FakeTask.Settings=$Settings
 $global:FakeTask.Triggers=$Trigger
}
foreach($Command in @('Get-ScheduledTask','Disable-ScheduledTask','Stop-ScheduledTask','Enable-ScheduledTask','Start-ScheduledTask','Unregister-ScheduledTask','Register-ScheduledTask')) {
 if((Get-Command $Command).Definition -notmatch '\$global:(Events|FakeTask)') { throw 'Task command was not intercepted' }
}
$Failure=$null
if($Mode -eq 'missing') { Remove-Item -LiteralPath (Join-Path $Root 'qq\runtime\windows_qq.py') }
try { & $Target -Root $Root -Pythonw $Pythonw -Action $Action } catch { $Failure=$_.Exception.Message }
$Result=[ordered]@{events=@($global:Events);error=$Failure;enabled=$global:FakeTask.Enabled}
if($global:FakeTask.Settings) {
 $Result.hidden=$global:FakeTask.Settings.Hidden
 $Result.instances=[int]$global:FakeTask.Settings.MultipleInstances
 $Result.intervals=@($global:FakeTask.Triggers|ForEach-Object{$_.Repetition.Interval})
 $Result.durations=@($global:FakeTask.Triggers|ForEach-Object{$_.Repetition.Duration})
 $Result.execute=$global:FakeTask.Actions[0].Execute
}
$Result|ConvertTo-Json -Depth 4
'''
        cases = [('install', 'legacy'), ('start', 'current'), ('restart', 'current'),
                 ('stop', 'current'), ('remove', 'current'), ('install', 'foreign'), ('install', 'other-user'), ('start', 'missing')]
        for action, mode in cases:
            with self.subTest(action=action, mode=mode), tempfile.TemporaryDirectory(prefix='qq tasks ') as folder:
                root = Path(folder)
                (root / 'local.json').write_text('{"id":"qq-test"}')
                runner = root / 'qq/runtime/windows_qq.py'
                runner.parent.mkdir(parents=True)
                runner.write_text('# fixture')
                script = root / 'harness.ps1'
                script.write_text(harness, encoding='utf-8')
                result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                                         str(PACKAGE / 'scripts/qq-windows.ps1'), str(root),
                                         str(Path(sys.executable).with_name('pythonw.exe')), action, mode], capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(result.stdout)
                if mode in ('foreign', 'other-user', 'missing'):
                    self.assertTrue(record['error'])
                    self.assertEqual(record['events'], [])
                else:
                    self.assertIsNone(record['error'])
                    if action in ('install', 'restart', 'stop', 'remove'):
                        self.assertEqual(record['events'][:2], ['disable', 'stop'])
                    if action in ('install', 'start', 'restart'):
                        self.assertIn('enable', record['events'])
                        self.assertTrue(record['enabled'])
                        self.assertTrue(record['hidden'])
                        self.assertEqual(record['instances'], 2)
                        self.assertIn('PT1M', record['intervals'])
                        self.assertFalse(any(record['durations']))
                        self.assertEqual(Path(record['execute']).name, 'pythonw.exe')
                    else:
                        self.assertNotIn('register', record['events'])
                        self.assertNotIn('start', record['events'])
                    if action == 'stop':
                        self.assertFalse(record['enabled'])
                    if action == 'remove':
                        self.assertIn('remove', record['events'])
