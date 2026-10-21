"""Pins the defaults engine (shared/drip_ai/sizing.py): golden plans for synthetic models, the DCP
and context rule, capability gating, and the SM_VLLM_* mapper's flag rules."""

import json
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from drip_ai import capabilities, config, modelinfo, sizing  # noqa: E402
from drip_ai.capabilities import Evidence, Runtime  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DLC = "sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"
VLLM_OPENAI = "sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90"
MTP_1 = '{"method":"mtp","num_speculative_tokens":1}'
REPETITION_1 = '{"repetition_penalty":1.0}'
SUPPORTED = {"status": "supported", "class": "measured", "evidence": "synthetic"}
# A runtime with every flag and engine path the mla_dsa plan uses, on every architecture.
EVERYWHERE = Runtime("sha256:test", "a synthetic image", None, "unknown",
                     {flag: SUPPORTED for flag in ("--decode-context-parallel-size", "--dcp-comm-backend",
                                                   "--kv-cache-dtype", "--speculative-config", "--enable-prefix-caching",
                                                   "--max-num-batched-tokens", "--gpu-memory-utilization")},
                     {"kv_layout:fp8_ds_mla": SUPPORTED, "dcp:mla_dsa": SUPPORTED, "mtp": SUPPORTED})


def facts(name="example_mla_dsa", weight_bytes=756 * 10**9):
    return modelinfo.parse_config((FIXTURES / f"{name}.json").read_bytes(), weight_bytes)


def resolved(**cli):
    return config.resolve({"HOME": "/nonexistent-home"}, cli=cli)


def plan(instance="ml.p5en.48xlarge", rt=None, cfg=None, model=None, substrate="sagemaker", **kwargs):
    return sizing.plan(model or facts(), sizing.hardware(instance), rt or capabilities.runtime(DLC), cfg or resolved(),
                       substrate, **kwargs)


def flags(serve_plan):
    return dict(serve_plan.pairs)


class GoldenTest(unittest.TestCase):
    def test_h200_on_the_pinned_image_matches_the_working_endpoint(self):
        p = plan()
        self.assertEqual((p.dcp, p.max_model_len, p.kv_cache_dtype, p.mtp_tokens), (8, 1048576, "fp8", 1))
        got = flags(p)
        self.assertEqual(got["--decode-context-parallel-size"], "8")
        self.assertEqual(got["--dcp-comm-backend"], "ag_rs")
        self.assertEqual(got["--kv-cache-dtype"], "fp8")
        self.assertEqual(got["--speculative-config"], MTP_1)
        self.assertEqual(got["--max-num-batched-tokens"], "8192")
        self.assertIn("--enable-prefix-caching", got)
        self.assertIn("--async-scheduling", got)
        self.assertEqual(got["--override-generation-config"], REPETITION_1)
        # Unknown on this image, so SageMaker drops it rather than risk a second scarce node.
        self.assertNotIn("--kv-offloading-size", got)
        decisions = {d.key: d for d in p.decisions}
        self.assertEqual(decisions["kv.bytes_per_token"].value, (656 + 132) * 79)
        self.assertIn("smallest DCP with pool >= 2x1048576x1.10", decisions["serve.dcp"].why)
        self.assertEqual(decisions["serve.max_model_len"].why, "config.json max_position_embeddings")

    def test_b300_holds_two_full_sequences_at_dcp_1_on_a_thin_margin(self):
        p = plan("ml.p6-b300.48xlarge", rt=EVERYWHERE)
        self.assertEqual((p.dcp, p.max_model_len, p.kv_cache_dtype), (1, 1048576, "fp8"))
        self.assertGreaterEqual(p.pool_tokens, 2 * 1048576 * 1.10)
        self.assertLess(p.pool_tokens, 2 * 1048576 * 1.10 * 1.05)

    def test_b300_on_the_pinned_image_has_no_blackwell_evidence_and_sizes_bf16(self):
        p = plan("ml.p6-b300.48xlarge")
        self.assertEqual((p.kv_cache_dtype, p.dcp, p.max_model_len), ("auto", 1, 1048576))
        decisions = {d.key: d for d in p.decisions}
        self.assertIn("on sm103", decisions["serve.kv_cache_dtype"].why)
        self.assertIn("DCP unsupported", decisions["serve.dcp"].why)

    def test_without_dcp_the_window_drops_to_what_one_shard_holds_and_says_why(self):
        rt = Runtime(EVERYWHERE.digest, EVERYWHERE.name, None, "unknown",
                     {k: v for k, v in EVERYWHERE.flags.items() if k != "--decode-context-parallel-size"},
                     EVERYWHERE.features)
        p = plan(rt=rt)
        self.assertEqual(p.dcp, 1)
        self.assertLess(p.max_model_len, 1048576)
        self.assertEqual(p.max_model_len % 1024, 0)
        self.assertLessEqual(p.max_model_len * 1.10, p.pool_tokens)
        decisions = {d.key: d for d in p.decisions}
        self.assertIn("floor(", decisions["serve.max_model_len"].why)
        self.assertIn("DCP unsupported", decisions["serve.dcp"].why)

    def test_a_gqa_model_shards_its_cache_across_tensor_parallel_ranks(self):
        model = facts("example_gqa", 64 * 10**9)
        p = plan("ml.g6e.48xlarge", model=model)
        decisions = {d.key: d for d in p.decisions}
        self.assertEqual(decisions["kv.bytes_per_token"].value, 2 * 1 * 128 * 2 * 48)
        self.assertEqual(p.dcp, 1)
        self.assertEqual(p.max_model_len, 262144)

    def test_no_derived_window_appears_without_its_reason(self):
        cases = [("example_mla_dsa", 756, t) for t in ("ml.p5en.48xlarge", "ml.p6-b300.48xlarge", "ml.p6-b200.48xlarge")]
        cases += [("example_gqa", 64, t) for t in ("ml.g6e.48xlarge", "ml.g6e.12xlarge", "ml.p5.48xlarge")]
        for name, gigabytes, instance in cases:
            with self.subTest(name=name, instance=instance):
                try:
                    p = plan(instance, model=facts(name, gigabytes * 10**9))
                except sizing.PlanRefusal:
                    continue
                [window] = [d for d in p.decisions if d.key == "serve.max_model_len"]
                self.assertTrue(window.why)
                self.assertIn(window.source, ("derived",))
                self.assertTrue("max_position_embeddings" in window.why or "floor(" in window.why)


class RuleTest(unittest.TestCase):
    def test_explicit_values_are_kept_and_refused_when_they_cannot_fit(self):
        p = plan(cfg=resolved(**{"serve.dcp": 4, "serve.max_model_len": 524288}))
        self.assertEqual((p.dcp, p.max_model_len), (4, 524288))
        self.assertEqual({d.key: d.source for d in p.decisions}["serve.dcp"], "cli")
        with self.assertRaisesRegex(sizing.PlanRefusal, "serve.max_model_len 1048576 needs"):
            plan(cfg=resolved(**{"serve.dcp": 1, "serve.max_model_len": 1048576}))

    def test_below_min_context_the_plan_refuses(self):
        with self.assertRaisesRegex(sizing.PlanRefusal, "below serve.min_context"):
            plan("ml.p6-b200.48xlarge", model=facts(weight_bytes=1200 * 10**9),
                 cfg=resolved(**{"serve.min_context": 262144}))

    def test_weights_that_leave_no_kv_memory_are_refused(self):
        with self.assertRaisesRegex(sizing.PlanRefusal, "leave no KV-cache memory on ml.p5.48xlarge"):
            plan("ml.p5.48xlarge")

    def test_a_measured_pool_replaces_the_modeled_one(self):
        measured = sizing.Measured(pool_tokens=1_000_000, pool_dcp=8)
        p = plan(measured=measured)
        self.assertEqual(p.pool_class, "measured")
        # A measured pool below max_model_len x 1.10 lowers the next plan's window.
        self.assertEqual(p.max_model_len, int(1_000_000 / 1.10) // 1024 * 1024)

    def test_ec2_tries_unknown_flags_that_sagemaker_drops(self):
        rt = capabilities.runtime(VLLM_OPENAI)
        p = plan("p5en.48xlarge", rt=rt, substrate="ec2")
        self.assertIn("--async-scheduling", flags(p))
        self.assertEqual(p.dcp, 8)
        self.assertTrue(any(note.startswith("kept ") for note in p.notes) or p.notes == ())


class LearnedDefaultsTest(unittest.TestCase):
    """Defaults learned serving an mla_dsa model at 1M context on SageMaker H200, 2026-10-09."""

    def test_mla_dsa_with_dcp_always_passes_the_ag_rs_backend(self):
        p = plan()
        env = sizing.sm_env(list(p.pairs), {})
        self.assertEqual(env["SM_VLLM_DECODE_CONTEXT_PARALLEL_SIZE"], "8")
        self.assertEqual(env["SM_VLLM_DCP_COMM_BACKEND"], "ag_rs")
        explicit = plan(rt=EVERYWHERE, cfg=resolved(**{"serve.dcp": 4, "serve.max_model_len": 524288}))
        self.assertEqual(flags(explicit)["--dcp-comm-backend"], "ag_rs")
        self.assertNotIn("--dcp-comm-backend", flags(plan(rt=EVERYWHERE, cfg=resolved(**{"serve.dcp": 1,
                                                                                        "serve.max_model_len": 262144}))))

    def test_an_image_without_the_dcp_backend_gets_no_dcp_on_sagemaker(self):
        rt = Runtime(EVERYWHERE.digest, EVERYWHERE.name, None, "unknown",
                     {k: v for k, v in EVERYWHERE.flags.items() if k != "--dcp-comm-backend"}, EVERYWHERE.features)
        p = plan(rt=rt)
        self.assertEqual(p.dcp, 1)
        self.assertNotIn("--decode-context-parallel-size", flags(p))
        self.assertIn("DCP unsupported: mla_dsa needs --dcp-comm-backend ag_rs",
                      {d.key: d for d in p.decisions}["serve.dcp"].why)
        on_ec2 = plan("p5en.48xlarge", rt=rt, substrate="ec2")  # EC2 tries an unknown flag
        self.assertEqual((on_ec2.dcp, flags(on_ec2)["--dcp-comm-backend"]), (8, "ag_rs"))

    def test_the_repetition_penalty_overrides_the_checkpoint_and_shows_in_the_plan(self):
        p = plan()
        self.assertEqual(sizing.sm_env(list(p.pairs), {})["SM_VLLM_OVERRIDE_GENERATION_CONFIG"], REPETITION_1)
        decision = {d.key: d for d in p.decisions}["serve.repetition_penalty"]
        self.assertEqual((decision.value, decision.source), (1.0, "builtin"))
        self.assertIn("generation_config.json", decision.line())
        kept = plan(cfg=resolved(**{"serve.repetition_penalty": None}))
        self.assertNotIn("--override-generation-config", flags(kept))
        self.assertIn("keeps the checkpoint's", {d.key: d for d in kept.decisions}["serve.repetition_penalty"].why)
        self.assertEqual(flags(plan(cfg=resolved(**{"serve.repetition_penalty": 1.05})))["--override-generation-config"],
                         '{"repetition_penalty":1.05}')

    def test_an_explicit_generation_override_is_merged_not_replaced(self):
        profile = ["--override-generation-config", '{"temperature": 0.6}']
        self.assertEqual(json.loads(flags(plan(profile_args=profile))["--override-generation-config"]),
                         {"temperature": 0.6, "repetition_penalty": 1.0})
        own = ["--override-generation-config", '{"repetition_penalty": 1.05}']
        self.assertEqual(flags(plan(profile_args=own))["--override-generation-config"], '{"repetition_penalty":1.05}')
        key_wins = plan(profile_args=own, cfg=resolved(**{"serve.repetition_penalty": 1.0}))
        self.assertEqual(flags(key_wins)["--override-generation-config"], REPETITION_1)
        with self.assertRaisesRegex(sizing.ArgsError, "takes a JSON object"):
            plan(cfg=resolved(**{"serve.extra_args": ["--override-generation-config", "[1]"]}))


class CapabilityTest(unittest.TestCase):
    def test_unknown_flags_are_dropped_on_sagemaker_and_kept_on_ec2(self):
        rt = capabilities.runtime(DLC)
        pairs = [("--kv-offloading-backend", "native"), ("--enable-prefix-caching", None)]
        kept, notes = capabilities.gate(pairs, set(), rt, "sagemaker")
        self.assertEqual(kept, [("--enable-prefix-caching", None)])
        self.assertIn("dropped --kv-offloading-backend", notes[0])
        kept, _ = capabilities.gate(pairs, set(), rt, "ec2")
        self.assertEqual(kept, pairs)

    def test_a_learned_rejection_drops_the_flag_and_an_explicit_one_refuses(self):
        rt = capabilities.runtime(DLC)
        learned = Evidence(rejected=frozenset({"--enable-prefix-caching"}))
        kept, notes = capabilities.gate([("--enable-prefix-caching", None)], set(), rt, "ec2", learned)
        self.assertEqual(kept, [])
        self.assertIn("unrecognized arguments", notes[0])
        cfg = resolved(**{"serve.extra_args": ["--enable-prefix-caching"]})
        with self.assertRaisesRegex(sizing.PlanRefusal, "--enable-prefix-caching is configured"):
            plan(cfg=cfg, evidence=learned)

    def test_an_explicit_unknown_flag_is_kept_with_a_note(self):
        p = plan(cfg=resolved(**{"serve.extra_args": ["--kv-offloading-backend", "native"]}))
        self.assertEqual(flags(p)["--kv-offloading-backend"], "native")
        self.assertTrue(any(n.startswith("kept --kv-offloading-backend: configured explicitly") for n in p.notes))

    def test_architecture_evidence_does_not_carry_to_another_architecture(self):
        rt = capabilities.runtime(DLC)
        self.assertEqual(capabilities.feature_status(rt, "dcp:mla_dsa", "sm90")[0], "supported")
        self.assertEqual(capabilities.feature_status(rt, "dcp:mla_dsa", "sm103")[0], "unknown")

    def test_a_known_version_supports_flags_from_their_first_release(self):
        rt = capabilities.runtime(VLLM_OPENAI)
        self.assertEqual(capabilities.flag_status(rt, "--async-scheduling")[0], "supported")
        old = Runtime("sha256:old", "old", "0.9.0", "reported")
        self.assertEqual(capabilities.flag_status(old, "--decode-context-parallel-size")[0], "unknown")


class SmEnvTest(unittest.TestCase):
    OWNED = {"SM_VLLM_MODEL": "/opt/ml/model", "SM_VLLM_SERVED_MODEL_NAME": "drip-coder"}

    def test_json_flags_round_trip_byte_for_byte_and_a_bare_flag_is_true(self):
        pairs = sizing.parse_args(["--speculative-config", '{"method": "mtp", "num_speculative_tokens": 1}',
                                   "--enable-auto-tool-choice"], "test")
        env = sizing.sm_env(pairs, self.OWNED)
        self.assertEqual(env["SM_VLLM_SPECULATIVE_CONFIG"], MTP_1)
        self.assertEqual(json.loads(env["SM_VLLM_SPECULATIVE_CONFIG"]), {"method": "mtp", "num_speculative_tokens": 1})
        self.assertEqual(env["SM_VLLM_ENABLE_AUTO_TOOL_CHOICE"], "true")
        self.assertEqual(env["SM_VLLM_MODEL"], "/opt/ml/model")

    def test_the_flag_rules_refuse_what_terraform_used_to(self):
        cases = {
            "a value with a newline": ["--tool-call-parser", "a\nb"],
            "--flag=value": ["--tool-call-parser=x"],
            "an underscore": ["--tool_call_parser", "x"],
            "a stray value": ["--tool-call-parser", "x", "extra"],
            "a repeated flag": ["--enforce-eager", "--enforce-eager"],
            "an api key": ["--api-key", "x"],
            "a token flag": ["--hf-token", "x"],
            "an underscore secret hidden as a value": ["--enable-auto-tool-choice", "--hf_token", "x"],
            "an owned flag": ["--max-model-len", "524288"],
            "bad json": ["--speculative-config", "{not json"],
        }
        for label, tokens in cases.items():
            with self.subTest(label), self.assertRaises(sizing.ArgsError):
                sizing.parse_args(tokens, "test")
        with self.assertRaises(sizing.ArgsError):
            sizing.sm_env([("--tool-call-parser", "a\x07")], self.OWNED)

    def test_token_named_flags_that_are_not_secrets_pass(self):
        pairs = sizing.parse_args(["--tokenizer-mode", "auto", "--max-num-batched-tokens", "8192",
                                   "--return-tokens-as-token-ids"], "test")
        env = sizing.sm_env(pairs, {})
        self.assertEqual(env["SM_VLLM_RETURN_TOKENS_AS_TOKEN_IDS"], "true")

    def test_later_lists_win_true_is_bare_and_false_removes(self):
        merged = sizing.merge_args([("--tool-call-parser", "profile"), ("--enable-auto-tool-choice", None)],
                                   [("--tool-call-parser", "mine"), ("--enable-auto-tool-choice", "false"),
                                    ("--enforce-eager", "true")])
        self.assertEqual(merged, [("--tool-call-parser", "mine"), ("--enforce-eager", None)])


if __name__ == "__main__":
    unittest.main()
