"""Pins `bin/drip-ai runtime probe` (shared/drip_ai/probe.py): the help-text parser and the one
catalog write it makes."""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[1]))

import drip_ai  # noqa: E402
from drip_ai import capabilities, probe  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "vllm_serve_help.txt"
NEW = "sha256:" + "ab" * 32


class ProbeTest(unittest.TestCase):
    def test_the_parser_reads_the_version_and_every_listed_flag(self):
        version, flags = probe.parse_help(FIXTURE.read_text())
        self.assertEqual(version, "0.11.2")
        for flag in ("--decode-context-parallel-size", "--async-scheduling", "--no-async-scheduling",
                     "--enable-prefix-caching", "--kv-offloading-backend", "--speculative-config", "--help"):
            self.assertIn(flag, flags)
        self.assertNotIn("--model", flags)

    def test_a_probe_records_each_checked_flag_for_its_digest_and_nothing_else(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        for name in ("runtimes", "vllm_flags"):
            shutil.copy(drip_ai.CATALOG_DIR / f"{name}.json", folder / f"{name}.json")
        before = json.loads((folder / "runtimes.json").read_text())
        out = io.StringIO()
        with mock.patch.object(drip_ai, "CATALOG_DIR", folder), mock.patch.object(probe, "CATALOG_DIR", folder), \
                contextlib.redirect_stdout(out):
            code = probe.main(Namespace(digest=NEW, help_file=str(FIXTURE), docker=False, region=None), {})
            self.assertEqual(code, 0)
            rt = capabilities.runtime(NEW)
        after = json.loads((folder / "runtimes.json").read_text())
        self.assertEqual({k: v for k, v in after["runtimes"].items() if k != NEW}, before["runtimes"])
        self.assertEqual((rt.vllm_version, rt.version_class), ("0.11.2", "measured"))
        self.assertEqual(rt.flags["--async-scheduling"]["status"], "supported")
        self.assertEqual(rt.flags["--reasoning-parser"]["status"], "supported")
        self.assertEqual(capabilities.flag_status(rt, "--async-scheduling")[0], "supported")
        self.assertIn("--async-scheduling: supported", out.getvalue())

    def test_text_with_no_flags_is_refused_without_a_write(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        empty = folder / "empty.txt"
        empty.write_text("not help output\n")
        err = io.StringIO()
        with mock.patch.object(probe, "record") as record, contextlib.redirect_stderr(err):
            code = probe.main(Namespace(digest=NEW, help_file=str(empty), docker=False, region=None), {})
        self.assertEqual(code, 1)
        record.assert_not_called()
        self.assertIn("lists no --flag", err.getvalue())


class DiskFloorTest(unittest.TestCase):
    """--docker pulls an image of tens of GB, so it first checks disk.min_free_gb (default 30)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.args = Namespace(digest=None, help_file=None, docker=True, region="us-east-2")

    def probe(self, free_gb):
        usage = SimpleNamespace(total=1000 * 10**9, used=0, free=int(free_gb * 10**9))
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(probe.shutil, "disk_usage", return_value=usage) as disk, \
                mock.patch.object(probe, "docker_help", return_value=FIXTURE.read_text()) as docker, \
                mock.patch.object(probe, "record", return_value={}), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = probe.main(self.args, {"HOME": str(self.home)})
        return code, err.getvalue(), docker, disk

    def test_docker_is_refused_under_the_floor_and_points_at_the_remote_path(self):
        code, err, docker, _ = self.probe(free_gb=12.5)
        self.assertEqual(code, 1)
        docker.assert_not_called()
        self.assertIn(f"{self.home} has 12.5 GB free, under disk.min_free_gb 30", err)
        self.assertIn("--help-file", err)

    def test_docker_runs_with_room_and_the_floor_is_configurable(self):
        code, _, docker, _ = self.probe(free_gb=31)
        self.assertEqual(code, 0)
        docker.assert_called_once()
        user = self.home / ".config" / "drip-ai" / "config.json"
        user.parent.mkdir(parents=True)
        user.write_text(json.dumps({"disk": {"min_free_gb": 0}}))
        user.chmod(0o600)
        code, _, docker, disk = self.probe(free_gb=1)
        self.assertEqual(code, 0)
        docker.assert_called_once()
        disk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
