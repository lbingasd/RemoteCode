"""Focused installer regressions; system package managers are never executed here."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import setup
import agents

spec = importlib.util.spec_from_file_location("local_service", setup.SCRIPTS / "local-service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def options(role, action="install", *extra):
    return setup.parser().parse_args([role, action, *extra])


class SetupTests(unittest.TestCase):
    def test_private_node_npm_remains_usable_without_shell_path_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder).resolve() / "tools/node"
            node = prefix / "bin/node"
            common.write(node, "fixture")
            cli = prefix / "lib/node_modules/npm/bin/npm-cli.js"
            common.write(cli, "fixture")
            with patch.object(agents.shutil, "which", return_value=None):
                self.assertEqual(agents.npm_command(str(node)), [str(node), str(cli)])

    def test_os_lock_prevents_parallel_changes_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            with setup.deployment_lock(root):
                with self.assertRaises(ValueError):
                    with setup.deployment_lock(root):
                        self.fail("parallel mutation was permitted")
            with setup.deployment_lock(root):
                pass

    def test_offline_cache_rejects_tampering_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            item = {"name": "asset", "sha256": "0" * 64, "url": "https://official.example/asset"}
            (cache / "asset").write_bytes(b"tampered")
            with patch.object(setup, "segmented_download") as segment, patch.object(setup, "download") as full:
                with self.assertRaises(ValueError):
                    setup.fetch_item(item, cache, options("local", "install", "--offline"))
                segment.assert_not_called()
                full.assert_not_called()

    def test_range_failure_falls_back_without_disabling_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            item = {"name": "asset", "sha256": "1" * 64, "url": "https://official.example/asset"}
            with patch.object(setup, "segmented_download", side_effect=RuntimeError("no Range")), patch.object(setup, "download") as full:
                setup.fetch_item(item, Path(folder), options("cloud"))
            self.assertEqual(full.call_args.args[2], item["sha256"])

    def test_bundled_nssm_integrity_architecture_and_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            root.mkdir()
            args = options("local", "install", "--offline")
            with patch.object(setup, "host_platform", return_value="windows-amd64"), patch.object(setup, "download") as fetch:
                binary = setup.prepare_nssm(root, args)
                content = binary.read_bytes()
                self.assertEqual(content[:2], b"MZ")
                offset = int.from_bytes(content[0x3C:0x40], "little")
                self.assertEqual(content[offset:offset + 4], b"PE\0\0")
                self.assertEqual(int.from_bytes(content[offset + 4:offset + 6], "little"), 0x8664)
                inode = binary.stat().st_ino
                self.assertEqual(setup.prepare_nssm(root, args).stat().st_ino, inode)
                fetch.assert_not_called()

    def test_tampered_bundled_asset_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder)
            common.write(package / "assets/asset", "tampered")
            with patch.object(setup, "PACKAGE", package):
                with self.assertRaises(ValueError):
                    setup.bundled_asset({"name": "asset", "sha256": "0" * 64})

    def test_settings_merge_preserves_custom_agents_and_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "data/Machine/settings.json"
            original = {"editor.fontSize": 19, "acp.agents": {"custom": {"command": "/custom"}}, "acp.autoApprovePermissions": "ask"}
            common.write(path, original)
            common.write(root / "acp-settings.json", {"acp.agents": {"Codex (local ACP)": {"command": "/local"}}, "acp.autoApprovePermissions": "ask", "acp.logTraffic": False})
            setup.merge_settings(root)
            setup.merge_settings(root)
            merged = common.read(path)
            self.assertEqual(merged["editor.fontSize"], 19)
            self.assertEqual(set(merged["acp.agents"]), {"custom", "Codex (local ACP)"})
            self.assertEqual(common.read(path.with_suffix(".json.remotecode-backup")), original)

    def test_invalid_settings_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "data/Machine/settings.json"
            common.write(path, '// existing comments\n{"editor.fontSize": bad,}\n')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                setup.merge_settings(root)
            self.assertEqual(path.read_bytes(), before)

    def test_jsonc_preserves_string_comments_and_accepts_trailing_comma(self):
        text = r'''// header
        {"url":"https://example/a//b", "quote":"a\"/*b", /* comment */
         "array":["a,}",2,], "enabled":true,}'''
        self.assertEqual(setup.parse_jsonc(text), {"url": "https://example/a//b", "quote": 'a"/*b', "array": ["a,}", 2], "enabled": True})

    def test_existing_install_keeps_versions_and_rejects_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            common.write(root / "download.lock.json", {"platform": "darwin-amd64", "frp_version": "old"})
            args = options("local")
            self.assertEqual(setup.select_lock(args, root, "darwin-amd64", local=True), root / "download.lock.json")
            args.lock = root / "new.lock"
            common.write(args.lock, {"platform": "darwin-amd64", "frp_version": "new"})
            with self.assertRaises(ValueError):
                setup.select_lock(args, root, "darwin-amd64", local=True)

    def test_cloud_rerun_preserves_credentials_and_avoids_force_recreate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "cloud"
            state = {"ip": "203.0.113.10", "image": "nginx@sha256:" + "1" * 64, "image_source": "private.registry/nginx:fixed", "machines": [{"id": "pc1", "frp_token": "unchanged"}]}
            common.write(root / "state.json", state)
            common.write(root / "compose.json", {})
            common.write(root / "live/nginx.conf", "config")
            args = options("cloud", "install", "--ip", state["ip"], "--image", "private.registry/nginx:fixed")
            completed = subprocess.CompletedProcess([], 0)
            with patch.object(setup.sys, "platform", "linux"), patch.object(setup.shutil, "which", return_value="/tool"), patch.object(setup.subprocess, "check_output", return_value="linux/x86_64"), patch.object(setup.subprocess, "run", return_value=completed), patch.object(setup, "script") as execute:
                setup.cloud_setup(args, root)
                self.assertIn(("cloud.py", "--state", root, "up"), [c.args for c in execute.call_args_list])
                execute.reset_mock()
                setup.cloud_setup(args, root)
                self.assertNotIn(("cloud.py", "--state", root, "up"), [c.args for c in execute.call_args_list])
                self.assertTrue(all("add" not in c.args and "rotate" not in c.args and "pull" not in c.args for c in execute.call_args_list))
                execute.reset_mock()
                common.write(root / "live/nginx.conf", "changed credential/config")
                setup.cloud_setup(args, root)
                self.assertIn(("cloud.py", "--state", root, "up"), [c.args for c in execute.call_args_list])
            self.assertEqual(common.read(root / "state.json"), state)

    def test_cloud_ip_change_fails_before_mutating(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = {"ip": "203.0.113.10"}
            common.write(root / "state.json", state)
            args = options("cloud", "install", "--ip", "203.0.113.11")
            with patch.object(setup.sys, "platform", "linux"), patch.object(setup.shutil, "which", return_value="/tool"), patch.object(setup.subprocess, "check_output", return_value="linux/x86_64"), patch.object(setup, "script") as execute:
                with self.assertRaises(ValueError):
                    setup.cloud_setup(args, root)
                execute.assert_not_called()
            self.assertEqual(common.read(root / "state.json"), state)

    def test_uninstall_absent_is_repeatable_but_foreign_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            for role, function in (("cloud", setup.cloud_setup), ("local", setup.home_setup)):
                root = Path(folder) / role
                args = options(role, "uninstall", "--purge")
                function(args, root)
                function(args, root)
                common.write(root / "unrelated.txt", "keep")
                with self.assertRaises(ValueError):
                    function(args, root)
                self.assertEqual((root / "unrelated.txt").read_text(), "keep")

    def test_launchagents_install_repeat_remove_and_foreign_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            root = home / "runtime"
            config = {"id": "fixture", "workspace": str(home / "project"), "server_dir": str(root / "server"), "local_port": 18080}
            common.write(root / "local.json", config)
            (home / "Library/LaunchAgents").mkdir(parents=True)
            active = subprocess.CompletedProcess([], 0)
            with patch.object(service.Path, "home", return_value=home), patch.object(service.platform, "system", return_value="Darwin"), patch.object(service.os, "getuid", return_value=501), patch.object(service, "run") as execute, patch.object(service.subprocess, "run", return_value=active), patch.object(sys, "argv", ["local-service.py", "install", "--root", str(root)]):
                service.main()
                self.assertEqual(execute.call_count, 2)
                execute.reset_mock()
                service.main()
                execute.assert_not_called()
                config["server_dir"] = "/foreign/server"
                common.write(root / "local.json", config)
                with self.assertRaises(ValueError):
                    service.main()
                execute.assert_not_called()
                config["server_dir"] = str(root / "server")
                common.write(root / "local.json", config)
                sys.argv[1] = "remove"
                service.main()
                service.main()
            self.assertFalse(list((home / "Library/LaunchAgents").glob("*.plist")))


if __name__ == "__main__":
    unittest.main()
