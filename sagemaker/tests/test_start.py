"""Pins `sagemaker/start.sh` (start flow, race, model picker, live dashboard) against stub CLIs."""

import json
import signal
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "testing"))

from cli_stubs import StubRepo  # noqa: E402

SCRIPT = "sagemaker/start.sh"
ACCOUNT = "111122223333"
ARN = f"arn:aws:iam::{ACCOUNT}:user/alice"
OTHER_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/Dev/bob"
P5EN, P5E, P5 = "drip-ai-p5en-48xlarge", "drip-ai-p5e-48xlarge", "drip-ai-p5-48xlarge"
LANES = ((P5EN, "ml.p5en.48xlarge"), (P5E, "ml.p5e.48xlarge"))  # the default instance_types
DEFAULT_TYPES = '["ml.p5en.48xlarge", "ml.p5e.48xlarge"]'


def lane_arn(name):
    return f"arn:aws:sagemaker:us-east-1:{ACCOUNT}:endpoint/{name}"


ENDPOINT_ARN = lane_arn(P5EN)
STATE_BUCKET = f"drip-ai-tfstate-{ACCOUNT}-us-east-1"
INITIALISED = {
    f"{root}/.terraform/terraform.tfstate": json.dumps(
        {"version": 3, "backend": {"type": "s3", "config": {"bucket": STATE_BUCKET}}})
    for root in ("sagemaker/terraform", "shared/terraform")
}
NOT_FOUND = ('An error occurred (ValidationException) when calling the DescribeEndpoint operation:'
             ' Could not find endpoint.\n')
BUCKET = "drip-ai-models-canary"
NAME = "Org/Big-Model"
REVISION = "0123456789abcdef0123456789abcdef01234567"
KEY = "sk-CanaryApiKey0123456789abcdefghijklmnopqrstuvwxyzAB"  # "sk-" and 48 letters and digits, like the real key
CURL_CONFIG = f'header = "Authorization: Bearer {KEY}"\n'
CREATED = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
NOW = int(CREATED.timestamp()) + 3 * 3600 + 12 * 60
GB = 10**9
WEIGHTS = {
    "config.json": 1000,
    "model-00001-of-00002.safetensors": 400 * GB,
    "model-00002-of-00002.safetensors": 400 * GB - 1000,
}
TUNING = "sagemaker/terraform/terraform.tfvars"
TEARDOWN = "bin/drip-ai sagemaker stop"
ENDPOINT = {
    "EndpointName": P5EN,
    "EndpointArn": ENDPOINT_ARN,
    "EndpointConfigName": "drip-ai-p5en-48xlarge-20261005",
    "EndpointStatus": "InService",
    "CreationTime": "2026-10-05T10:00:00.000000+00:00",
    "LastModifiedTime": "2026-10-05T10:41:00+00:00",
    "ProductionVariants": [{"VariantName": "main", "CurrentInstanceCount": 1}],
}
LOG_LINES = "t line1\nt line2\nt line3\nt line4 \x1b[31mred\x1b[0m\nt line5\n"
DASHBOARD_ENV = {"DRIP_AI_NOW_EPOCH": str(NOW), "DRIP_AI_WATCH_ITERATIONS": "1"}
MODEL_CONFIG = {"model_type": "example_type", "architectures": ["ExampleForCausalLM"]}
MODEL_ARGS = '["--enable-auto-tool-choice", "--tool-call-parser", "example_tool", "--reasoning-parser", "example_reasoning"]'
READ_ONLY_AWS = {
    "cp", "list-objects-v2", "describe-endpoint", "describe-endpoint-config", "describe-model",
    "list-tags", "get-metric-data", "get-parameter", "tail", "get-caller-identity", "list-endpoints",
}
# Wraps the terraform stub so that `apply` keeps running, as Terraform's endpoint wait does, until
# the APPLY_INTS-th INT (default 1) or a TERM reaches it (each recorded in $HOME/apply.signal) or 60
# seconds pass.
HANGING_TERRAFORM = """#!/bin/sh
"$0.stub" "$@" || exit $?
case " $* " in *" apply "*) ;; *) exit 0 ;; esac
ints=0
trap 'ints=$((ints + 1)); echo INT >>"$HOME/apply.signal"; [ "$ints" -lt "${APPLY_INTS:-1}" ] || exit 130' INT
trap 'echo TERM >"$HOME/apply.signal"; exit 143' TERM
i=0
while [ "$i" -lt 300 ]; do sleep 0.2; i=$((i + 1)); done
"""


def make_repo(files=None, initialised=True, arn=ARN):
    """A StubRepo with the caller identity, a terminal that types yes, and, by default, both roots
    initialised."""
    model_fixtures = {
        "shared/model-profiles/profiles.json": json.dumps({"example_type": {
            "vllm_args": json.loads(MODEL_ARGS), "source": "https://example.com/model-card"}}),
        "shared/model-profiles/vllm-architectures.json": json.dumps({
            "vllm_ref": "v0.30.0", "architectures": ["ExampleForCausalLM", "UnprofiledForCausalLM"]}),
    }
    repo = StubRepo(files={**(INITIALISED if initialised else {}), **model_fixtures, **(files or {})})
    repo.stub("aws", "sts", "get-caller-identity", stdout=f"{ACCOUNT}\t{arn}\n")
    type_on_tty(repo, "yes\n")
    return repo


def type_on_tty(repo, text):
    (repo.home / "tty").write_text(text)


def hang_apply(repo):
    terraform = repo.bin_dir / "terraform"
    terraform.rename(repo.bin_dir / "terraform.stub")
    terraform.write_text(HANGING_TERRAFORM)
    terraform.chmod(0o755)


def listed(states):
    """A ListEndpoints answer: `states` maps endpoint name to status, in the order AWS lists them."""
    return "".join(f"{name}\t{status}\t{lane_arn(name)}\n" for name, status in states.items())


def script_lists(repo, *answers):
    """Answers ListEndpoints with each of `answers` once, in order; the last one repeats."""
    for index, states in enumerate(answers):
        repo.stub("aws", "sagemaker", "list-endpoints", stdout=listed(states),
                  times=None if index == len(answers) - 1 else 1)


def plan_json(lanes=LANES, actions=("create",)):
    return {"resource_changes": [
        {"address": f'aws_sagemaker_endpoint.server["{type_}"]', "type": "aws_sagemaker_endpoint",
         "index": type_, "change": {"actions": list(actions), "after": {"name": name}}}
        for name, type_ in lanes
    ]}


def output(repo, root, name, value):
    repo.stub("terraform", f"-chdir={repo.root}/{root}/terraform", "output", "-raw", name,
              stdout=value)


def script_owner(repo, owner, status="InService", tags_exit=0, name=P5EN):
    """The owner reads of lane `name`, tagged `owner` ("None" is how the CLI prints a missing tag)."""
    repo.stub("aws", "sagemaker", "describe-endpoint", name, "text",
              stdout=f"{lane_arn(name)}\t{status}\t2026-10-05T10:00:00+00:00\tdrip-ai-20261005\n")
    repo.stub("aws", "sagemaker", "list-tags", lane_arn(name), stdout=f"{owner}\n", exit=tags_exit,
              stderr="An error occurred (AccessDeniedException)\n" if tags_exit else "")


def script_gate(repo, owner=None, status="InService"):
    """The owner gate: no endpoint when `owner` is None, else lane drip-ai-p5en-48xlarge tagged
    `owner`. Later ListEndpoints calls see the same."""
    if owner is None:
        script_lists(repo, {})
        return
    script_lists(repo, {P5EN: status})
    script_owner(repo, owner, status)


def script_race(repo, apply_exit=0, plan=None):
    """The race's Terraform calls (plan, show, apply, and a state list holding the p5en lane), the
    caller's StartedBy on every lane, deletes that succeed, and DescribeEndpoint finding no lane.
    ListEndpoints is scripted apart."""
    repo.stub("terraform", "plan")
    repo.stub("terraform", "show", "-json", stdout=json.dumps(plan or plan_json()))
    repo.stub("terraform", "apply", exit=apply_exit)
    repo.stub("terraform", "state", "list", stdout='aws_sagemaker_endpoint.server["ml.p5en.48xlarge"]\n')
    repo.stub("aws", "sagemaker", "list-tags", stdout=f"{ARN}\n")
    repo.stub("aws", "sagemaker", "delete-endpoint")
    repo.stub("aws", "sagemaker", "describe-endpoint", "text", stderr=NOT_FOUND, exit=254)


def terraform_calls(repo):
    return [c for c in repo.calls() if c.tool == "terraform"]


def tfvars_files(repo):
    """Every tfvars file in the repo copy, as {relative path: text}."""
    return {str(path.relative_to(repo.root)): path.read_text()
            for path in repo.root.rglob("*.tfvars")}


def status_object(files, status="complete"):
    return {
        "repo": NAME,
        "revision": REVISION,
        "status": status,
        "files": [{"path": path, "size": size} for path, size in files.items()],
        "total_bytes": sum(files.values()),
    }


def listing(prefix, files, with_status=True):
    contents = [{"Key": prefix + path, "Size": size} for path, size in files.items()]
    if with_status:
        contents.append({"Key": prefix + "drip-ai-fetch.json", "Size": 300})
    return {"Contents": contents}


def metrics(invocation_ages=(), fail=False):
    """A get-metric-data answer; `invocation_ages` are minutes before NOW with one call each."""
    def iso(age_minutes):
        return datetime.fromtimestamp(NOW - age_minutes * 60, timezone.utc).isoformat()

    results = [
        {"Id": "inv", "Timestamps": [iso(a) for a in invocation_ages],
         "Values": [1.0] * len(invocation_ages), "StatusCode": "Complete"},
        {"Id": "lat", "Timestamps": [iso(1)], "Values": [1840000.0], "StatusCode": "Complete"},
        {"Id": "gpu", "Timestamps": [iso(1)], "Values": [412.0], "StatusCode": "Complete"},
    ]
    return {"MetricDataResults": results}


def script_dashboard_cli(repo, endpoint=ENDPOINT, metric_data=None, metric_exit=0, owner=ARN, key=KEY):
    """The dashboard's reads of lane drip-ai-p5en-48xlarge; ListEndpoints is scripted apart."""
    repo.stub("aws", "ssm", "get-parameter", "/drip-ai/api-key", stdout=key + "\n")
    repo.stub("aws", "sagemaker", "describe-endpoint", "json", stdout=json.dumps(endpoint))
    repo.stub("aws", "sagemaker", "list-tags", ENDPOINT_ARN, stdout=f"{owner}\n")
    repo.stub("aws", "sagemaker", "describe-endpoint-config", "drip-ai-p5en-48xlarge-20261005",
              "ProductionVariants[0].[InstanceType,InitialInstanceCount]", stdout="ml.p5en.48xlarge\t1\n")
    repo.stub("aws", "sagemaker", "describe-endpoint-config", "drip-ai-p5en-48xlarge-20261005",
              "ProductionVariants[0].ModelName", stdout="drip-ai-0a1b2c3d\n")
    repo.stub("aws", "sagemaker", "describe-model", "drip-ai-0a1b2c3d",
              "PrimaryContainer.Environment.[SM_VLLM_SERVED_MODEL_NAME,SM_VLLM_MAX_MODEL_LEN]",
              stdout="big-model\t131072\n")
    repo.stub(
        "aws", "cloudwatch", "get-metric-data",
        stdout="" if metric_exit else json.dumps(metric_data or metrics([5])),
        stderr="An error occurred (AccessDenied)\n" if metric_exit else "",
        exit=metric_exit,
    )
    repo.stub("aws", "logs", "tail", stdout=LOG_LINES)
    repo.stub("curl", stdin=f'header = "Authorization: Bearer {key}"\n', stdout="200")


def script_config(repo, name, config=MODEL_CONFIG):
    """The model's config.json in S3; `config` None makes the read fail."""
    uri = f"s3://{BUCKET}/models/{name}/config.json"
    if config is None:
        repo.stub("aws", "s3", "cp", uri, stderr="fatal error: 404\n", exit=1)
    else:
        repo.stub("aws", "s3", "cp", uri, stdout=json.dumps(config))


def script_start_cli(repo, status=None, files=WEIGHTS, with_status=True, name=NAME, config=MODEL_CONFIG,
                     lists=({}, {P5EN: "InService"}), apply_exit=0, plan=None):
    """A start of models/<name>/: by default an empty gate, then drip-ai-p5en-48xlarge wins alone."""
    prefix = f"models/{name}/"
    output(repo, "shared", "models_bucket", BUCKET)
    script_config(repo, name, config)
    status_uri = f"s3://{BUCKET}/{prefix}drip-ai-fetch.json"
    if status is None:
        repo.stub("aws", "s3", "cp", status_uri, stderr="fatal error: 404\n", exit=1)
    else:
        repo.stub("aws", "s3", "cp", status_uri, stdout=json.dumps(status))
    repo.stub("aws", "s3api", "list-objects-v2", prefix,
              stdout=json.dumps(listing(prefix, files, with_status)))
    script_race(repo, apply_exit=apply_exit, plan=plan)
    script_lists(repo, *lists)
    script_dashboard_cli(repo)


def operations(repo):
    """(tool, operation) per call: the terraform subcommand, the aws API, or curl."""
    found = []
    for call in repo.calls():
        if call.tool == "terraform":
            found.append(("terraform", call.argv[1]))
        elif call.tool == "aws":
            found.append(("aws", call.argv[1]))
        else:
            found.append((call.tool, ""))
    return found


def run_start(repo, *args, **env):
    """Runs a start with the race's waits skipped; RaceSignalTest keeps them real."""
    repo.skip_sleeps()
    return repo.run(SCRIPT, *args, env={**DASHBOARD_ENV, "DRIP_AI_TTY": str(repo.home / "tty"), **env})


def plan_argv(repo):
    [plan] = [c for c in terraform_calls(repo) if "plan" in c.argv]
    return plan.argv


def plan_var(repo, name):
    """The value the plan got as `-var NAME=...`."""
    argv = plan_argv(repo)
    [value] = [argv[i + 1].partition("=")[2] for i in range(len(argv) - 1)
               if argv[i] == "-var" and argv[i + 1].startswith(name + "=")]
    return value


class StartFlowTest(unittest.TestCase):
    def assert_refused_without_changes(self, repo, result, message):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(message, result.stderr)
        self.assertEqual(tfvars_files(repo), {})
        self.assertFalse({("terraform", "plan"), ("terraform", "apply")} & set(operations(repo)))

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
            # Unlisted files, not a nested model: the marker is two levels down.
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
            self.assertIn("model_source=my-upload", plan_argv(repo))
            self.assertFalse([c for c in repo.calls() if c.tool == "aws" and c.argv[1] == "cp"
                              and c.argv[2].endswith("/drip-ai-fetch.json")])

    def test_refuses_second_segment_not_starting_with_letter_or_digit(self):
        with make_repo() as repo:
            result = repo.run(SCRIPT, "org/-x", env=DASHBOARD_ENV)
            self.assertEqual(result.returncode, 1)
            self.assertIn("each part starting with a letter or digit", result.stderr)
            self.assertEqual(repo.calls(), [])

    def test_uploaded_model_with_own_original_config_is_startable(self):
        variants = {
            "lone config": {"original/config.json": 10},
            "config and tokenizer": {"original/config.json": 10, "original/tokenizer.json": 10},
            "weights only": {"consolidated/model.safetensors": 5},
        }
        for label, extra in variants.items():
            with self.subTest(label), make_repo() as repo:
                script_start_cli(repo, files=dict(WEIGHTS, **extra), with_status=False, name="solo")
                result = run_start(repo, "solo", "--no-manifest")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("model_source=solo", plan_argv(repo))

    def test_marker_two_levels_down_is_ignored(self):
        deep = {"a/b/config.json": 10, "a/b/model.safetensors": 5, "a/b/drip-ai-fetch.json": 300}
        with make_repo() as repo:
            script_start_cli(repo, files=dict(WEIGHTS, **deep), with_status=False, name="solo")
            result = run_start(repo, "solo", "--no-manifest")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("model_source=solo", plan_argv(repo))

    def test_accepts_sub_folder_config_listed_in_own_manifest(self):
        own = dict(WEIGHTS, **{"original/config.json": 10})
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(own), files=own, name="solo")
            result = run_start(repo, "solo")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("model_source=solo", plan_argv(repo))

    def test_races_only_the_lanes_whose_gpu_memory_holds_the_model(self):
        tuning = 'instance_types = ["ml.p5en.48xlarge", "ml.p5e.48xlarge", "ml.p5.48xlarge"] # H200, H200, H100\n'
        smaller = {"config.json": 1000, "model.safetensors": 400 * GB - 1000}
        three = (*LANES, (P5, "ml.p5.48xlarge"))
        cases = {
            "800 GB skips the 640 GB lane": (
                WEIGHTS, LANES, ["Skipping ml.p5.48xlarge: 800.0 GB is over 90% of its 640 GB GPU memory"]),
            "400 GB races all three": (smaller, three, []),
        }
        for label, (files, lanes, skipped) in cases.items():
            with self.subTest(label), make_repo(files={TUNING: tuning}) as repo:
                script_start_cli(repo, status=status_object(files), files=files, plan=plan_json(lanes=lanes))
                result = run_start(repo, NAME)
                self.assertEqual(result.returncode, 0, result.stderr)
                types = ", ".join(f'"{type_}"' for _, type_ in lanes)
                self.assertEqual(plan_var(repo, "instance_types"), f"[{types}]")
                self.assertEqual([line for line in result.stdout.splitlines() if line.startswith("Skipping")],
                                 skipped)
                self.assertIn(f"Worst case: all {len(lanes)} bill together", result.stdout)
                self.assertEqual(tfvars_files(repo), {TUNING: tuning})

    def test_refuses_when_no_lane_holds_the_model(self):
        tuning = 'instance_types = ["ml.p5.48xlarge"]\n'
        with make_repo(files={TUNING: tuning}) as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"s3://{BUCKET}/models/{NAME}/ holds 800.0 GB, over 90% of the GPU memory of every"
                          " type in instance_types; pick a smaller model or add a larger type", result.stderr)
            self.assertIn("nothing was created", result.stderr)
            self.assertFalse({("terraform", "plan"), ("terraform", "apply")} & set(operations(repo)))
            self.assertEqual(tfvars_files(repo), {TUNING: tuning})

    def test_refuses_the_replaced_instance_type_key(self):
        tuning = 'instance_type = "ml.p5en.48xlarge"\n'
        with make_repo(files={TUNING: tuning}) as repo:
            script_gate(repo)
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn('sets instance_type, which is now a list raced in order; replace that line with:'
                          ' instance_types = ["ml.p5en.48xlarge", "ml.p5e.48xlarge"]', result.stderr)
            self.assertEqual(terraform_calls(repo), [])

    def test_start_passes_vars_and_started_by_never_writes_tfvars(self):
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("needs about 222 MB/s from S3", result.stderr)
            argv = plan_argv(repo)
            self.assertEqual(argv[:3], [f"-chdir={repo.root}/sagemaker/terraform", "plan", "-input=false"])
            self.assertRegex(argv[3], r"^-out=.+/start-sagemaker\.[^/]+/plan$")
            self.assertEqual(argv[4:], [
                "-var", f"model_source={NAME}", "-var", "model_from_s3=true",
                "-var", f"started_by={ARN}", "-var", f"vllm_model_args={MODEL_ARGS}",
                "-var", f"instance_types={DEFAULT_TYPES}",
            ])
            [apply] = [c for c in terraform_calls(repo) if "apply" in c.argv]
            self.assertEqual(apply.env["TF_VAR_region"], "us-east-1")
            self.assertEqual(tfvars_files(repo), {})
            self.assertFalse((repo.root / "infra").exists())
            # The owner gate lists the endpoints before the first Terraform call.
            calls = repo.calls()
            first_terraform = next(i for i, c in enumerate(calls) if c.tool == "terraform")
            gate = next(i for i, c in enumerate(calls) if "list-endpoints" in c.argv)
            self.assertLess(gate, first_terraform)
            # The dashboard follows the winner.
            stdout = result.stdout
            self.assertLess(stdout.index(f"Endpoint {P5EN} (ml.p5en.48xlarge) serves models/{NAME}"),
                            stdout.index("Cost: running"))

    def test_started_by_sanitized(self):
        long_arn = f"arn:aws:iam::{ACCOUNT}:user/" + "p" * 300
        cases = {
            "commas and brackets": (f"arn:aws:sts::{ACCOUNT}:assumed-role/Dev/al,ice(x)~y",
                                    f"arn:aws:sts::{ACCOUNT}:assumed-role/Dev/al-ice-x--y"),
            "longer than 256": (long_arn, long_arn[:256]),
        }
        for label, (arn, expected) in cases.items():
            with self.subTest(label), make_repo(arn=arn) as repo:
                repo.stub("aws", "sagemaker", "list-tags", stdout=f"{expected}\n")
                script_start_cli(repo, status=status_object(WEIGHTS))
                result = run_start(repo, NAME)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(plan_var(repo, "started_by"), expected)
        # The tag holds the sanitized value, so it still reads as the caller's own endpoint.
        arn, expected = cases["commas and brackets"]
        with make_repo(arn=arn) as repo:
            script_gate(repo, expected)
            script_dashboard_cli(repo, owner=expected)
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already exists (yours)", result.stdout)

    def test_someone_elses_lane_blocks_start_before_any_terraform_call(self):
        cases = {
            "other identity": (dict(owner=OTHER_ARN),
                               f"endpoint {P5EN} was started by {OTHER_ARN} at 2026-10-05T10:00:00+00:00"),
            "no StartedBy tag": (dict(owner="None"), "with no StartedBy tag"),
            "tag unreadable": (dict(owner="", tags_exit=254), "of unknown owner"),
        }
        for label, (owner_args, message) in cases.items():
            for args in ((NAME,), ()):
                with self.subTest(label, args=args), make_repo() as repo:
                    script_lists(repo, {P5EN: "InService"})
                    script_owner(repo, **owner_args)
                    type_on_tty(repo, f"{P5EN}\n1\nyes\n")
                    result = run_start(repo, *args)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(message, result.stderr)
                    self.assertIn("rerun with --take-over and type the endpoint name", result.stderr)
                    self.assertEqual(terraform_calls(repo), [])
                    self.assertNotIn("Type the endpoint name", result.stdout)

    def test_take_over_types_each_name_then_stops_then_races(self):
        for typed, accepted in ((f"{P5EN}\nyes\n", True), ("drip-ai\n", False), ("", False)):
            with self.subTest(repr(typed)), make_repo() as repo:
                repo.install_stub("shared/lib/codex_link.py", "codex_link")
                repo.stub("codex_link", "unlink")
                repo.stub("terraform", "destroy")
                # Bob's Failed lane is read at the gate, at the typed name, and before its delete.
                repo.stub("aws", "sagemaker", "describe-endpoint", P5EN, "text", times=3,
                          stdout=f"{ENDPOINT_ARN}\tFailed\t2026-10-05T10:00:00+00:00\tdrip-ai-1\n")
                repo.stub("aws", "sagemaker", "list-tags", ENDPOINT_ARN, times=3, stdout=f"{OTHER_ARN}\n")
                repo.stub("aws", "sagemaker", "describe-endpoint", P5EN, "text", exit=254,
                          stderr=f'An error occurred (ValidationException): Could not find endpoint "{P5EN}".\n')
                repo.stub("aws", "sagemaker", "wait", "endpoint-deleted")
                # Gate, after the destroy (outside state), then the race's first poll.
                script_start_cli(repo, status=status_object(WEIGHTS),
                                 lists=({P5EN: "Failed"}, {P5EN: "Failed"}, {P5EN: "InService"}))
                type_on_tty(repo, typed)
                result = run_start(repo, NAME, "--take-over")
                self.assertIn(f"Endpoint {P5EN} was started by {OTHER_ARN}", result.stdout)
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    order = [c.argv[1] for c in repo.calls() if c.tool == "terraform" and c.argv[1] != "output"]
                    self.assertEqual(order, ["destroy", "plan", "show", "apply", "state"])
                    self.assertEqual(plan_var(repo, "started_by"), ARN)
                    deletes = [c.argv for c in repo.calls() if "delete-endpoint" in c.argv]
                    self.assertEqual(len(deletes), 1)
                    self.assertIn(P5EN, deletes[0])
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(f"the typed name did not match {P5EN}; nothing changed", result.stderr)
                    self.assertEqual([c for c in repo.calls() if c.tool in ("terraform", "codex_link")], [])

    def test_yes_never_types_a_take_over_name(self):
        for typed in (None, f"{P5EN}\n"):
            with self.subTest(typed=typed), make_repo() as repo:
                repo.install_stub("shared/lib/codex_link.py", "codex_link")
                repo.stub("codex_link", "unlink")
                repo.stub("terraform", "destroy")
                repo.stub("aws", "sagemaker", "describe-endpoint", P5EN, "text", times=3,
                          stdout=f"{ENDPOINT_ARN}\tFailed\t2026-10-05T10:00:00+00:00\tdrip-ai-1\n")
                repo.stub("aws", "sagemaker", "list-tags", ENDPOINT_ARN, times=3, stdout=f"{OTHER_ARN}\n")
                repo.stub("aws", "sagemaker", "describe-endpoint", P5EN, "text", exit=254, stderr=NOT_FOUND)
                repo.stub("aws", "sagemaker", "wait", "endpoint-deleted")
                script_start_cli(repo, status=status_object(WEIGHTS),
                                 lists=({P5EN: "Failed"}, {P5EN: "Failed"}, {P5EN: "InService"}))
                tty = repo.home / ("tty" if typed else "no-such-tty")
                if typed:
                    tty.write_text(typed)
                result = run_start(repo, NAME, "--take-over", "--yes", DRIP_AI_TTY=str(tty))
                if typed is None:
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(f"the typed name did not match {P5EN}; nothing changed", result.stderr)
                    self.assertEqual(terraform_calls(repo), [])
                else:
                    # The typed name took over; --yes answered only Terraform's destroy and the race.
                    self.assertEqual(result.returncode, 0, result.stderr)
                    [destroy] = [c.argv for c in terraform_calls(repo) if "destroy" in c.argv]
                    self.assertEqual(destroy[1:4], ["destroy", "-auto-approve", "-input=false"])
                    self.assertIn("Approved with --yes", result.stdout)

    def test_own_lane_opens_dashboard_without_a_plan(self):
        for args in ((NAME,), ()):
            with self.subTest(args=args), make_repo() as repo:
                script_gate(repo, ARN)
                script_dashboard_cli(repo)
                result = run_start(repo, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Endpoint {P5EN} already exists (yours).", result.stdout)
                self.assertIn("bin/drip-ai sagemaker watch", result.stdout)
                self.assertIn("bin/drip-ai sagemaker tunnel", result.stdout)
                self.assertIn(f"started by {ARN}", result.stdout)
                self.assertIn("Cost: running 3h12m", result.stdout)
                self.assertEqual(terraform_calls(repo), [])
                self.assertFalse({"cp", "list-objects-v2"} & {op for _, op in operations(repo)})

    def test_profile_args_go_to_terraform_from_the_model_type(self):
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"vllm_model_args={MODEL_ARGS}", plan_argv(repo))
            self.assertIn("Model profile: example_type (ExampleForCausalLM): --enable-auto-tool-choice"
                          " --tool-call-parser example_tool --reasoning-parser example_reasoning", result.stderr)
            # The profile is read before Terraform sees the model.
            calls = repo.calls()
            config_read = next(i for i, c in enumerate(calls) if any(a.endswith("/config.json") for a in c.argv))
            plan = next(i for i, c in enumerate(calls) if c.tool == "terraform" and "plan" in c.argv)
            self.assertLess(config_read, plan)

    def test_refuses_a_model_when_no_profiles_are_configured(self):
        with make_repo(files={"shared/model-profiles/profiles.json": "{}"}) as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME)
            self.assert_refused_without_changes(
                repo, result, "no vLLM profile for model_type 'example_type' (ExampleForCausalLM)")
            self.assertEqual(terraform_calls(repo), [c for c in terraform_calls(repo) if "output" in c.argv])

    def test_refuses_a_model_type_without_a_profile(self):
        config = {"model_type": "unprofiled_type", "architectures": ["UnprofiledForCausalLM"]}
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS), config=config)
            result = run_start(repo, NAME)
            self.assert_refused_without_changes(
                repo, result, "no vLLM profile for model_type 'unprofiled_type' (UnprofiledForCausalLM)")
            self.assertIn('\n  "unprofiled_type": {"vllm_args": ["--enable-auto-tool-choice", "--tool-call-parser",'
                          ' "<parser>", "--reasoning-parser", "<parser>"], "source": "<model card URL>"},',
                          result.stderr)
            self.assertEqual(terraform_calls(repo), [c for c in terraform_calls(repo) if "output" in c.argv])

    def test_refuses_an_architecture_the_pinned_vllm_does_not_register(self):
        for label, config in (
                ("unknown", {"model_type": "example_type", "architectures": ["FutureForCausalLM"]}),
                ("embedding only", {"model_type": "embedding_type", "architectures": ["ExampleEmbeddingModel"]})):
            with self.subTest(label), make_repo() as repo:
                script_start_cli(repo, status=status_object(WEIGHTS), config=config)
                result = run_start(repo, NAME)
                self.assert_refused_without_changes(
                    repo, result, f"names architecture '{config['architectures'][0]}', which vLLM v0.30.0"
                    " in the pinned image does not register")

    def test_refuses_an_unreadable_or_malformed_config(self):
        for label, config, message in (
                ("missing", None, f"could not read s3://{BUCKET}/models/{NAME}/config.json"),
                ("no model_type", {"architectures": ["ExampleForCausalLM"]}, "has no plain model_type"),
                ("no architectures", {"model_type": "example_type"}, "has no architectures list")):
            with self.subTest(label), make_repo() as repo:
                script_start_cli(repo, status=status_object(WEIGHTS), config=config)
                result = run_start(repo, NAME)
                self.assert_refused_without_changes(repo, result, message)

    def test_unchecked_model_skips_both_checks_with_a_warning(self):
        config = {"model_type": "future_type", "architectures": ["FutureForCausalLM"]}
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS), config=config)
            result = run_start(repo, NAME, "--unchecked-model")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("warning: --unchecked-model", result.stderr)
            self.assertEqual(plan_var(repo, "vllm_model_args"), "[]")
            self.assertFalse([c for c in repo.calls() if any(a.endswith("/config.json") for a in c.argv)])


def deletes(repo):
    """The endpoint names delete-endpoint was called with, in order."""
    return [c.argv[c.argv.index("--endpoint-name") + 1] for c in repo.calls() if "delete-endpoint" in c.argv]


class RaceTest(unittest.TestCase):
    """The approval, the plan check, and the race loop, with sleeps skipped."""

    def race(self, repo, *polls, typed="yes\n", apply_exit=0, plan=None, **env):
        """Starts NAME after an empty gate; `polls` are the race's ListEndpoints answers."""
        script_start_cli(repo, status=status_object(WEIGHTS), lists=({}, *polls), apply_exit=apply_exit,
                         plan=plan)
        type_on_tty(repo, typed)
        return run_start(repo, NAME, DRIP_AI_RACE_POLL_SECONDS="1", **env)

    def test_anything_but_yes_creates_nothing(self):
        for typed in ("no\n", "\n", ""):
            with self.subTest(repr(typed)), make_repo() as repo:
                result = self.race(repo, {P5EN: "InService"}, typed=typed)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("Type yes to create these endpoints", result.stdout)
                self.assertIn("not approved; nothing was created", result.stderr)
                self.assertIn(("terraform", "plan"), operations(repo))
                self.assertNotIn(("terraform", "apply"), operations(repo))

    def test_yes_applies_the_saved_plan_once_after_showing_each_lanes_price(self):
        with make_repo() as repo:
            result = self.race(repo, {P5EN: "InService"})
            self.assertEqual(result.returncode, 0, result.stderr)
            [apply] = [c.argv for c in terraform_calls(repo) if "apply" in c.argv]
            plan_file = plan_argv(repo)[3].removeprefix("-out=")
            self.assertEqual(apply, [f"-chdir={repo.root}/sagemaker/terraform", "apply", "-input=false",
                                     "-no-color", plan_file])
            self.assertFalse([arg for c in terraform_calls(repo) for arg in c.argv if "auto-approve" in arg])
            self.assertIn(f"  1) {P5EN}  ml.p5en.48xlarge  about $72.80/h\n"
                          f"  2) {P5E}  ml.p5e.48xlarge  price unknown\n"
                          "Worst case: all 2 bill together until one is InService, up to the 3-hour cap;"
                          " the total is unknown because a lane's price is unknown.", result.stdout)
        with make_repo(files={TUNING: 'instance_types = ["ml.p5en.48xlarge"]\n'}) as repo:
            result = self.race(repo, {P5EN: "InService"}, plan=plan_json(lanes=LANES[:1]))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Worst case: all 1 bill together, about $72.80/h, until one is InService; about"
                          " $218 if none is by the 3-hour cap.", result.stdout)
            self.assertEqual(deletes(repo), [])

    def test_yes_creates_after_the_prices_without_a_terminal(self):
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME, "--yes", DRIP_AI_TTY=str(repo.home / "no-such-tty"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Type yes", result.stdout)
            self.assertLess(result.stdout.index(f"  1) {P5EN}  ml.p5en.48xlarge  about $72.80/h"),
                            result.stdout.index("Approved with --yes; creating these endpoints."))
            # The saved plan never asks, so --yes adds nothing to the apply.
            [apply] = [c.argv for c in terraform_calls(repo) if "apply" in c.argv]
            self.assertEqual(apply[1:4], ["apply", "-input=false", "-no-color"])
            self.assertFalse([arg for c in terraform_calls(repo) for arg in c.argv if "auto-approve" in arg])

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
        # Exit 0 would make `up` open a tunnel to an endpoint that never serves.
        for status in ("Failed", "Deleting"):
            with self.subTest(status), make_repo() as repo:
                script_gate(repo, ARN, status=status)
                script_dashboard_cli(repo)
                result = run_start(repo, NAME, "--no-watch")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"endpoint {P5EN} is {status}; run bin/drip-ai sagemaker stop, then up", result.stderr)
                self.assertNotIn("== drip-ai SageMaker endpoint", result.stdout)
                self.assertEqual(terraform_calls(repo), [])
                self.assertEqual([c for c in repo.calls() if c.tool == "curl" or "get-parameter" in c.argv], [])
        with make_repo() as repo:
            script_gate(repo, ARN, status="Creating")
            self.assertEqual(run_start(repo, NAME, "--no-watch").returncode, 0)

    def test_refuses_a_plan_that_does_more_than_create_each_lane(self):
        cases = {
            "update": (plan_json(actions=("update",)), "the plan would update aws_sagemaker_endpoint.server"),
            "replace": (plan_json(actions=("delete", "create")), "the plan would delete/create"),
            "keep": (plan_json(actions=("no-op",)), "the plan would no-op"),
            "lane missing": (plan_json(lanes=LANES[:1]), "are not one drip-ai-<type> endpoint per instance_types"),
        }
        for label, (plan, message) in cases.items():
            with self.subTest(label), make_repo() as repo:
                result = self.race(repo, {P5EN: "InService"}, plan=plan)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertIn("nothing was created", result.stderr)
                self.assertNotIn("Type yes", result.stdout)
                self.assertNotIn(("terraform", "apply"), operations(repo))

    def test_the_first_in_service_lane_wins_and_only_the_loser_is_deleted(self):
        with make_repo() as repo:
            result = self.race(repo, {P5EN: "Creating", P5E: "Creating"}, {P5EN: "InService", P5E: "Creating"},
                               {P5EN: "InService", P5E: "Deleting"}, {P5EN: "InService"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(deletes(repo), [P5E])
            self.assertIn(f"{P5EN} (ml.p5en.48xlarge) is InService and wins", result.stdout)
            self.assertIn(f"== drip-ai SageMaker endpoint {P5EN}", result.stdout)
            self.assertNotIn("not in Terraform state", result.stderr)

    def test_a_tie_goes_to_the_first_instance_type(self):
        # ListEndpoints lists the p5e lane first; the instance_types order still decides.
        with make_repo() as repo:
            result = self.race(repo, {P5E: "InService", P5EN: "InService"}, {P5EN: "InService"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(deletes(repo), [P5E])

    def test_a_refused_delete_is_asked_again_at_the_next_poll(self):
        with make_repo() as repo:
            repo.stub("aws", "sagemaker", "delete-endpoint", times=1, exit=254,
                      stderr="An error occurred (ValidationException) when calling the DeleteEndpoint"
                             " operation: Cannot delete while Creating\n")
            result = self.race(repo, {P5EN: "InService", P5E: "Creating"}, {P5EN: "InService", P5E: "Creating"},
                               {P5EN: "InService"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(deletes(repo), [P5E, P5E])
            self.assertIn(f"SageMaker refused to delete {P5E} (", result.stderr)

    def test_every_lane_failed_deletes_each_and_exits_1(self):
        with make_repo() as repo:
            repo.stub("aws", "sagemaker", "describe-endpoint", "FailureReason", stdout="CUDA out of memory\n")
            # Failed until Terraform has exited and the deletes went out, then gone.
            failed = [{P5EN: "Failed", P5E: "Failed"}] * 20
            result = self.race(repo, *failed, {}, apply_exit=1)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertEqual(set(deletes(repo)), {P5EN, P5E})
            for name, type_ in LANES:
                self.assertIn(f"{name} ({type_}) failed: CUDA out of memory (logs: /aws/sagemaker/Endpoints/{name})",
                              result.stderr)
            self.assertIn(f"Start again: bin/drip-ai sagemaker start {NAME}", result.stdout)
            self.assertNotIn("Cost: running", result.stdout)

    def test_a_lane_listendpoints_misses_after_terraform_exits_still_counts(self):
        with make_repo() as repo:
            repo.stub("aws", "sagemaker", "describe-endpoint", P5EN, "text",
                      stdout=f"{ENDPOINT_ARN}\tCreating\t2026-10-05T10:00:00+00:00\tdrip-ai-1\n")
            # Terraform exits at once; ListEndpoints shows the p5en lane only some polls later.
            result = self.race(repo, *[{}] * 5, {P5EN: "InService"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("did not start", result.stderr)
            self.assertNotIn("nothing bills", result.stdout)
            self.assertIn(f"{P5EN} (ml.p5en.48xlarge) is InService and wins", result.stdout)

    def test_no_winner_by_the_cap_deletes_every_lane(self):
        with make_repo() as repo:
            result = self.race(repo, {P5EN: "Creating", P5E: "Creating"}, {}, DRIP_AI_RACE_CAP_SECONDS="0")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("no lane was InService after 0 minutes; deleting every lane", result.stderr)
            self.assertEqual(sorted(deletes(repo)), sorted([P5EN, P5E]))

    def test_warns_when_the_winner_is_outside_terraform_state(self):
        with make_repo() as repo:
            repo.stub("terraform", "state", "list", stdout="")
            result = self.race(repo, {P5EN: "InService"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"warning: {P5EN} is not in Terraform state", result.stderr)
            self.assertIn(f"{TEARDOWN} still deletes it", result.stderr)

    def test_a_lane_whose_tag_read_fails_is_never_chosen_or_deleted(self):
        with make_repo() as repo:
            repo.stub("aws", "sagemaker", "list-tags", lane_arn(P5E), times=1, exit=254,
                      stderr="An error occurred (ThrottlingException)\n")
            repo.stub("aws", "sagemaker", "list-tags", lane_arn(P5E), stdout=f"{OTHER_ARN}\n")
            result = self.race(repo, {P5E: "InService", P5EN: "Creating"},
                               {P5E: "InService", P5EN: "InService"}, {P5EN: "InService"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"{P5E} unchecked", result.stdout)
            self.assertIn(f"endpoint {P5E} was started by {OTHER_ARN}, not by this run; leaving it alone",
                          result.stderr)
            self.assertIn(f"{P5EN} (ml.p5en.48xlarge) is InService and wins", result.stdout)
            self.assertEqual(deletes(repo), [])


class RaceSignalTest(unittest.TestCase):
    """Signals during a race whose Terraform apply keeps running until it gets INT."""

    def run_race(self, repo, *polls, signals=(), **env):
        """Starts NAME with 1 s polls; sends SIGINT after each text in `signals` appears in its
        output, and returns (returncode, stdout, stderr)."""
        script_start_cli(repo, status=status_object(WEIGHTS), lists=({}, *polls))
        hang_apply(repo)
        out_path, err_path = repo.home / "out", repo.home / "err"
        with open(out_path, "w") as out, open(err_path, "w") as err:
            proc = repo.popen(SCRIPT, NAME, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                              env=dict(DASHBOARD_ENV, DRIP_AI_TTY=str(repo.home / "tty"),
                                       DRIP_AI_RACE_POLL_SECONDS="1", **env))
            for text in signals:
                deadline = time.monotonic() + 30
                while text not in out_path.read_text() + err_path.read_text():
                    self.assertIsNone(proc.poll(), err_path.read_text())
                    self.assertLess(time.monotonic(), deadline, "never printed: " + text)
                    time.sleep(0.1)
                proc.send_signal(signal.SIGINT)
            returncode = proc.wait(timeout=30)
        return returncode, out_path.read_text(), err_path.read_text()

    def apply_signal(self, repo):
        path = repo.home / "apply.signal"
        return path.read_text().strip() if path.exists() else None

    def test_interrupt_before_a_winner_stops_terraform_and_deletes_every_lane(self):
        with make_repo() as repo:
            creating = [{P5EN: "Creating", P5E: "Creating"}] * 5
            returncode, out, err = self.run_race(repo, *creating, {}, signals=["After 0 min:"])
            self.assertEqual(returncode, 130, err)
            self.assertEqual(self.apply_signal(repo), "INT")
            self.assertEqual(set(deletes(repo)), {P5EN, P5E})
            self.assertIn("Every lane is deleted; nothing bills for them.", out)
            self.assertNotIn("Cost: running", out)

    def test_interrupt_after_a_winner_keeps_it(self):
        with make_repo() as repo:
            returncode, out, err = self.run_race(
                repo, {P5EN: "InService", P5E: "Creating"}, {P5EN: "InService", P5E: "Deleting"},
                {P5EN: "InService"}, signals=["is InService and wins"], DRIP_AI_RACE_APPLY_WAIT_SECONDS="600")
            self.assertEqual(returncode, 0, err)
            self.assertEqual(deletes(repo), [P5E])
            self.assertEqual(self.apply_signal(repo), "INT")
            self.assertIn(f"Endpoint {P5EN} (ml.p5en.48xlarge) serves models/{NAME}", out)
            self.assertNotIn("Cost: running", out)

    def test_a_second_interrupt_names_the_lanes_that_still_bill(self):
        with make_repo() as repo:
            repo.stub("aws", "sagemaker", "delete-endpoint", exit=254,
                      stderr="An error occurred (ValidationException): Cannot delete while Creating\n")
            # The second Ctrl-C waits until the race has seen Terraform exit; before that it takes the
            # Terraform-still-running path the next test pins.
            returncode, out, err = self.run_race(repo, {P5EN: "Creating", P5E: "Creating"},
                                                 signals=["After 0 min:", "Terraform exited (130)."])
            self.assertEqual(returncode, 130, err)
            self.assertIn(f"SageMaker refused to delete {P5EN}", err)
            self.assertIn(f"stopped; these endpoints still exist and each bills until {TEARDOWN}:"
                          f" {P5EN} (Creating) {P5E} (Creating)", err)

    def test_a_second_interrupt_while_terraform_runs_counts_every_lane_as_billing(self):
        with make_repo() as repo:
            returncode, out, err = self.run_race(repo, {P5EN: "Creating", P5E: "Creating"},
                                                 signals=["After 0 min:", f"Deleting {P5E}."], APPLY_INTS="2")
            self.assertEqual(returncode, 130, err)
            self.assertRegex(err, r"stopped while Terraform \(pid \d+\) was still running; it may still create"
                                  f" endpoints. Each of these may bill until {TEARDOWN}: {P5EN} {P5E}\n")
            deadline = time.monotonic() + 10
            while self.apply_signal(repo) != "INT\nINT" and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertEqual(self.apply_signal(repo), "INT\nINT")

    def test_a_hung_apply_gets_int_after_the_wait(self):
        with make_repo() as repo:
            returncode, out, err = self.run_race(repo, {P5EN: "InService", P5E: "Creating"}, {P5EN: "InService"},
                                                 DRIP_AI_RACE_APPLY_WAIT_SECONDS="1")
            self.assertEqual(returncode, 0, err)
            self.assertEqual(self.apply_signal(repo), "INT")
            self.assertEqual(deletes(repo), [P5E])
            self.assertLess(out.index("is InService and wins"), out.index("Stopping Terraform (INT)"))
            self.assertIn("Cost: running", out)


class DashboardTest(unittest.TestCase):
    def watch(self, repo, **env):
        script_lists(repo, {P5EN: "InService"})
        return repo.run(SCRIPT, "--watch", env=dict(DASHBOARD_ENV, **env))

    def test_dashboard_renders_each_panel(self):
        idle = f"idle 30+ min: still billing ≈ $72.80/h. Stop: {TEARDOWN}"
        up = "Proxy: up on localhost:8000"
        # A key that would break the curl config line is refused before curl runs.
        refused = ("Proxy: unavailable (could not read the API key: "
                   "the stored key is not sk- followed by letters and digits)")
        # (script_dashboard_cli arguments, lines shown, lines not shown, curl calls)
        cases = {
            "cost from creation time": ({}, ["running 3h12m  ≈ $232.96 so far  ($72.80/h, ≈ $1,747/day)",
                                             "InService  ml.p5en.48xlarge x1",
                                             f"== drip-ai SageMaker endpoint {P5EN}  started by {ARN}",
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

    def test_api_key_never_in_argv(self):
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Proxy: up on localhost:8000", result.stdout)
            curl_calls = [c for c in repo.calls() if c.tool == "curl"]
            # The stub answers 200 only when the header arrives on stdin, so "up" proves delivery.
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
            self.assertEqual(tfvars_files(repo), {})

    def test_dashboard_non_tty_appends_lines(self):
        with make_repo() as repo:
            repo.skip_sleeps()
            script_dashboard_cli(repo)
            result = self.watch(repo, DRIP_AI_WATCH_ITERATIONS="2", DRIP_AI_WATCH_SECONDS="1")
            self.assertEqual(repo.sleeps(), ["5"])  # the interval floor, once between two frames
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count(f"== drip-ai SageMaker endpoint {P5EN}"), 2)
            # Log lines are the last three, with control characters removed.
            self.assertNotIn("\x1b", result.stdout)
            self.assertIn("t line4 [31mred[0m\n", result.stdout)
            self.assertIn("t line3\n", result.stdout)
            self.assertNotIn("line2", result.stdout)

    def test_dashboard_works_without_initialised_state(self):
        with make_repo(initialised=False) as repo:
            # The state bucket exists, so only the view itself keeps it from `terraform init`.
            repo.stub("aws", "s3api", "head-bucket", STATE_BUCKET)
            repo.stub("terraform", "init")
            script_dashboard_cli(repo, owner=OTHER_ARN)
            repo.stub("aws", "logs", "tail", f"/aws/sagemaker/Endpoints/{P5EN}", stdout=LOG_LINES)
            result = self.watch(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(terraform_calls(repo), [])
            self.assertIn(f"== drip-ai SageMaker endpoint {P5EN}  started by {OTHER_ARN}", result.stdout)
            self.assertIn("Cost: running 3h12m", result.stdout)
            self.assertIn("Proxy: up on localhost:8000", result.stdout)
            self.assertIn("t line5", result.stdout)
            self.assertNotIn("unavailable", result.stdout)

    def test_watch_refuses_while_a_race_runs(self):
        with make_repo() as repo:
            script_lists(repo, {P5EN: "Creating", P5E: "Creating"})
            result = repo.run(SCRIPT, "--watch", env=DASHBOARD_ENV)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"race in progress or interrupted ({P5EN} Creating, {P5E} Creating)", result.stderr)


UPLOADED = {"config.json": 10, "model.safetensors": 20 * GB}
HUGE = {"config.json": 10, "model.safetensors": 1100 * GB}


def script_bucket(repo):
    """Org/Big-Model (complete), Org/Partial (copying), huge (too big), junk (no model), my-upload."""
    def answer(prefix, body):
        repo.stub("aws", "s3api", "list-objects-v2", "--delimiter", prefix, stdout=json.dumps(body))

    answer("models/", {"CommonPrefixes": [{"Prefix": f"models/{p}/"} for p in
                                          ("Org", "huge", "junk", "my-upload")]})
    answer("models/Org/", {"CommonPrefixes": [{"Prefix": "models/Org/Big-Model/"},
                                              {"Prefix": "models/Org/Partial/"}]})
    for name, files, status in (("Org/Big-Model", WEIGHTS, "complete"),
                                ("Org/Partial", WEIGHTS, "copying"), ("huge", HUGE, "complete")):
        prefix = f"models/{name}/"
        answer(prefix, listing(prefix, files))
        repo.stub("aws", "s3", "cp", f"s3://{BUCKET}/{prefix}drip-ai-fetch.json",
                  stdout=json.dumps(status_object(files, status)))
        repo.stub("aws", "s3api", "list-objects-v2", prefix,
                  stdout=json.dumps(listing(prefix, files)))
    answer("models/junk/", listing("models/junk/", {"README.md": 5}, with_status=False))
    answer("models/my-upload/", listing("models/my-upload/", UPLOADED, with_status=False))
    repo.stub("aws", "s3api", "list-objects-v2", "models/my-upload/",
              stdout=json.dumps(listing("models/my-upload/", UPLOADED, with_status=False)))


class PickerTest(unittest.TestCase):
    def pick(self, repo, typed):
        type_on_tty(repo, typed)
        return run_start(repo)

    def setup_repo(self, repo, *lists):
        """The bucket and the ListEndpoints answers: by default no endpoint, for good."""
        script_lists(repo, *(lists or ({},)))
        output(repo, "shared", "models_bucket", BUCKET)
        script_bucket(repo)

    def assert_unchanged(self, repo, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tfvars_files(repo), {})
        self.assertNotIn(("terraform", "apply"), operations(repo))

    def test_picker_lists_fetched_and_uploaded_models(self):
        with make_repo() as repo:
            self.setup_repo(repo)
            result = self.pick(repo, "q\n")
            self.assert_unchanged(repo, result)
            rows = [line for line in result.stdout.splitlines() if line.lstrip()[:2] in ("1)", "4)")]
            self.assertEqual(len(rows), 2, result.stdout)
            for part in ("Org/Big-Model", "fetched", f"{NAME}@0123456", "complete", "800.0 GB",
                         "3 files", "fits"):
                self.assertIn(part, rows[0])
            for part in ("my-upload", "uploaded", "20.0 GB", "2 files", "fits"):
                self.assertIn(part, rows[1])
            self.assertNotIn("[current]", result.stdout)
            self.assertNotIn("junk", result.stdout)

    def test_picker_marks_incomplete_and_oversized_unselectable(self):
        with make_repo() as repo:
            self.setup_repo(repo)
            result = self.pick(repo, "2\n3\nq\n")
            self.assert_unchanged(repo, result)
            self.assertIn("2) Org/Partial is not selectable: still copying", result.stdout)
            self.assertIn("3) huge is not selectable: 1100.0 GB is over 90% of ml.p5en.48xlarge"
                          " GPU memory (1015 GB)", result.stdout)

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
                self.setup_repo(repo, {}, {P5EN: "InService"})
                script_config(repo, name)
                script_race(repo)
                script_dashboard_cli(repo)
                result = self.pick(repo, typed + "yes\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"model_source={name}", plan_argv(repo))
                self.assertIn(("terraform", "apply"), operations(repo))
                status_reads = [c for c in repo.calls() if c.tool == "aws" and c.argv[1] == "cp"
                                and f"/models/{name}/drip-ai-fetch.json" in c.argv[2]]
                # Fetched: once for the table, once more in the start flow. Uploaded: never.
                self.assertEqual(len(status_reads), 2 if manifest else 0)
                self.assertIn("Cost: running", result.stdout)

    def script_solo_bucket(self, repo, files):
        """A bucket holding only the uploaded one-segment model `solo` with sub-folders."""
        script_lists(repo, {})
        output(repo, "shared", "models_bucket", BUCKET)
        repo.stub("aws", "s3api", "list-objects-v2", "--delimiter", "models/",
                  stdout=json.dumps({"CommonPrefixes": [{"Prefix": "models/solo/"}]}))
        top = listing("models/solo/", {k: v for k, v in files.items() if "/" not in k},
                      with_status=False)
        top["CommonPrefixes"] = [{"Prefix": f"models/solo/{sub}/"}
                                 for sub in sorted({k.split("/")[0] for k in files if "/" in k})]
        repo.stub("aws", "s3api", "list-objects-v2", "--delimiter", "models/solo/",
                  stdout=json.dumps(top))
        repo.stub("aws", "s3api", "list-objects-v2", "models/solo/",
                  stdout=json.dumps(listing("models/solo/", files, with_status=False)))

    def test_picker_marks_model_with_nested_model_unselectable(self):
        nested = dict(UPLOADED, **{"Inner/config.json": 10, "Inner/model.safetensors": 5})
        with make_repo() as repo:
            self.script_solo_bucket(repo, nested)
            result = self.pick(repo, "1\nq\n")
            self.assert_unchanged(repo, result)
            self.assertIn("not selectable: contains nested model solo/Inner", result.stdout)
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
                      "ProductionVariants[0].ModelName", stdout="drip-ai-0a1b2c3d\n")
            repo.stub("aws", "describe-model", "drip-ai-0a1b2c3d",
                      stdout=f"s3://{BUCKET}/models/my-upload/\n")
            script_owner(repo, OTHER_ARN)
            self.setup_repo(repo, {P5EN: "InService"})
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
