"""Credential replacement checks use fabricated keys and no network requests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'deepseek_key', Path(__file__).resolve().parents[1] / 'scripts/update-deepseek-key.py')
keytool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keytool)


class DeepseekKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'settings.json'
        self.settings = {'enabledPlugins': {'example': True}, 'theme': 'light', 'env': {
            'ANTHROPIC_BASE_URL': 'https://api.deepseek.com/anthropic',
            'ANTHROPIC_AUTH_TOKEN': 'fabricated-old-key', 'ANTHROPIC_MODEL': 'preserve-model'}}
        self.original = json.dumps(self.settings).encode()
        self.path.write_bytes(self.original)

    def test_invalid_key_does_not_change_config_or_create_backup(self):
        with self.assertRaises(ValueError):
            keytool.update(self.path, 'fabricated-new-key', verify=lambda _: (_ for _ in ()).throw(ValueError('401')))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_success_changes_only_key_and_keeps_private_original_backup(self):
        backup = keytool.update(self.path, 'fabricated-new-key', verify=lambda _: None)
        expected = json.loads(self.original)
        expected['env']['ANTHROPIC_AUTH_TOKEN'] = 'fabricated-new-key'
        self.assertEqual(json.loads(self.path.read_bytes()), expected)
        self.assertEqual(backup.read_bytes(), self.original)
        if os.name != 'nt':
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_change_during_verification_is_preserved(self):
        def verify(_):
            self.path.write_text('{"external":true}')
        with self.assertRaises(ValueError):
            keytool.update(self.path, 'fabricated-new-key', verify=verify)
        self.assertEqual(json.loads(self.path.read_bytes()), {'external': True})
        self.assertFalse(list(self.folder.glob('*backup*')))

    def test_concurrent_change_before_replace_is_preserved_and_temp_removed(self):
        chmod = keytool.os.chmod
        def change(path, mode):
            chmod(path, mode)
            self.path.write_text('{"external":true}')
        with patch.object(keytool.os, 'chmod', side_effect=change), self.assertRaises(ValueError):
            keytool.update(self.path, 'fabricated-new-key', verify=lambda _: None)
        self.assertEqual(json.loads(self.path.read_bytes()), {'external': True})
        self.assertFalse(list(self.folder.glob('.remotecode-key-*')))

    def test_unknown_route_or_ambiguous_credentials_refused_before_network(self):
        for env in [dict(self.settings['env'], ANTHROPIC_BASE_URL='https://example.org/anthropic'),
                    dict(self.settings['env'], ANTHROPIC_API_KEY='other-fabricated-key')]:
            with self.subTest(env_keys=list(env)):
                raw = json.dumps(dict(self.settings, env=env)).encode()
                self.path.write_bytes(raw)
                with patch.object(keytool, 'check_key') as check:
                    with self.assertRaises(ValueError):
                        keytool.update(self.path, 'fabricated-new-key', verify=check)
                    check.assert_not_called()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_check_sends_key_only_via_stdin_and_uses_fixed_https_endpoint(self):
        result = subprocess.CompletedProcess([], 0, '{"object":"list","data":[]}\nHTTP_STATUS=200', '')
        with patch.object(keytool.subprocess, 'run', return_value=result) as execute:
            keytool.check_key('fabricated-new-key')
        args, kwargs = execute.call_args
        self.assertNotIn('fabricated-new-key', ' '.join(args[0]))
        self.assertEqual(args[0][1:], ['--disable', '--config', '-'])
        self.assertIn('https://api.deepseek.com/models', kwargs['input'])
        self.assertIn('Bearer fabricated-new-key', kwargs['input'])
        self.assertNotIn('insecure', kwargs['input'])
        self.assertLessEqual(kwargs['timeout'], 25)

    def test_auth_failure_and_network_errors_do_not_echo_response_or_key(self):
        result = subprocess.CompletedProcess([], 0, 'echo fabricated-new-key\nHTTP_STATUS=401', 'fabricated-new-key')
        for effect in [result, subprocess.TimeoutExpired('curl', 25)]:
            with self.subTest(effect=type(effect).__name__):
                options = {'return_value': effect} if isinstance(effect, subprocess.CompletedProcess) else {'side_effect': effect}
                with patch.object(keytool.subprocess, 'run', **options):
                    with self.assertRaises(ValueError) as error:
                        keytool.check_key('fabricated-new-key')
                self.assertNotIn('fabricated-new-key', str(error.exception))

    def test_noninteractive_entry_refused_before_secret_prompt(self):
        with patch.object(keytool.sys, 'argv', ['update-deepseek-key.py']), \
                patch.object(keytool.sys.stdin, 'isatty', return_value=False), \
                patch.object(keytool.getpass, 'getpass') as prompt:
            with self.assertRaises(ValueError):
                keytool.main()
            prompt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
