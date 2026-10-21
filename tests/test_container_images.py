"""Registry failover must retain the image digest and existing pairing state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cloud
import common
import container_images as images
import setup

DIGEST = "sha256:" + "1" * 64
OTHER = "sha256:" + "2" * 64


class ContainerImageTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, REMOTECODE_CN="0")
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_cn_uses_three_registries_then_official_with_one_trusted_digest(self):
        lock = common.read(images.PACKAGE / "examples/nginx-image.lock.json")
        for name in ("nginx:stable-alpine", "library/nginx:stable-alpine", "docker.io/library/nginx:stable-alpine"):
            refs, expected, timeout = images.candidates(name, name, cn=True)
            self.assertEqual(expected, lock["digest"])
            self.assertEqual([r.split("/")[0] for r in refs],
                             ["m.daocloud.io", "docker.1ms.run", "gh-proxy.com", "docker.io"])
            self.assertTrue(all(r.endswith("@" + expected) for r in refs))
            self.assertEqual(timeout, 180)
        self.assertEqual(set(lock["platforms"]), {"linux-amd64", "linux-arm64"})

    def test_cache_miss_on_existing_pin_never_uses_a_new_bundled_version(self):
        refs, expected, _ = images.candidates("old.mirror/library/nginx@" + DIGEST,
                                               "nginx:stable-alpine", cn=True)
        self.assertEqual(expected, DIGEST)
        self.assertTrue(all(r.endswith("@" + DIGEST) for r in refs))

    def test_custom_registry_and_non_cn_reference_are_not_redirected(self):
        for ref, cn in (("private.registry:5000/team/nginx:fixed", True),
                        ("nginx:stable-alpine", False), ("nginx:1.28-alpine", True)):
            refs, expected, _ = images.candidates(ref, ref, cn=cn)
            self.assertEqual(refs, [ref])
            self.assertIsNone(expected)

    def test_inspect_selects_matching_repository_and_digest_not_first_entry(self):
        ref = "m.daocloud.io/docker.io/library/nginx@" + DIGEST
        values = ["other.registry/nginx@" + DIGEST, ref.rsplit("@", 1)[0] + "@" + OTHER, ref]
        with patch.object(images.subprocess, "check_output", return_value=json.dumps(values)):
            self.assertEqual(images.inspect_digest(ref, DIGEST), ref)
        with patch.object(images.subprocess, "check_output", return_value=json.dumps(values[:-1])):
            with self.assertRaises(ValueError):
                images.inspect_digest(ref, DIGEST)
        with patch.object(images.subprocess, "check_output", return_value=json.dumps(["nginx@" + DIGEST])):
            self.assertEqual(images.inspect_digest("docker.io/library/nginx@" + DIGEST, DIGEST), "nginx@" + DIGEST)

    def test_pull_error_or_timeout_switches_registry(self):
        for failure in (subprocess.CalledProcessError(1, ["docker", "pull"]),
                        subprocess.TimeoutExpired(["docker", "pull"], 180)):
            with self.subTest(error=type(failure).__name__):
                refs, expected, timeout = images.candidates("nginx:stable-alpine", None, cn=True)
                with patch.object(images, "run", side_effect=[failure, subprocess.CompletedProcess([], 0)]) as execute, patch.object(images.subprocess, "check_output", return_value=json.dumps([refs[1]])):
                    self.assertEqual(images.pull_image("nginx:stable-alpine", cn=True), refs[1])
                    self.assertEqual([c.args[0] for c in execute.call_args_list],
                                     [["docker", "pull", refs[0]], ["docker", "pull", refs[1]]])
                    self.assertTrue(all(c.kwargs["timeout"] == timeout for c in execute.call_args_list))

    def test_wrong_digest_is_rejected_before_next_registry_is_accepted(self):
        refs, expected, _ = images.candidates("nginx:stable-alpine", None, cn=True)
        wrong = refs[0].rsplit("@", 1)[0] + "@" + OTHER
        with patch.object(images, "run") as execute, patch.object(images.subprocess, "check_output", side_effect=[json.dumps([wrong]), json.dumps([refs[1]])]):
            self.assertEqual(images.pull_image("nginx:stable-alpine", cn=True), refs[1])
            self.assertEqual(execute.call_count, 2)

    def test_offline_only_inspects_existing_image(self):
        ref = "nginx@" + DIGEST
        with patch.object(images, "run") as execute, patch.object(images, "candidates") as plan, patch.object(images.subprocess, "check_output", return_value=json.dumps([ref])):
            self.assertEqual(images.pull_image(ref, cn=True, offline=True), ref)
            execute.assert_not_called()
            plan.assert_not_called()

    def test_all_registry_failures_leave_state_pairing_and_compose_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "state"
            common.write(root / "state.json", {"image": "nginx:stable-alpine", "image_source": "nginx:stable-alpine", "machines": [{"id": "mac1", "frp_token": "KEEP"}]})
            for name in ("compose.json", "certs/tunnel.key", "pki/root-ca.key", "pki/root-ca.crt", "enrollment/mac1/machine.json"):
                common.write(root / name, "KEEP")
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            argv = ["cloud.py", "--state", str(root), "pull", "--cn"]
            with patch.object(sys, "argv", argv), patch.object(images, "run", side_effect=subprocess.CalledProcessError(1, ["docker", "pull"])) as execute, patch.object(cloud, "render") as render:
                with self.assertRaises(RuntimeError):
                    cloud.main()
                self.assertEqual(execute.call_count, 4)
                render.assert_not_called()
            self.assertEqual({p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)

    def test_success_keeps_logical_source_and_updates_both_compose_services(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "state"
            ref = "m.daocloud.io/docker.io/library/nginx@" + DIGEST
            state = {"image": "nginx:stable-alpine", "image_source": "nginx:stable-alpine", "ip": "203.0.113.10",
                     "machines": [{"id": "mac1", "bind_port": 20001, "proxy_port": 30001,
                                   "password_hash": "fixture", "frp_token": "a" * 64}]}
            common.write(root / "state.json", state)
            argv = ["cloud.py", "--state", str(root), "pull", "--cn"]
            with patch.object(sys, "argv", argv), patch.object(cloud, "pull_image", return_value=ref):
                cloud.main()
            saved = common.read(root / "state.json")
            self.assertEqual(saved["image_source"], "nginx:stable-alpine")
            self.assertEqual(saved["image"], ref)
            self.assertEqual(common.read(root / "compose.json")["services"]["edge"]["image"], ref)
            self.assertEqual(common.read(root / "compose.json")["services"]["frps-mac1"]["image"], ref)

    def test_resume_setup_after_failed_tag_pull_forwards_cn_and_preserves_pairing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "state"
            state = {"ip": "203.0.113.10", "image": "nginx:stable-alpine", "image_source": "nginx:stable-alpine", "machines": [{"id": "mac1", "frp_token": "KEEP"}]}
            common.write(root / "state.json", state)
            common.write(root / "compose.json", {})
            common.write(root / "enrollment/mac1/machine.json", "KEEP")
            args = setup.parser().parse_args(["cloud", "install", "--ip", state["ip"], "--machine", "mac1", "--cn"])

            def execute(*argv):
                if "pull" in argv:
                    state["image"] = "m.daocloud.io/docker.io/library/nginx@" + DIGEST
                    common.write(root / "state.json", state)

            with patch.object(setup, "preflight_port"), patch.object(setup.sys, "platform", "linux"), patch.object(setup.shutil, "which", return_value="/tool"), patch.object(setup.subprocess, "check_output", return_value="linux/x86_64"), patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), patch.object(setup, "script", side_effect=execute) as call:
                setup.cloud_setup(args, root)
            self.assertIn(("cloud.py", "--state", root, "pull", "--cn"), [c.args for c in call.call_args_list])
            self.assertTrue(all("init" not in c.args and "add" not in c.args and "rotate" not in c.args for c in call.call_args_list))
            self.assertEqual((root / "enrollment/mac1/machine.json").read_text(), "KEEP")

    def test_invalid_refs_and_digest_are_refused_before_running_docker(self):
        for ref in ("--help", "https://private.registry/image", "nginx@sha256:short", "nginx\nsecret"):
            with self.subTest(ref=ref), patch.object(images, "run") as execute:
                with self.assertRaises(ValueError):
                    images.pull_image(ref, cn=True)
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
