import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import ssl
import socket
import threading
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import qq_bot
from acp_qq_proxy import Proxy


class ProxyTests(unittest.TestCase):
    def permission(self, api):
        proxy = Proxy(Path('/unused'), ['unused'], sink=io.BytesIO())
        proxy.api = api
        proxy.child = type('Child', (), {'stdin': io.BytesIO()})()
        message = {'jsonrpc': '2.0', 'id': 5, 'method': 'session/request_permission',
                   'params': {'sessionId': 's', 'options': [], 'toolCall': {'title': 'test'}}}
        state = {'session': 's', 'cancelled': False}
        proxy.pending['5'] = state
        return proxy, message, state

    def test_remote_decision_is_response_to_original_rpc(self):
        def api(route, body=None):
            return {'route': 'qq', 'id': 'REMOTE'} if route == '/permission' else {
                'result': {'outcome': {'outcome': 'selected', 'optionId': 'actual-option'}}}
        proxy, message, state = self.permission(api)
        proxy.permission(message, state)
        result = json.loads(proxy.child.stdin.getvalue())
        self.assertEqual(result['id'], 5)
        self.assertEqual(result['result']['outcome']['optionId'], 'actual-option')
        self.assertEqual(proxy.sink.getvalue(), b'')

    def test_reused_rpc_id_creates_fresh_remote_approval_identity(self):
        requests = []
        def api(route, body=None):
            if route == '/permission':
                requests.append(body['request_key'])
                return {'route': 'ide'}
        proxy, message, state = self.permission(api)
        proxy.permission(message, state)
        proxy.pending['5'] = {'session': 's', 'cancelled': False}
        proxy.permission(message, proxy.pending['5'])
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0], requests[1])

    def test_unavailable_service_preserves_ide_permission(self):
        def api(*_):
            raise OSError('unavailable')
        proxy, message, state = self.permission(api)
        proxy.permission(message, state)
        self.assertEqual(json.loads(proxy.sink.getvalue()), message)
        self.assertEqual(proxy.child.stdin.getvalue(), b'')

    def test_disconnect_after_routing_cancels_instead_of_falling_back(self):
        def api(route, body=None):
            if route == '/permission':
                return {'route': 'qq', 'id': 'REMOTE'}
            raise OSError('disconnected')
        proxy, message, state = self.permission(api)
        proxy.permission(message, state)
        self.assertEqual(json.loads(proxy.child.stdin.getvalue())['result']['outcome']['outcome'], 'cancelled')
        self.assertEqual(proxy.sink.getvalue(), b'')

    def test_cancellation_wins_before_response_is_written(self):
        proxy = None
        def api(route, body=None):
            if route == '/permission':
                return {'route': 'qq', 'id': 'REMOTE'}
            proxy.cancel('s')
            return {'result': {'outcome': {'outcome': 'selected', 'optionId': 'YES'}}}
        proxy, message, state = self.permission(api)
        # Avoid recursion of the mocked cancellation endpoint.
        original = proxy.cancel
        proxy.cancel = lambda session: state.update(cancelled=True)
        proxy.permission(message, state)
        self.assertEqual(json.loads(proxy.child.stdin.getvalue())['result']['outcome']['outcome'], 'cancelled')

    def test_stdio_fallback_roundtrip_with_real_child_process(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / 'agent.py'
            fake.write_text('''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='initialize':
  print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'protocolVersion':1}}),flush=True)
 elif m.get('method')=='session/prompt':
  print(json.dumps({'jsonrpc':'2.0','id':77,'method':'session/request_permission','params':{'sessionId':'s','toolCall':{'title':'test'},'options':[]}}),flush=True)
 elif m.get('id')==77:
  print(json.dumps({'jsonrpc':'2.0','id':2,'result':m['result']}),flush=True)
''')
            process = subprocess.Popen([sys.executable, str(Path(qq_bot.__file__).with_name('acp_qq_proxy.py')),
                                        '--root', folder, '--', sys.executable, str(fake)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}) + '\n'); process.stdin.flush()
                self.assertEqual(json.loads(process.stdout.readline())['id'], 1)
                process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'session/prompt'}) + '\n'); process.stdin.flush()
                self.assertEqual(json.loads(process.stdout.readline())['method'], 'session/request_permission')
                result = {'outcome': {'outcome': 'selected', 'optionId': 'YES'}}
                process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 77, 'result': result}) + '\n'); process.stdin.flush()
                self.assertEqual(json.loads(process.stdout.readline())['result'], result)
            finally:
                process.stdin.close(); process.wait(timeout=10)
                process.stdout.close(); process.stderr.close()


class ConfigurationTests(unittest.TestCase):
    def test_pair_wait_outputs_nonmatching_message_and_regular_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'qq').mkdir()
            sleeps = 0
            def sleep(_):
                nonlocal sleeps
                sleeps += 1
                if sleeps == 7:
                    qq_bot.write(root / 'qq/binding.json', {'pair_code': 'ABCDEF123456', 'reply_confirmed': True})
            def call(_root, route, body=None, timeout=None):
                if route == '/pair':
                    return {'code': 'ABCDEF123456'}
                return {'configured': True, 'online': True, 'bound': False,
                        'event_counts': {'C2C_MESSAGE_CREATE': 2}, 'last_message': {'result': 'unbound_sender'}}
            output = io.StringIO()
            with patch.object(qq_bot, 'local_call', side_effect=call), patch.object(qq_bot.time, 'sleep', side_effect=sleep), patch('sys.stdout', output):
                qq_bot.pair(root)
            text = output.getvalue()
            self.assertIn('开始 QQ 配对', text)
            self.assertIn('没有识别到配对码', text)
            self.assertIn('收到单聊 2 条', text)
            self.assertIn('确认回复已发送', text)

    def test_macos_uses_os_ca_without_disabling_verification(self):
        context = Mock()
        with patch.object(qq_bot.ssl, 'create_default_context', return_value=context), \
             patch.object(qq_bot.platform, 'system', return_value='Darwin'), \
             patch.object(qq_bot.Path, 'is_file', return_value=True), \
             patch.dict(qq_bot.os.environ, {'SSL_CERT_FILE': '', 'SSL_CERT_DIR': ''}):
            self.assertIs(qq_bot.qq_tls_context(), context)
        context.load_verify_locations.assert_called_once_with(cafile=str(Path('/etc/ssl/cert.pem')))
        self.assertNotIn('check_hostname', context.__dict__)
        self.assertNotIn('verify_mode', context.__dict__)

    def test_explicit_ca_override_is_preserved(self):
        context = Mock()
        with patch.object(qq_bot.ssl, 'create_default_context', return_value=context), \
             patch.object(qq_bot.platform, 'system', return_value='Darwin'), \
             patch.dict(qq_bot.os.environ, {'SSL_CERT_FILE': '/explicit/trusted-ca.pem'}):
            qq_bot.qq_tls_context()
        context.load_verify_locations.assert_not_called()

    def test_auth_errors_are_specific_and_do_not_echo_remote_messages(self):
        cases = [
            (qq_bot.urllib.error.URLError(ssl.SSLCertVerificationError(1, 'secret in TLS message')), 'TLS_CERT_VERIFY_FAILED'),
            (qq_bot.urllib.error.URLError(socket.gaierror(8, 'secret in DNS message')), 'DNS_LOOKUP_FAILED'),
            (qq_bot.urllib.error.URLError(TimeoutError('secret timeout')), 'CONNECTION_TIMEOUT'),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected), patch.object(qq_bot.urllib.request, 'build_opener') as builder:
                builder.return_value.open.side_effect = error
                with self.assertRaisesRegex(ValueError, expected) as raised:
                    qq_bot.validate_credentials({'app_id': '12345678', 'app_secret': 'SECRET'})
                self.assertNotIn('secret', str(raised.exception).lower())

    def test_auth_200_business_error_and_non_object_response_are_rejected(self):
        for result, expected in [({'code': 100016}, '100016'), ({'err_code': 100007}, '100007'), ([], 'RESPONSE_INVALID')]:
            with self.subTest(result=result), patch.object(qq_bot.urllib.request, 'build_opener') as builder:
                response = builder.return_value.open.return_value.__enter__.return_value
                response.read.return_value = json.dumps(result)
                with self.assertRaisesRegex(ValueError, expected):
                    qq_bot.validate_credentials({'app_id': '12345678', 'app_secret': 'SECRET'})

    def test_install_repeat_preserves_credentials_custom_settings_and_original_agents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'runtime'; root.mkdir()
            codex_skill = Path(folder) / 'codex/skills/remotecode-qq'
            claude_skill = Path(folder) / 'claude/skills/remotecode-qq'
            codex_agent = {'command': '/node', 'args': ['/codex-acp.js'], 'env': {'PATH': '/bin'}}
            claude_agent = {'command': '/node', 'args': ['/claude-acp.js'], 'env': {'PATH': '/bin'}}
            qq_bot.write(root / 'local.json', {'id': 'test', 'local_port': 18080})
            qq_bot.write(root / 'data/data/Machine/settings.json', {'editor.fontSize': 22,
                'acp.agents': {'Codex (local ACP)': codex_agent, 'Claude (local ACP)': claude_agent,
                               'custom': {'command': '/custom'}}})
            with patch('agents.find_node', return_value='/node'), \
                 patch.object(qq_bot, 'skill_directories', return_value=[codex_skill, claude_skill]):
                qq_bot.install(root, no_service=True)
                qq_bot.write(root / 'qq/config.json', {'app_secret': 'KEEP_SECRET'})
                token = (root / 'qq/local-token').read_bytes()
                qq_bot.install(root, no_service=True)
                settings = qq_bot.read(root / 'data/data/Machine/settings.json')
                self.assertEqual(settings['editor.fontSize'], 22)
                self.assertEqual(settings['acp.agents']['Codex (local ACP)'], codex_agent)
                self.assertEqual(settings['acp.agents']['Claude (local ACP)'], claude_agent)
                self.assertEqual(settings['acp.agents']['custom'], {'command': '/custom'})
                self.assertIn('Codex (QQ approvals)', settings['acp.agents'])
                self.assertIn('Claude (QQ approvals)', settings['acp.agents'])
                self.assertIn('acp_qq_proxy.py', settings['acp.agents']['Claude (QQ approvals)']['args'][0])
                self.assertEqual(qq_bot.read(root / 'qq/config.json')['app_secret'], 'KEEP_SECRET')
                self.assertEqual((root / 'qq/local-token').read_bytes(), token)
                self.assertIn(str(root), (codex_skill / 'SKILL.md').read_text())
                self.assertIn(str(root), (claude_skill / 'SKILL.md').read_text())
                skills = qq_bot.read(root / 'qq/skills.json')
                self.assertEqual({s['path'] for s in skills}, {str(codex_skill), str(claude_skill)})
                (claude_skill / 'SKILL.md').write_text('user edited skill')
                with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
                    qq_bot.install(root, no_service=True)

    def test_legacy_single_entry_and_skill_migrate_to_plural_manifests(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'runtime'; root.mkdir()
            qq_bot.write(root / 'local.json', {'id': 'test', 'local_port': 18080})
            adapter = {'command': '/node', 'args': ['/adapter.js'], 'env': {'PATH': '/bin'}}
            qq_bot.write(root / 'data/data/Machine/settings.json',
                {'acp.agents': {'Codex (local ACP)': adapter, 'Claude (local ACP)': adapter}})
            legacy_entry = {'command': '/python', 'args': ['/proxy.py', '--root', str(root), '--', '/node', '/adapter.js'],
                            'env': {'PATH': '/bin'}}
            qq_bot.write(root / 'qq/entry.json', legacy_entry)
            legacy_skill = {'path': str(Path(folder) / 'old-skill'), 'sha256': 'deadbeef'}
            qq_bot.write(root / 'qq/skill.json', legacy_skill)
            skills = [Path(folder) / 's1', Path(folder) / 's2']
            with patch('agents.find_node', return_value='/node'), \
                 patch.object(qq_bot, 'skill_directories', return_value=skills):
                qq_bot.install(root, no_service=True)
            self.assertFalse((root / 'qq/entry.json').exists())
            self.assertFalse((root / 'qq/skill.json').exists())
            entries = qq_bot.read(root / 'qq/entries.json')
            self.assertIn('Codex (QQ approvals)', entries)
            self.assertIn('Claude (QQ approvals)', entries)
            self.assertEqual(len(qq_bot.read(root / 'qq/skills.json')), 2)

    def test_secret_input_refuses_noninteractive_terminal(self):
        with patch.object(sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, '交互终端'):
                qq_bot.configure(Path('/unused'))

    def test_failed_validation_preserves_existing_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'qq').mkdir()
            original = {'app_id': '12345678', 'app_secret': 'OLD', 'sandbox': False}
            qq_bot.write(root / 'qq/config.json', original)
            before = (root / 'qq/config.json').read_bytes()
            with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(sys.stderr, 'isatty', return_value=True), \
                 patch('builtins.input', side_effect=['87654321', 'n']), patch.object(qq_bot.getpass, 'getpass', return_value='NEW'), \
                 patch.object(qq_bot, 'validate_credentials', side_effect=ValueError('invalid')):
                with self.assertRaisesRegex(ValueError, 'invalid'):
                    qq_bot.configure(root)
            self.assertEqual((root / 'qq/config.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
