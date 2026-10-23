#!/usr/bin/env python3
"""Install the home-machine runtime; never bind an IDE or SSH service to the LAN."""
import argparse
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import urllib.request
import zipfile
from common import extract, identifier, private_dir, read, require, run, sha256, write
from webview_compat import apply_server


def host_platform():
    system = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    arch = {"x86_64": "amd64", "AMD64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine())
    require(system and arch, "仅支持 Windows/macOS/Linux x64 或 ARM64")
    return f"{system}-{arch}"


def validate_pair(pair):
    identifier(pair["id"])
    require(ipaddress.ip_address(pair["server_ip"]).version == 4, "需要 IPv4")
    require(pair["tunnel_name"] == f"{pair['id']}.tunnel.invalid", "SNI 名称不合法")
    if "url" in pair:
        require(pair["url"] == f"https://{pair['server_ip']}/m/{pair['id']}/", "访问 URL 与机器配置不匹配")
    for key in ("frp_token", "ide_token"):
        require(re.fullmatch(r"[0-9a-f]{64}", pair[key]), "配对密钥格式不合法")
    for key in ("local_port", "remote_port"):
        require(type(pair[key]) is int and 1024 < pair[key] < 65536, "端口不合法")


def frpc_config(root, pair):
    ca_path = (root / "ca.pem").as_posix()
    return f"""serverAddr = "{pair['server_ip']}"
serverPort = 443
loginFailExit = false
user = "{pair['id']}"
auth.method = "token"
auth.token = "{pair['frp_token']}"
auth.additionalScopes = ["HeartBeats", "NewWorkConns"]
transport.protocol = "wss"
transport.tls.enable = true
transport.tls.serverName = "{pair['tunnel_name']}"
transport.tls.trustedCaFile = {json.dumps(ca_path, ensure_ascii=False)}
transport.tcpMux = true
log.to = "console"
log.level = "warn"

[[proxies]]
name = "ide"
type = "tcp"
localIP = "127.0.0.1"
localPort = {pair['local_port']}
remotePort = {pair['remote_port']}
"""


def server_args(root, config, windows=None):
    server = Path(config["server_dir"])
    windows = os.name == "nt" if windows is None else windows
    node = server / ("node.exe" if windows else "node")
    return [str(node), str(server / "out/server-main.js"),
            "--host", "127.0.0.1", "--port", str(config["local_port"]),
            "--server-base-path", f"/m/{config['id']}",
            "--connection-token-file", str(root / "ide-token"),
            "--server-data-dir", str(root / "data"),
            "--extensions-dir", str(root / "extensions"),
            "--default-folder", config["workspace"],
            "--disable-telemetry", "--accept-server-license-terms"]


def write_windows_task(root, config, target=None):
    command = server_args(root, config, windows=True)
    write((target or root) / "windows-task.json", {
        "executable": command[0], "arguments": subprocess.list2cmdline(command[1:]),
        "working_directory": config["workspace"]})


def restrict_windows(root):
    if os.name == "nt":
        # chmod does not implement Windows ACLs. Keep profile secrets private on NTFS.
        sid = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True).strip().split(",")[-1].strip('"')
        require(re.fullmatch(r"S-1-[0-9-]+", sid), "无法读取当前 Windows SID")
        # Recursive targets need an effective ACE on each existing file. Applying
        # only (OI)(CI) to files leaves them with no usable permissions on Windows.
        run(["icacls", root, "/inheritance:r", "/grant:r", f"*{sid}:F",
             "*S-1-5-18:F", "*S-1-5-32-544:F", "/T", "/Q"], stdout=subprocess.DEVNULL)
        # Keep inheritable ACEs on the runtime root for files created later.
        run(["icacls", root, "/grant:r", f"*{sid}:(OI)(CI)F",
             "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "/Q"], stdout=subprocess.DEVNULL)
        return sid


def install_runtime(root, bundle, lock, cache, workspace, pair):
    """Stage all files and verify binaries before atomically publishing the runtime."""
    require(not root.exists() or not any(root.iterdir()), "安装目录必须为空；不会覆盖或接管已有目录")
    ssl.create_default_context(cafile=str(bundle / "ca.pem"))
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".remotecode-install-", dir=root.parent) as temp:
        stage = private_dir(Path(temp) / "runtime")
        restrict_windows(stage)
        private_dir(stage / "bin")
        private_dir(stage / "logs")
        frp_dir = Path(temp) / "frp"
        extract(cache / lock["files"]["frp"]["name"], frp_dir)
        binary, = frp_dir.rglob("frpc.exe" if os.name == "nt" else "frpc")
        shutil.copy2(binary, stage / "bin" / binary.name)
        (stage / "bin" / binary.name).chmod(0o755)
        web_dir = Path(temp) / "web"
        extract(cache / lock["files"]["vscode_web"]["name"], web_dir)
        server_main, = web_dir.rglob("out/server-main.js")
        shutil.move(str(server_main.parent.parent), stage / "server")
        node = stage / "server" / ("node.exe" if os.name == "nt" else "node")
        require(node.is_file(), "Web Server 包中缺少匹配平台的 Node")
        node.chmod(0o755)
        run([node, "--version"], stdout=subprocess.DEVNULL)
        apply_server(stage / "server")
        write(stage / "ca.pem", (bundle / "ca.pem").read_text())
        write(stage / "ide-token", pair["ide_token"] + "\n")
        write(stage / "frpc.toml", frpc_config(stage, pair))
        run([stage / "bin" / binary.name, "verify", "-c", stage / "frpc.toml"])
        write(stage / "frpc.toml", frpc_config(root, pair))
        config = {"schema": 1, "id": pair["id"], "local_port": pair["local_port"],
                  "url": pair["url"], "workspace": str(workspace),
                  "server_dir": str(root / "server"), "platform": lock["platform"],
                  "frp_version": lock["frp_version"], "server_commit": lock["server_commit"],
                  "browser_user": pair["browser_user"], "browser_password": pair["browser_password"]}
        write(stage / "local.json", config)
        write(stage / "download.lock.json", lock)
        write_windows_task(root, config, target=stage)
        for filename in ("local.py", "common.py", "webview_compat.py"):
            shutil.copy2(Path(__file__).parent / filename, stage / filename)
        # Recheck immediately before replacing an empty target; never merge with user data.
        if root.exists():
            require(not any(root.iterdir()), "安装期间目标目录已被写入，拒绝覆盖")
            root.rmdir()
        stage.replace(root)
    sid = restrict_windows(root)
    if sid:
        config["owner_sid"] = sid
        write(root / "local.json", config)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / "RemoteCode")
    subs = parser.add_subparsers(dest="action", required=True)
    install = subs.add_parser("install")
    install.add_argument("--bundle", required=True, type=Path)
    install.add_argument("--lock", required=True, type=Path)
    install.add_argument("--cache", type=Path, default=Path("downloads"))
    install.add_argument("--workspace", required=True, type=Path)
    install.add_argument("--accept-license", action="store_true", help="阅读并接受微软个人 VS Code Server 许可")
    pair = subs.add_parser("pair")
    pair.add_argument("--bundle", required=True, type=Path, help="云端 rotate 后停机更新凭据")
    subs.add_parser("run-ide")
    subs.add_parser("run-frpc")
    subs.add_parser("doctor")
    url = subs.add_parser("url")
    url.add_argument("--show-secrets", action="store_true", help="显式在本机终端显示初次访问链接和浏览器密码")
    vsix = subs.add_parser("install-extension")
    vsix.add_argument("--vsix", required=True, type=Path)
    vsix.add_argument("--sha256", required=True)
    uninstall = subs.add_parser("uninstall")
    uninstall.add_argument("--purge", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    require(root not in (Path.home(), Path.cwd(), Path(root.anchor)), "root 必须是专用安装子目录")
    configfile = root / "local.json"
    if args.action == "install":
        require(args.accept_license, "请阅读 https://code.visualstudio.com/license/server 后自行添加 --accept-license；仅个人使用")
        require(not configfile.exists(), "已安装。换密钥用 pair；升级先卸载运行项并备份，使用新的安装目录验证")
        workspace = args.workspace.resolve()
        require(workspace.is_dir(), "工作目录必须预先存在")
        require(not workspace.is_relative_to(root) and not root.is_relative_to(workspace),
                "工作区与安装目录必须彼此独立，避免误删项目或暴露凭据")
        bundle = args.bundle.resolve()
        pair = read(bundle / "machine.json")
        validate_pair(pair)
        lock = read(args.lock)
        require(lock["platform"] == host_platform(), "下载 lock 与本机系统/架构不匹配")
        require(pair["frp_version"] == lock["frp_version"], "frpc/frps 需要相同版本")
        for key in ("frp", "vscode_web"):
            item = lock["files"][key]
            require(Path(item["name"]).name == item["name"], "缓存文件名不合法")
            archive = args.cache / item["name"]
            require(archive.is_file() and sha256(archive) == item["sha256"], "缓存缺失或损坏，请先 download.py fetch")
        install_runtime(root, bundle, lock, args.cache, workspace, pair)
        print(f"安装完成: {root}。先以前台 run-ide/run-frpc 验证，再注册开机运行项。")
        return
    require(configfile.is_file(), "请先 install")
    config = read(configfile)
    if args.action == "pair":
        pair = read(args.bundle / "machine.json")
        validate_pair(pair)
        require(pair["id"] == config["id"] and pair["frp_version"] == config["frp_version"], "不能将其他机器或 frp 版本配对到此安装")
        write(root / "ca.pem", (args.bundle / "ca.pem").read_text())
        write(root / "ide-token", pair["ide_token"] + "\n")
        write(root / "frpc.toml", frpc_config(root, pair))
        config.update(local_port=pair["local_port"], url=pair["url"], browser_user=pair["browser_user"], browser_password=pair["browser_password"])
        write(configfile, config)
        write_windows_task(root, config)
        restrict_windows(root)
        print("配对已更新，请重启本机 IDE 和 frpc。")
    elif args.action == "run-ide":
        # Use the developer account, never LocalSystem/Administrator.
        run(server_args(root, config), cwd=config["workspace"])
    elif args.action == "run-frpc":
        run([root / "bin" / ("frpc.exe" if os.name == "nt" else "frpc"), "-c", root / "frpc.toml"], cwd=root)
    elif args.action == "install-extension":
        require(args.vsix.is_file() and sha256(args.vsix) == args.sha256.lower(), "VSIX SHA-256 不匹配")
        require(zipfile.is_zipfile(args.vsix), "VSIX 必须是 ZIP；请检查是否误存了 HTTP gzip 压缩层或截断文件")
        server = Path(config["server_dir"])
        run([server / ("node.exe" if os.name == "nt" else "node"), server / "out/server-main.js",
             "--install-extension", args.vsix.resolve(), "--extensions-dir", root / "extensions",
             "--server-data-dir", root / "data", "--accept-server-license-terms"])
    elif args.action == "url":
        print(config["url"])
        if args.show_secrets:
            print("首次访问:", config["url"] + "?tkn=" + (root / "ide-token").read_text().strip())
            print("Basic Auth:", config["browser_user"], config["browser_password"])
    elif args.action == "doctor":
        frpc = root / "bin" / ("frpc.exe" if os.name == "nt" else "frpc")
        run([frpc, "verify", "-c", root / "frpc.toml"])
        import tomllib
        transport = tomllib.loads((root / "frpc.toml").read_text())
        context = ssl.create_default_context(cafile=str(root / "ca.pem"))
        with socket.create_connection((transport["serverAddr"], transport["serverPort"]), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=transport["transport"]["tls"]["serverName"]) as tls:
                print("云端 TLS 证书校验成功:", tls.version())
        url = f"http://127.0.0.1:{config['local_port']}/m/{config['id']}/?tkn=" + (root / "ide-token").read_text().strip()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with opener.open(url, timeout=10) as response:
            require(response.status == 200, "本机 IDE HTTP 状态异常")
            print("本机 IDE HTTP 访问成功。仍需在 iPad 验证终端、ACP、断线恢复。")
    elif args.action == "uninstall":
        # Refuse deletion while the local IDE port is still active.
        with socket.socket() as sock:
            active = sock.connect_ex(("127.0.0.1", config["local_port"])) == 0
        require(not active, "先移除自动运行项并停止 IDE/frpc，再卸载")
        if args.purge:
            shutil.rmtree(root)
            print("已删除专用运行目录。项目、用户现有 .codex/.claude、Node 和 Git 均保留。")
        else:
            print("无系统级安装文件；停止自动运行项后即停用。--purge 删除此专用运行目录。")


if __name__ == "__main__":
    main()
