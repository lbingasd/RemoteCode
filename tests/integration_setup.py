#!/usr/bin/env python3
"""Real macOS entrypoint smoke: cached packages, rerun, recovery and purge.

No model calls. --with-services briefly creates then removes unique LaunchAgents;
its loopback TLS fixture checks doctor, not a real cloud/frp deployment.
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cloud
from common import read, require, run, write
from local import server_args
from agents import npm_command

PACKAGE = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--vsix", type=Path, required=True)
    p.add_argument("--npm-cache", type=Path)
    p.add_argument("--with-services", action="store_true")
    p.add_argument("--accept-license", action="store_true", required=True)
    p.add_argument("--report", type=Path, required=True)
    args = p.parse_args()
    require(sys.platform == "darwin", "此实测脚本仅支持当前 macOS")
    report = {"scope": "macOS real installer with cached assets; no system dependency installation, Linux Docker, Windows, iPad or model calls", "checks": []}
    with tempfile.TemporaryDirectory(prefix="remotecode-oneclick-") as folder:
        base = Path(folder).resolve()
        root, cache, project = base / "runtime", base / "cache", base / "project"
        cache.mkdir()
        lock = read(PACKAGE / "examples/darwin-amd64.lock.json")
        for item in lock["files"].values():
            (cache / item["name"]).symlink_to(args.cache.resolve() / item["name"])
        ext = read(PACKAGE / "examples/acp-extension.lock.json")
        (cache / ext["name"]).symlink_to(args.vsix.resolve())
        ca = base / "gateway"
        (ca / "certs").mkdir(parents=True)
        cloud.make_pki(ca, "127.0.0.1")
        name = "smoke" + secrets.token_hex(4)
        machine = {"id": name, "proxy_port": free_port(), "frp_token": secrets.token_hex(32), "ide_token": secrets.token_hex(32)}
        cloud.export_machine(ca, {"ip": "127.0.0.1", "frp_version": lock["frp_version"]}, machine, "test-only")
        bundle = ca / "enrollment" / name
        pair = read(bundle / "machine.json")
        pair["local_port"] = free_port()
        write(bundle / "machine.json", pair)
        command = ["bash", PACKAGE / "deploy-local.sh", "install", "--root", root, "--bundle", bundle,
                   "--workspace", project, "--cache", cache, "--offline", "--accept-license", "--no-service", "--no-agents"]
        ide = None
        fixture = None
        try:
            run(command)
            report["checks"].append("fresh offline runtime and real VSIX install via deploy-local.sh")
            before = (root / "ide-token").read_bytes()
            inode = (root / "server/node").stat().st_ino
            run(command)
            require((root / "ide-token").read_bytes() == before and (root / "server/node").stat().st_ino == inode, "重跑覆盖安装/密钥")
            report["checks"].append("rerun retains credentials, program files and installed extension")
            if args.npm_cache:
                # Simulate an interrupted install after package-lock creation.
                versions = read(PACKAGE / "examples/agents-tested-versions.json")["packages"]
                write(root / "agents/package.json", {"name": "remotecode-local-agents", "version": "1.0.0", "private": True, "dependencies": versions})
                (cache / "npm").symlink_to(args.npm_cache.resolve(), target_is_directory=True)
                run(npm_command(shutil.which("node")) + ["install", "--package-lock-only", "--offline", "--prefix", str(root / "agents"), "--cache", str(cache / "npm"), "--no-audit", "--no-fund"])
                command.remove("--no-agents")
                run(command)
                marker = read(root / "setup.json")
                require(marker.get("agents"), "代理安装标记缺失")
                module_inode = (root / "agents/node_modules/@openai/codex/package.json").stat().st_ino
                settings = root / "data/data/Machine/settings.json"
                settings.write_text('// retain my settings\n' + settings.read_text().rstrip()[:-1] + ', "editor.fontSize":19,}\n')
                run(command)
                require((root / "agents/node_modules/@openai/codex/package.json").stat().st_ino == module_inode, "重跑仍然执行了 npm ci")
                require(read(settings)["editor.fontSize"] == 19, "用户设置丢失")
                report["checks"].append("resume from npm lock; real cached npm ci; subsequent run skips ci; JSONC settings retained")
            if args.with_services:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(ca / "certs/tunnel.pem", ca / "certs/tunnel.key")
                class Handler(socketserver.BaseRequestHandler):
                    def handle(self):
                        try:
                            with context.wrap_socket(self.request, server_side=True) as stream:
                                stream.settimeout(1)
                                stream.recv(1024)
                        except (OSError, ssl.SSLError):
                            pass
                fixture = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
                fixture.daemon_threads = True
                threading.Thread(target=fixture.serve_forever, daemon=True).start()
                frpc = root / "frpc.toml"
                frpc.write_text(frpc.read_text().replace("serverPort = 443", f"serverPort = {fixture.server_address[1]}"))
                command.remove("--no-service")
                run(command)
                run(command)
                report["checks"].append("real LaunchAgents install + identical rerun + doctor against local TLS fixture and native IDE")
            else:
                config = read(root / "local.json")
                ide = subprocess.Popen(server_args(root, config), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            url = f"http://127.0.0.1:{pair['local_port']}/m/{name}/?tkn={pair['ide_token']}"
            for attempt in range(40):
                try:
                    with opener.open(url, timeout=2) as response:
                        require(response.status == 200, "IDE HTTP 失败")
                    break
                except OSError:
                    if attempt == 39:
                        raise
                    time.sleep(.25)
            report["checks"].append("real IDE HTTP 200 with token/cookie authentication")
            # Changed credentials must fail without altering the running installation.
            changed = dict(pair, ide_token="f" * 64)
            write(bundle / "machine.json", changed)
            result = subprocess.run([str(x) for x in command], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            require(result.returncode != 0 and (root / "ide-token").read_bytes() == before, "不应覆盖轮换凭据")
            report["checks"].append("changed pairing refused without overwriting credentials")
        finally:
            if ide:
                ide.terminate()
                ide.wait(timeout=15)
            if fixture:
                fixture.shutdown()
                fixture.server_close()
            if (root / "local.json").exists():
                uninstall = ["bash", PACKAGE / "deploy-local.sh", "uninstall", "--root", root, "--cache", cache, "--purge"]
                run(uninstall)
                run(uninstall)
            require(not root.exists(), "卸载未删除专用运行目录")
            require(project.is_dir(), "卸载误删项目")
            require(not list((Path.home() / "Library/LaunchAgents").glob(f"local.remotecode.{name}.*")), "残留测试运行项")
        report["checks"].append("purge and repeated uninstall preserve workspace; no test services remain")
    report["status"] = "PASS"
    report["system_dependencies_modified"] = False
    report["real_model_requests"] = 0
    write(args.report, report, 0o644)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
