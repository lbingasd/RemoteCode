#!/usr/bin/env python3
"""Real native nginx + frp + VS Code Web integration; all listeners are loopback."""
import argparse
import base64
import http.client
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cloud import export_machine, make_pki, password_hash, render
from common import private_dir, read, require, run, write
from local import server_args


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--nginx", type=Path, required=True)
    p.add_argument("--nginx-prefix", type=Path, required=True)
    p.add_argument("--frp-dir", type=Path, required=True)
    p.add_argument("--lock", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--report", type=Path)
    args = p.parse_args()
    report = {"checks": [], "scope": "native macOS loopback; no Docker, iPad or model calls"}
    processes = []
    with tempfile.TemporaryDirectory(prefix="remotecode-stack-") as folder:
        root = Path(folder)
        cloud_root = private_dir(root / "cloud")
        private_dir(cloud_root / "certs")
        make_pki(cloud_root, "127.0.0.1")
        for target, source in (("fullchain.pem", "tunnel.pem"), ("privkey.pem", "tunnel.key")):
            write(cloud_root / "certs" / target, (cloud_root / "certs" / source).read_text())
        edge_port = free_port()
        passwords, machines, configs, homes = {}, [], {}, {}
        for name in ("pc1", "pc2"):
            password = secrets.token_urlsafe(32)
            machine = {"id": name, "bind_port": free_port(), "proxy_port": free_port(),
                       "frp_token": secrets.token_hex(32), "ide_token": secrets.token_hex(32),
                       "password_hash": password_hash(password)}
            machines.append(machine)
            passwords[name] = password
        state = {"ip": "127.0.0.1", "image": "test", "frp_version": read(args.lock)["frp_version"], "machines": machines}
        render(cloud_root, state)
        for machine in machines:
            name = machine["id"]
            export_machine(cloud_root, state, machine, passwords[name])
            bundle = cloud_root / "enrollment" / name
            pair = read(bundle / "machine.json")
            pair["local_port"] = free_port()
            write(bundle / "machine.json", pair)
            home = root / name
            project = root / (name + "-project")
            project.mkdir()
            (project / "README.md").write_text("RemoteCode native integration workspace\n")
            if name == "pc1":
                run([sys.executable, Path(__file__).resolve().parents[1] / "scripts/local.py",
                     "--root", home, "install", "--bundle", bundle, "--lock", args.lock.resolve(),
                     "--cache", args.cache.resolve(), "--workspace", project, "--accept-license"])
            else:
                # Two independent servers share immutable program files only in this fixture.
                private_dir(home)
                private_dir(home / "logs")
                config = dict(configs["pc1"], id=name, local_port=pair["local_port"], workspace=str(project))
                write(home / "local.json", config)
                write(home / "ide-token", pair["ide_token"] + "\n")
                write(home / "ca.pem", (bundle / "ca.pem").read_text())
                from local import frpc_config
                write(home / "frpc.toml", frpc_config(home, pair))
            homes[name] = home
            configs[name] = read(home / "local.json")
            text = (home / "frpc.toml").read_text().replace("serverPort = 443", f"serverPort = {edge_port}")
            write(home / "frpc.toml", text)
        text = (cloud_root / "live/nginx.conf").read_text()
        text = text.replace("user nginx;", "").replace("worker_processes auto;", "worker_processes 1;")
        text = text.replace("listen 443 ssl", f"listen 127.0.0.1:{edge_port} ssl")
        for old, new in (("/state/", str(cloud_root / "live") + "/"), ("/certs/", str(cloud_root / "certs") + "/"),
                         ("/etc/nginx/mime.types", str(args.nginx_prefix.resolve() / "conf/mime.types")),
                         ("/tmp/nginx.pid", str(root / "nginx.pid")), ("/dev/stderr", str(root / "nginx-error.log")),
                         ("/dev/stdout", str(root / "nginx-access.log"))):
            text = text.replace(old, new)
        write(root / "nginx.conf", text)
        (root / "logs").mkdir()
        run([args.nginx.resolve(), "-t", "-p", str(root) + "/", "-c", root / "nginx.conf"])
        report["checks"].append("nginx syntax and TLS files")

        def start(label, command, cwd=None):
            log = (root / (label + ".log")).open("wb")
            process = subprocess.Popen([str(x) for x in command], cwd=cwd, stdout=log, stderr=log)
            processes.append((label, process, log))
            return process

        def auth(name):
            return "Basic " + base64.b64encode(("developer:" + passwords[name]).encode()).decode()

        context = ssl.create_default_context(cafile=str(cloud_root / "pki/root-ca.crt"))
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), urllib.request.HTTPCookieProcessor(jar))
        base = f"https://127.0.0.1:{edge_port}"

        def get(path, name=None, client=opener):
            headers = {"Authorization": auth(name)} if name else {}
            request = urllib.request.Request(base + path, headers=headers)
            try:
                with client.open(request, timeout=10) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        try:
            for machine in machines:
                name = machine["id"]
                start(name + "-ide", server_args(homes[name], configs[name]), cwd=configs[name]["workspace"])
                start(name + "-frps", [args.frp_dir.resolve() / "frps", "-c", cloud_root / "live/frps" / (name + ".toml")])
            start("nginx", [args.nginx.resolve(), "-p", str(root) + "/", "-c", root / "nginx.conf", "-g", "daemon off;"])
            start("pc1-frpc", [args.frp_dir.resolve() / "frpc", "-c", homes["pc1"] / "frpc.toml"])
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                try:
                    status, page = get("/m/pc1/?tkn=" + machines[0]["ide_token"], "pc1")
                    if status == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError("IDE stack did not become ready")
            require(b"vscode-workbench-web-configuration" in page, "未返回真实 VS Code Web 页面")
            report["checks"].append("real IDE through nginx HTTPS + WSS/frp")
            require(get("/m/pc1/")[0] == 401, "Basic Auth 未阻止匿名请求")
            require(get("/m/pc2/", "pc1")[0] == 401, "机器密码未隔离")
            report["checks"].append("anonymous and cross-machine Basic Auth rejected")
            # Wrong device token must not register the other machine's proxy.
            wrong = (homes["pc2"] / "frpc.toml").read_text().replace(machines[1]["frp_token"], machines[0]["frp_token"])
            write(root / "wrong-frpc.toml", wrong)
            bad = start("wrong-frpc", [args.frp_dir.resolve() / "frpc", "-c", root / "wrong-frpc.toml"])
            time.sleep(1)
            require(get("/m/pc2/", "pc2")[0] == 502, "错误 frp token 注册了另一台机器")
            bad.terminate()
            bad.wait(timeout=5)
            start("pc2-frpc", [args.frp_dir.resolve() / "frpc", "-c", homes["pc2"] / "frpc.toml"])
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status, _page = get("/m/pc2/?tkn=" + machines[1]["ide_token"], "pc2")
                if status == 200:
                    break
                time.sleep(0.2)
            require(status == 200 and get("/m/pc1/", "pc1")[0] == 200, "多机器 Cookie/路由切换失败")
            report["checks"].append("per-device frp token and two IDE cookie paths")
            # TLS verification remains mandatory.
            try:
                urllib.request.urlopen(base + "/m/pc1/", timeout=5)
            except urllib.error.URLError as exc:
                require(isinstance(exc.reason, ssl.SSLCertVerificationError), "预期 CA 校验失败")
            else:
                raise ValueError("未受信 CA 被接受")
            report["checks"].append("untrusted TLS CA rejected")
            request = urllib.request.Request(base + "/m/pc1/")
            jar.add_cookie_header(request)
            connection = http.client.HTTPSConnection("127.0.0.1", edge_port, context=context, timeout=5)
            connection.request("GET", "/m/pc1/", headers={"Authorization": auth("pc1"), "Cookie": request.get_header("Cookie", ""),
                "Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode()})
            require(connection.getresponse().status == 101, "真实 IDE WebSocket Upgrade 失败")
            connection.close()
            report["checks"].append("real IDE WebSocket upgrade through full proxy chain")
            run([sys.executable, homes["pc1"] / "local.py", "--root", homes["pc1"], "doctor"])
            report["checks"].append("installed runtime doctor with cookie-aware redirect")
            log = (root / "nginx-access.log").read_text()
            require(all(m["ide_token"] not in log for m in machines), "访问日志记录了 token")
            report["checks"].append("access log omits connection secrets")
            report["status"] = "PASS"
            print(json.dumps(report, ensure_ascii=False, indent=2))
        except BaseException:
            for label, process, log in processes:
                log.flush()
                if process.poll() is not None or "ide" in label:
                    # Test-only logs may contain tokens; do not print bodies to the transcript.
                    print(f"Diagnostic log: {label}, bytes={(root / (label + '.log')).stat().st_size}, exit={process.poll()}")
            raise
        finally:
            for _label, process, log in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                log.close()
            for home in homes.values():
                if (home / "local.py").is_file():
                    run([sys.executable, home / "local.py", "--root", home, "uninstall", "--purge"])
            if args.report:
                write(args.report, report)


if __name__ == "__main__":
    main()
