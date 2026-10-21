#!/usr/bin/env python3
"""Local, hidden-entry replacement of an existing Claude DeepSeek credential."""
import argparse
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid


def check_key(key):
    if not key or any(ch.isspace() or ord(ch) < 32 for ch in key):
        raise ValueError("Key 为空或包含空白；配置未修改。")
    curl = '/usr/bin/curl' if sys.platform == 'darwin' else shutil.which('curl')
    if not curl:
        raise ValueError("缺少 curl；配置未修改。")
    # The credential travels through stdin, never command arguments or a file.
    config = '\n'.join([
        'url = "https://api.deepseek.com/models"',
        'header = ' + json.dumps('Authorization: Bearer ' + key),
        'connect-timeout = 8', 'max-time = 20', 'silent', 'show-error',
        'write-out = "\\nHTTP_STATUS=%{http_code}"',
    ])
    try:
        result = subprocess.run([curl, '--disable', '--config', '-'], input=config,
                                capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Key 验证连接失败或超时；配置未修改。") from None
    body, sep, status = result.stdout.rpartition('\nHTTP_STATUS=')
    if result.returncode or not sep:
        raise ValueError("Key 验证网络失败；配置未修改。")
    if status != '200':
        if status == '401':
            raise ValueError("DeepSeek 返回 HTTP 401：Key 无效；配置未修改。")
        raise ValueError(f"DeepSeek 返回 HTTP {status}；配置未修改。")
    try:
        response = json.loads(body)
        if response.get('object') != 'list' or not isinstance(response.get('data'), list):
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError("DeepSeek 返回的模型列表无效；配置未修改。") from None


def update(path, key, verify=check_key):
    path = path.expanduser().resolve(strict=True)
    raw = path.read_bytes()
    settings = json.loads(raw)
    if not isinstance(settings, dict) or not isinstance(settings.get('env'), dict):
        raise ValueError("Claude settings 或 env 格式无效；配置未修改。")
    env = settings.get('env', {})
    route = urlsplit(env.get('ANTHROPIC_BASE_URL', ''))
    if (route.scheme, route.netloc, route.path.rstrip('/'), route.query, route.fragment) != (
            'https', 'api.deepseek.com', '/anthropic', '', ''):
        raise ValueError("现有配置不是 DeepSeek 官方 Anthropic 接口；停止，未修改配置。")
    if env.get('ANTHROPIC_API_KEY'):
        raise ValueError("同时配置了 ANTHROPIC_API_KEY；为避免覆盖其他凭据，请先确认认证来源。")
    verify(key)
    if path.read_bytes() != raw:
        raise ValueError("验证期间配置被修改；停止，请重跑。")
    env['ANTHROPIC_AUTH_TOKEN'] = key
    settings['env'] = env
    content = (json.dumps(settings, ensure_ascii=False, indent=2) + '\n').encode()
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = path.with_name(path.name + '.remotecode-backup-' + stamp + '-' + uuid.uuid4().hex[:6])
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    fd, name = tempfile.mkstemp(prefix='.remotecode-key-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.chmod(name, 0o600)
        if path.read_bytes() != raw:
            raise ValueError("写入前检测到配置变化；未覆盖，原始备份保留。")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=Path.home() / '.claude/settings.json')
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise ValueError("请直接在本机终端运行，以隐藏方式输入 Key；不要通过管道传入。")
    key = getpass.getpass('新的 DeepSeek API Key（输入不显示）: ').strip()
    print('正在通过官方模型列表接口验证 Key…', flush=True)
    backup = update(args.settings, key)
    print('验证通过，Claude 的 DeepSeek Key 已更新。Key 未显示，也未写入命令历史。')
    print('原始配置备份:', backup)
    print('现在在 iPad 点 Stop，断开 Claude (local ACP)，再连接并新建会话。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\n已取消，未更新凭据。', file=sys.stderr)
        sys.exit(1)
