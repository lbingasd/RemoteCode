"""Detect port conflicts early without stopping services or blocking own containers."""
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cloud
import cloud_ports as ports
import common
import setup


class CloudPortTests(unittest.TestCase):
    def test_published_port_parser_covers_ipv4_ipv6_ranges_and_tcp_only(self):
        for value in ("0.0.0.0:443->443/tcp", "[::]:443->8443/tcp", ":::443->443/tcp",
                      "127.0.0.1:440-445->840-845/tcp", "80/tcp, 0.0.0.0:443->443/tcp, [::]:443->443/tcp"):
            self.assertTrue(ports.publishes(value, 443), value)
        for value in ("443/tcp", "443/udp", "0.0.0.0:443->443/udp", "0.0.0.0:8443->443/tcp"):
            self.assertFalse(ports.publishes(value, 443), value)

    def test_own_running_edge_is_allowed_without_binding_or_stopping_it(self):
        root = Path("/tmp/own-state").resolve()
        row = {"id": "123", "name": "own-edge", "ports": "0.0.0.0:443->443/tcp",
               "project": "own", "service": "edge", "workdir": str(root)}
        with patch.object(ports.subprocess, "check_output", return_value=json.dumps(row)), patch.object(ports.socket, "socket") as bind, patch.object(ports.subprocess, "run") as execute:
            ports.preflight_port(root, {"project": "own"})
            bind.assert_not_called()
            execute.assert_not_called()

    def test_foreign_container_blocked_even_when_no_host_proxy_socket_exists(self):
        row = {"id": "123", "name": "other-edge", "ports": "0.0.0.0:443->8443/tcp", "project": "other"}
        with patch.object(ports.subprocess, "check_output", return_value=json.dumps(row)), patch.object(ports.socket, "socket") as bind, patch.object(ports.subprocess, "run") as execute:
            with self.assertRaisesRegex(ValueError, "other-edge"):
                ports.preflight_port(Path("/tmp/own-state"), {"project": "own"})
            bind.assert_not_called()
            execute.assert_not_called()

    def test_same_project_from_another_state_or_service_is_not_treated_as_own(self):
        root = Path("/tmp/own-state").resolve()
        for service, workdir in (("edge", "/another/state"), ("frps-mac1", str(root))):
            row = {"id": "123", "name": "foreign", "ports": "0.0.0.0:443->443/tcp",
                   "project": "own", "service": service, "workdir": workdir}
            with self.subTest(service=service), patch.object(ports.subprocess, "check_output", return_value=json.dumps(row)):
                with self.assertRaises(ValueError):
                    ports.preflight_port(root, {"project": "own"})

    @unittest.skipIf(os.name == "nt", "Linux/Mac socket binding behavior")
    def test_real_listener_is_reported_preserved_and_closed_port_passes(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen(1)
            with patch.object(ports.subprocess, "check_output", return_value=""):
                with self.assertRaisesRegex(ValueError, f"TCP {port} 已被占用"):
                    ports.preflight_port(Path("/tmp/own-state"), port=port)
            client = socket.create_connection(("127.0.0.1", port), timeout=1)
            peer, _ = listener.accept()
            client.close()
            with peer:
                self.assertEqual(peer.recv(1), b"")  # still accepts after the failed preflight
        with patch.object(ports.subprocess, "check_output", return_value=""):
            ports.preflight_port(Path("/tmp/own-state"), port=port)

    def test_limited_binding_permissions_do_not_break_docker_group_usage(self):
        with patch.object(ports.subprocess, "check_output", return_value=""), patch.object(ports, "listeners", return_value=None), patch.object(ports.socket, "socket") as probe:
            probe.return_value.__enter__.return_value.bind.side_effect = OSError(errno.EACCES, "denied")
            ports.preflight_port(Path("/tmp/own-state"))

    def test_first_install_conflict_stops_before_downloads_init_or_pairing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "new-state"
            args = setup.parser().parse_args(["cloud", "install", "--ip", "203.0.113.10"])
            with patch.object(setup.sys, "platform", "linux"), patch.object(setup.shutil, "which", return_value="/tool"), patch.object(setup.subprocess, "check_output", return_value="linux/x86_64"), patch.object(setup, "preflight_port", side_effect=ValueError("TCP 443 occupied")), patch.object(setup, "fetch_item") as fetch, patch.object(setup, "script") as execute:
                with self.assertRaisesRegex(ValueError, "443 occupied"):
                    setup.cloud_setup(args, root)
                fetch.assert_not_called()
                execute.assert_not_called()
            self.assertFalse(root.exists())

    def test_low_level_up_conflict_preserves_state_before_any_container_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "state"
            state = {"image": "nginx@sha256:" + "1" * 64, "machines": []}
            common.write(root / "state.json", state)
            argv = ["cloud.py", "--state", str(root), "up"]
            with patch.object(sys, "argv", argv), patch.object(cloud, "preflight_port", side_effect=ValueError("TCP 443 occupied")), patch.object(cloud, "compose") as compose, patch.object(cloud, "run") as run:
                with self.assertRaisesRegex(ValueError, "443 occupied"):
                    cloud.main()
                compose.assert_not_called()
                run.assert_not_called()
            self.assertEqual(common.read(root / "state.json"), state)

    def test_status_down_and_config_checks_do_not_require_a_free_port(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "state"
            common.write(root / "state.json", {"image": "nginx@sha256:" + "1" * 64, "machines": []})
            for action in ("status", "down", "check"):
                with self.subTest(action=action), patch.object(sys, "argv", ["cloud.py", "--state", str(root), action]), patch.object(cloud, "preflight_port") as guard, patch.object(cloud, "compose"):
                    cloud.main()
                    guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
