"""Pins the tunnel registry and port policy (shared/drip_ai/procs.py and serve.py): only a registered
process with a matching start time is ever stopped; anything else on a port is named, not touched."""

import argparse
import contextlib
import io
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from drip_ai import procs, serve  # noqa: E402

HAS_LSOF = shutil.which("lsof") is not None


def sleeper(case):
    """A running process the test owns; it is killed and reaped when the test ends."""
    proc = subprocess.Popen(["sleep", "60"])
    case.addCleanup(proc.wait)
    case.addCleanup(proc.kill)
    deadline = time.monotonic() + 5
    while procs.start_time(proc.pid) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return proc


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.env = {"HOME": str(self.home)}

    def test_a_record_is_live_only_while_its_pid_keeps_its_start_time(self):
        proc = sleeper(self)
        path = procs.register(self.env, proc.pid, ["tunnel"], port=8000, endpoint="drip-ai-h200-us-east-1-1",
                              region="us-east-1", provider="drip-ai")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        [(record, live)] = procs.records(self.env)
        self.assertTrue(live)
        record["start_time"] = "Thu Jan  1 00:00:00 1970"
        path.write_text(json.dumps({k: v for k, v in record.items() if k != "path"}))
        [(record, live)] = procs.records(self.env)
        self.assertFalse(live)
        self.assertFalse(procs.stop(record))  # a reused pid is never signalled
        self.assertIsNone(proc.poll())

    def test_a_registered_process_with_a_matching_start_time_is_stopped(self):
        proc = sleeper(self)
        procs.register(self.env, proc.pid, ["tunnel"], port=8000)
        [(record, live)] = procs.records(self.env)
        self.assertTrue(procs.stop(record))
        self.assertIsNotNone(proc.wait(timeout=10))

    def test_listeners_name_the_pid_and_command_on_a_port(self):
        with socket.socket() as squatter:
            squatter.bind(("127.0.0.1", 0))
            squatter.listen()
            port = squatter.getsockname()[1]
            found = procs.listeners(port, port)
            if not HAS_LSOF:
                self.assertIsNone(found)
                return
            self.assertIn(port, [listener.port for listener in found])
            self.assertTrue(any(listener.command.lower().startswith("python") for listener in found))


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.env = {"HOME": str(self.home), "DRIP_AI_PORT": str(self.port)}
        self.me = sleeper(self)

    def prepare(self, endpoint="drip-ai-h200-us-east-1-1", region="us-east-1", **env):
        args = argparse.Namespace(backend="sagemaker", region=region, endpoint=endpoint, pid=self.me.pid)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = serve.prepare(args, {**self.env, **env})
        return code, dict(line.split("=", 1) for line in out.getvalue().splitlines() if "=" in line), err.getvalue()

    def test_a_free_port_is_taken_and_registered(self):
        code, lines, _ = self.prepare()
        self.assertEqual(code, 0)
        self.assertEqual((lines["port"], lines["provider"], lines["default_model"]), (str(self.port), "drip-ai", "no"))
        self.assertTrue(Path(lines["record"]).exists())

    def test_a_registered_tunnel_for_the_same_endpoint_is_reused(self):
        other = sleeper(self)
        procs.register(self.env, other.pid, ["tunnel"], port=self.port, endpoint="drip-ai-h200-us-east-1-1",
                       region="us-east-1", provider="drip-ai")
        code, lines, _ = self.prepare()
        self.assertEqual(code, 0)
        self.assertEqual(lines["reuse"], f"pid {other.pid} on 127.0.0.1:{self.port}")

    def test_an_unregistered_listener_is_refused_by_pid_unless_ports_auto(self):
        with socket.socket() as squatter:
            squatter.bind(("127.0.0.1", self.port))
            squatter.listen()
            code, _, err = self.prepare()
            self.assertEqual(code, 1)
            self.assertIn(f"port {self.port} is held by", err)
            self.assertIn("which drip-ai did not start", err)
            if HAS_LSOF:
                self.assertRegex(err, r"held by pid \d+ \(")
            config = self.home / ".config" / "drip-ai" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"ports": {"auto": True, "range": 50}}))
            config.chmod(0o600)
            code, lines, err = self.prepare()
            self.assertEqual(code, 0, err)
            self.assertNotEqual(lines["port"], str(self.port))

    def test_a_second_tunnel_gets_a_regional_opencode_provider(self):
        other = sleeper(self)
        procs.register(self.env, other.pid, ["tunnel"], port=self.port + 1, endpoint="drip-ai-h200-us-east-1-1",
                       region="us-east-1", provider="drip-ai")
        code, lines, _ = self.prepare(endpoint="drip-ai-h200-us-east-2-1", region="us-east-2")
        self.assertEqual(code, 0)
        self.assertEqual(lines["provider"], "drip-ai-us-east-2")


if __name__ == "__main__":
    unittest.main()
