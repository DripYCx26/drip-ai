"""Shared fixtures for the start and race tests: a StubRepo scripted with the caller, the S3 copy,
the planner's reads, the race lease, and fake_sagemaker.py in front of the aws stub."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "testing"))

from cli_stubs import StubRepo  # noqa: E402

SCRIPT = "sagemaker/start.sh"
FAKE_SAGEMAKER = Path(__file__).resolve().parent / "fake_sagemaker.py"
ACCOUNT = "111122223333"
ARN = f"arn:aws:iam::{ACCOUNT}:user/alice"
OTHER_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/Dev/bob"
HOST = os.uname().nodename
P5EN_TYPE, P5E_TYPE, P5_TYPE = "ml.p5en.48xlarge", "ml.p5e.48xlarge", "ml.p5.48xlarge"
B200_TYPE, B300_TYPE = "ml.p6-b200.48xlarge", "ml.p6-b300.48xlarge"
H200_POOL = [P5EN_TYPE, P5E_TYPE]
CAPACITY = ("Unable to provision requested ML compute capacity due to InsufficientInstanceCapacity error."
            " Please retry using a different ML instance type or after some time.")
EXPIRED = "aws: [ERROR]: Your session has expired. Please reauthenticate using 'aws login'."


def lane(cls="h200", number=1, region="us-east-1"):
    """A lane's endpoint name: drip-ai-<GPU class>-<region>-<number>."""
    return f"drip-ai-{cls}-{region}-{number}"


H1, H2, H3, H4 = (lane(number=n) for n in (1, 2, 3, 4))
LANES = (H1, H2, H3, H4)  # four H200 lanes: quota 2 for each of the class's two types


def lane_arn(name, region="us-east-1"):
    return f"arn:aws:sagemaker:{region}:{ACCOUNT}:endpoint/{name}"


ENDPOINT_ARN = lane_arn(H1)
STATE_BUCKET = f"drip-ai-tfstate-{ACCOUNT}-us-east-1"
INITIALISED = {
    f"{root}/.terraform/terraform.tfstate": json.dumps(
        {"version": 3, "backend": {"type": "s3", "config": {"bucket": STATE_BUCKET}}})
    for root in ("sagemaker/terraform", "shared/terraform")
}
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
TEARDOWN = "bin/drip-ai sagemaker stop"
PINNED = "sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"
# A synthetic GQA model: 48 layers, 8 KV heads of 128, a 131072-token window.
MODEL_CONFIG = {"model_type": "example_type", "architectures": ["ExampleForCausalLM"], "num_hidden_layers": 48,
                "num_attention_heads": 64, "num_key_value_heads": 8, "head_dim": 128,
                "max_position_embeddings": 131072}
MODEL_ARGS = ["--enable-auto-tool-choice", "--tool-call-parser", "example_tool", "--reasoning-parser",
              "example_reasoning"]
ENDPOINT = {
    "EndpointName": H1,
    "EndpointArn": ENDPOINT_ARN,
    "EndpointConfigName": "drip-ai-c-0a1b2c3d-1",
    "EndpointStatus": "InService",
    "CreationTime": "2026-10-05T10:00:00.000000+00:00",
    "LastModifiedTime": "2026-10-05T10:41:00+00:00",
    "ProductionVariants": [{"VariantName": "main", "CurrentInstanceCount": 1}],
}
LOG_LINES = "t line1\nt line2\nt line3\nt line4 \x1b[31mred\x1b[0m\nt line5\n"
DASHBOARD_ENV = {"DRIP_AI_NOW_EPOCH": str(NOW), "DRIP_AI_WATCH_ITERATIONS": "1"}
READ_ONLY_AWS = {
    "cp", "list-objects-v2", "describe-endpoint", "describe-endpoint-config", "describe-model",
    "list-tags", "get-metric-data", "get-parameter", "tail", "get-caller-identity", "list-endpoints",
}
LEASE_KEY = "leases/sagemaker-race.json"
PRECONDITION_FAILED = ("An error occurred (PreconditionFailed) when calling the PutObject operation:"
                       " At least one of the pre-conditions you specified did not hold\n")
H200_ONLY = {"race": {"classes": ["h200"]}}


def write_config(repo, document):
    """The user's drip-ai config file, 0600 as config set writes it."""
    path = repo.home / ".config" / "drip-ai" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    os.chmod(path, 0o600)


def make_repo(files=None, initialised=True, arn=ARN, user_config=H200_ONLY):
    """A StubRepo with the caller identity, a terminal that types yes, both roots initialised, and a
    user config that races only the H200 class."""
    model_fixtures = {
        "shared/model-profiles/profiles.json": json.dumps({"example_type": {
            "vllm_args": MODEL_ARGS, "source": "https://example.com/model-card"}}),
        "shared/model-profiles/vllm-architectures.json": json.dumps({
            "vllm_ref": "v0.30.0", "architectures": ["ExampleForCausalLM", "UnprofiledForCausalLM"]}),
    }
    repo = StubRepo(files={**(INITIALISED if initialised else {}), **model_fixtures, **(files or {})})
    repo.stub("aws", "sts", "get-caller-identity", "text", stdout=f"{ACCOUNT}\t{arn}\n")
    repo.stub("aws", "sts", "get-caller-identity", stdout=json.dumps({"Account": ACCOUNT, "Arn": arn}))
    type_on_tty(repo, "yes\n")
    if user_config is not None:
        write_config(repo, user_config)
    return repo


def type_on_tty(repo, text):
    (repo.home / "tty").write_text(text)


def listed(states, region="us-east-1"):
    """A ListEndpoints text answer: `states` maps endpoint name to status, in the order AWS lists them."""
    return "".join(f"{name}\t{status}\t{lane_arn(name, region)}\n" for name, status in states.items())


def script_lists(repo, *answers):
    """Answers ListEndpoints with each of `answers` once, in order; the last one repeats."""
    for index, states in enumerate(answers):
        repo.stub("aws", "sagemaker", "list-endpoints", stdout=listed(states),
                  times=None if index == len(answers) - 1 else 1)


def script_quotas(repo, quotas=None, exit_code=0, region=None):
    """The SageMaker endpoint quotas in REGION (every region when None): `quotas` ({type: value}),
    else 2 for every type the tests race; a nonzero `exit_code` makes the read fail."""
    every = {type_: 2 for type_ in (P5EN_TYPE, P5E_TYPE, P5_TYPE, B200_TYPE, B300_TYPE)}
    rows = [[f"{type_} for endpoint usage", float(value)] for type_, value in (quotas if quotas is not None else every).items()]
    tokens = ("service-quotas", "list-service-quotas", "sagemaker") + ((region,) if region else ())
    repo.stub("aws", *tokens, exit=exit_code, stdout="" if exit_code else json.dumps(rows),
              stderr="An error occurred (AccessDeniedException) when calling the ListServiceQuotas operation\n"
              if exit_code else "")


def script_foundation(repo, region=None, missing=False, log_decision="allowed"):
    """The execution role in REGION (every region when None) and its log-permission simulation."""
    tokens = (region,) if region else ()
    if missing:
        repo.stub("aws", "iam", "get-role", *tokens, exit=254,
                  stderr="An error occurred (NoSuchEntity) when calling the GetRole operation: The role cannot be found.\n")
        return
    suffix = "" if region in (None, "us-east-1") else f"-{region}"
    repo.stub("aws", "iam", "get-role", *tokens,
              stdout=json.dumps(f"arn:aws:iam::{ACCOUNT}:role/drip-ai-sagemaker-execution{suffix}"))
    repo.stub("aws", "iam", "simulate-principal-policy", *tokens,
              stdout=json.dumps([["logs:CreateLogStream", log_decision], ["logs:PutLogEvents", log_decision]]))


def output(repo, root, name, value):
    repo.stub("terraform", f"-chdir={repo.root}/{root}/terraform", "output", "-raw", name, stdout=value)


def script_owner(repo, owner, status="InService", tags_exit=0, name=H1):
    """The owner reads of lane `name`, tagged `owner` ("None" is how the CLI prints a missing tag)."""
    repo.stub("aws", "sagemaker", "describe-endpoint", name, "text",
              stdout=f"{lane_arn(name)}\t{status}\t2026-10-05T10:00:00+00:00\tdrip-ai-20261005\n")
    repo.stub("aws", "sagemaker", "list-tags", lane_arn(name), stdout=f"{owner}\n", exit=tags_exit,
              stderr="An error occurred (AccessDeniedException)\n" if tags_exit else "")


def script_gate(repo, owner=None, status="InService"):
    """The owner gate: no endpoint when `owner` is None, else lane drip-ai-h200-us-east-1-1 tagged
    `owner`. Later ListEndpoints calls see the same."""
    if owner is None:
        script_lists(repo, {})
        return
    script_lists(repo, {H1: status})
    script_owner(repo, owner, status)


def lease(owner, host, pid=4242):
    """A race lease taken a minute before NOW, held until ten minutes after."""
    def iso(epoch):
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return json.dumps({"owner_arn": owner, "host": host, "pid": pid,
                       "acquired_at": iso(NOW - 60), "expires_at": iso(NOW + 600)})


def script_lease(repo, holder=None):
    """The race lease: free, so start takes it and finds it its own to release; or held by
    `holder`, an (owner ARN, host) pair."""
    if holder is None:
        repo.stub("aws", "s3api", "put-object", LEASE_KEY, stdout='"e1"\n')
        repo.stub("aws", "s3api", "get-object", LEASE_KEY, stdout='"e1"\n', last_arg_file=lease(ARN, HOST))
        repo.stub("aws", "s3api", "delete-object", LEASE_KEY)
    else:
        repo.stub("aws", "s3api", "put-object", LEASE_KEY, exit=254, stderr=PRECONDITION_FAILED)
        repo.stub("aws", "s3api", "get-object", LEASE_KEY, stdout='"e1"\n', last_arg_file=lease(*holder))


def fake_sagemaker(repo, scripts=None, events=None, endpoints=None, create_errors=None, expired=None,
                   create_refusals=None, describe_errors=None, old_streams=None):
    """Puts fake_sagemaker.py in front of the aws stub for sagemaker, logs, and sts calls."""
    state = repo.home / "sagemaker.json"
    state.write_text(json.dumps({"now": NOW, "scripts": scripts or {}, "create_errors": create_errors or {},
                                 "create_refusals": create_refusals or {}, "describe_errors": describe_errors or {},
                                 "events": {str(k): v for k, v in (events or {}).items()},
                                 "endpoints": endpoints or {}, "polls": 0, "creations": 0, "made": {},
                                 "old_streams": old_streams or {},
                                 **({"expired": expired} if expired else {})}))
    repo.put_in_front("aws", ("sagemaker", "logs", "sts"), FAKE_SAGEMAKER, str(state))


def fake_state(repo):
    return json.loads((repo.home / "sagemaker.json").read_text())


def endpoints_left(repo):
    """The fake's endpoints that still exist, by name."""
    return fake_state(repo)["endpoints"]


def script_race(repo, scripts=None, events=None, endpoints=None, lease_holder=None, quotas=None,
                create_errors=None, expired=None, create_refusals=None, describe_errors=None, old_streams=None):
    """A race: the endpoint quotas, the foundation, the race lease, and the fake SageMaker API, where
    by default every lane is InService at the first poll after its creation."""
    script_quotas(repo, quotas)
    script_foundation(repo)
    script_lease(repo, lease_holder)
    fake_sagemaker(repo, scripts, events, endpoints, create_errors, expired, create_refusals, describe_errors,
                   old_streams)


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
    """The dashboard's reads of lane drip-ai-h200-us-east-1-1; ListEndpoints is scripted apart."""
    repo.stub("aws", "ssm", "get-parameter", "/drip-ai/api-key", stdout=key + "\n")
    repo.stub("aws", "sagemaker", "describe-endpoint", "json", stdout=json.dumps(endpoint))
    repo.stub("aws", "sagemaker", "list-tags", ENDPOINT_ARN, stdout=f"{owner}\n")
    repo.stub("aws", "sagemaker", "describe-endpoint-config", ENDPOINT["EndpointConfigName"],
              "ProductionVariants[0].[InstanceType,InitialInstanceCount]",
              stdout="ml.p5en.48xlarge\t1\n")
    repo.stub("aws", "sagemaker", "describe-endpoint-config", ENDPOINT["EndpointConfigName"],
              "ProductionVariants[0].ModelName", stdout="drip-ai-m-0a1b2c3d\n")
    repo.stub("aws", "sagemaker", "describe-model", "drip-ai-m-0a1b2c3d",
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
                     dashboard=True, **race):
    """A start of models/<name>/: by default no endpoint at the gate, then a race (script_race takes
    `race`) that drip-ai-h200-us-east-1-1 wins."""
    prefix = f"models/{name}/"
    output(repo, "shared", "models_bucket", BUCKET)
    script_config(repo, name, config)
    status_uri = f"s3://{BUCKET}/{prefix}drip-ai-fetch.json"
    if status is None:
        repo.stub("aws", "s3", "cp", status_uri, stderr="fatal error: 404\n", exit=1)
    else:
        repo.stub("aws", "s3", "cp", status_uri, stdout=json.dumps(status))
    repo.stub("aws", "s3api", "list-objects-v2", prefix, stdout=json.dumps(listing(prefix, files, with_status)))
    repo.stub("aws", "s3api", "head-object", f"{prefix}config.json", stdout="{}")
    script_race(repo, **race)
    if dashboard:
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
    """Runs a start with the race's waits on a virtual clock; RaceSignalTest keeps them real."""
    repo.skip_sleeps()
    return repo.run(SCRIPT, *args, env={**DASHBOARD_ENV, "DRIP_AI_TTY": str(repo.home / "tty"),
                                        "DRIP_AI_RACE_POLL_SECONDS": "1", "DRIP_AI_RACE_VIRTUAL_CLOCK": "1", **env},
                    timeout=180)


def option(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


def creates(repo):
    """(name, endpoint config, {tag: value}) per create-endpoint call, in order."""
    found = []
    for call in repo.calls():
        if call.argv[1:2] == ["create-endpoint"]:
            tags = {tag["Key"]: tag["Value"] for tag in json.loads(option(call.argv, "--tags"))}
            found.append((option(call.argv, "--endpoint-name"), option(call.argv, "--endpoint-config-name"), tags))
    return found


def deletes(repo):
    """The endpoint names delete-endpoint was called with, in order."""
    return [option(c.argv, "--endpoint-name") for c in repo.calls() if c.argv[1:2] == ["delete-endpoint"]]


def lane_operations(repo, name):
    """The create-endpoint and delete-endpoint calls for endpoint NAME, in order."""
    return [c.argv[1] for c in repo.calls()
            if c.argv[1:2] in (["create-endpoint"], ["delete-endpoint"]) and option(c.argv, "--endpoint-name") == name]


def models_made(repo):
    """The fake's models, by name: region, container, role, and tags."""
    return fake_state(repo)["models"]


def configs_made(repo):
    return fake_state(repo)["configs"]


def tfvars_files(repo):
    """Every tfvars file in the repo copy, as {relative path: text}."""
    return {str(path.relative_to(repo.root)): path.read_text() for path in repo.root.rglob("*.tfvars")}


def terraform_calls(repo):
    return [c for c in repo.calls() if c.tool == "terraform"]


MUTATING_AWS = {"create-model", "create-endpoint-config", "create-endpoint", "delete-endpoint",
                "delete-endpoint-config", "delete-model", "create-log-group", "put-retention-policy",
                "delete-log-group", "put-object", "delete-object", "purchase-capacity-block", "send-command",
                "run-task", "stop-task", "update-endpoint"}
