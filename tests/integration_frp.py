#!/usr/bin/env python3
"""Optional native frp test. The TLS/WebSocket edge is a test proxy, not nginx."""
import argparse
import http.server
from pathlib import Path
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
from cloud import make_pki, render
from common import write
from local import frpc_config


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frp-dir", required=True, type=Path)
    args = parser.parse_args()
    suffix = ".exe" if sys.platform == "win32" else ""
    servers = []
    processes = []
    with tempfile.TemporaryDirectory(prefix="remotecode-frp-integration-") as folder:
        root = Path(folder)
        (root / "certs").mkdir()
        make_pki(root, "127.0.0.1")
        bind_port, proxy_port, home_port, edge_port = [free_port() for _ in range(4)]
        machine = {"id": "pc1", "bind_port": bind_port, "proxy_port": proxy_port,
                   "password_hash": "test", "frp_token": "a" * 64}
        render(root, {"ip": "127.0.0.1", "image": "test", "machines": [machine]})
        pair = {"id": "pc1", "server_ip": "127.0.0.1", "tunnel_name": "pc1.tunnel.invalid",
                "frp_token": "a" * 64, "ide_token": "b" * 64,
                "remote_port": proxy_port, "local_port": home_port}
        write(root / "ca.pem", (root / "pki/root-ca.crt").read_text())
        write(root / "frpc.toml", frpc_config(root, pair).replace("serverPort = 443", f"serverPort = {edge_port}"))
        seen = []

        class Home(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = b"remotecode-home-tool-ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(root / "certs/tunnel.pem", root / "certs/tunnel.key")
        tls.set_servername_callback(lambda _sock, name, _ctx: seen.append(name))

        class Edge(socketserver.BaseRequestHandler):
            def handle(self):
                with tls.wrap_socket(self.request, server_side=True) as incoming:
                    header = b""
                    while b"\r\n\r\n" not in header:
                        data = incoming.recv(4096)
                        if not data:
                            return
                        header += data
                        if len(header) > 16384:
                            return
                    assert header.startswith(b"GET /~!frp HTTP/1.1")
                    assert b"host: pc1.tunnel.invalid" in header.lower()
                    with socket.create_connection(("127.0.0.1", bind_port), timeout=10) as upstream:
                        upstream.settimeout(None)
                        upstream.sendall(header)

                        def relay(source, target):
                            try:
                                while True:
                                    data = source.recv(65536)
                                    if not data:
                                        break
                                    target.sendall(data)
                            except (OSError, ssl.SSLError):
                                pass
                            finally:
                                try:
                                    target.shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass
                        thread = threading.Thread(target=relay, args=(incoming, upstream), daemon=True)
                        thread.start()
                        relay(upstream, incoming)

        class ThreadServer(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            home = http.server.ThreadingHTTPServer(("127.0.0.1", home_port), Home)
            edge = ThreadServer(("127.0.0.1", edge_port), Edge)
            for server in (home, edge):
                servers.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
            for binary, config in (("frps", root / "live/frps/pc1.toml"), ("frpc", root / "frpc.toml")):
                log = (root / f"{binary}.log").open("wb")
                processes.append((subprocess.Popen([str(args.frp_dir.resolve() / (binary + suffix)), "-c", str(config)], stdout=log, stderr=log), log))
                if binary == "frps":
                    time.sleep(0.3)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    # The published proxy endpoint is itself only loopback.
                    with urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/", timeout=1) as response:
                        assert response.read() == b"remotecode-home-tool-ok"
                    assert "pc1.tunnel.invalid" in seen
                    print("PASS: native frpc -> verified TLS + synthetic SNI -> WebSocket edge -> loopback frps -> home HTTP")
                    break
                except (OSError, TimeoutError):
                    time.sleep(0.2)
            else:
                for name in ("frpc", "frps"):
                    print((root / f"{name}.log").read_text(errors="replace"))
                raise RuntimeError("frp integration timed out")
        finally:
            for process, log in processes:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                log.close()
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    main()
