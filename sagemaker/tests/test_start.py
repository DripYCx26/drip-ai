"""Pins `sagemaker/start.sh` (start flow, model picker, live dashboard) and the race it hands to
sagemaker/race.py, against stub CLIs and a fake SageMaker API."""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from start_fixtures import (  # noqa: E402
    ARN, BUCKET, CURL_CONFIG, DASHBOARD_ENV, ENDPOINT_ARN, GB, H1, H2, H3, H4,
    H200_POOL, KEY, LANES, LOG_LINES, MODEL_CONFIG, NAME, OTHER_ARN, P5E_TYPE, P5EN_TYPE, PINNED, READ_ONLY_AWS, SCRIPT, TEARDOWN, WEIGHTS, configs_made,
    creates, deletes, endpoints_left, listing, make_repo, metrics,
    models_made, operations, output, run_start, script_config, script_dashboard_cli, script_gate, script_lists,
    script_owner, script_quotas, script_race, script_start_cli, status_object, terraform_calls, tfvars_files,
    type_on_tty)

TUNING = "sagemaker/terraform/terraform.tfvars"


class StartFlowTest(unittest.TestCase):
    def assert_refused_without_changes(self, repo, result, message):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(message, result.stderr)
        self.assertEqual(tfvars_files(repo), {})
        self.assertEqual(terraform_calls(repo), [c for c in terraform_calls(repo) if "output" in c.argv])
        self.assertEqual(creates(repo), [])
        self.assertEqual(models_made(repo), {})

    def test_refuses_an_s3_folder_that_is_not_one_complete_model(self):
        def nested(sub):
            return (f"s3://{BUCKET}/models/solo/ contains nested model solo/{sub} (s3://{BUCKET}/models/solo/{sub}/),"
                    f" and SageMaker would load both; move solo/{sub} out of models/solo/")

        fetched = status_object(WEIGHTS)
        holding_inner = dict(WEIGHTS, **{"Inner/config.json": 10, "Inner/model.safetensors": 5})
        # (script_start_cli arguments, model name, start arguments, refusal)
        cases = {
            "no status object": (dict(status=None), NAME, (), "no readable fetch status object"),
            "copy still running": (dict(status=status_object(WEIGHTS, "copying")), NAME, (), "not a complete copy"),
            "a size differs": (dict(status=fetched, files=dict(WEIGHTS, **{"model-00002-of-00002.safetensors": 1})),
                               NAME, (), "S3 does not match"),
            "a file is missing": (dict(status=fetched, files={k: v for k, v in WEIGHTS.items() if k != "config.json"}),
                                  NAME, (), "S3 does not match"),
            "unlisted files two levels down": (
                dict(status=fetched, files=dict(WEIGHTS, **{"a/b/config.json": 10, "a/b/model.safetensors": 5,
                                                            "a/b/drip-ai-fetch.json": 300})),
                "solo", (), "S3 does not match"),
            "upload without weights": (dict(files={"config.json": 10, "tokenizer.json": 10}, with_status=False),
                                       "my-upload", ("--no-manifest",), "no *.safetensors"),
            "fetched holding a model": (dict(status=fetched, files=holding_inner), "solo", (), nested("Inner")),
            "upload holding a model": (dict(files=holding_inner, with_status=False), "solo", ("--no-manifest",),
                                       nested("Inner")),
            "upload holding config and weights": (
                dict(files=dict(WEIGHTS, **{"sub/config.json": 10, "sub/model.safetensors": 5}), with_status=False),
                "solo", ("--no-manifest",), nested("sub")),
            "upload holding a fetch status object": (
                dict(files=dict(WEIGHTS, **{"sub/drip-ai-fetch.json": 300}), with_status=False),
                "solo", ("--no-manifest",), nested("sub")),
        }
        for label, (start_cli, name, args, message) in cases.items():
            with self.subTest(label), make_repo() as repo:
                script_start_cli(repo, name=name, **start_cli)
                result = run_start(repo, name, *args)
                self.assert_refused_without_changes(repo, result, message)
                if "nested model" not in message:
                    self.assertNotIn("nested model", result.stderr)

    def test_no_manifest_starts_an_upload_without_reading_a_status_object(self):
        with make_repo() as repo:
            script_start_cli(repo, files=WEIGHTS, with_status=False, name="my-upload")
            result = run_start(repo, "my-upload", "--no-manifest")
            self.assertEqual(result.returncode, 0, result.stderr)
            [model] = models_made(repo).values()
            self.assertEqual(model["body"]["ModelDataSource"]["S3DataSource"]["S3Uri"],
                             f"s3://{BUCKET}/models/my-upload/")
            self.assertFalse([c for c in repo.calls() if c.tool == "aws" and c.argv[1] == "cp"
                              and c.argv[2].endswith("/drip-ai-fetch.json")])

    def test_refuses_second_segment_not_starting_with_letter_or_digit(self):
        with make_repo() as repo:
            result = repo.run(SCRIPT, "org/-x", env=DASHBOARD_ENV)
            self.assertEqual(result.returncode, 1)
            self.assertIn("each part starting with a letter or digit", result.stderr)
            self.assertEqual(repo.calls(), [])

    def test_uploaded_model_with_own_sub_folder_files_is_startable(self):
        variants = {
            "lone config": {"original/config.json": 10},
            "config and tokenizer": {"original/config.json": 10, "original/tokenizer.json": 10},
            "weights only": {"consolidated/model.safetensors": 5},
            "a marker two levels down": {"a/b/config.json": 10, "a/b/model.safetensors": 5,
                                         "a/b/drip-ai-fetch.json": 300},
        }
        for label, extra in variants.items():
            with self.subTest(label), make_repo() as repo:
                script_start_cli(repo, files=dict(WEIGHTS, **extra), with_status=False, name="solo")
                result = run_start(repo, "solo", "--no-manifest")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(creates(repo)), 4)

    def test_accepts_sub_folder_config_listed_in_own_manifest(self):
        own = dict(WEIGHTS, **{"original/config.json": 10})
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(own), files=own, name="solo")
            result = run_start(repo, "solo")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_races_only_the_classes_whose_gpus_hold_the_model(self):
        every_class = {"race": {"classes": ["h200", "b300", "b200", "h100"], "max_lanes": 8}}

        def weights(gigabytes):
            return {"config.json": 1000, "model.safetensors": gigabytes * GB - 1000}

        # (weights, classes raced, skipped classes)
        cases = {
            "800 GB skips the H100 class": (WEIGHTS, ["h200", "b300", "b200"], ["h100"]),
            "400 GB races all four": (weights(400), ["h200", "b300", "b200", "h100"], []),
            "1200 GB races the B-series classes": (weights(1200), ["b300", "b200"], ["h200", "h100"]),
            "1900 GB races only the B300 class": (weights(1900), ["b300"], ["h200", "b200", "h100"]),
        }
        for label, (files, raced, skipped) in cases.items():
            with self.subTest(label), make_repo(user_config=every_class) as repo:
                script_start_cli(repo, status=status_object(files), files=files)
                result = run_start(repo, NAME, "--no-watch")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sorted({name.split("-")[2] for name, _, _ in creates(repo)}), sorted(raced))
                shown = [line.split(" ")[1] for line in result.stdout.splitlines() if line.startswith("Skipping ")]
                self.assertEqual(sorted(shown), sorted(skipped))
                for cls in skipped:
                    self.assertRegex(result.stdout, rf"Skipping {cls} \(.*\): .* GB of weights leave no KV-cache memory")

    def test_a_class_that_holds_the_weights_but_not_the_window_serves_a_shorter_one(self):
        files = {"config.json": 1000, "model.safetensors": 1200 * GB - 1000}
        with make_repo(user_config={"race": {"classes": ["b200"]}}) as repo:
            script_start_cli(repo, status=status_object(files), files=files)
            result = run_start(repo, NAME, "--no-watch")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"serve\.max_model_len = \d+  derived  \(floor\([\d,]+ modeled tokens / 1\.10\)"
                                            r" to a multiple of 1024, below config\.json max_position_embeddings 131072\)")
            [model] = models_made(repo).values()
            window = int(model["body"]["Environment"]["SM_VLLM_MAX_MODEL_LEN"])
            self.assertLess(window, 131072)
            self.assertEqual(window % 1024, 0)

    def test_refuses_when_no_class_holds_the_model(self):
        with make_repo(user_config={"race": {"classes": ["h100"]}}) as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"s3://{BUCKET}/models/{NAME}/ fits no GPU class drip-ai races (800.0 GB of weights leave"
                          " no KV-cache memory on ml.p5.48xlarge", result.stderr)
            self.assertIn("nothing was created", result.stderr)
            self.assertEqual(creates(repo), [])
            self.assertNotIn(("aws", "put-object"), operations(repo))

    def test_refuses_tuning_left_in_the_sagemaker_tfvars(self):
        for line in ('instance_type = "ml.p5en.48xlarge"\n', 'instance_types = ["ml.p5en.48xlarge"]\n',
                     "max_model_len = 131072 # mine\n"):
            key = line.split(" ")[0]
            with self.subTest(key), make_repo(files={TUNING: line}) as repo:
                script_gate(repo)
                output(repo, "shared", "models_bucket", BUCKET)
                result = run_start(repo, NAME)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"sagemaker/terraform/terraform.tfvars sets {key}, which now lives in your drip-ai"
                              " config; move it there with: bin/drip-ai config import-tfvars", result.stderr)
                self.assertEqual(creates(repo), [])

    def test_start_creates_tagged_resources_through_the_api_and_runs_no_terraform(self):
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("needs about 222 MB/s from S3", result.stderr)
            self.assertEqual({c.argv[1] for c in terraform_calls(repo)}, {"output"})
            self.assertEqual(tfvars_files(repo), {})
            [(model_name, model)] = models_made(repo).items()
            self.assertRegex(model_name, r"^drip-ai-m-[0-9a-f]{8}$")
            body = model["body"]
            self.assertEqual(body["Image"], f"763104351884.dkr.ecr.us-east-1.amazonaws.com/vllm@{PINNED}")
            self.assertEqual(model["role"], "arn:aws:iam::111122223333:role/drip-ai-sagemaker-execution")
            env = body["Environment"]
            self.assertEqual(env["SM_VLLM_MODEL"], "/opt/ml/model")
            self.assertEqual(env["SM_VLLM_MAX_MODEL_LEN"], "131072")
            self.assertEqual(env["SM_VLLM_TENSOR_PARALLEL_SIZE"], "8")
            self.assertEqual(env["STANDARD_AUTO_INSTALL_REQ"], "false")
            self.assertFalse([k for k in env if re.search("(?i)(api_?key|secret|password|credential|_token$)", k)])
            tags = {"Project": "drip-ai", "ManagedBy": "drip-ai", "Stack": "sagemaker", "StartedBy": ARN}
            run_tags = {t["Key"]: t["Value"] for t in model["tags"]}
            self.assertEqual({k: run_tags[k] for k in tags}, tags)
            self.assertRegex(run_tags["RunId"], r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
            self.assertEqual(len(configs_made(repo)), 4)
            for name, config in configs_made(repo).items():
                self.assertRegex(name, r"^drip-ai-c-[0-9a-f]{8}-[1-4]$")
                [variant] = config["body"]
                self.assertEqual(variant["InstancePools"], [{"InstanceType": P5EN_TYPE, "Priority": 1},
                                                            {"InstanceType": P5E_TYPE, "Priority": 2}])
                self.assertEqual(variant["VariantInstanceProvisionTimeoutInSeconds"], 3600)
                self.assertNotIn("InstanceType", variant)
            groups = {c.argv[c.argv.index("--log-group-name") + 1] for c in repo.calls()
                      if c.argv[1:2] == ["create-log-group"]}
            self.assertEqual(groups, {f"/aws/sagemaker/Endpoints/{name}" for name in LANES})
            retention = [c.argv for c in repo.calls() if c.argv[1:2] == ["put-retention-policy"]]
            self.assertEqual({argv[argv.index("--retention-in-days") + 1] for argv in retention}, {"14"})
            # The owner gate lists the endpoints before anything is created.
            calls = repo.calls()
            gate = next(i for i, c in enumerate(calls) if "list-endpoints" in c.argv)
            first_create = next(i for i, c in enumerate(calls) if c.argv[1:2] == ["create-model"])
            self.assertLess(gate, first_create)
            # The dashboard follows the winner.
            stdout = result.stdout
            self.assertLess(stdout.index(f"Endpoint {H1} (h200 in us-east-1) serves models/{NAME}"),
                            stdout.index(f"== drip-ai SageMaker endpoint {H1}"))
            self.assertIn("Cost: running", stdout)

    def test_started_by_sanitized(self):
        long_arn = "arn:aws:iam::111122223333:user/" + "p" * 300
        cases = {
            "commas and brackets": ("arn:aws:sts::111122223333:assumed-role/Dev/al,ice(x)~y",
                                    "arn:aws:sts::111122223333:assumed-role/Dev/al-ice-x--y"),
            "longer than 256": (long_arn, long_arn[:256]),
        }
        for label, (arn, expected) in cases.items():
            with self.subTest(label), make_repo(arn=arn) as repo:
                script_start_cli(repo, status=status_object(WEIGHTS))
                result = run_start(repo, NAME, "--no-watch")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual({tags["StartedBy"] for _, _, tags in creates(repo)}, {expected})
        arn, expected = cases["commas and brackets"]
        with make_repo(arn=arn) as repo:
            script_gate(repo, expected)
            script_dashboard_cli(repo, owner=expected)
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already exists (yours)", result.stdout)

    def test_someone_elses_lane_blocks_start_before_anything_is_created(self):
        cases = {
            "other identity": (dict(owner=OTHER_ARN),
                               f"endpoint {H1} was started by {OTHER_ARN} at 2026-10-05T10:00:00+00:00"),
            "no StartedBy tag": (dict(owner="None"), "with no StartedBy tag"),
            "tag unreadable": (dict(owner="", tags_exit=254), "of unknown owner"),
        }
        for label, (owner_args, message) in cases.items():
            for args in ((NAME,), ()):
                with self.subTest(label, args=args), make_repo() as repo:
                    script_lists(repo, {H1: "InService"})
                    script_owner(repo, **owner_args)
                    type_on_tty(repo, f"{H1}\n1\nyes\n")
                    result = run_start(repo, *args)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(message, result.stderr)
                    self.assertIn("rerun with --take-over and type the endpoint name", result.stderr)
                    self.assertEqual(terraform_calls(repo), [])
                    self.assertEqual(creates(repo), [])
                    self.assertNotIn("Type the endpoint name", result.stdout)

    def script_take_over(self, repo):
        """Bob's Failed lane drip-ai-h200-us-east-1-1, then a race; the stop path's unlink."""
        repo.install_stub("shared/lib/codex_link.py", "codex_link")
        repo.stub("codex_link", "unlink")
        repo.stub("aws", "sagemaker", "list-tags", ENDPOINT_ARN, times=3, stdout=f"{OTHER_ARN}\n")
        bobs = {"arn": ENDPOINT_ARN, "created": "2026-10-05T10:00:00+00:00", "config": "drip-ai-1",
                "steps": [["Failed", "CUDA out of memory"]], "step": 0}
        script_start_cli(repo, status=status_object(WEIGHTS), endpoints={H1: bobs})

    def test_take_over_types_each_name_then_stops_then_races(self):
        for typed, accepted in ((f"{H1}\nyes\n", True), ("drip-ai\n", False), ("", False)):
            with self.subTest(repr(typed)), make_repo() as repo:
                self.script_take_over(repo)
                type_on_tty(repo, typed)
                result = run_start(repo, NAME, "--take-over", "--no-watch")
                self.assertIn(f"Endpoint {H1} was started by {OTHER_ARN}", result.stdout)
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(terraform_calls(repo), [c for c in terraform_calls(repo) if "output" in c.argv])
                    # Bob's lane goes before the race; the race's losers go after its win.
                    self.assertEqual(deletes(repo), [H1, H2, H3, H4])
                    self.assertEqual({tags["StartedBy"] for _, _, tags in creates(repo)}, {ARN})
                    self.assertEqual(list(endpoints_left(repo)), [H1])
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(f"the typed name did not match {H1}; nothing changed", result.stderr)
                    self.assertEqual([c for c in repo.calls() if c.tool == "codex_link"], [])
                    self.assertEqual(creates(repo), [])

    def test_yes_never_types_a_take_over_name(self):
        for typed in (None, f"{H1}\n"):
            with self.subTest(typed=typed), make_repo() as repo:
                self.script_take_over(repo)
                tty = repo.home / ("tty" if typed else "no-such-tty")
                if typed:
                    tty.write_text(typed)
                result = run_start(repo, NAME, "--take-over", "--yes", "--no-watch", DRIP_AI_TTY=str(tty))
                if typed is None:
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(f"the typed name did not match {H1}; nothing changed", result.stderr)
                    self.assertEqual(creates(repo), [])
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Approved with --yes", result.stdout)

    def test_own_lane_opens_dashboard_without_a_plan(self):
        for args in ((NAME,), ()):
            with self.subTest(args=args), make_repo() as repo:
                script_gate(repo, ARN)
                script_dashboard_cli(repo)
                result = run_start(repo, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Endpoint {H1} already exists (yours).", result.stdout)
                self.assertIn("bin/drip-ai sagemaker watch", result.stdout)
                self.assertIn(f"started by {ARN}", result.stdout)
                self.assertIn("Cost: running 3h12m", result.stdout)
                self.assertEqual(terraform_calls(repo), [])
                self.assertFalse({"cp", "list-objects-v2"} & {op for _, op in operations(repo)})

    def test_profile_args_reach_the_model_environment(self):
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 0, result.stderr)
            [model] = models_made(repo).values()
            env = model["body"]["Environment"]
            self.assertEqual((env["SM_VLLM_ENABLE_AUTO_TOOL_CHOICE"], env["SM_VLLM_TOOL_CALL_PARSER"],
                              env["SM_VLLM_REASONING_PARSER"]), ("true", "example_tool", "example_reasoning"))
            self.assertIn("Model profile: example_type (ExampleForCausalLM): --enable-auto-tool-choice"
                          " --tool-call-parser example_tool --reasoning-parser example_reasoning", result.stderr)

    def test_serve_extra_args_win_over_the_profile_and_reach_the_model(self):
        user = {"race": {"classes": ["h200"]}, "serve": {"extra_args": ["--tool-call-parser", "override_tool",
                                                                         "--enforce-eager"]}}
        with make_repo(user_config=user) as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME, "--no-watch")
            self.assertEqual(result.returncode, 0, result.stderr)
            [model] = models_made(repo).values()
            env = model["body"]["Environment"]
            self.assertEqual(env["SM_VLLM_TOOL_CALL_PARSER"], "override_tool")
            self.assertEqual(env["SM_VLLM_ENFORCE_EAGER"], "true")

    def test_refuses_a_model_when_no_profiles_are_configured(self):
        with make_repo(files={"shared/model-profiles/profiles.json": "{}"}) as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME)
            self.assert_refused_without_changes(
                repo, result, "no vLLM profile for model_type 'example_type' (ExampleForCausalLM)")

    def test_refuses_a_model_type_without_a_profile(self):
        config = dict(MODEL_CONFIG, model_type="unprofiled_type", architectures=["UnprofiledForCausalLM"])
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS), config=config)
            result = run_start(repo, NAME)
            self.assert_refused_without_changes(
                repo, result, "no vLLM profile for model_type 'unprofiled_type' (UnprofiledForCausalLM)")
            self.assertIn('\n  "unprofiled_type": {"vllm_args": ["--enable-auto-tool-choice", "--tool-call-parser",'
                          ' "<parser>", "--reasoning-parser", "<parser>"], "source": "<model card URL>"},',
                          result.stderr)

    def test_refuses_an_architecture_the_pinned_vllm_does_not_register(self):
        for label, arch in (("unknown", "FutureForCausalLM"), ("embedding only", "ExampleEmbeddingModel")):
            with self.subTest(label), make_repo() as repo:
                script_start_cli(repo, status=status_object(WEIGHTS), config=dict(MODEL_CONFIG, architectures=[arch]))
                result = run_start(repo, NAME)
                self.assert_refused_without_changes(
                    repo, result, f"names architecture '{arch}', which vLLM v0.30.0 in the pinned image does not register")

    def test_refuses_an_unreadable_or_malformed_config(self):
        no_type = {k: v for k, v in MODEL_CONFIG.items() if k != "model_type"}
        no_arch = {k: v for k, v in MODEL_CONFIG.items() if k != "architectures"}
        for label, config, message in (
                ("missing", None, f"could not read s3://{BUCKET}/models/{NAME}/config.json"),
                ("no model_type", no_type, "has no plain model_type"),
                ("no architectures", no_arch, "has no plain architectures list")):
            with self.subTest(label), make_repo() as repo:
                script_start_cli(repo, status=status_object(WEIGHTS), config=config)
                result = run_start(repo, NAME)
                self.assert_refused_without_changes(repo, result, message)

    def test_unchecked_model_skips_both_checks_with_a_warning(self):
        config = dict(MODEL_CONFIG, model_type="future_type", architectures=["FutureForCausalLM"])
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS), config=config)
            result = run_start(repo, NAME, "--unchecked-model")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("warning: --unchecked-model", result.stderr)
            [model] = models_made(repo).values()
            self.assertNotIn("SM_VLLM_TOOL_CALL_PARSER", model["body"]["Environment"])


class ApprovalTest(unittest.TestCase):
    """The plan shown before the race and the approval that starts it."""

    def race(self, repo, *args, typed="yes\n", env=None, **race):
        script_start_cli(repo, status=status_object(WEIGHTS), **race)
        type_on_tty(repo, typed)
        return run_start(repo, NAME, "--no-watch", *args, **(env or {}))

    def test_anything_but_yes_creates_nothing(self):
        for typed in ("no\n", "\n", ""):
            with self.subTest(repr(typed)), make_repo() as repo:
                result = self.race(repo, typed=typed)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("Type yes to start this race", result.stdout)
                self.assertIn("not approved; nothing was created", result.stderr)
                self.assertEqual(creates(repo), [])
                self.assertEqual(models_made(repo), {})
                self.assertIn(("aws", "delete-object"), operations(repo))  # the lease goes back

    def test_the_plan_shows_lanes_prices_retries_cap_and_worst_case(self):
        with make_repo() as repo:
            result = self.race(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("us-east-1 h200: 4 lanes (quota ml.p5en.48xlarge 2, ml.p5e.48xlarge 2)\n", result.stdout)
            self.assertIn("".join(f"  {i}) {name}  us-east-1  ml.p5en.48xlarge about $72.80/h, ml.p5e.48xlarge price"
                                  " unknown\n" for i, name in enumerate(LANES, 1))
                          + "Retries: a lane that fails for lack of capacity is deleted and created again at once;"
                          " any other failure ends that lane.\n"
                          "Cap: 8 hours without an InService lane, then every lane is deleted.\n"
                          "Worst case: up to 4 endpoints can bill at once if several get machines; the hourly total"
                          " is unknown because a pool's price is unknown.\n", result.stdout)
            self.assertRegex(result.stdout, r"serve\.max_model_len = 131072  derived  \(config\.json max_position_embeddings\)")
        user = {"race": {"classes": ["h200"], "instance_types": [P5EN_TYPE], "max_lanes": 2}}
        with make_repo(user_config=user) as repo:
            result = self.race(repo, "--hours", "12")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Cap: 12 hours without an InService lane", result.stdout)
            self.assertIn("Worst case: up to 2 endpoints can bill at once if several get machines, about $145.60/h",
                          result.stdout)

    def test_hours_takes_1_to_24(self):
        with make_repo() as repo:
            for hours in ("0", "25", "08", "1.5", "x"):
                with self.subTest(hours=hours):
                    result = run_start(repo, NAME, "--hours", hours)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn("--hours takes a whole number of hours from 1 to 24", result.stderr)
            self.assertEqual(run_start(repo, NAME, "--hours").returncode, 1)
            self.assertEqual(repo.calls(), [])

    def test_yes_starts_the_race_after_the_prices_without_a_terminal(self):
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME, "--yes", "--no-watch", DRIP_AI_TTY=str(repo.home / "no-such-tty"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Type yes", result.stdout)
            self.assertLess(result.stdout.index(f"  1) {H1}  us-east-1"),
                            result.stdout.index("Approved with --yes; starting the race."))

    def test_no_watch_returns_once_an_endpoint_of_yours_serves(self):
        for label, setup in (("race won", lambda r: script_start_cli(r, status=status_object(WEIGHTS))),
                             ("already yours", lambda r: (script_gate(r, ARN), script_dashboard_cli(r)))):
            with self.subTest(label), make_repo() as repo:
                setup(repo)
                result = run_start(repo, NAME, "--no-watch")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("== drip-ai SageMaker endpoint", result.stdout)
        with make_repo() as repo:
            script_lists(repo, {})
            output(repo, "shared", "models_bucket", BUCKET)
            script_bucket(repo)
            type_on_tty(repo, "q\n")
            result = run_start(repo, "--no-watch")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("Cancelled; nothing changed.", result.stdout)

    def test_no_watch_refuses_an_own_endpoint_that_cannot_serve(self):
        # A Failed one of your own no longer blocks: the race replaces it (test_race pins how).
        with make_repo() as repo:
            script_gate(repo, ARN, status="Deleting")
            script_dashboard_cli(repo)
            result = run_start(repo, NAME, "--no-watch")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"endpoint {H1} is Deleting; run bin/drip-ai sagemaker stop, then up", result.stderr)
            self.assertEqual([c for c in repo.calls() if c.tool == "curl" or "get-parameter" in c.argv], [])
        with make_repo() as repo:
            script_gate(repo, ARN, status="Creating")
            self.assertEqual(run_start(repo, NAME, "--no-watch").returncode, 0)

    def test_a_second_start_refuses_while_the_race_lease_is_held(self):
        with make_repo() as repo:
            result = self.race(repo, lease_holder=(OTHER_ARN, "build-box"))
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"sagemaker-race is in use by {OTHER_ARN} on build-box (pid 4242) until", result.stderr)
            self.assertIn("could not take the sagemaker-race lease; nothing was created", result.stderr)
            self.assertEqual(creates(repo), [])
            self.assertEqual(models_made(repo), {})

    def test_the_plan_lowers_lanes_to_the_endpoint_quota(self):
        cases = {
            "quotas 2 and 1 hold three lanes": ({P5EN_TYPE: 2, P5E_TYPE: 1},
                                                "us-east-1 h200: 3 lanes (quota ml.p5en.48xlarge 2, ml.p5e.48xlarge 1)",
                                                [H1, H2, H3], H200_POOL),
            "a type with quota 0 leaves the pool": ({P5EN_TYPE: 0, P5E_TYPE: 1},
                                                    "us-east-1 h200: 1 lane (quota ml.p5en.48xlarge 0, ml.p5e.48xlarge 1)",
                                                    [H1], [P5E_TYPE]),
        }
        for label, (quotas, line, lanes, pool) in cases.items():
            with self.subTest(label), make_repo() as repo:
                result = self.race(repo, "--yes", quotas=quotas)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(line + "\n", result.stdout)
                self.assertEqual([name for name, _, _ in creates(repo)], lanes)
                for config in configs_made(repo).values():
                    self.assertEqual([p["InstanceType"] for p in config["body"][0]["InstancePools"]], pool)

    def test_an_unreadable_quota_races_one_lane_per_class_with_a_warning(self):
        with make_repo() as repo:
            script_quotas(repo, exit_code=254)
            result = self.race(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("could not read the SageMaker endpoint quotas (An error occurred (AccessDeniedException)",
                          result.stderr)
            self.assertIn("us-east-1 h200: 1 lane (quota ml.p5en.48xlarge unknown, ml.p5e.48xlarge unknown)",
                          result.stdout)
            self.assertEqual([name for name, _, _ in creates(repo)], [H1])

    def test_refuses_when_every_class_has_no_endpoint_quota(self):
        with make_repo() as repo:
            result = self.race(repo, quotas={P5EN_TYPE: 0, P5E_TYPE: 0})
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("no region has a GPU class with endpoint quota, a foundation, and the weights", result.stderr)
            self.assertNotIn(("aws", "put-object"), operations(repo))

    def test_a_spend_cap_admits_only_the_lanes_it_can_price(self):
        user = {"race": {"classes": ["h200"], "instance_types": [P5EN_TYPE], "max_spend_per_hour_usd": 150}}
        with make_repo(user_config=user) as repo:
            result = self.race(repo, "--yes")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([name for name, _, _ in creates(repo)], [H1, H2])
            self.assertIn("about $145.60/h", result.stdout)


class DashboardTest(unittest.TestCase):
    def watch(self, repo, *args, **env):
        script_lists(repo, {H1: "InService"})
        return repo.run(SCRIPT, "--watch", *args, env=dict(DASHBOARD_ENV, **env))

    def test_dashboard_renders_each_panel(self):
        idle = f"idle 30+ min: still billing ≈ $72.80/h. Stop: {TEARDOWN}"
        up = "Proxy: up on localhost:8000"
        refused = ("Proxy: unavailable (could not read the API key: "
                   "the stored key is not sk- followed by letters and digits)")
        cases = {
            "cost from creation time": ({}, ["running 3h12m  ≈ $232.96 so far  ($72.80/h, ≈ $1,747/day)",
                                             "InService  ml.p5en.48xlarge x1",
                                             f"== drip-ai SageMaker endpoint {H1}  started by {ARN}",
                                             "Connect: http://localhost:8000/v1  model big-model"], [], 1),
            "idle": (dict(metric_data=metrics((45,))), [idle], [], 1),
            "busy": (dict(metric_data=metrics((45, 5))), [], [idle], 1),
            "metric failure": (dict(metric_exit=255), ["Usage: unavailable (An error occurred (AccessDenied))",
                                                       "Cost: running 3h12m", "the endpoint keeps billing"],
                               ["idle 30+ min"], 1),
            "real key": (dict(key=KEY), [up], [], 1),
            "plain key": (dict(key=KEY.removeprefix("sk-")), [up], [], 1),
            **{f"key {bad!r}": (dict(key=bad), [refused], [up], 0)
               for bad in ('sk-"quoted', "sk-back\\slash", "sk-with space", "sk-two\nlines", "sk-bell\x07", "sk-", "")},
        }
        for label, (dashboard_cli, shown, hidden, curls) in cases.items():
            with self.subTest(label), make_repo() as repo:
                script_dashboard_cli(repo, **dashboard_cli)
                result = self.watch(repo)
                self.assertEqual(result.returncode, 0, result.stderr)
                for line in shown:
                    self.assertIn(line, result.stdout)
                for line in hidden:
                    self.assertNotIn(line, result.stdout)
                self.assertEqual(len([c for c in repo.calls() if c.tool == "curl"]), curls)

    def test_watch_follows_a_named_endpoint_without_picking(self):
        with make_repo() as repo:
            script_dashboard_cli(repo)
            result = repo.run(SCRIPT, "--watch", "--endpoint", H1, env=DASHBOARD_ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"== drip-ai SageMaker endpoint {H1}", result.stdout)
            self.assertNotIn("list-endpoints", [c.argv[1] for c in repo.calls() if c.tool == "aws"])
            self.assertEqual(repo.run(SCRIPT, "--watch", "--endpoint", "theirs-1", env=DASHBOARD_ENV).returncode, 1)

    def test_api_key_never_in_argv(self):
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Proxy: up on localhost:8000", result.stdout)
            curl_calls = [c for c in repo.calls() if c.tool == "curl"]
            self.assertEqual([c.stdin for c in curl_calls], [CURL_CONFIG])
            for call in repo.calls():
                self.assertFalse(any(KEY in arg for arg in call.argv), call)
            self.assertNotIn(KEY, result.stdout + result.stderr)

    def test_watch_does_not_call_mutating_apis(self):
        with make_repo() as repo:
            script_dashboard_cli(repo)
            result = self.watch(repo, DRIP_AI_WATCH_ITERATIONS="1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(terraform_calls(repo), [])
            for tool, operation in operations(repo):
                if tool == "aws":
                    self.assertIn(operation, READ_ONLY_AWS)

    def test_dashboard_non_tty_appends_lines(self):
        with make_repo() as repo:
            repo.skip_sleeps()
            script_dashboard_cli(repo)
            result = self.watch(repo, DRIP_AI_WATCH_ITERATIONS="2", DRIP_AI_WATCH_SECONDS="1")
            self.assertEqual(repo.sleeps(), ["5"])  # the interval floor, once between two frames
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count(f"== drip-ai SageMaker endpoint {H1}"), 2)
            self.assertNotIn("\x1b", result.stdout)
            self.assertIn("t line4 [31mred[0m\n", result.stdout)
            self.assertIn("t line3\n", result.stdout)
            self.assertNotIn("line2", result.stdout)

    def test_dashboard_works_without_initialised_state(self):
        with make_repo(initialised=False) as repo:
            repo.stub("terraform", "init")
            script_dashboard_cli(repo, owner=OTHER_ARN)
            repo.stub("aws", "logs", "tail", f"/aws/sagemaker/Endpoints/{H1}", stdout=LOG_LINES)
            result = self.watch(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(terraform_calls(repo), [])
            self.assertIn(f"== drip-ai SageMaker endpoint {H1}  started by {OTHER_ARN}", result.stdout)
            self.assertIn("Proxy: up on localhost:8000", result.stdout)
            self.assertNotIn("unavailable", result.stdout)

    def test_watch_refuses_while_a_race_runs(self):
        with make_repo() as repo:
            script_lists(repo, {H1: "Creating", H2: "Creating"})
            result = repo.run(SCRIPT, "--watch", env=DASHBOARD_ENV)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"race in progress or interrupted ({H1} Creating, {H2} Creating)", result.stderr)


UPLOADED = {"config.json": 10, "model.safetensors": 20 * GB}
HUGE = {"config.json": 10, "model.safetensors": 1100 * GB}


def script_bucket(repo):
    """Org/Big-Model (complete), Org/Partial (copying), huge (too big), junk (no model), my-upload."""
    def answer(prefix, body):
        repo.stub("aws", "s3api", "list-objects-v2", "--delimiter", prefix, stdout=json.dumps(body))

    answer("models/", {"CommonPrefixes": [{"Prefix": f"models/{p}/"} for p in ("Org", "huge", "junk", "my-upload")]})
    answer("models/Org/", {"CommonPrefixes": [{"Prefix": "models/Org/Big-Model/"}, {"Prefix": "models/Org/Partial/"}]})
    for name, files, status in (("Org/Big-Model", WEIGHTS, "complete"),
                                ("Org/Partial", WEIGHTS, "copying"), ("huge", HUGE, "complete")):
        prefix = f"models/{name}/"
        answer(prefix, listing(prefix, files))
        repo.stub("aws", "s3", "cp", f"s3://{BUCKET}/{prefix}drip-ai-fetch.json",
                  stdout=json.dumps(status_object(files, status)))
        repo.stub("aws", "s3api", "list-objects-v2", prefix, stdout=json.dumps(listing(prefix, files)))
    answer("models/junk/", listing("models/junk/", {"README.md": 5}, with_status=False))
    answer("models/my-upload/", listing("models/my-upload/", UPLOADED, with_status=False))
    repo.stub("aws", "s3api", "list-objects-v2", "models/my-upload/",
              stdout=json.dumps(listing("models/my-upload/", UPLOADED, with_status=False)))


class PickerTest(unittest.TestCase):
    def pick(self, repo, typed):
        type_on_tty(repo, typed)
        return run_start(repo)

    def setup_repo(self, repo, *lists):
        script_lists(repo, *(lists or ({},)))
        output(repo, "shared", "models_bucket", BUCKET)
        script_bucket(repo)

    def assert_unchanged(self, repo, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tfvars_files(repo), {})
        self.assertEqual(creates(repo), [])

    def test_picker_lists_fetched_and_uploaded_models(self):
        with make_repo() as repo:
            self.setup_repo(repo)
            result = self.pick(repo, "q\n")
            self.assert_unchanged(repo, result)
            rows = [line for line in result.stdout.splitlines() if line.lstrip()[:2] in ("1)", "4)")]
            self.assertEqual(len(rows), 2, result.stdout)
            for part in ("Org/Big-Model", "fetched", f"{NAME}@0123456", "complete", "800.0 GB", "3 files", "fits"):
                self.assertIn(part, rows[0])
            for part in ("my-upload", "uploaded", "20.0 GB", "2 files", "fits"):
                self.assertIn(part, rows[1])
            self.assertNotIn("[current]", result.stdout)
            self.assertNotIn("junk", result.stdout)
            self.assertIn("weights must fit in the GPU memory of the largest raced type, ml.p5en.48xlarge", result.stdout)

    def test_picker_marks_incomplete_and_oversized_unselectable(self):
        with make_repo() as repo:
            self.setup_repo(repo)
            result = self.pick(repo, "2\n3\nq\n")
            self.assert_unchanged(repo, result)
            self.assertIn("2) Org/Partial is not selectable: still copying", result.stdout)
            self.assertIn("3) huge is not selectable: 1100.0 GB is over 90% of ml.p5en.48xlarge"
                          " GPU memory (1085 GB)", result.stdout)

    def test_picker_cancel_makes_no_changes(self):
        for typed in ("\n", "q\n", ""):
            with self.subTest(repr(typed)), make_repo() as repo:
                self.setup_repo(repo)
                result = self.pick(repo, typed)
                self.assert_unchanged(repo, result)
                self.assertIn("Cancelled; nothing changed.", result.stdout)

    def test_picker_selection_runs_start_flow(self):
        for typed, name, manifest in (("1\n", NAME, True), ("4\n", "my-upload", False)):
            with self.subTest(name), make_repo() as repo:
                self.setup_repo(repo)
                script_config(repo, name)
                script_race(repo)  # its fake SageMaker answers the gate's ListEndpoints too
                repo.stub("aws", "s3api", "head-object", f"models/{name}/config.json", stdout="{}")
                script_dashboard_cli(repo)
                result = self.pick(repo, typed + "yes\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                [model] = models_made(repo).values()
                self.assertEqual(model["body"]["ModelDataSource"]["S3DataSource"]["S3Uri"],
                                 f"s3://{BUCKET}/models/{name}/")
                self.assertIn("Cost: running", result.stdout)

    def script_solo_bucket(self, repo, files):
        script_lists(repo, {})
        output(repo, "shared", "models_bucket", BUCKET)
        repo.stub("aws", "s3api", "list-objects-v2", "--delimiter", "models/",
                  stdout=json.dumps({"CommonPrefixes": [{"Prefix": "models/solo/"}]}))
        top = listing("models/solo/", {k: v for k, v in files.items() if "/" not in k}, with_status=False)
        top["CommonPrefixes"] = [{"Prefix": f"models/solo/{sub}/"}
                                 for sub in sorted({k.split("/")[0] for k in files if "/" in k})]
        repo.stub("aws", "s3api", "list-objects-v2", "--delimiter", "models/solo/", stdout=json.dumps(top))
        repo.stub("aws", "s3api", "list-objects-v2", "models/solo/",
                  stdout=json.dumps(listing("models/solo/", files, with_status=False)))

    def test_picker_marks_model_with_nested_model_unselectable(self):
        nested = dict(UPLOADED, **{"Inner/config.json": 10, "Inner/model.safetensors": 5})
        with make_repo() as repo:
            self.script_solo_bucket(repo, nested)
            result = self.pick(repo, "1\nq\n")
            self.assert_unchanged(repo, result)
            self.assertIn("1) solo is not selectable: contains nested model solo/Inner", result.stdout)

    def test_picker_keeps_uploaded_model_with_own_sub_folder_config_selectable(self):
        own = dict(UPLOADED, **{"original/config.json": 10})
        with make_repo() as repo:
            self.script_solo_bucket(repo, own)
            result = self.pick(repo, "q\n")
            self.assert_unchanged(repo, result)
            self.assertIn("1) solo", result.stdout)
            self.assertNotIn("not selectable", result.stdout)

    def test_list_flag_is_read_only_and_marks_running_model(self):
        with make_repo() as repo:
            repo.stub("aws", "describe-endpoint-config", "drip-ai-20261005",
                      "ProductionVariants[0].ModelName", stdout="drip-ai-m-0a1b2c3d\n")
            repo.stub("aws", "describe-model", "drip-ai-m-0a1b2c3d", stdout=f"s3://{BUCKET}/models/my-upload/\n")
            script_owner(repo, OTHER_ARN)
            self.setup_repo(repo, {H1: "InService"})
            result = repo.run(SCRIPT, "--list")
            self.assert_unchanged(repo, result)
            self.assertIn("1) Org/Big-Model", result.stdout)
            [current] = [line for line in result.stdout.splitlines() if "[current]" in line]
            self.assertIn("4) my-upload", current)
            self.assertNotIn("Pick a number", result.stdout)
            for tool, operation in operations(repo):
                self.assertIn(operation, {"output"} if tool == "terraform" else READ_ONLY_AWS)


if __name__ == "__main__":
    unittest.main()
