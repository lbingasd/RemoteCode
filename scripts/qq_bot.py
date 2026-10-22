#!/usr/bin/env python3
"""Local QQ deployment, hidden credential input, lifecycle and screenshot delivery."""
import argparse
import base64
from contextlib import contextmanager
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

from common import read, require, write

SOURCE = Path(__file__).resolve().parent

QQ_AGENTS = (('Codex (local ACP)', 'Codex (QQ approvals)'), ('Claude (local ACP)', 'Claude (QQ approvals)'))


def skill_directories():
    codex = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'skills/remotecode-qq'
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))) / 'skills/remotecode-qq'
    return [codex, claude]


def managed_entries(directory):
    manifest = directory / 'entries.json'
    if manifest.exists():
        value = read(manifest)
        return value if isinstance(value, dict) else {}
    legacy = directory / 'entry.json'
    return {'Codex (QQ approvals)': read(legacy)} if legacy.exists() else {}


def managed_skills(directory):
    manifest = directory / 'skills.json'
    if manifest.exists():
        value = read(manifest)
        return value if isinstance(value, list) else []
    legacy = directory / 'skill.json'
    return [read(legacy)] if legacy.exists() else []


def local_call(root, route, body=None, timeout=45):
    directory = Path(root) / 'qq'
    endpoint = read(directory / 'endpoint.json')
    port = endpoint['port']
    require(type(port) is int and 0 < port < 65536, 'QQ 本机端口无效')
    token = (directory / 'local-token').read_text().strip()
    request = urllib.request.Request(f'http://127.0.0.1:{port}{route}',
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    # Explicitly bypass proxy environment variables for the private loopback channel.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            error = json.load(exc).get('error', 'LOCAL_HTTP_ERROR')
        except (ValueError, OSError):
            error = 'LOCAL_HTTP_ERROR'
        require(False, 'QQ 本机服务拒绝请求: ' + str(error))
    return result


def secure_dir(directory):
    directory.mkdir(parents=True, exist_ok=True)
    require(not directory.is_symlink(), 'QQ 配置目录不能是符号链接')
    if os.name == 'nt':
        sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True)
        match = re.search(r'S-1-\d+(?:-\d+)+', sid)
        require(match, '无法获取当前用户 SID')
        subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r',
                        f'*{match.group()}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        directory.chmod(0o700)


@contextmanager
def operation_lock(directory):
    # Cross-process install/configure guard, including getpass and settings updates.
    secure_dir(directory)
    with (directory / 'configure.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('QQ 部署/配置已在另一个终端运行') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def qq_tls_context():
    context = ssl.create_default_context()
    # python.org's macOS Python can lack its OpenSSL CA file even though the
    # system trust bundle is installed. Add the OS bundle without weakening TLS
    # or changing Python/global certificates. Preserve explicit CA overrides.
    if platform.system() == 'Darwin' and not any(os.environ.get(k) for k in ('SSL_CERT_FILE', 'SSL_CERT_DIR')):
        bundle = Path('/etc/ssl/cert.pem')
        if bundle.is_file():
            context.load_verify_locations(cafile=str(bundle))
    return context


def auth_network_error(exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return 'TLS_CERT_VERIFY_FAILED（证书校验失败）'
    if isinstance(reason, socket.gaierror):
        return 'DNS_LOOKUP_FAILED（域名解析失败）'
    if isinstance(reason, TimeoutError):
        return 'CONNECTION_TIMEOUT（连接或读取超时）'
    if isinstance(reason, ssl.SSLError):
        return 'TLS_HANDSHAKE_FAILED（TLS 握手失败）'
    return 'CONNECTION_FAILED（连接失败）'


def validate_credentials(config):
    request = urllib.request.Request('https://api.bot.qq.com/app/getAppAccessToken',
        data=json.dumps({'appId': config['app_id'], 'clientSecret': config['app_secret']}).encode(),
        headers={'Content-Type': 'application/json'})
    # Do not expose credentials to an implicit HTTP proxy or follow redirects with the body.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=qq_tls_context()))
    try:
        with opener.open(request, timeout=20) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError(f'QQ 鉴权 HTTP {exc.code}；现有凭据未修改') from None
    except OSError as exc:
        raise ValueError('QQ 鉴权 ' + auth_network_error(exc) + '；现有凭据未修改') from None
    except ValueError:
        raise ValueError('QQ 鉴权 RESPONSE_NOT_JSON（响应不是有效 JSON）；现有凭据未修改') from None
    require(isinstance(result, dict), 'QQ 鉴权 RESPONSE_INVALID（响应结构无效）；现有凭据未修改')
    code = result.get('err_code', result.get('code', 0))
    require(str(code) == '0' and isinstance(result.get('access_token'), str) and bool(result['access_token']),
            f'QQ 鉴权失败，错误码 {code if str(code).isdigit() else "UNKNOWN"}；现有凭据未修改')


def configure(root):
    require(sys.stdin.isatty() and sys.stderr.isatty(), '请在自己的交互终端运行；拒绝以明文 stdin 读取 AppSecret')
    directory = root / 'qq'
    with operation_lock(directory):
        old = read(directory / 'config.json') if (directory / 'config.json').exists() else {}
        print('QQ 信息只保存在本机。AppSecret 隐藏输入，不会写入日志或命令行。')
        app_id = input('QQ Bot AppID' + ('（留空保留）' if old else '') + ': ').strip() or old.get('app_id', '')
        secret = getpass.getpass('QQ Bot AppSecret' + ('（留空保留）' if old else '') + ': ').strip() or old.get('app_secret', '')
        require(re.fullmatch(r'\d{4,30}', app_id) and secret and len(secret) <= 512
                and not any(c.isspace() for c in secret), 'AppID 或 AppSecret 格式无效')
        sandbox = input('使用 QQ 沙箱？[y/N，正式发布/已授权用户选 N]: ').strip().lower() == 'y'
        config = {'schema': 1, 'app_id': app_id, 'app_secret': secret, 'sandbox': sandbox}
        print('正在向 QQ 官方验证凭据…', flush=True)
        validate_credentials(config)
        if old and (old['app_id'] != app_id or old.get('sandbox', False) != sandbox):
            write(directory / 'binding.json', {})
        write(directory / 'config.json', config)
        print('QQ 凭据验证成功，已保存。')
    service(root, 'restart')
    wait_service(root)


def wait_service(root):
    for _ in range(15):
        try:
            return local_call(root, '/status', timeout=1)
        except (OSError, ValueError):
            time.sleep(0.5)
    raise ValueError('QQ 本机服务未启动；运行 status 检查日志')


def service(root, action):
    deployment = read(root / 'qq/deployment.json')
    command = deployment['command']
    name = 'remotecode-qq-' + read(root / 'local.json')['id']
    require(re.fullmatch(r'[A-Za-z0-9_.-]+', name), '机器标识无效')
    logdir = root / 'logs'; logdir.mkdir(exist_ok=True)
    if platform.system() == 'Darwin':
        label = 'local.' + name
        parent = Path.home() / 'Library/LaunchAgents'; parent.mkdir(parents=True, exist_ok=True)
        target = parent / (label + '.plist')
        scope = f'gui/{os.getuid()}'
        if target.exists():
            require(plistlib.loads(target.read_bytes()).get('ProgramArguments') == command,
                    '已有 QQ LaunchAgent 属于另一个运行目录，拒绝覆盖')
        loaded = subprocess.run(['launchctl', 'print', f'{scope}/{label}'], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL).returncode == 0
        if action in ('stop', 'restart', 'remove') and loaded:
            subprocess.run(['launchctl', 'bootout', f'{scope}/{label}'], check=True)
            loaded = False
        if action == 'remove':
            target.unlink(missing_ok=True)
        elif action in ('install', 'start', 'restart'):
            payload = {'Label': label, 'ProgramArguments': command, 'WorkingDirectory': str(root),
                       'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                       'StandardOutPath': str(logdir / 'qq.out.log'), 'StandardErrorPath': str(logdir / 'qq.err.log'),
                       'EnvironmentVariables': {'PATH': str(Path(command[0]).parent) + ':/usr/bin:/bin'}}
            if not target.exists():
                target.write_bytes(plistlib.dumps(payload)); target.chmod(0o600)
            if not loaded:
                subprocess.run(['launchctl', 'bootstrap', scope, str(target)], check=True)
    elif platform.system() == 'Linux':
        parent = Path.home() / '.config/systemd/user'; parent.mkdir(parents=True, exist_ok=True)
        target = parent / (name + '.service')
        def quote(value):
            return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
        line = 'ExecStart=' + ' '.join(quote(x) for x in command)
        if target.exists():
            require(line in target.read_text().splitlines(), 'QQ systemd unit 属于另一个运行目录')
        if action in ('install', 'start', 'restart'):
            write(target, '[Unit]\nDescription=RemoteCode QQ Bot\nAfter=network-online.target\n[Service]\n'
                  + line + '\nWorkingDirectory=' + quote(root) + '\nRestart=always\nRestartSec=10\nUMask=0077\n[Install]\nWantedBy=default.target\n')
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', '--user', 'enable', name], check=True)
            subprocess.run(['systemctl', '--user', 'restart' if action == 'restart' else 'start', name], check=True)
        elif action in ('stop', 'remove'):
            subprocess.run(['systemctl', '--user', 'disable' if action == 'remove' else 'stop', '--now', name] if action == 'remove'
                           else ['systemctl', '--user', 'stop', name], check=True)
            if action == 'remove':
                target.unlink(missing_ok=True)
                subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    elif os.name == 'nt':
        # Current-user interactive task, matching the IDE's user/session boundary.
        powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        def ps(value):
            return "'" + str(value).replace("'", "''") + "'"
        runner = root / 'qq/run-qq.ps1'
        write(runner, '$ErrorActionPreference="Stop"\n& ' + ' '.join(ps(x) for x in command)
              + ' >> ' + ps(logdir / 'qq.out.log') + ' 2>> ' + ps(logdir / 'qq.err.log') + '\nexit $LASTEXITCODE\n')
        script = '$ErrorActionPreference="Stop"; $n=' + ps(name) + '; $old=Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue; '
        expected = '-NoProfile -ExecutionPolicy Bypass -File "' + str(runner) + '"'
        script += 'if($old -and ($old.Actions.Count -ne 1 -or $old.Actions[0].Execute -ne ' + ps(powershell) + ' -or $old.Actions[0].Arguments -ne ' + ps(expected) + ')){throw "Task ownership mismatch"}; '
        if action in ('stop', 'restart', 'remove'):
            script += 'if($old){Stop-ScheduledTask -TaskName $n}; '
        if action == 'remove':
            script += 'if($old){Unregister-ScheduledTask -TaskName $n -Confirm:$false}; '
        elif action in ('install', 'start', 'restart'):
            script += '$u=[Security.Principal.WindowsIdentity]::GetCurrent().Name; $a=New-ScheduledTaskAction -Execute ' + ps(powershell) + ' -Argument ' + ps(expected) + '; '
            script += '$t=New-ScheduledTaskTrigger -AtLogOn -User $u; $p=New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited; '
            script += '$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; '
            script += 'Register-ScheduledTask -TaskName $n -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null; Start-ScheduledTask -TaskName $n; '
        encoded = base64.b64encode(script.encode('utf-16-le')).decode()
        subprocess.run([powershell, '-NoProfile', '-EncodedCommand', encoded], check=True)
    else:
        raise ValueError('QQ 服务支持 macOS/Linux/Windows')


def install(root, no_service=False):
    from agents import find_node
    node = find_node(root)
    require(node, '需要现有 Node.js 22+；请先部署本机 agents')
    directory = root / 'qq'
    with operation_lock(directory):
        runtime = directory / 'runtime'; runtime.mkdir(exist_ok=True)
        for name in ('qq_bot.py', 'acp_qq_proxy.py', 'qq-bot.mjs', 'common.py'):
            source = SOURCE / name
            target = runtime / name
            if source.resolve() != target.resolve():
                temp = target.with_suffix(target.suffix + '.new')
                shutil.copyfile(source, temp); temp.chmod(0o600); temp.replace(target)
        if not (directory / 'local-token').exists():
            write(directory / 'local-token', secrets.token_urlsafe(32) + '\n')
        write(directory / 'deployment.json', {'schema': 1, 'python': sys.executable,
              'command': [str(Path(node).absolute()), str(runtime / 'qq-bot.mjs'), str(root)]})
        # Install a self-contained user skill into the Codex and Claude skill directories.
        # Only replace our previous managed copies.
        template = SOURCE.parent / 'skills/remotecode-qq/SKILL.md'
        text = template.read_text().replace('@PYTHON@', sys.executable).replace('@SCRIPT@', str(runtime / 'qq_bot.py')).replace('@ROOT@', str(root))
        previous_skills = managed_skills(directory)
        manifests = []
        for home_skill in skill_directories():
            previous = next((m for m in previous_skills if m.get('path') == str(home_skill)), {})
            if home_skill.exists():
                existing = home_skill / 'SKILL.md'
                require(existing.is_file() and previous.get('sha256') == hashlib.sha256(existing.read_bytes()).hexdigest(),
                        '已有 remotecode-qq skill 并非当前部署管理或已被修改，拒绝覆盖')
            write(home_skill / 'SKILL.md', text)
            manifests.append({'path': str(home_skill), 'sha256': hashlib.sha256(text.encode()).hexdigest()})
        write(directory / 'skills.json', manifests)
        (directory / 'skill.json').unlink(missing_ok=True)
        # Add dedicated QQ entries; retain the original Codex/Claude settings for IDE-only sessions.
        from setup import parse_jsonc
        settings = root / 'data/data/Machine/settings.json'
        current = parse_jsonc(settings.read_text()) if settings.exists() else {}
        agents = current.setdefault('acp.agents', {})
        old_entries = managed_entries(directory)
        entries = {}
        for base_label, label in QQ_AGENTS:
            base = agents.get(base_label)
            require(base and isinstance(base.get('args', []), list), f'找不到 {base_label}，请先安装本机 agents')
            entry = {'command': sys.executable,
                     'args': [str(runtime / 'acp_qq_proxy.py'), '--root', str(root), '--', base['command'], *base.get('args', [])],
                     'env': base.get('env', {})}
            old_entry = old_entries.get(label)
            require(label not in agents or agents[label] in (entry, old_entry), '同名 QQ agent 已被自定义，拒绝覆盖')
            agents[label] = entry
            entries[label] = entry
        if settings.exists() and not (directory / 'settings-before.json').exists():
            write(directory / 'settings-before.json', settings.read_text())
        # Auto-approve in the client cannot skip permissions intercepted by this proxy.
        current['acp.logTraffic'] = False
        write(settings, current)
        write(directory / 'entries.json', entries)
        (directory / 'entry.json').unlink(missing_ok=True)
    if not no_service:
        service(root, 'restart')
        wait_service(root)
    print('本机 QQ 组件和截图 skill 已部署。ACP 中选择 Codex (QQ approvals) 或 Claude (QQ approvals)；原有会话需重新连接。')


def pair(root, wait=True):
    print('开始 QQ 配对：正在连接本机服务…', flush=True)
    status = local_call(root, '/status', timeout=3)
    require(status['configured'], '请先 configure 输入 AppID/AppSecret')
    for _ in range(20):
        if status['online']:
            break
        time.sleep(1)
        print('等待 QQ 网关就绪…', flush=True)
        status = local_call(root, '/status', timeout=3)
    require(status['online'], 'QQ 网关未上线；错误 ' + status.get('last_error', '') + '；运行 status，检查机器人权限及网络')
    if status['bound']:
        require(input('已有绑定，输入 REBIND 才允许更换接收用户: ').strip() == 'REBIND', '保留已有绑定')
    result = local_call(root, '/pair', {}, timeout=3)
    print('请用接收通知的 QQ 私聊机器人，5 分钟内发送：\n绑定 ' + result['code'], flush=True)
    if wait:
        previous_result = None
        for elapsed in range(300):
            time.sleep(1)
            # Code consumed only after the authenticated QQ event binds its sender.
            state = read(root / 'qq/binding.json') if (root / 'qq/binding.json').exists() else {}
            if state.get('pair_code') == result['code']:
                if state.get('reply_error'):
                    raise ValueError('QQ 用户已绑定，但确认回复失败：' + state['reply_error'] + '；运行 status 查看状态')
                if state.get('reply_confirmed') is not False:
                    print('QQ 用户绑定成功，确认回复已发送。', flush=True); return
            if elapsed % 5 == 4:
                try:
                    live = local_call(root, '/status', timeout=2)
                except (OSError, ValueError):
                    if elapsed % 15 == 14:
                        print('等待绑定：本机 QQ 服务暂不可达。', flush=True)
                    continue
                received = live.get('last_message') or {}
                outcome = received.get('result')
                hints = {'pair_code_mismatch': '已收到绑定消息，但配对码不匹配；请复制本终端的新码。',
                         'pair_format_invalid': '已收到绑定消息，但格式无效；请发送完整的“绑定 配对码”。',
                         'pair_expired': '已收到绑定消息，但配对码已过期；重新运行 pair。',
                         'pair_not_started': '已收到消息，但配对会话已失效；重新运行 pair。',
                         'unbound_sender': '已收到 QQ 消息，但没有识别到配对码；请复制上面的完整“绑定 配对码”。',
                         'duplicate': '收到重复消息；请重新发送完整绑定命令。',
                         'missing_sender_or_message_id': '收到的事件缺少用户或消息标识。'}
                if outcome != previous_result and outcome in hints:
                    print('等待绑定：' + hints[outcome], flush=True)
                previous_result = outcome
                count = live.get('event_counts', {}).get('C2C_MESSAGE_CREATE', 0)
                print(f'等待绑定：已等 {elapsed + 1} 秒；收到单聊 {count} 条；最近处理结果 {outcome or "尚无消息"}。', flush=True)
                if not received:
                    print('尚未收到 QQ 单聊事件；请确认私聊的是当前 AppID 的机器人，且发送账号具有当前环境的测试/使用权限。', flush=True)
                if not live.get('online'):
                    print('等待绑定：QQ 网关离线，错误码 ' + live.get('last_error', ''), flush=True)
        raise ValueError('配对等待超时；运行 pair 生成新配对码')


def uninstall(root):
    directory = root / 'qq'
    if not (directory / 'deployment.json').exists():
        print('QQ 组件未安装。'); return
    service(root, 'remove')
    from setup import parse_jsonc
    settings = root / 'data/data/Machine/settings.json'
    if settings.exists():
        current = parse_jsonc(settings.read_text())
        removed = False
        for label, entry in managed_entries(directory).items():
            if current.get('acp.agents', {}).get(label) == entry:
                current['acp.agents'].pop(label, None)
                removed = True
        if removed:
            write(settings, current)
    for manifest in managed_skills(directory):
        target = Path(manifest['path']) / 'SKILL.md'
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == manifest['sha256']:
            target.unlink()
            if not any(target.parent.iterdir()):
                target.parent.rmdir()
    # Explicit component uninstall revokes local credentials and binding.
    shutil.rmtree(directory)
    print('QQ 服务、受管理的 agent/skill、机器人凭据和绑定已删除。')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['install', 'configure', 'pair', 'status', 'start', 'stop', 'restart', 'uninstall', 'unbind', 'send-image'])
    p.add_argument('--root', type=Path, default=Path.home() / 'RemoteCode')
    p.add_argument('--no-configure', action='store_true')
    p.add_argument('--no-service', action='store_true')
    p.add_argument('--path', type=Path)
    p.add_argument('--session', default='')
    args = p.parse_args()
    root = args.root.expanduser().resolve()
    require((root / 'local.json').is_file(), '需要已部署的 RemoteCode 家庭运行目录')
    if args.action == 'install':
        install(root, args.no_service)
        if not args.no_configure and not args.no_service:
            if not (root / 'qq/config.json').exists():
                configure(root)
            if not local_call(root, '/status')['bound']:
                pair(root)
    elif args.action == 'configure':
        configure(root)
        if not local_call(root, '/status')['bound']:
            pair(root)
    elif args.action == 'pair':
        pair(root)
    elif args.action == 'status':
        try:
            print(json.dumps(local_call(root, '/status', timeout=2), ensure_ascii=False, indent=2))
        except (OSError, ValueError):
            print('QQ 本机服务未运行；凭据不会显示。')
            sys.exit(1)
    elif args.action in ('start', 'stop', 'restart'):
        service(root, args.action)
    elif args.action == 'uninstall':
        uninstall(root)
    elif args.action == 'unbind':
        print(json.dumps(local_call(root, '/unbind', {})))
    elif args.action == 'send-image':
        require(args.path and args.path.is_file(), '请用 --path 提供已获授权发送的 PNG/JPEG 截图')
        with args.path.open('rb') as stream:
            data = stream.read(10 * 1024 * 1024 + 1)
        require(len(data) <= 10 * 1024 * 1024, '截图不能超过 10 MiB')
        print(json.dumps(local_call(root, '/image', {'data': base64.b64encode(data).decode(), 'session': args.session}, timeout=120)))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        # Never print raw urllib errors (which can include URLs) or secret-bearing data.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print('RemoteCode QQ 未完成: ' + message, file=sys.stderr)
        sys.exit(1)
