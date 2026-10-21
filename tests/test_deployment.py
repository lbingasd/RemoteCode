"""Targeted checks for the security and routing invariants of generated deployments."""
import io
from pathlib import Path
import ssl
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cloud
import common
import local


class DeploymentTests(unittest.TestCase):
    def test_compose_lifecycle_uses_the_states_own_project(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ("remotecode-one", "remotecode-two"):
                root = Path(folder) / name
                common.write(root / "state.json", {"project": name})
                with patch.object(cloud, "run") as execute:
                    cloud.compose(root, "down", "--remove-orphans")
                command = execute.call_args.args[0]
                self.assertEqual(command[command.index("--project-name") + 1], name)

    def test_cloud_only_publishes_443_and_isolates_each_machine(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = {"ip": "203.0.113.10", "image": "nginx@sha256:" + "1" * 64,
                     "machines": [{"id": "pc1", "bind_port": 20001, "proxy_port": 30001,
                                   "password_hash": "hash", "frp_token": "a" * 64},
                                  {"id": "mac1", "bind_port": 20002, "proxy_port": 30002,
                                   "password_hash": "hash2", "frp_token": "b" * 64}]}
            cloud.render(root, state)
            services = common.read(root / "compose.json")["services"]
            self.assertEqual(services["edge"]["ports"], ["443:443/tcp"])
            for machine in state["machines"]:
                service = services["frps-" + machine["id"]]
                self.assertNotIn("ports", service)
                self.assertEqual(service["network_mode"], "service:edge")
                config = tomllib.loads((root / "live/frps" / (machine["id"] + ".toml")).read_text())
                self.assertEqual(config["bindAddr"], "127.0.0.1")
                self.assertEqual(config["proxyBindAddr"], "127.0.0.1")
                self.assertEqual(config["allowPorts"], [{"single": machine["proxy_port"]}])
                self.assertEqual(config["auth"]["token"], machine["frp_token"])
            nginx = (root / "live/nginx.conf").read_text()
            self.assertIn("location /m/pc1/", nginx)
            self.assertIn("proxy_cookie_path ~^/$ /m/mac1/", nginx)
            self.assertIn("server_name pc1.tunnel.invalid", nginx)
            self.assertNotIn("$request_uri", nginx)
            self.assertNotIn("$http_authorization", nginx)
            self.assertEqual(nginx.count("{"), nginx.count("}"))

    def test_frpc_dials_ip_but_authenticates_synthetic_sni(self):
        pair = {"id": "pc1", "server_ip": "203.0.113.10", "tunnel_name": "pc1.tunnel.invalid",
                "frp_token": "a" * 64, "ide_token": "b" * 64,
                "local_port": 18080, "remote_port": 30001}
        local.validate_pair(pair)
        config = tomllib.loads(local.frpc_config(Path("/tmp/with spaces"), pair))
        self.assertEqual(config["serverAddr"], pair["server_ip"])
        self.assertEqual(config["serverPort"], 443)
        self.assertEqual(config["transport"]["protocol"], "wss")
        self.assertEqual(config["transport"]["tls"]["serverName"], pair["tunnel_name"])
        self.assertIn("trustedCaFile", config["transport"]["tls"])
        self.assertEqual(config["proxies"][0]["localIP"], "127.0.0.1")
        self.assertFalse(config["loginFailExit"])
        pair["frp_token"] += '\n[[proxies]]'
        with self.assertRaises(ValueError):
            local.validate_pair(pair)

    def test_ide_uses_token_file_and_matching_base_path(self):
        root = Path("/tmp/runtime path")
        config = {"server_dir": str(root / "server"), "local_port": 18080,
                  "id": "pc1", "workspace": "/tmp/project"}
        args = local.server_args(root, config)
        self.assertEqual(args[args.index("--host") + 1], "127.0.0.1")
        self.assertEqual(args[args.index("--server-base-path") + 1], "/m/pc1")
        self.assertIn("--connection-token-file", args)
        self.assertNotIn("--without-connection-token", args)
        self.assertNotIn("--disable-workspace-trust", args)

    def test_archive_traversal_and_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "bad.tar.gz"
            for name, kind in (("../outside", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
                with tarfile.open(archive, "w:gz") as stream:
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    item.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
                    stream.addfile(item)
                with self.assertRaises(ValueError):
                    common.extract(archive, root / "out")
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("..\\outside", "no")
            with self.assertRaises(ValueError):
                common.extract(archive, root / "out")
            self.assertFalse((root / "outside").exists())

    def test_download_never_accepts_tampered_cached_or_mirrored_bytes(self):
        from contextlib import contextmanager
        @contextmanager
        def source(_url, _headers=None):
            yield common.HTTPResult(io.BytesIO(b"tampered"), 200, {"Content-Length": "8"})
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "asset"
            dest.write_bytes(b"stale")
            with patch.object(common, "request", source), patch.object(common.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    common.download("https://official.example/file", dest, "0" * 64,
                                    "https://mirror.example/{url}")
            self.assertEqual(dest.read_bytes(), b"stale")
            self.assertFalse(dest.with_name("asset.part").exists())

    def test_personal_ca_is_ip_valid_and_sni_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "certs").mkdir()
            cloud.make_pki(root, "203.0.113.10")
            cert = ssl._ssl._test_decode_cert(str(root / "certs/tunnel.pem"))
            self.assertIn(("IP Address", "203.0.113.10"), cert["subjectAltName"])
            self.assertIn(("DNS", "*.tunnel.invalid"), cert["subjectAltName"])
            verified = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            verified.load_cert_chain(root / "certs/tunnel.pem", root / "certs/tunnel.key")

    def test_failed_renewal_preserves_working_certificate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "certs").mkdir()
            cloud.make_pki(root, "127.0.0.1")
            cert_before = (root / "certs/tunnel.pem").read_bytes()
            key_before = (root / "certs/tunnel.key").read_bytes()
            (root / "pki/root-ca.key").unlink()
            with self.assertRaises(ValueError):
                cloud.issue_certificate(root, "127.0.0.1")
            self.assertEqual((root / "certs/tunnel.pem").read_bytes(), cert_before)
            self.assertEqual((root / "certs/tunnel.key").read_bytes(), key_before)


if __name__ == "__main__":
    unittest.main()
