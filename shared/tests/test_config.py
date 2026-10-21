"""Pins the layered config (shared/drip_ai/config.py): layer order with provenance, the schema's
refusals, the user file's safety, the environment table, and the tfvars import."""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from drip_ai import config  # noqa: E402


class LayerTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.env = {"HOME": str(self.home)}

    def write_user(self, document, mode=0o600):
        path = self.home / ".config" / "drip-ai" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
        os.chmod(path, mode)
        return path

    def test_each_layer_overrides_the_one_below_and_says_so(self):
        key = "race.max_lanes"
        self.assertEqual((config.resolve(self.env)[key], config.resolve(self.env).source(key)), (4, "builtin"))
        profile = {"serve.max_model_len": 262144}
        self.assertEqual(config.resolve(self.env, profile=profile).source("serve.max_model_len"), "profile")
        self.write_user({"version": 1, "race": {"max_lanes": 6}, "serve": {"max_model_len": 131072}})
        resolved = config.resolve(self.env, profile=profile)
        self.assertEqual((resolved[key], resolved.source(key)), (6, "user"))
        self.assertEqual((resolved["serve.max_model_len"], resolved.source("serve.max_model_len")), (131072, "user"))
        env = {**self.env, "DRIP_AI_PORT": "8100"}
        self.assertEqual(config.resolve(env).source("ports.base"), "env")
        resolved = config.resolve(env, cli={key: 2, "ports.base": 8200})
        self.assertEqual([(resolved[k], resolved.source(k)) for k in (key, "ports.base")], [(2, "cli"), (8200, "cli")])

    def test_derived_values_fill_only_null_keys_no_explicit_layer_set(self):
        resolved = config.resolve(self.env, cli={"serve.dcp": 4})
        filled = resolved.with_derived({"serve.dcp": 8, "serve.max_model_len": 1048576, "race.max_lanes": 9},
                                       {"serve.max_model_len": "config.json max_position_embeddings"})
        self.assertEqual((filled["serve.dcp"], filled.source("serve.dcp")), (4, "cli"))
        self.assertEqual((filled["serve.max_model_len"], filled.source("serve.max_model_len")), (1048576, "derived"))
        self.assertEqual(filled["race.max_lanes"], 4)  # builtin 4 is not null, so the engine cannot set it
        lines = config.explain_lines(filled, ["serve.max_model_len"])
        self.assertIn("builtin (shared/config/defaults.json) < derived", lines[0])
        self.assertEqual(lines[1], "serve.max_model_len = 1048576  derived  (config.json max_position_embeddings)")

    def test_unknown_keys_and_wrong_types_are_refused_at_their_layer(self):
        cases = [
            (lambda: config.resolve(self.env, cli={"race.max_lane": 3}), "not a drip-ai config key"),
            (lambda: config.resolve(self.env, cli={"race.max_lanes": "3"}), "must be a whole number"),
            (lambda: config.resolve(self.env, cli={"race.max_lanes": 99}), "must be at most 12"),
            (lambda: config.resolve(self.env, cli={"serve.dcp": 3}), "must be one of 1, 2, 4, 8"),
            (lambda: config.resolve(self.env, cli={"regions": ["mars-1"]}), "is not an AWS region"),
            (lambda: config.resolve(self.env, cli={"clients.link": ["cursor"]}), "names 'cursor'"),
            (lambda: config.resolve(self.env, cli={"regions": ["us-east-1", "us-east-1"]}), "repeats an entry"),
            (lambda: config.parse_sets(["race.max_lanes"]), "takes key=value"),
            (lambda: config.profile_layer({"vllm_args": [], "source": "https://x", "serve": {"bogus": 1}}),
             "not a drip-ai config key"),
        ]
        for action, message in cases:
            with self.subTest(message), self.assertRaisesRegex(config.ConfigError, message):
                action()
        self.write_user({"race": {"max_lanes": 3}, "surprise": True})
        with self.assertRaisesRegex(config.ConfigError, "'surprise', which is not a drip-ai config key"):
            config.resolve(self.env)

    def test_the_user_file_must_be_private_small_plain_json(self):
        path = self.write_user({"race": {"max_lanes": 3}}, mode=0o666)
        with self.assertRaisesRegex(config.ConfigError, "writable by other users"):
            config.resolve(self.env)
        os.chmod(path, 0o600)
        path.write_text("{ // comment\n}")
        with self.assertRaisesRegex(config.ConfigError, "not plain JSON"):
            config.resolve(self.env)
        path.write_text(json.dumps({"lifecycle": {"protect": ["x" * 100] * 3000}}))
        with self.assertRaisesRegex(config.ConfigError, "larger than 256 KB"):
            config.resolve(self.env)
        explicit = self.home / "elsewhere.json"
        explicit.write_text(json.dumps({"race": {"max_lanes": 5}}))
        os.chmod(explicit, 0o600)
        self.assertEqual(config.resolve({**self.env, "DRIP_AI_CONFIG": str(explicit)})["race.max_lanes"], 5)

    def test_the_environment_table_keeps_the_documented_variables(self):
        env = {**self.env, "DRIP_AI_LINK_CLIENTS": "all", "DRIP_AI_PORT": "8001", "DRIP_AI_LEARN": "off"}
        resolved = config.resolve(env)
        self.assertEqual(resolved["clients.link"], ["codex", "claude", "opencode"])
        self.assertEqual(resolved["ports.base"], 8001)
        self.assertIs(resolved["learn.enabled"], False)
        self.assertEqual(config.resolve({**self.env, "DRIP_AI_LINK_CLIENTS": "none"})["clients.link"], [])
        self.assertEqual(config.resolve({**self.env, "DRIP_AI_LINK_CLIENTS": "opencode,codex"})["clients.link"],
                         ["opencode", "codex"])
        # opencode by default; Codex and Claude Code only when named.
        self.assertEqual(config.resolve(self.env)["clients.link"], ["opencode"])
        self.assertEqual(config.resolve(self.env, cli={"clients.link": []})["clients.link"], [])

    def test_config_set_writes_a_private_file_and_an_empty_value_removes_the_key(self):
        path = config.set_user_values(self.env, ["race.max_lanes=6", "regions=us-east-2,us-east-1",
                                                 "notify.argv=[\"osascript\", \"-e\"]"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), {"version": 1, "notify": {"argv": ["osascript", "-e"]},
                                                        "race": {"max_lanes": 6}, "regions": ["us-east-2", "us-east-1"]})
        config.set_user_values(self.env, ["race.max_lanes="])
        self.assertNotIn("race", json.loads(path.read_text()))

    def test_import_tfvars_moves_the_tuning_once_and_keeps_the_region(self):
        tfvars = self.home / "terraform.tfvars"
        original = ('region = "us-east-1"\ninstance_types = ["ml.p5en.48xlarge", "ml.p5e.48xlarge"] # raced\n'
                    'lanes_per_type = 2\nmax_model_len = 131072\nvllm_extra_args = []\nserved_model_name = "drip-coder"\n')
        tfvars.write_text(original)
        moved = config.import_tfvars(self.env, tfvars)
        self.assertEqual([m[1] for m in moved], ["race.instance_types", "serve.max_model_len", "serve.extra_args",
                                                 "serve.served_model_name"])
        self.assertEqual(tfvars.read_text(), 'region = "us-east-1"\n')
        self.assertEqual((self.home / "terraform.tfvars.pre-import").read_text(), original)
        resolved = config.resolve(self.env)
        self.assertEqual(resolved["race.instance_types"], ["ml.p5en.48xlarge", "ml.p5e.48xlarge"])
        tfvars.write_text("max_model_len = 65536\n")
        with self.assertRaisesRegex(config.ConfigError, "already sets serve.max_model_len"):
            config.import_tfvars(self.env, tfvars)


class ProfileTest(unittest.TestCase):
    def test_a_v2_profile_sets_serve_keys_and_weights(self):
        layer = config.profile_layer({"vllm_args": ["--x"], "source": "https://e", "weights": "hf",
                                      "serve": {"max_model_len": 262144, "target_sequences": 1}})
        self.assertEqual(layer, {"serve.max_model_len": 262144, "serve.target_sequences": 1, "serve.weights": "hf"})
        profiles = {"t": {"vllm_args": [], "source": "https://e"}, "Org/M@abc": {"vllm_args": ["--y"], "source": "https://e"}}
        self.assertEqual(config.find_profile(profiles, "t", "Org/M@abc")[0], "Org/M@abc")
        self.assertEqual(config.find_profile(profiles, "t", None)[0], "t")
        self.assertIsNone(config.find_profile(profiles, "u", None))


if __name__ == "__main__":
    unittest.main()
