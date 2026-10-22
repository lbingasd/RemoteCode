#!/usr/bin/env python3
"""Manage a private RemoteCode gateway with Linux Docker Compose containers."""
import argparse
import hashlib
import ipaddress
from datetime import datetime, timezone
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
from common import extract, identifier, private_dir, read, require, run, sha256, write
from container_images import pull_image
from cloud_ports import preflight_port


def compose(root, *args, **kwargs):
    project = read(root / "state.json").get("project", "remotecode")
    return run(["docker", "compose", "--project-name", project, "-f", root / "compose.json", *args], **kwargs)


def password_hash(password):
    # APR1 is supported by nginx on Linux/macOS. Passwords are random 256-bit values.
    return subprocess.check_output(["openssl", "passwd", "-apr1", "-stdin"],
                                   input=(password + "\n").encode()).decode().strip()


def nginx_server(machine):
    name = machine["id"]
    return f"""
    location = /m/{name} {{ return 308 /m/{name}/; }}
    location /m/{name}/ {{
        auth_basic "RemoteCode-{name}";
        auth_basic_user_file /state/auth/{name};
        limit_req zone=web burst=60 nodelay;
        proxy_pass http://127.0.0.1:{machine['proxy_port']};
        include /state/proxy.conf;
        proxy_cookie_path ~^/$ /m/{name}/;
    }}
"""


def nginx_tunnel(machine):
    name = machine["id"]
    return f"""
server {{
    listen 443 ssl;
    # Synthetic SNI name: frpc dials the IP directly; this never needs DNS.
    server_name {name}.tunnel.invalid;
    ssl_certificate /certs/tunnel.pem;
    ssl_certificate_key /certs/tunnel.key;
    location = /~!frp {{
        limit_req zone=tunnel burst=30 nodelay;
        proxy_pass http://127.0.0.1:{machine['bind_port']};
        include /state/proxy.conf;
    }}
    location / {{ return 404; }}
}}
"""


def render(root, state):
    live = private_dir(root / "live")
    live.chmod(0o755)  # nginx worker must traverse auth files; root itself remains 0700.
    # Mounted files are readable by the dedicated unprivileged frps uid inside containers.
    # The parent state directory is 0700 on the host and is never mounted wholesale.
    for directory in [live / "auth", live / "frps"]:
        directory.mkdir(exist_ok=True)
        directory.chmod(0o755)
    proxy = """proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection $connection_upgrade;
proxy_set_header Host $http_host;
proxy_set_header X-Forwarded-Proto https;
proxy_set_header X-Forwarded-Host $http_host;
proxy_set_header X-Original-Host "";
proxy_set_header X-Forwarded-Prefix "";
proxy_set_header X-Forwarded-Port "";
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header Authorization "";
proxy_read_timeout 86400s;
proxy_send_timeout 86400s;
proxy_buffering off;
"""
    write(live / "proxy.conf", proxy, 0o644)
    nginx = """user nginx;
worker_processes auto;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 4096; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    server_tokens off;
    # Do not log query strings, cookies or Authorization headers.
    log_format safe '$remote_addr $host $request_method $uri $status';
    access_log /dev/stdout safe;
    map $http_upgrade $connection_upgrade { default upgrade; '' close; }
    limit_req_zone $binary_remote_addr zone=web:10m rate=60r/s;
    limit_req_zone $binary_remote_addr zone=tunnel:10m rate=10r/s;
    ssl_certificate /certs/fullchain.pem;
    ssl_certificate_key /certs/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_tickets off;
    client_max_body_size 100m;
    add_header Strict-Transport-Security "max-age=31536000" always;
"""
    web = "    server {\n        listen 443 ssl default_server;\n        server_name _;\n"
    services = {"edge": {
        "image": state["image"], "entrypoint": ["nginx"],
        "command": ["-g", "daemon off;"], "restart": "unless-stopped",
        "ports": ["443:443/tcp"],
        "volumes": ["./live:/state:ro", "./certs:/certs:ro"],
        "read_only": True, "tmpfs": ["/tmp", "/var/cache/nginx", "/var/run"],
        "security_opt": ["no-new-privileges:true"],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        "healthcheck": {"test": ["CMD", "nginx", "-t", "-c", "/state/nginx.conf"], "interval": "30s", "timeout": "5s", "retries": 3}}}
    services["edge"]["command"] += ["-c", "/state/nginx.conf"]
    for machine in state["machines"]:
        name = machine["id"]
        write(live / "auth" / name, f"developer:{machine['password_hash']}\n", 0o644)
        frps = f"""bindAddr = "127.0.0.1"
bindPort = {machine['bind_port']}
proxyBindAddr = "127.0.0.1"
allowPorts = [{{single = {machine['proxy_port']}}}]
maxPortsPerClient = 1
auth.method = "token"
auth.token = "{machine['frp_token']}"
auth.additionalScopes = ["HeartBeats", "NewWorkConns"]
# TLS terminates at nginx; frps only accepts loopback WebSocket traffic.
transport.tls.force = false
log.to = "console"
log.level = "warn"
"""
        write(live / "frps" / f"{name}.toml", frps, 0o644)
        web += nginx_server(machine)
        nginx += nginx_tunnel(machine)
        services[f"frps-{name}"] = {
            "image": state["image"], "entrypoint": ["/frps"],
            "command": ["-c", f"/configs/{name}.toml"],
            "network_mode": "service:edge", "depends_on": ["edge"],
            "volumes": ["./bin/frps:/frps:ro", "./live/frps:/configs:ro"],
            "user": "65534:65534", "cap_drop": ["ALL"],
            "read_only": True, "security_opt": ["no-new-privileges:true"],
            "restart": "unless-stopped",
            "logging": {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}}}
    nginx += web + "        location / { return 404; }\n    }\n}\n"
    write(live / "nginx.conf", nginx, 0o644)
    write(root / "compose.json", {"name": state.get("project", "remotecode"), "services": services})


def export_machine(root, state, machine, password):
    target = private_dir(root / "enrollment" / machine["id"])
    write(target / "ca.pem", (root / "pki/root-ca.crt").read_text())
    write(target / "machine.json", {
        "schema": 1, "id": machine["id"],
        "url": f"https://{state['ip']}/m/{machine['id']}/",
        "server_ip": state["ip"],
        "tunnel_name": f"{machine['id']}.tunnel.invalid",
        "frp_version": state["frp_version"], "frp_token": machine["frp_token"],
        "remote_port": machine["proxy_port"], "local_port": 18080,
        "ide_token": machine["ide_token"], "browser_user": "developer",
        "browser_password": password})
    print(f"配对目录（包含密钥，请安全复制到该家庭主机）: {target}")


def make_pki(root, address):
    pki = private_dir(root / "pki")
    run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes",
         "-days", "3650", "-subj", "/CN=RemoteCode Personal Root CA",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign",
         "-keyout", pki / "root-ca.key", "-out", pki / "root-ca.crt"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    issue_certificate(root, address)
    (pki / "root-ca.key").chmod(0o600)
    print("个人 CA 已生成；请安全传送 root-ca.crt 到 iPad 并安装、开启完全信任。")


def issue_certificate(root, address):
    pki = root / "pki"
    require((pki / "root-ca.key").is_file() and (pki / "root-ca.crt").is_file(), "CA 资料不完整；保留现有叶子证书，不执行替换")
    # .invalid is reserved; these SANs select device-specific WSS routes without DNS.
    with tempfile.TemporaryDirectory(prefix="leaf-", dir=pki) as folder:
        stage = Path(folder)
        write(stage / "leaf.ext", f"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:{address},DNS:*.tunnel.invalid\n")
        run(["openssl", "req", "-new", "-newkey", "rsa:3072", "-nodes",
             "-subj", "/CN=RemoteCode Gateway", "-keyout", stage / "leaf.key",
             "-out", stage / "leaf.csr"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run(["openssl", "x509", "-req", "-in", stage / "leaf.csr", "-CA", pki / "root-ca.crt",
             "-CAkey", pki / "root-ca.key", "-set_serial", "0x" + secrets.token_hex(16),
             "-days", "365", "-sha256", "-extfile", stage / "leaf.ext", "-out", stage / "leaf.pem"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        validate_certificate(stage / "leaf.pem", stage / "leaf.key", address)
        write(root / "certs/tunnel.pem", (stage / "leaf.pem").read_text())
        write(root / "certs/tunnel.key", (stage / "leaf.key").read_text())


def validate_certificate(cert, key, address):
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    run(["openssl", "x509", "-in", cert, "-noout", "-checkip", address],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run(["openssl", "x509", "-in", cert, "-noout", "-checkend", "0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("state/cloud"))
    subs = parser.add_subparsers(dest="action", required=True)
    init = subs.add_parser("init")
    init.add_argument("--ip", required=True, help="固定公网 IPv4；浏览器无需域名")
    init.add_argument("--cert", type=Path, help="可选：公网 IP 的受信任证书；省略则使用个人 CA")
    init.add_argument("--key", type=Path)
    init.add_argument("--image", default="nginx:stable-alpine", help="pull 后锁定 digest；可指定自己的镜像仓库")
    init.add_argument("--lock", required=True, type=Path, help="云端 Linux 平台下载 lock")
    init.add_argument("--cache", type=Path, default=Path("downloads"))
    for action in ("add", "remove", "rotate"):
        p = subs.add_parser(action)
        p.add_argument("id", type=identifier)
    subs.add_parser("list")
    pull = subs.add_parser("pull")
    pull.add_argument("--image", help="新镜像标签或自有仓库引用；解析后固定到 digest")
    pull.add_argument("--offline", action="store_true", help="只检查已 docker load 的镜像并锁定 digest")
    pull.add_argument("--cn", action="store_true", help="默认 nginx 按官方 digest 从多个国内源拉取，失败切换")
    subs.add_parser("up")
    subs.add_parser("down")
    subs.add_parser("status")
    subs.add_parser("check")
    subs.add_parser("renew-private-cert")
    renew = subs.add_parser("renew-cert")
    renew.add_argument("--cert", required=True, type=Path)
    renew.add_argument("--key", required=True, type=Path)
    uninstall = subs.add_parser("uninstall")
    uninstall.add_argument("--purge", action="store_true", help="还会删除此 state 内的密钥、配对与证书")
    args = parser.parse_args()
    root = args.state.resolve()
    require(root not in (Path(root.anchor), Path.home(), Path.cwd()), "state 必须是独立子目录")
    statefile = root / "state.json"
    if args.action == "init":
        require(not statefile.exists(), "已初始化；新增机器请用 add")
        require(not root.exists() or not any(root.iterdir()), "初始化目录必须为空，避免覆盖已有文件")
        address = ipaddress.ip_address(args.ip)
        require(address.version == 4, "此脚本当前实现 IPv4；IPv6 需要补充 URL 括号和 Docker 网络验收")
        require(bool(args.cert) == bool(args.key), "--cert 和 --key 必须同时提供")
        if args.cert:
            validate_certificate(args.cert, args.key, str(address))
        lock = read(args.lock)
        require(lock["platform"] in ("linux-amd64", "linux-arm64"), "云端容器必须使用 Linux frps")
        item = lock["files"]["frp"]
        require(Path(item["name"]).name == item["name"], "缓存文件名不合法")
        archive = args.cache / item["name"]
        require(archive.is_file() and sha256(archive) == item["sha256"], "先用 download.py fetch 获取并校验云端 frp")
        docker_arch = subprocess.check_output(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"], text=True).strip()
        arch = "arm64" if "aarch64" in docker_arch or "arm64" in docker_arch else "amd64"
        require(docker_arch.startswith("linux/") and lock["platform"] == f"linux-{arch}",
                "Docker 必须使用匹配架构的 Linux 容器；Windows Server 请使用 Linux VM")
        require(args.image and not args.image.startswith("-"), "镜像名不合法")
        root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".remotecode-cloud-", dir=root.parent) as temp:
            stage = private_dir(Path(temp) / "state")
            private_dir(stage / "certs")
            private_dir(stage / "bin")
            make_pki(stage, str(address))
            write(stage / "certs/fullchain.pem", args.cert.read_text() if args.cert else (stage / "certs/tunnel.pem").read_text())
            write(stage / "certs/privkey.pem", args.key.read_text() if args.key else (stage / "certs/tunnel.key").read_text())
            unpacked = Path(temp) / "unpacked"
            extract(archive, unpacked)
            binary, = unpacked.rglob("frps")
            shutil.copy2(binary, stage / "bin/frps")
            (stage / "bin/frps").chmod(0o755)
            state = {"schema": 1, "ip": str(address), "private_web_cert": not bool(args.cert), "image": args.image, "image_source": args.image,
                     "project": "remotecode-" + hashlib.sha256(str(root).encode()).hexdigest()[:12],
                     "frp_version": lock["frp_version"], "machines": [], "next_slot": 1,
                     "created_at": datetime.now(timezone.utc).isoformat()}
            write(stage / "state.json", state)
            render(stage, state)
            if root.exists():
                require(not any(root.iterdir()), "初始化期间目标目录被写入，拒绝覆盖")
                root.rmdir()
            stage.replace(root)
        print("初始化完成。下一步 add，然后 pull、check、up。仅发布 TCP 443。")
        return
    require(statefile.is_file(), "请先 init")
    state = read(statefile)
    if args.action in ("add", "remove", "rotate"):
        found = next((m for m in state["machines"] if m["id"] == args.id), None)
        if args.action == "add":
            require(found is None, "机器已存在；更换密钥请用 rotate")
            slot = state["next_slot"]
            require(slot < 1000, "实例数量达到脚本上限")
            state["next_slot"] += 1
            found = {"id": args.id, "bind_port": 20000 + slot, "proxy_port": 30000 + slot}
            state["machines"].append(found)
        else:
            require(found, "机器不存在")
        if args.action == "remove":
            # Stop the old authenticated control session before removing its route.
            compose(root, "rm", "--stop", "--force", f"frps-{args.id}")
            state["machines"].remove(found)
            shutil.rmtree(root / "enrollment" / args.id, ignore_errors=True)
            for path in (root / "live/auth" / args.id, root / "live/frps" / f"{args.id}.toml"):
                path.unlink(missing_ok=True)
        else:
            password = secrets.token_urlsafe(32)
            found.update(frp_token=secrets.token_hex(32), ide_token=secrets.token_hex(32),
                         password_hash=password_hash(password))
            export_machine(root, state, found, password)
            if args.action == "rotate":
                compose(root, "rm", "--stop", "--force", f"frps-{args.id}")
        write(statefile, state)
        render(root, state)
        print("配置已生成；运行 up 应用（会短暂断开所有浏览器连接）。rotate 后须重新配对家庭主机。")
    elif args.action == "list":
        for machine in state["machines"]:
            print(f"{machine['id']}  https://{state['ip']}/m/{machine['id']}/")
    elif args.action == "pull":
        image = args.image or state["image"]
        source = args.image or state.get("image_source", image)
        resolved = pull_image(image, source=source, cn=args.cn, offline=args.offline)
        state["image"] = resolved
        state["image_source"] = source
        write(statefile, state)
        render(root, state)
        print("镜像已固定到 digest。离线云端可在其他机器 docker save/load 同一镜像。")
    elif args.action in ("check", "up"):
        require("@sha256:" in state["image"], "先 pull 将镜像锁定到 digest")
        if args.action == "up":
            preflight_port(root, state)
        compose(root, "config", "--quiet")
        compose(root, "run", "--rm", "--no-deps", "edge", "-t", "-c", "/state/nginx.conf")
        # A throwaway container verifies every frps config without opening any public ports.
        for machine in state["machines"]:
            run(["docker", "run", "--rm", "--entrypoint", "/frps",
                 "-v", f"{root / 'bin/frps'}:/frps:ro",
                 "-v", f"{root / 'live/frps'}:/configs:ro", state["image"],
                 "verify", "-c", f"/configs/{machine['id']}.toml"])
        if args.action == "up":
            preflight_port(root, state)
            # Force recreation also tears down already-upgraded websocket sessions after revocation.
            compose(root, "up", "-d", "--force-recreate", "--remove-orphans")
    elif args.action == "renew-cert":
        validate_certificate(args.cert, args.key, state["ip"])
        write(root / "certs/fullchain.pem", args.cert.read_text())
        write(root / "certs/privkey.pem", args.key.read_text())
        state["private_web_cert"] = False
        write(statefile, state)
        compose(root, "exec", "-T", "edge", "nginx", "-t", "-c", "/state/nginx.conf")
        compose(root, "exec", "-T", "edge", "nginx", "-s", "reload", "-c", "/state/nginx.conf")
    elif args.action == "renew-private-cert":
        issue_certificate(root, state["ip"])
        if state["private_web_cert"]:
            write(root / "certs/fullchain.pem", (root / "certs/tunnel.pem").read_text())
            write(root / "certs/privkey.pem", (root / "certs/tunnel.key").read_text())
        compose(root, "exec", "-T", "edge", "nginx", "-t", "-c", "/state/nginx.conf")
        compose(root, "exec", "-T", "edge", "nginx", "-s", "reload", "-c", "/state/nginx.conf")
    elif args.action == "status":
        compose(root, "ps")
    elif args.action == "down":
        compose(root, "down", "--remove-orphans")
    elif args.action == "uninstall":
        compose(root, "down", "--remove-orphans")
        if args.purge:
            shutil.rmtree(root)
        else:
            print("已停止并删除本项目容器；state 保留用于恢复。彻底删除请追加 --purge。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print("云端操作未完成: " + str(exc), file=sys.stderr)
        sys.exit(1)
