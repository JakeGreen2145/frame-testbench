"""CPU-only public ABI integration tests. No SteamVR runtime or hardware needed."""
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "input-tests"


class InputProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = ROOT / "scripts/build-input.sh"
        if not script.exists():
            raise AssertionError("Missing input proxy build implementation")
        subprocess.run(["bash", str(script), str(BUILD)], check=True)
        import shlex
        flags = [os.environ.get("CXX", "g++"), "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pthread", "-isystem", str(ROOT / "vendor")]
        flags += shlex.split(os.environ.get("CXXFLAGS", ""))
        subprocess.run(flags + ["-shared", "-fPIC", str(ROOT / "tests/input_fake_cv.cpp"), "-o", str(BUILD / "driver_cv.so")], check=True)
        subprocess.run(flags + [str(ROOT / "tests/input_fake_runtime.cpp"), "-ldl", "-o", str(BUILD / "vrserver")], check=True)

    def setUp(self):
        scratch = os.environ.get("TMPDIR", str(ROOT / "build"))
        self.temp = tempfile.TemporaryDirectory(prefix="fi-", dir=scratch)
        self.sock = str(Path(self.temp.name) / "s")
        self.env = dict(os.environ, FRAME_TESTBENCH_SOCKET=self.sock,
                        FRAME_TESTBENCH_REAL_DRIVER=str(BUILD / "driver_cv.so"),
                        FRAME_TESTBENCH_PROXY=str(BUILD / "libframe_input.so"),
                        LD_PRELOAD=str(BUILD / "libframe_loader.so"))
        self.p = subprocess.Popen([str(BUILD / "vrserver")], env=self.env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
        assert self.p.stdout is not None
        self.assertEqual(self.p.stdout.readline().strip(), "ready")

    def tearDown(self):
        if self.p.poll() is None:
            self.p.communicate("quit\n", timeout=5)
        self.assertEqual(self.p.returncode, 0)
        self.temp.cleanup()

    def runtime(self, command):
        assert self.p.stdin is not None and self.p.stdout is not None
        self.p.stdin.write(command + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def rpc(self, command):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2)
            client.connect(self.sock)
            client.sendall(command.encode() + b"\n")
            stream = client.makefile("rb")
            return json.loads(stream.readline())

    def test_public_abi_forwarding_and_identity(self):
        s = self.rpc("status")
        self.assertTrue(s["ok"])
        self.assertEqual(s["hmd_index"], 7)
        self.assertEqual(s["hmd_container"], 107)
        self.assertTrue(s["proximity_ready"])
        self.assertFalse(s["pose_override"])
        self.assertIsNone(s["worn_override"])
        self.assertEqual(stat.S_IMODE(os.stat(self.sock).st_mode), 0o600)
        r = self.runtime("forward")
        self.assertTrue(r["all_forwarded"])
        self.assertTrue(r["component_identity"])
        self.assertTrue(r["controller_identity"])
        self.assertTrue(r["factory_identity"])

    def test_raw_pose_persists_and_only_hmd_is_replaced(self):
        s = self.rpc("pose 1 2 3 2 0 0 0")
        self.assertTrue(s["ok"])
        self.assertEqual(s["requested_pose"], [1, 2, 3, 1, 0, 0, 0])
        self.assertTrue(s["pose_override"])
        self.runtime("physical")
        time.sleep(0.06)
        r = self.runtime("metrics")
        self.assertEqual(r["hmd_pose"], [1, 2, 3, 1, 0, 0, 0])
        self.assertTrue(r["raw_transform"])
        self.assertTrue(r["valid"])
        self.assertEqual(r["other_x"], 99)
        self.assertGreater(r["pose_count"], 3)
        self.assertIsNone(self.rpc("status")["worn_override"])
        # A short-lived controller connection must not release its override.
        time.sleep(0.1)
        self.assertTrue(self.rpc("status")["pose_override"])

    def test_independent_worn_suppression_ticks_and_release_latest_physical(self):
        before = self.rpc("status")
        s = self.rpc("worn 1")
        self.assertGreater(s["sequence"], before["sequence"])
        self.assertFalse(s["pose_override"])
        self.runtime("physical")
        time.sleep(0.05)
        r = self.runtime("metrics")
        self.assertTrue(r["worn"])
        self.assertFalse(r["other_worn"])
        self.assertGreater(r["worn_count"], 3)
        self.assertEqual(r["hmd_pose"][0], 42)
        self.rpc("pose 1 2 3 1 0 0 0")
        s = self.rpc("release")
        self.assertFalse(s["pose_override"])
        self.assertIsNone(s["worn_override"])
        self.assertEqual(s["physical_pose"]["position"][0], 42)
        self.assertFalse(s["physical_worn"])
        r = self.runtime("metrics")
        self.assertFalse(r["worn"])
        self.assertEqual(r["hmd_pose"][0], 42)
        self.assertFalse(r["raw_transform"])
        self.assertFalse(r["valid"])
        self.rpc("worn 0")
        self.assertFalse(self.runtime("metrics")["worn"])

    def test_malformed_commands_do_not_mutate_state(self):
        s = self.rpc("pose 1 2 3 1 0 0 0")
        for cmd in ["", "blah", "status extra", "release extra", "worn 2", "worn -1", "worn 1 extra",
                    "pose 1 2", "pose nan 0 0 1 0 0 0", "pose 0 0 0 0 0 0 0",
                    "pose 0 0 0 inf 0 0 0", "pose 0 0 0 1 0 0 0 extra"]:
            with self.subTest(cmd=cmd):
                bad = self.rpc(cmd)
                self.assertFalse(bad["ok"])
                self.assertEqual(bad["sequence"], s["sequence"])
                self.assertEqual(bad["requested_pose"], s["requested_pose"])

    def test_idle_client_does_not_block_publisher_or_other_clients(self):
        self.rpc("worn 1")
        with socket.socket(socket.AF_UNIX) as idle:
            idle.connect(self.sock)
            idle.sendall(b"po")
            time.sleep(0.04)
            self.assertTrue(self.rpc("status")["worn_override"])
            self.assertGreater(self.runtime("metrics")["worn_count"], 2)

    def test_selective_release_keeps_other_override(self):
        self.rpc("pose 1 2 3 1 0 0 0")
        self.rpc("worn 1")
        s = self.rpc("pose-release")
        self.assertTrue(s["ok"])
        self.assertFalse(s["pose_override"])
        self.assertTrue(s["worn_override"])
        self.assertEqual(self.runtime("metrics")["hmd_pose"][0], 42)
        self.rpc("pose 4 5 6 1 0 0 0")
        s = self.rpc("worn-release")
        self.assertTrue(s["ok"])
        self.assertTrue(s["pose_override"])
        self.assertIsNone(s["worn_override"])
        self.assertFalse(self.runtime("metrics")["worn"])

    def test_physical_pose_reports_its_driver_transforms(self):
        p = self.rpc("status")["physical_pose"]
        self.assertEqual(p["world_from_driver"]["position"], [5, 0, 0])
        self.assertEqual(p["driver_from_head"]["position"], [0, 6, 0])
        self.assertFalse(p["valid"])

    def test_deactivate_stops_ticks_and_clears_readiness(self):
        self.rpc("pose 1 2 3 1 0 0 0")
        self.rpc("worn 1")
        self.assertTrue(self.runtime("deactivate")["deactivated"])
        s = self.rpc("status")
        self.assertIsNone(s["hmd_index"])
        self.assertFalse(s["proximity_ready"])
        count = self.runtime("metrics")["pose_count"]
        time.sleep(0.05)
        self.assertEqual(count, self.runtime("metrics")["pose_count"])
        self.assertFalse(self.rpc("worn 0")["ok"])
        self.assertFalse(self.rpc("pose 0 0 0 1 0 0 0")["ok"])

    def test_loader_leaves_other_executables_and_other_paths_untouched(self):
        import shutil
        unscoped = Path(self.temp.name) / "not-vrserver"
        shutil.copy2(BUILD / "vrserver", unscoped)
        env = dict(self.env, FRAME_TESTBENCH_SOCKET=str(Path(self.temp.name) / "unused"))
        r = subprocess.run([str(unscoped)], env=env, input="forward\nquit\n", text=True, capture_output=True, timeout=3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout.splitlines()[1])["all_forwarded"])
        self.assertFalse(os.path.exists(env["FRAME_TESTBENCH_SOCKET"]))
        r = subprocess.run([str(BUILD / "vrserver"), "scope"], env=self.env, text=True, capture_output=True, timeout=3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "scope-ok")

    def test_restart_reclaims_only_stale_owned_socket(self):
        path = str(Path(self.temp.name) / "stale")
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(path)
        env = dict(self.env, FRAME_TESTBENCH_SOCKET=path)
        r = subprocess.run([str(BUILD / "vrserver")], env=env, input="quit\n", text=True, capture_output=True, timeout=3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(path))

    def test_artifacts_export_only_loader_and_factory_and_no_cpp_runtime_dependency(self):
        expected = {"libframe_input.so": {"HmdDriverFactory"},
                    "libframe_loader.so": {"dlopen", "frame_real_dlopen"}}
        for name, symbols in expected.items():
            exports = subprocess.check_output(["nm", "-D", "--defined-only", str(BUILD / name)], text=True)
            self.assertEqual({line.split()[-1] for line in exports.splitlines()}, symbols)
            elf = subprocess.check_output(["objdump", "-p", str(BUILD / name)], text=True)
            needed = [line for line in elf.splitlines() if "NEEDED" in line]
            self.assertFalse(any("libstdc++" in line for line in needed))

    def test_concurrent_commands_and_physical_updates_finish_coherently(self):
        from concurrent.futures import ThreadPoolExecutor
        self.rpc("pose 1 2 3 1 0 0 0")
        def physical_updates():
            for _ in range(40):
                self.runtime("physical")
        def commands():
            for _ in range(20):
                self.assertTrue(self.rpc("worn 1")["ok"])
                self.assertTrue(self.rpc("pose 4 5 6 1 1 0 0")["ok"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(physical_updates)
            b = pool.submit(commands)
            a.result(timeout=5)
            b.result(timeout=5)
        self.rpc("release")
        time.sleep(0.05)
        result = self.runtime("metrics")
        self.assertEqual(result["hmd_pose"][0], 42)
        self.assertFalse(result["worn"])

    def test_oversized_and_fragmented_requests(self):
        self.assertFalse(self.rpc("x" * 2000)["ok"])
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2)
            client.connect(self.sock)
            client.sendall(b"sta")
            time.sleep(0.01)
            client.sendall(b"tus\n")
            with client.makefile("rb") as stream:
                self.assertTrue(json.loads(stream.readline())["ok"])

    def test_pose_normalizes_huge_and_nontrivial_quaternions(self):
        s = self.rpc("pose 0 0 0 1e308 1e308 0 0")
        self.assertTrue(s["ok"])
        q = s["pose"]["quaternion"]
        self.assertAlmostEqual(sum(v*v for v in q), 1)
        self.assertAlmostEqual(q[0], q[1])
        self.assertEqual(q[2:], [0, 0])

    def test_socket_collision_never_replaces_live_socket_or_file(self):
        from pathlib import Path
        for path in [self.sock, str(Path(self.temp.name) / "file")]:
            if path != self.sock:
                Path(path).write_text("preserve-me")
            inode = os.lstat(path).st_ino
            env = dict(self.env, FRAME_TESTBENCH_SOCKET=path)
            result = subprocess.run([str(BUILD / "vrserver")], env=env, input="quit\n", text=True, capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(os.lstat(path).st_ino, inode)
        self.assertTrue(self.rpc("status")["ok"])
        self.assertEqual(Path(self.temp.name, "file").read_text(), "preserve-me")

    def test_cleanup_stops_worker_and_removes_socket(self):
        self.rpc("pose 1 2 3 1 0 0 0")
        self.assertTrue(self.runtime("cleanup")["cleaned"])
        self.assertFalse(os.path.exists(self.sock))
        first = self.runtime("metrics")["pose_count"]
        time.sleep(0.04)
        self.assertEqual(first, self.runtime("metrics")["pose_count"])


if __name__ == "__main__":
    unittest.main()
