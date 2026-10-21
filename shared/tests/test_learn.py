"""Pins the learning store (shared/drip_ai/learn.py): validation on read, decay, forget, secret
refusal, private files, rotation, and that DRIP_AI_LEARN=off plans exactly as an empty store."""

import json
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parents[2] / "sagemaker"))

from drip_ai import config, learn, modelinfo  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DAY = 24 * 3600
NOW = 1_790_000_000.0
DIGEST = "sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"


def attempt(region="us-east-2", outcome="placed", placed="ml.p5en.48xlarge"):
    return {"region": region, "substrate": "sagemaker", "pool": ["ml.p5en.48xlarge", "ml.p5e.48xlarge"],
            "outcome": outcome, "wait_s": 1800, "placed_type": placed if outcome == "placed" else None}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.env = {"HOME": str(self.home)}
        self.folder = self.home / ".local" / "state" / "drip-ai"

    def test_the_store_is_private(self):
        learn.append(self.env, "capacity_attempt", "measured", attempt(), now=NOW)
        store = self.folder / learn.STORE_NAME
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)

    def test_a_corrupt_line_is_skipped_and_counted(self):
        learn.append(self.env, "capacity_attempt", "measured", attempt(), now=NOW)
        with open(self.folder / learn.STORE_NAME, "a") as handle:
            handle.write("{not json\n")
            handle.write(json.dumps({"v": 1, "kind": "capacity_attempt", "ts": NOW, "class": "measured"}) + "\n")
            handle.write(json.dumps({"v": 9, "kind": "capacity_attempt"}) + "\n")
        store = learn.read(self.env)
        self.assertEqual((len(store.records), store.bad_lines), (1, 3))
        self.assertEqual(learn.summarize(store, NOW).bad_lines, 3)

    def test_placement_decays_with_a_seven_day_half_life(self):
        learn.append(self.env, "capacity_attempt", "measured", attempt(), now=NOW - 7 * DAY)
        learn.append(self.env, "capacity_attempt", "measured", attempt("us-east-1", "insufficient"), now=NOW)
        summary = learn.summarize(learn.read(self.env), NOW)
        placed, failed = summary.counts("us-east-2", "ml.p5en.48xlarge")
        self.assertAlmostEqual(placed, 0.5)
        self.assertAlmostEqual(summary.chance("us-east-2", "ml.p5en.48xlarge"), 1.5 / 2.5)
        self.assertAlmostEqual(summary.chance("us-east-1", "ml.p5e.48xlarge"), 1 / 3)
        self.assertAlmostEqual(summary.chance("eu-west-1", "ml.p5en.48xlarge"), 0.5)  # no data: Beta(1, 1)

    def test_forget_by_kind_and_age(self):
        learn.append(self.env, "capacity_attempt", "measured", attempt(), now=NOW - 30 * DAY)
        learn.append(self.env, "capacity_attempt", "measured", attempt(), now=NOW)
        learn.append(self.env, "credentials", "measured",
                     {"profile": None, "event": "expired", "expiry_seen": None, "session_age_s": None}, now=NOW)
        self.assertEqual(learn.forget(self.env, before=NOW - DAY), 1)
        self.assertEqual(learn.forget(self.env, kind="credentials"), 1)
        self.assertEqual([r["kind"] for r in learn.read(self.env).records], ["capacity_attempt"])
        with self.assertRaises(learn.LearnError):
            learn.forget(self.env, kind="bogus")

    def test_a_secret_or_malformed_observation_is_refused(self):
        with self.assertRaisesRegex(learn.LearnError, "looks like a secret"):
            learn.append(self.env, "credentials", "measured",
                         {"profile": "sk-CanaryApiKey0123456789abcdefghijklmnop", "event": "expired",
                          "expiry_seen": None, "session_age_s": None})
        with self.assertRaisesRegex(learn.LearnError, "bad outcome"):
            learn.append(self.env, "capacity_attempt", "measured", attempt(outcome="maybe"))
        self.assertEqual(learn.read(self.env).records, ())

    def test_the_store_rotates_keeping_one_old_file(self):
        with mock.patch.object(learn, "ROTATE_BYTES", 600):
            for index in range(6):
                learn.append(self.env, "capacity_attempt", "measured", attempt(), now=NOW + index)
        self.assertTrue((self.folder / learn.ROTATED_NAME).exists())
        self.assertGreater(len(learn.read(self.env).records), 1)

    def test_the_summary_cache_is_rebuilt_when_the_log_changes(self):
        learn.append(self.env, "capacity_attempt", "measured", attempt(), now=NOW)
        first = learn.summary(self.env, NOW)
        self.assertTrue((self.folder / learn.SUMMARY_NAME).exists())
        learn.append(self.env, "capacity_attempt", "measured", attempt(outcome="insufficient"), now=NOW)
        second = learn.summary(self.env, NOW)
        self.assertNotEqual(first.counts("us-east-2", "ml.p5e.48xlarge"), second.counts("us-east-2", "ml.p5e.48xlarge"))

    def test_learning_off_reads_writes_and_plans_nothing(self):
        import lanes  # sagemaker/lanes.py

        learn.append(self.env, "kv_capacity", "measured", {
            "model_key": "example_mla_dsa:GlmMoeDsaForCausalLM:fp8", "instance_type": "ml.p5en.48xlarge",
            "image_digest": DIGEST, "dcp": 8, "kv_dtype": "fp8", "gpu_memory_utilization": 0.9,
            "pool_tokens": 1_000_000, "available_kv_gib": None}, now=NOW)
        facts = modelinfo.parse_config((FIXTURES / "example_mla_dsa.json").read_bytes(), 756 * 10**9)
        off = {**self.env, "DRIP_AI_LEARN": "off"}
        self.assertFalse(learn.append(off, "capacity_attempt", "measured", attempt()))
        self.assertEqual(learn.read(off).records, ())

        def plans(environ):
            cfg = config.resolve(environ)
            summary = learn.summary(environ) if cfg["learn.enabled"] else learn.Summary()
            found, _ = lanes.serve_plans(facts, cfg, [], summary)
            return {cls: (p.max_model_len, p.dcp, p.argv) for cls, (p, _) in found.items()}

        empty = {"HOME": tempfile.mkdtemp(dir=self.home)}
        self.assertEqual(plans(off), plans(empty))
        self.assertNotEqual(plans(self.env)["h200"], plans(empty)["h200"])  # the measured pool counts when on


if __name__ == "__main__":
    unittest.main()
