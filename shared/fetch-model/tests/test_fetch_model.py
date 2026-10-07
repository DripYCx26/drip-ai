"""Pins `shared/fetch-model/fetch-model.sh` against stub CLIs and a stub copier.

The copier is replaced by writing a stub `shared/fetch-model/copier.py` into the StubRepo copy, so the
script runs its real `python3 -I shared/fetch-model/copier.py plan` call. Prompts read from the file named
by DRIP_AI_TTY, never argv: the token, then the launch confirmation. Watch tests skip real sleeps; Ctrl-C
tests keep them, so the signal lands in the wait and not inside a stub call.
Terraform roots start initialised against the caller's state bucket, so `tf` runs no `init`; the
fetch-model lease lives in that bucket as `leases/fetch-model.json`.
The `aws` stub is wrapped so `ssm put-parameter --cli-input-json file://PATH` records the file's path,
mode, and content while the call runs, then hands that content to the stub as stdin, so the stub's
`stdin=` rules check the JSON the script wrote.
"""

import json
import os
import pty
import re
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "testing"))

from cli_stubs import StubRepo  # noqa: E402

SCRIPT = "shared/fetch-model/fetch-model.sh"
REPO_ID = "acme/tiny-model"
SHA = "0123456789abcdef0123456789abcdef01234567"
TOKEN = "hf_" + "Canary7" * 5
BUCKET = "drip-ai-models-canary"
CLUSTER = "drip-ai-fetch-model"
TASK_DEFINITION = "arn:aws:ecs:us-east-1:111122223333:task-definition/drip-ai-fetch-model:3"
TASK_ID = "0123456789abcdef0123456789abcdef"
TASK_ARN = f"arn:aws:ecs:us-east-1:111122223333:task/{CLUSTER}/{TASK_ID}"
NETWORK = '{"awsvpcConfiguration":{"subnets":["subnet-1"],"securityGroups":["sg-1"],"assignPublicIp":"ENABLED"}}'
LOG_GROUP = "/drip-ai/fetch-model"
PARAMETER = "/drip-ai/hf-token"
PREFIX = f"models/{REPO_ID}/"
FILES = [
    {"path": "config.json", "size": 1000},
    {"path": "model-00001.safetensors", "size": 3_000_000_000},
    {"path": "model-00002.safetensors", "size": 2_000_000_000},
]
TOTAL = sum(f["size"] for f in FILES)
TFVARS = 'region = "us-east-1"\nmodel_source = "Org/Model"  # serving model\n'
ACCOUNT = "111122223333"
ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/Dev/alice@example.com"
OTHER_ARN = f"arn:aws:iam::{ACCOUNT}:user/bob"
HOST = os.uname().nodename
STATE_BUCKET = f"drip-ai-tfstate-{ACCOUNT}-us-east-1"
LEASE_KEY = "leases/fetch-model.json"
# Above the pid ceiling of Linux (4194304) and macOS (99999), so no process on the test host has it.
DEAD_PID = 2147483647
# A holder on another host: never looked up locally, so any value is safe.
OTHER_HOST_PID = 4242
PRECONDITION_FAILED = (
    "An error occurred (PreconditionFailed) when calling the PutObject operation: "
    "At least one of the pre-conditions you specified did not hold\n"
)
SHARED_ROOT = "shared/terraform"
FETCH_ROOT = "shared/fetch-model/terraform"
OUTPUTS = {
    "models_bucket": BUCKET,
    "region": "us-east-1",
    "fetch_model_cluster": CLUSTER,
    "fetch_model_task_definition": TASK_DEFINITION,
    "fetch_model_network": NETWORK,
    "fetch_model_log_group": LOG_GROUP,
    "fetch_model_token_parameter": PARAMETER,
}
S3_CONSOLE_URL = (
    f"https://us-east-1.console.aws.amazon.com/s3/buckets/{BUCKET}?region=us-east-1&prefix={PREFIX}"
)

# Logs its argv and environment, then replies from plan-reply.json beside it.
STUB_COPIER = """import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "copier-calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}) + "\\n")
with open(os.path.join(here, "plan-reply.json")) as f:
    reply = json.load(f)
sys.stdout.write(reply["stdout"])
sys.exit(reply["exit"])
"""


def plan_reply(gated=False, exit_code=0):
    doc = {"repo": REPO_ID, "revision_sha": SHA, "files": FILES, "total_bytes": TOTAL, "gated": gated}
    return json.dumps({"stdout": json.dumps(doc) + "\n" if exit_code == 0 else "", "exit": exit_code})


# Wraps the aws stub. A put-parameter whose --cli-input-json names a regular file records the file at
# call time in put-parameter-inputs.jsonl beside the stub's bin dir (outside HOME, so the token
# canary's walk of HOME does not see it) and passes the content to the stub on stdin. Every later aws
# call logs any recorded input file that still exists to put-parameter-leftovers.jsonl.
PUT_INPUT_WRAPPER = """#!{python} -I
import json, os, stat, subprocess, sys
REAL, LOG, LEFT = {real!r}, {log!r}, {left!r}
argv = sys.argv[1:]
if os.path.exists(LOG):
    with open(LOG) as f:
        earlier = [json.loads(line)["path"] for line in f if line.strip()]
    for path in earlier:
        if os.path.lexists(path):
            with open(LEFT, "a") as f:
                f.write(json.dumps({{"argv": argv[:2], "path": path}}) + "\\n")
ref = argv[argv.index("--cli-input-json") + 1] if "--cli-input-json" in argv[:-1] else ""
if argv[:2] == ["ssm", "put-parameter"] and ref.startswith("file://"):
    path = ref[len("file://"):]
    info = os.lstat(path) if os.path.lexists(path) else None
    if info is not None and stat.S_ISREG(info.st_mode):
        with open(path) as f:
            content = f.read()
        with open(LOG, "a") as f:
            dir_mode = stat.S_IMODE(os.lstat(os.path.dirname(path)).st_mode)
            f.write(json.dumps({{"path": path, "mode": stat.S_IMODE(info.st_mode), "dir_mode": dir_mode,
                                "content": content}}) + "\\n")
        sys.exit(subprocess.run([REAL] + argv, input=content, text=True).returncode)
os.execv(REAL, [REAL] + argv)
"""


def wrap_put_parameter_inputs(repo):
    base = repo.bin_dir.parent
    real = base / "aws-stub"
    shutil.move(str(repo.bin_dir / "aws"), str(real))
    wrapper = repo.bin_dir / "aws"
    wrapper.write_text(PUT_INPUT_WRAPPER.format(
        python=sys.executable, real=str(real), log=str(base / "put-parameter-inputs.jsonl"),
        left=str(base / "put-parameter-leftovers.jsonl")))
    wrapper.chmod(0o755)


def put_parameter_inputs(repo):
    """Each put-parameter input file as the aws CLI saw it: {"path", "mode", "dir_mode", "content"}."""
    log = repo.bin_dir.parent / "put-parameter-inputs.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def make_repo(gated=False, plan_exit=0, arn=ARN, real_sleep=False):
    backend = json.dumps({"version": 3, "backend": {"type": "s3", "config": {"bucket": STATE_BUCKET}}})
    repo = StubRepo(
        tfvars=TFVARS,
        files={
            "shared/fetch-model/copier.py": STUB_COPIER,
            "shared/fetch-model/plan-reply.json": plan_reply(gated, plan_exit),
            f"{SHARED_ROOT}/.terraform/terraform.tfstate": backend,
            f"{FETCH_ROOT}/.terraform/terraform.tfstate": backend,
        },
    )
    wrap_put_parameter_inputs(repo)
    if not real_sleep:
        repo.skip_sleeps()
    repo.stub("aws", "sts", "get-caller-identity", stdout=f"{ACCOUNT}\t{arn}\n")
    repo.caller_arn = arn
    return repo


def chdir(repo, root):
    return f"-chdir={repo.root}/{root}"


def lease_doc(owner=ARN, host=HOST, pid=DEAD_PID):
    return json.dumps({"owner_arn": owner, "host": host, "pid": pid,
                       "acquired_at": "2026-10-05T10:00:00Z", "expires_at": "2099-01-01T00:00:00Z"})


def stub_lease(repo, holder=None):
    """The fetch-model lease. Free (holder None): the first create wins, a later acquire finds the
    caller's lease on this host under a dead pid (the test cannot know the script's own pid) and
    takes it over with If-Match, and release deletes it. Held by `holder`: every create fails its
    condition and the read names that holder."""
    if holder:
        repo.stub("aws", "s3api", "put-object", "--if-none-match", stderr=PRECONDITION_FAILED, exit=254)
        repo.stub("aws", "s3api", "get-object", LEASE_KEY, stdout='"e1"\n',
                  last_arg_file=lease_doc(holder, "build-box", OTHER_HOST_PID))
        return
    repo.stub("aws", "s3api", "put-object", "--if-none-match", stdout='"e1"\n', times=1)
    repo.stub("aws", "s3api", "put-object", "--if-none-match", stderr=PRECONDITION_FAILED, exit=254)
    repo.stub("aws", "s3api", "get-object", LEASE_KEY, stdout='"e1"\n', last_arg_file=lease_doc(repo.caller_arn))
    repo.stub("aws", "s3api", "put-object", "--if-match", stdout='"e2"\n')
    repo.stub("aws", "s3api", "delete-object", LEASE_KEY)


def lease_calls(calls, operation):
    return [c for c in calls if c.tool == "aws" and c.argv[:2] == ["s3api", operation] and LEASE_KEY in c.argv]


def tty_env(repo, typed=TOKEN + "\ny\n"):
    tty = repo.home / "tty-input"
    repo.write(tty, typed)
    return {"DRIP_AI_TTY": str(tty), "DRIP_AI_WATCH_SECONDS": "5"}


def copier_calls(repo):
    path = repo.root / "shared" / "fetch-model" / "copier-calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def task_doc(status, exit_code=None):
    container = {"name": "fetch"}
    if exit_code is not None:
        container["exitCode"] = exit_code
    task = {"taskArn": TASK_ARN, "lastStatus": status, "createdAt": time.time() - 125, "containers": [container]}
    if status == "STOPPED":
        task["stoppedReason"] = "Essential container in task exited"
    return json.dumps({"tasks": [task], "failures": []})


def listing(paths):
    sizes = {f["path"]: f["size"] for f in FILES}
    rows = [f"{PREFIX}{path}\t{sizes[path]}" for path in paths] + [f"{PREFIX}drip-ai-fetch.json\t400"]
    return "\n".join(rows) + "\n"


def status_doc(status):
    return json.dumps({"repo": REPO_ID, "revision": SHA, "status": status, "files": FILES, "total_bytes": TOTAL})


def log_page(*messages, token="f/2"):
    events = [{"timestamp": 1, "message": message} for message in messages]
    return json.dumps({"events": events, "nextForwardToken": token, "nextBackwardToken": "b/0"})


LOG_MESSAGES = (
    "PLAN " + json.dumps({"repo": REPO_ID, "revision_sha": SHA, "files": FILES, "total_bytes": TOTAL, "gated": False}),
    'FILE_DONE {"path": "config.json", "size": 1000}',
    'FILE_DONE {"path": "model-00001.safetensors", "size": 3000000000}',
    'PROGRESS {"path": "model-00002.safetensors", "done": 500000000, "size": 2000000000}',
    'PROGRESS {"path": "evil\x1b[31m.bin", "done": 0, "size": 1}',
)


def stub_outputs(repo, applied=True, shared=True):
    """The shared root's models_bucket, and the fetch-model root's outputs, which are missing
    until the first apply when `applied` is False."""
    if shared:
        repo.stub("terraform", chdir(repo, SHARED_ROOT), "output", "-raw", "models_bucket", stdout=BUCKET)
    if not applied:
        repo.stub("terraform", chdir(repo, FETCH_ROOT), "output", "-raw", "fetch_model_cluster", exit=1, times=1,
                  stderr='Error: Output "fetch_model_cluster" not found\n')
    for name, value in OUTPUTS.items():
        repo.stub("terraform", chdir(repo, FETCH_ROOT), "output", "-raw", name, stdout=value)


def stub_watch(repo, statuses, first_listing=("config.json", "model-00001.safetensors")):
    """`statuses` scripts describe-tasks per tick; the last entry repeats. S3 shows a partial copy
    on the first tick when more ticks follow, else the complete one."""
    for status, exit_code in statuses[:-1]:
        repo.stub("aws", "ecs", "describe-tasks", stdout=task_doc(status, exit_code), times=1)
    repo.stub("aws", "ecs", "describe-tasks", stdout=task_doc(*statuses[-1]))
    if len(statuses) > 1:
        repo.stub("aws", "s3api", "list-objects-v2", stdout=listing(first_listing), times=1)
        repo.stub("aws", "s3", "cp", stdout=status_doc("copying"), times=1)
    repo.stub("aws", "s3api", "list-objects-v2", stdout=listing([f["path"] for f in FILES]))
    repo.stub("aws", "s3", "cp", stdout=status_doc("complete"))
    repo.stub("aws", "logs", "get-log-events", "--next-token", "f/2", stdout=log_page(token="f/2"))
    repo.stub("aws", "logs", "get-log-events", stdout=log_page(*LOG_MESSAGES), times=1)
    repo.stub("aws", "ssm", "delete-parameter")
    stub_lease(repo)


def put_parameter_stdin(value):
    return json.dumps(
        {"Name": PARAMETER, "Value": value, "Type": "SecureString", "Overwrite": True}, separators=(",", ":")
    )


def stub_launch(repo, statuses, token_value=TOKEN, apply_exit=0):
    """A first run: no fetch outputs before the apply, all of them after."""
    stub_outputs(repo, applied=False)
    repo.stub("terraform", chdir(repo, FETCH_ROOT), "apply", exit=apply_exit)
    repo.stub("aws", "ecs", "list-tasks", stdout="None\n")
    repo.stub("aws", "ssm", "put-parameter", stdin=put_parameter_stdin(token_value))
    repo.stub("aws", "ecs", "run-task", stdout=json.dumps({"tasks": [{"taskArn": TASK_ARN}], "failures": []}))
    stub_watch(repo, statuses)


def stub_reattach(repo, statuses):
    stub_outputs(repo)
    repo.stub("aws", "ecs", "list-tasks", stdout=TASK_ARN + "\n")
    stub_watch(repo, statuses)


def aws_calls(calls, service, operation):
    return [call for call in calls if call.tool == "aws" and call.argv[:2] == [service, operation]]


def wait_for_call(repo, service, operation, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if aws_calls(repo.calls(), service, operation):
            return
        time.sleep(0.1)
    raise AssertionError(f"no aws {service} {operation} call within {timeout} s")


def interrupt(repo, process_args, env, after):
    """Starts the script in its own process group, sends SIGINT like a terminal's Ctrl-C once the
    `after` call happened, and returns (returncode, stdout, stderr)."""
    process = repo.popen(
        SCRIPT,
        *process_args,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    try:
        wait_for_call(repo, *after)
        time.sleep(0.5)
        os.killpg(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    return process.returncode, stdout, stderr


class InputTest(unittest.TestCase):
    def test_invalid_repo_refused_before_any_cli_call(self):
        cases = [
            ("acme",),
            ("../acme/x",),
            ("acme/..",),
            ("acme/a..b",),
            ("acme/x/y",),
            ("acme/x;rm",),
            ("-acme/x",),
            (REPO_ID, "--revision", "main/../x"),
            (REPO_ID, "--revision", "a b"),
            (REPO_ID, "--name", "x/y/z"),
            (REPO_ID, "--name", "x/.."),
            (REPO_ID, "--name", "org/-x"),
            (REPO_ID, "--name", "org/.x"),
            ("--watch", "org/-x"),
            ("--watch", "org/.x"),
            ("--watch", REPO_ID, "--allow-concurrent"),
            ("--watch", REPO_ID, "--yes"),
            ("--yes",),
        ]
        for args in cases:
            with self.subTest(args=args), make_repo() as repo:
                result = repo.run(SCRIPT, *args, env=tty_env(repo))
                self.assertIn(result.returncode, (1, 2), result.stderr)
                self.assertEqual(repo.calls(), [])
                self.assertEqual(copier_calls(repo), [])
                self.assertEqual(repo.tfvars(), TFVARS)

    def test_malformed_token_rejected_before_ssm_or_apply(self):
        """The lease is taken before the prompt; a rejected token writes nothing and frees it."""
        for typed in ("hf_short", "ghp_" + "a" * 36, "hf_" + "a" * 30 + "!", "hf_" + "a" * 65, " " + TOKEN):
            with self.subTest(typed=typed), make_repo() as repo:
                stub_outputs(repo, applied=False)
                stub_lease(repo)
                result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo, typed + "\n"))
                self.assertEqual(result.returncode, 1)
                self.assertIn("not a Hugging Face token", result.stderr)
                calls = repo.calls()
                self.assertEqual({tuple(c.argv[:2]) for c in calls if c.tool == "aws"},
                                 {("sts", "get-caller-identity"), ("s3api", "put-object"),
                                  ("s3api", "get-object"), ("s3api", "delete-object")})
                self.assertEqual(len(lease_calls(calls, "delete-object")), 1)
                self.assertFalse(any("apply" in c.argv for c in calls))
                self.assertEqual(repo.tfvars(), TFVARS)
                self.assertNotIn(typed.strip(), result.stdout + result.stderr)

    def test_empty_token_for_gated_repo_refused(self):
        for gated, plan_exit in ((True, 0), (False, 5)):
            with self.subTest(gated=gated, plan_exit=plan_exit), make_repo(gated, plan_exit) as repo:
                stub_outputs(repo, applied=False)
                stub_lease(repo)
                result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo, "\n"))
                self.assertEqual(result.returncode, 1)
                self.assertIn("gated or private", result.stderr)
                calls = repo.calls()
                self.assertEqual([c for c in calls if c.tool == "aws" and c.argv[0] in ("ssm", "ecs")], [])
                self.assertFalse(any("apply" in c.argv for c in calls))
                self.assertEqual(len(lease_calls(calls, "delete-object")), 1)
                self.assertEqual(repo.tfvars(), TFVARS)


class LaunchTest(unittest.TestCase):
    def test_plain_apply_on_fetch_root_no_target(self):
        with make_repo() as repo:
            stub_launch(repo, [("STOPPED", 0)], apply_exit=1)
            result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 1)
            self.assertIn("apply failed or was declined", result.stderr)
            calls = repo.calls()
            applies = [c for c in calls if c.tool == "terraform" and "apply" in c.argv]
            self.assertEqual(len(applies), 1)
            self.assertEqual(applies[0].argv, [chdir(repo, FETCH_ROOT), "apply"])
            self.assertEqual(applies[0].env["TF_VAR_region"], "us-east-1")
            self.assertFalse(any(c.tool == "terraform" and f"-chdir={repo.root}/infra" in c.argv for c in calls))
            self.assertFalse(any("init" in c.argv for c in calls if c.tool == "terraform"))
            self.assertEqual(repo.tfvars(), TFVARS)
            self.assertEqual([c for c in calls if c.tool == "aws" and c.argv[0] in ("ssm", "ecs")], [])
            self.assertEqual(len(lease_calls(calls, "delete-object")), 1)

    def test_refuses_without_shared_root(self):
        missing = {"not applied": {"exit": 1, "stderr": 'Error: Output "models_bucket" not found\n'},
                   "no state": {"stdout": ""},
                   "unreadable": {"exit": 1, "stderr": "Error: No valid credential sources found\n"}}
        for label, reply in missing.items():
            with self.subTest(label), make_repo() as repo:
                repo.stub("terraform", chdir(repo, SHARED_ROOT), "output", "-raw", "models_bucket", **reply)
                stub_outputs(repo, shared=False)
                stub_lease(repo)
                result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
                self.assertEqual(result.returncode, 1)
                self.assertIn("run bin/drip-ai setup first (creates the shared models bucket)", result.stderr)
                if label == "unreadable":
                    self.assertIn("No valid credential sources found", result.stderr)
                calls = repo.calls()
                self.assertEqual([c.argv for c in calls if c.tool == "terraform"],
                                 [[chdir(repo, SHARED_ROOT), "output", "-raw", "models_bucket"]])
                self.assertEqual([c.argv[:2] for c in calls if c.tool == "aws"], [["sts", "get-caller-identity"]])
                self.assertNotIn("Hugging Face token", result.stderr)

    def test_run_task_tags_started_by(self):
        cases = {ARN: ARN, f"arn:aws:sts::{ACCOUNT}:assumed-role/Dev/a,b": f"arn:aws:sts::{ACCOUNT}:assumed-role/Dev/a_b"}
        for arn, tag in cases.items():
            with self.subTest(arn=arn), make_repo(arn=arn) as repo:
                stub_launch(repo, [("STOPPED", 0)])
                result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
                self.assertEqual(result.returncode, 0, result.stderr)
                [run] = aws_calls(repo.calls(), "ecs", "run-task")
                tags = run.argv[run.argv.index("--tags") + 1: run.argv.index("--tags") + 4]
                self.assertEqual(tags[2], f"key=StartedBy,value={tag}")

    def test_run_task_carries_project_tags_and_propagation(self):
        with make_repo() as repo:
            stub_launch(repo, [("STOPPED", 0)])
            result = repo.run(SCRIPT, REPO_ID, "--all-files", env=tty_env(repo))
            self.assertEqual(result.returncode, 0, result.stderr)
            runs = aws_calls(repo.calls(), "ecs", "run-task")
            self.assertEqual(len(runs), 1)
            argv = runs[0].argv

            def value(flag, count=1):
                index = argv.index(flag)
                return argv[index + 1: index + 1 + count]

            self.assertEqual(value("--cluster"), [CLUSTER])
            self.assertEqual(value("--task-definition"), [TASK_DEFINITION])
            self.assertEqual(value("--launch-type"), ["FARGATE"])
            self.assertEqual(value("--network-configuration"), [NETWORK])
            self.assertEqual(value("--propagate-tags"), ["TASK_DEFINITION"])
            self.assertIn("--enable-ecs-managed-tags", argv)
            self.assertEqual(
                value("--tags", 3), ["key=Project,value=drip-ai", "key=ManagedBy,value=shared/fetch-model/fetch-model.sh",
                                     f"key=StartedBy,value={ARN}"]
            )
            started_by = value("--started-by")[0]
            self.assertRegex(started_by, r"^[A-Za-z0-9_-]{1,36}$")
            listed = aws_calls(repo.calls(), "ecs", "list-tasks")
            # First run: no fetch outputs yet, so nothing to reattach; the one cluster-wide listing
            # checks for other fetch tasks still starting before the token goes.
            self.assertEqual([c for c in listed if "--started-by" in c.argv], [])
            self.assertEqual(len(listed), 1)
            overrides = json.loads(value("--overrides")[0])
            self.assertEqual(
                overrides,
                {
                    "containerOverrides": [
                        {
                            "name": "fetch",
                            "command": [
                                "copy", "--repo", REPO_ID, "--revision", SHA, "--name", REPO_ID,
                                "--bucket", BUCKET, "--region", "us-east-1", "--all-files",
                            ],
                        }
                    ]
                },
            )

    def test_failed_launch_deletes_token(self):
        """No task exists when put-parameter or run-task fails, so nothing needs the token."""
        for failing in ("put-parameter", "run-task"):
            with self.subTest(failing=failing), make_repo() as repo:
                stub_outputs(repo, applied=False)
                stub_lease(repo)
                repo.stub("terraform", "apply")
                if failing == "put-parameter":
                    repo.stub("aws", "ssm", "put-parameter", stdin=put_parameter_stdin(TOKEN), exit=254)
                else:
                    repo.stub("aws", "ssm", "put-parameter", stdin=put_parameter_stdin(TOKEN))
                    repo.stub("aws", "ecs", "run-task", exit=255)
                repo.stub("aws", "ssm", "delete-parameter")
                result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
                self.assertEqual(result.returncode, 1)
                deletes = aws_calls(repo.calls(), "ssm", "delete-parameter")
                self.assertEqual(len(deletes), 1)
                self.assertEqual(deletes[0].argv, ["ssm", "delete-parameter", "--region", "us-east-1", "--name", PARAMETER])
                self.assertEqual(aws_calls(repo.calls(), "ecs", "describe-tasks"), [])
                self.assertNotIn("stays in SSM", result.stderr)
                self.assertNotIn("Reattach:", result.stdout)

    def test_run_task_failures_stop_and_delete_the_token(self):
        with make_repo() as repo:
            stub_outputs(repo, applied=False)
            stub_lease(repo)
            repo.stub("terraform", "apply")
            repo.stub("aws", "ssm", "put-parameter", stdin=put_parameter_stdin(TOKEN))
            repo.stub("aws", "ecs", "run-task", stdout=json.dumps(
                {"tasks": [], "failures": [{"reason": "RESOURCE:ENI", "detail": "\x1b[2Jquota"}]}))
            repo.stub("aws", "ssm", "delete-parameter")
            result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 1)
            self.assertIn("ECS did not start the task", result.stderr)
            self.assertIn("RESOURCE:ENI ?[2Jquota", result.stderr)
            self.assertEqual(len(aws_calls(repo.calls(), "ssm", "delete-parameter")), 1)
            self.assertEqual(aws_calls(repo.calls(), "ecs", "describe-tasks"), [])


    def test_launch_requires_typed_yes(self):
        """A detached run can leave the token parameter in place, so the apply may ask nothing; the
        launch asks its own question, after the apply and before the token is written."""
        cases = {"y": (False, 0, f"{REPO_ID} @ 0123456: 3 files, 5.0 GB"),
                 "yes": (False, 0, f"{REPO_ID} @ 0123456: 3 files, 5.0 GB"),
                 "Y": (False, 0, f"{REPO_ID} @ 0123456: 3 files, 5.0 GB"),
                 "gated": (True, 5, f"{REPO_ID} @ main (private or gated; size shows once the task starts)")}
        for label, (gated, plan_exit, summary) in cases.items():
            answer = "y" if label == "gated" else label
            with self.subTest(label), make_repo(gated, plan_exit) as repo:
                stub_launch(repo, [("STOPPED", 0)])
                result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo, f"{TOKEN}\n{answer}\n"))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Start the copy? [y/N] ", result.stderr)
                self.assertIn(f"Ready to copy {summary} to models/{REPO_ID}/\nCost: Fargate task about $0.40/h",
                              result.stdout)
                # The question follows the apply and precedes everything that bills or holds the token.
                self.assertLess(result.stdout.index("Applying the fetch-model stack"), result.stdout.index("Ready to copy"))
                self.assertLess(result.stdout.index("Ready to copy"), result.stdout.index("Started task"))
                calls = repo.calls()
                self.assertEqual(len(aws_calls(calls, "ssm", "put-parameter")), 1)
                self.assertEqual(len(aws_calls(calls, "ecs", "run-task")), 1)

    def test_launch_declined_bills_nothing(self):
        for label, typed in {"enter": f"{TOKEN}\n\n", "n": f"{TOKEN}\nn\n", "no": f"{TOKEN}\nno\n",
                             "other text": f"{TOKEN}\nsure\n", "end of input": f"{TOKEN}\n"}.items():
            with self.subTest(label), make_repo() as repo:
                stub_launch(repo, [("STOPPED", 0)])
                result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo, typed))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Start the copy? [y/N] ", result.stderr)
                self.assertIn("Not started; nothing billed.\n", result.stdout)
                self.assertNotIn("Started task", result.stdout)
                calls = repo.calls()
                self.assertEqual(len([c for c in calls if c.tool == "terraform" and "apply" in c.argv]), 1)
                self.assertEqual([c for c in calls if c.tool == "aws" and c.argv[0] == "ssm"], [])
                self.assertEqual([c for c in calls if c.tool == "aws" and c.argv[0] == "ecs"], [])
                self.assertEqual(len(lease_calls(calls, "delete-object")), 1)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)


class YesTest(unittest.TestCase):
    """--yes answers the apply and the launch question, never the token prompt, and needs no terminal."""

    NO_TTY = {"DRIP_AI_TTY": "/nonexistent/drip-ai-tty", "DRIP_AI_WATCH_SECONDS": "5"}

    def test_yes_copies_a_public_repo_without_a_terminal(self):
        with make_repo() as repo:
            stub_launch(repo, [("STOPPED", 0)], token_value="none")
            result = repo.run(SCRIPT, REPO_ID, "--yes", env=self.NO_TTY)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Hugging Face token", result.stderr)
            self.assertNotIn("Start the copy?", result.stderr)
            self.assertIn(f"Ready to copy {REPO_ID} @ 0123456: 3 files, 5.0 GB to models/{REPO_ID}/\n"
                          "Cost: Fargate task about $0.40/h", result.stdout)
            self.assertLess(result.stdout.index("Ready to copy"), result.stdout.index("Starting the copy (--yes)."))
            calls = repo.calls()
            [apply] = [c.argv for c in calls if c.tool == "terraform" and "apply" in c.argv]
            self.assertEqual(apply, [chdir(repo, FETCH_ROOT), "apply", "-auto-approve", "-input=false"])
            self.assertEqual(len(aws_calls(calls, "ssm", "put-parameter")), 1)
            self.assertEqual(len(aws_calls(calls, "ecs", "run-task")), 1)

    def test_yes_refuses_a_gated_repo_before_anything_bills(self):
        for gated, plan_exit in ((True, 0), (False, 5)):
            with self.subTest(gated=gated, plan_exit=plan_exit), make_repo(gated, plan_exit) as repo:
                stub_launch(repo, [("STOPPED", 0)])
                result = repo.run(SCRIPT, REPO_ID, "--yes", env=self.NO_TTY)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"{REPO_ID} is private or gated, and its token is typed only at the hidden prompt;"
                              " rerun without --yes", result.stderr)
                self.assertEqual([c.argv[:2] for c in repo.calls()], [])


class TokenTest(unittest.TestCase):
    def test_token_never_in_argv_or_child_env(self):
        with make_repo() as repo:
            stub_launch(repo, [("STOPPED", 0)])
            result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = repo.calls()
            self.assertTrue(calls)
            for call in calls:
                self.assertNotIn(TOKEN, json.dumps(call.argv))
                self.assertNotIn(TOKEN, json.dumps(call.env))
            for call in copier_calls(repo):
                self.assertNotIn(TOKEN, json.dumps(call))
            carriers = [call for call in calls if call.stdin and TOKEN in call.stdin]
            self.assertEqual([c.argv[:2] for c in carriers], [["ssm", "put-parameter"]])
            self.assertNotIn(TOKEN, result.stdout + result.stderr)
            tty_input = repo.home / "tty-input"
            for path in repo.home.rglob("*"):
                if path.is_file() and path != tty_input:
                    self.assertNotIn(TOKEN, path.read_text(errors="replace"), path)
            self.assertEqual(sorted(p.name for p in repo.home.iterdir()), ["tty-input"])

    def assert_private_input_file(self, repo, value):
        """One put-parameter read a 0600 regular file holding the JSON in the script's 0700 mktemp -d
        state dir, and both are gone afterwards. Some mktemp builds ignore TMPDIR, so the state
        dir is identified by name rather than by sitting under HOME."""
        puts = aws_calls(repo.calls(), "ssm", "put-parameter")
        self.assertEqual(len(puts), 1)
        inputs = put_parameter_inputs(repo)
        self.assertEqual(len(inputs), 1)
        path = inputs[0]["path"]
        self.assertEqual(
            puts[0].argv,
            ["ssm", "put-parameter", "--region", "us-east-1", "--cli-input-json", "file://" + path],
        )
        self.assertEqual(os.path.basename(path), "token-param.json")
        state_dir = os.path.dirname(os.path.normpath(path))
        self.assertTrue(os.path.basename(state_dir).startswith("tmp."), state_dir)
        self.assertEqual(inputs[0]["dir_mode"], 0o700)
        self.assertEqual(inputs[0]["mode"], 0o600)
        self.assertEqual(
            json.loads(inputs[0]["content"]),
            {"Name": PARAMETER, "Value": value, "Type": "SecureString", "Overwrite": True},
        )
        self.assertFalse((repo.bin_dir.parent / "put-parameter-leftovers.jsonl").exists())
        self.assertFalse(os.path.lexists(path))
        self.assertFalse(os.path.lexists(state_dir))
        self.assertEqual(sorted(p.name for p in repo.home.iterdir()), ["tty-input"])

    def test_token_written_via_private_file_to_managed_parameter_only(self):
        for typed, value in ((TOKEN, TOKEN), ("", "none")):
            with self.subTest(value=value), make_repo() as repo:
                stub_launch(repo, [("STOPPED", 0)], token_value=value)
                result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo, typed + "\ny\n"))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_private_input_file(repo, value)

    def test_token_file_removed_when_put_fails(self):
        with make_repo() as repo:
            stub_outputs(repo, applied=False)
            stub_lease(repo)
            repo.stub("terraform", "apply")
            repo.stub("aws", "ssm", "put-parameter", stdin=put_parameter_stdin(TOKEN), exit=254)
            repo.stub("aws", "ssm", "delete-parameter")
            result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 1)
            self.assertIn("could not write the token", result.stderr)
            self.assert_private_input_file(repo, TOKEN)
            for call in repo.calls():
                self.assertNotIn(TOKEN, json.dumps(call.argv))
            self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_token_parameter_deleted_after_running(self):
        with make_repo() as repo:
            stub_launch(repo, [("RUNNING", None), ("STOPPED", 0)])
            result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = repo.calls()
            sequence = [
                tuple(c.argv[:2]) for c in calls
                if c.tool == "aws" and tuple(c.argv[:2]) in
                {("ssm", "put-parameter"), ("ecs", "run-task"), ("ecs", "describe-tasks"), ("ssm", "delete-parameter")}
            ]
            self.assertEqual(
                sequence,
                [("ssm", "put-parameter"), ("ecs", "run-task"), ("ecs", "describe-tasks"),
                 ("ssm", "delete-parameter"), ("ecs", "describe-tasks")],
            )
            delete = aws_calls(calls, "ssm", "delete-parameter")[0]
            self.assertEqual(delete.argv, ["ssm", "delete-parameter", "--region", "us-east-1", "--name", PARAMETER])
            self.assertNotIn("gated copy may then fail", result.stderr)

    def test_interrupt_while_pending_keeps_token_and_prints_cleanup(self):
        """ECS resolves the task's secrets before the container starts, so deleting the parameter
        while the task is PENDING would fail every copy with ResourceInitializationError. The lease a
        launch took stays with the token; --watch holds none."""
        runs = {"launch": (stub_launch, (REPO_ID,), True), "--watch": (stub_reattach, ("--watch", REPO_ID), False)}
        for label, (stub, args, holds_lease) in runs.items():
            with self.subTest(label), make_repo(real_sleep=True) as repo:
                stub(repo, [("PENDING", None)])
                code, stdout, stderr = interrupt(repo, args, tty_env(repo), ("ecs", "describe-tasks"))
                self.assertEqual(code, 130, stderr)
                calls = repo.calls()
                self.assertEqual(aws_calls(calls, "ssm", "delete-parameter"), [])
                self.assertEqual(aws_calls(calls, "ecs", "stop-task"), [])
                self.assertEqual(lease_calls(calls, "delete-object"), [])
                cleanup = [line for line in stderr.splitlines() if "stays in SSM" in line]
                self.assertEqual(len(cleanup), 1, stderr)
                self.assertIn(f"shared/fetch-model/fetch-model.sh --watch {REPO_ID}", cleanup[0])
                self.assertTrue(
                    cleanup[0].endswith(f"aws ssm delete-parameter --region us-east-1 --name {PARAMETER}"), cleanup[0]
                )
                kept = [line for line in stderr.splitlines() if "lease stays held" in line]
                self.assertEqual(len(kept), 1 if holds_lease else 0, stderr)
                if holds_lease:
                    self.assertIn(f"shared/fetch-model/fetch-model.sh --watch {REPO_ID} releases it", kept[0])
                self.assertIn("the task is still starting", stdout)
                self.assertNotIn("keeps running", stdout)
                self.assertIn(f"Reattach: shared/fetch-model/fetch-model.sh --watch {REPO_ID}", stdout)
                self.assertNotIn(TOKEN, stdout + stderr)


class WatchTest(unittest.TestCase):
    def test_reattaches_running_task_without_prompt_or_second_run(self):
        with make_repo() as repo:
            stub_reattach(repo, [("STOPPED", 0)])
            env = {"DRIP_AI_TTY": str(repo.home / "no-such-tty"), "DRIP_AI_WATCH_SECONDS": "5"}
            result = repo.run(SCRIPT, REPO_ID, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("token", result.stderr.lower())
            calls = repo.calls()
            self.assertEqual(aws_calls(calls, "ecs", "run-task"), [])
            self.assertEqual(aws_calls(calls, "ssm", "put-parameter"), [])
            self.assertFalse(any("apply" in c.argv for c in calls))
            self.assertEqual(repo.tfvars(), TFVARS)
            listed = aws_calls(calls, "ecs", "list-tasks")[0].argv
            self.assertEqual(listed[listed.index("--desired-status") + 1], "RUNNING")
            self.assertIn("Next: sagemaker/start.sh acme/tiny-model", result.stdout)

    def test_watch_renders_s3_and_log_progress(self):
        """A terminal gets one cleared frame per tick; anything else gets one line per tick and the
        console links once. Both wait the 5 s floor between ticks, whatever DRIP_AI_WATCH_SECONDS asks."""
        for on_tty in (True, False):
            with self.subTest(on_tty=on_tty), make_repo() as repo:
                stub_reattach(repo, [("RUNNING", None), ("STOPPED", 0)])
                env = {**tty_env(repo), "DRIP_AI_WATCH_SECONDS": "1"}
                if on_tty:
                    self.assert_tty_frames(repo, env)
                else:
                    self.assert_appended_lines(repo, env)
                self.assertEqual(repo.sleeps(), ["5"])

    def assert_tty_frames(self, repo, env):
        master, slave = pty.openpty()
        process = repo.popen(
            SCRIPT, "--watch", REPO_ID, env=env, stdin=subprocess.DEVNULL, stdout=slave, stderr=subprocess.PIPE,
        )
        os.close(slave)
        chunks = []
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        os.close(master)
        process.wait(timeout=60)
        stderr = process.stderr.read().decode()
        process.stderr.close()
        self.assertEqual(process.returncode, 0, stderr)
        output = b"".join(chunks).decode().replace("\r\n", "\n")
        frames = output.split("\x1b[H\x1b[J")[1:]
        self.assertEqual(len(frames), 2, output)
        first = frames[0]
        for expected in (
            S3_CONSOLE_URL,
            "acme/tiny-model @ 0123456\n",
            "task RUNNING, elapsed 2m 0",
            "S3 done files 2/3  3.00/5.00 GB (60%)\n",
            "in flight 1 files, 0.50 GB\n",
            "rate --, ETA --\n",
            '  FILE_DONE {"path": "model-00001.safetensors", "size": 3000000000}\n',
            '  PROGRESS {"path": "evil?[31m.bin", "done": 0, "size": 1}\n',
        ):
            self.assertIn(expected, first)
        self.assertNotIn("\x1b[31m", output)
        self.assertIn("S3 done files 3/3  5.00/5.00 GB (100%)", frames[1])
        self.assertRegex(frames[1], r"rate \d+\.\d MB/s, ETA 0m 00s")
        self.assertIn("Next: sagemaker/start.sh acme/tiny-model", frames[1])

    def assert_appended_lines(self, repo, env):
        result = repo.run(SCRIPT, "--watch", REPO_ID, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        ticks = [line for line in result.stdout.splitlines() if re.match(r"^\d\d:\d\d:\d\d \| ", line)]
        self.assertEqual(len(ticks), 2, result.stdout)
        self.assertIn(" | acme/tiny-model @ 0123456 | task RUNNING, elapsed 2m 0", ticks[0])
        self.assertIn(" | S3 done files 2/3  3.00/5.00 GB (60%) | in flight 1 files, 0.50 GB | ", ticks[0])
        self.assertIn(" | task STOPPED, ", ticks[1])
        self.assertEqual(result.stdout.count(S3_CONSOLE_URL), 1)
        self.assertIn(f"S3 (objects appear as each file completes): {S3_CONSOLE_URL}\n", result.stdout)
        self.assertIn(
            f"https://us-east-1.console.aws.amazon.com/ecs/v2/clusters/{CLUSTER}/tasks/{TASK_ID}/configuration",
            result.stdout,
        )

    def test_ctrl_c_detaches_without_stop_task(self):
        with make_repo(real_sleep=True) as repo:
            stub_reattach(repo, [("RUNNING", None)])
            code, stdout, stderr = interrupt(repo, ("--watch", REPO_ID), tty_env(repo), ("logs", "get-log-events"))
            self.assertEqual(code, 130, stderr)
            self.assertIn(f"Reattach: shared/fetch-model/fetch-model.sh --watch {REPO_ID}\n", stdout)
            self.assertIn(
                f"aws ecs stop-task --region us-east-1 --cluster {CLUSTER} --task {TASK_ARN}\n", stdout
            )
            self.assertEqual(aws_calls(repo.calls(), "ecs", "stop-task"), [])

    def test_failed_job_leaves_model_source_untouched(self):
        with make_repo() as repo:
            stub_launch(repo, [("STOPPED", 4)])
            result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 1)
            self.assertIn("copier exit 4: a file failed its size or hash check", result.stderr)
            self.assertIn(
                f"Rerunning the same command resumes: shared/fetch-model/fetch-model.sh {REPO_ID} --name {REPO_ID}"
                f" --revision {SHA}\n",
                result.stderr,
            )
            self.assertNotIn("Next:", result.stdout)
            self.assertEqual(repo.tfvars(), TFVARS)
            for call in repo.calls():
                self.assertIn('model_source = "Org/Model"  # serving model\n', call.tfvars)
            self.assertEqual(aws_calls(repo.calls(), "ecs", "stop-task"), [])

    def test_truncated_plan_line_still_verifies_from_status_object(self):
        """Docker splits log lines over 16 KB, so a PLAN line for a repo with many files arrives in
        pieces that do not parse; --watch has no preflight, so totals and the revision come from
        the status object."""
        many = [{"path": f"shard-{i:05d}/model.safetensors", "size": 1} for i in range(600)]
        plan_line = "PLAN " + json.dumps(
            {"repo": REPO_ID, "revision_sha": SHA, "files": FILES + many, "total_bytes": TOTAL, "gated": False}
        )
        self.assertGreater(len(plan_line), 16384)
        with make_repo() as repo:
            stub_outputs(repo)
            repo.stub("aws", "ecs", "list-tasks", stdout=TASK_ARN + "\n")
            repo.stub("aws", "logs", "get-log-events", "--next-token", "f/2", stdout=log_page(token="f/2"))
            repo.stub(
                "aws", "logs", "get-log-events",
                stdout=log_page(plan_line[:16384], plan_line[16384:], *LOG_MESSAGES[1:3]), times=1,
            )
            stub_watch(repo, [("STOPPED", 0)])
            result = repo.run(SCRIPT, "--watch", REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Copied 3 files, 5.00 GB to s3://{BUCKET}/{PREFIX}\n", result.stdout)
            self.assertIn(" | acme/tiny-model @ 0123456 | ", result.stdout)
            self.assertIn("S3 done files 3/3  5.00/5.00 GB (100%)", result.stdout)

    def test_exit_zero_with_non_sha_status_revision_is_not_success(self):
        with make_repo() as repo:
            stub_outputs(repo)
            repo.stub("aws", "ecs", "list-tasks", stdout=TASK_ARN + "\n")
            repo.stub("aws", "s3", "cp", stdout=status_doc("complete").replace(SHA, "main"))
            stub_watch(repo, [("STOPPED", 0)])
            result = repo.run(SCRIPT, "--watch", REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 1)
            self.assertIn("revision does not match", result.stderr)
            self.assertNotIn("Next:", result.stdout)

    def test_exit_zero_without_complete_status_is_not_success(self):
        with make_repo() as repo:
            stub_outputs(repo)
            repo.stub("aws", "ecs", "list-tasks", stdout=TASK_ARN + "\n")
            repo.stub("aws", "s3", "cp", stdout=status_doc("copying"))
            stub_watch(repo, [("STOPPED", 0)])
            result = repo.run(SCRIPT, "--watch", REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 1)
            self.assertIn("not verified", result.stderr)
            self.assertNotIn("Next:", result.stdout)


OTHER_TASK_ARN = f"arn:aws:ecs:us-east-1:111122223333:task/{CLUSTER}/{'f' * 32}"


def stub_ended_copy(repo, other_status=None, status=None):
    """--watch NAME after NAME's task ended: no task started by NAME; the cluster runs another
    fetch task in `other_status` (None: no task); S3 holds the full listing and a status object
    saying `status` (None: no status object)."""
    stub_outputs(repo)
    repo.stub("aws", "ecs", "list-tasks", "--started-by", stdout="None\n")
    repo.stub("aws", "ecs", "list-tasks", stdout=(OTHER_TASK_ARN if other_status else "None") + "\n")
    if other_status:
        task = {"taskArn": OTHER_TASK_ARN, "lastStatus": other_status, "containers": [{"name": "fetch"}]}
        repo.stub("aws", "ecs", "describe-tasks", OTHER_TASK_ARN, stdout=json.dumps({"tasks": [task], "failures": []}))
    repo.stub("aws", "s3api", "list-objects-v2", stdout=listing([f["path"] for f in FILES]))
    if status:
        repo.stub("aws", "s3", "cp", stdout=status_doc(status))
    else:
        repo.stub("aws", "s3", "cp", exit=1, stderr="An error occurred (404) when calling the HeadObject operation\n")
    repo.stub("aws", "ssm", "delete-parameter")
    stub_lease(repo)


def stub_other_fetch(repo, tags):
    """A first run for NAME while the cluster runs another fetch task carrying `tags`."""
    stub_outputs(repo)
    repo.stub("aws", "ecs", "list-tasks", "--started-by", stdout="None\n")
    repo.stub("aws", "ecs", "list-tasks", stdout=OTHER_TASK_ARN + "\n")
    task = {"taskArn": OTHER_TASK_ARN, "lastStatus": "RUNNING", "containers": [{"name": "fetch"}], "tags": tags}
    repo.stub("aws", "ecs", "describe-tasks", "--include", "TAGS", OTHER_TASK_ARN,
              stdout=json.dumps({"tasks": [task], "failures": []}))


def sequence(calls, wanted):
    """The order of the aws calls named in `wanted`, each as (service, operation)."""
    return [tuple(c.argv[:2]) for c in calls if c.tool == "aws" and tuple(c.argv[:2]) in wanted]


TOKEN_AND_LEASE = {("ssm", "put-parameter"), ("ssm", "delete-parameter"), ("s3api", "delete-object")}
OTHER_NAME = "acme/other-model"


def started_by_for(name):
    """The script's ECS startedBy value for a model name."""
    checksum = subprocess.run(["cksum"], input=name, capture_output=True, text=True, check=True).stdout.split()[0]
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:25] + "-" + checksum


def stub_starting_fetch(repo, status, name=OTHER_NAME, started_by=None):
    """A first run for NAME while the cluster holds a fetch task the caller started for `name`, in
    `status`."""
    stub_outputs(repo)
    repo.stub("aws", "ecs", "list-tasks", "--started-by", stdout="None\n")
    repo.stub("aws", "ecs", "list-tasks", stdout=OTHER_TASK_ARN + "\n")
    task = {"taskArn": OTHER_TASK_ARN, "lastStatus": status, "startedBy": started_by or started_by_for(name),
            "containers": [{"name": "fetch"}],
            "tags": [{"key": "Project", "value": "drip-ai"}, {"key": "StartedBy", "value": ARN}],
            "overrides": {"containerOverrides": [{"name": "fetch", "command": ["copy", "--repo", OTHER_NAME,
                                                                               "--name", name]}]}}
    repo.stub("aws", "ecs", "describe-tasks", "--include", "TAGS", OTHER_TASK_ARN,
              stdout=json.dumps({"tasks": [task], "failures": []}))


def stub_beside_other_task(repo, own_statuses, other_statuses):
    """A task for NAME runs (so --watch NAME and a rerun reattach to it) beside OTHER, another
    fetch task; `other_statuses` scripts OTHER's status per describe, the last entry repeating."""
    repo.stub("aws", "ecs", "list-tasks", "--started-by", stdout=TASK_ARN + "\n")
    repo.stub("aws", "ecs", "list-tasks", stdout=f"{TASK_ARN}\t{OTHER_TASK_ARN}\n")
    for index, status in enumerate(other_statuses):
        task = {"taskArn": OTHER_TASK_ARN, "lastStatus": status, "containers": [{"name": "fetch"}]}
        last = index == len(other_statuses) - 1
        repo.stub("aws", "ecs", "describe-tasks", OTHER_TASK_ARN,
                  stdout=json.dumps({"tasks": [task], "failures": []}), times=None if last else 1)
    stub_outputs(repo)
    stub_watch(repo, own_statuses)


def describe_and_delete(calls):
    """The order of describe-tasks (as 'own' or 'other') and delete-parameter calls."""
    order = []
    for call in calls:
        if call.tool == "aws" and call.argv[:2] == ["ecs", "describe-tasks"]:
            order.append("other" if OTHER_TASK_ARN in call.argv else "own")
        elif call.tool == "aws" and call.argv[:2] == ["ssm", "delete-parameter"]:
            order.append("delete")
    return order


class WatchTokenTest(unittest.TestCase):
    """Every fetch task reads the one token parameter when it starts, so a watch deletes the token
    only once no other fetch task may still be starting: on --watch NAME after NAME's task ended (S3
    then says how the copy ended), and on --watch or a rerun that reattaches to NAME's task alike."""

    PATHS = {"--watch": ("--watch", REPO_ID), "rerun": (REPO_ID,)}
    NOT_FOUND = "An error occurred (ParameterNotFound) when calling the DeleteParameter operation: \n"

    def cluster_list(self, repo):
        lists = [c.argv for c in aws_calls(repo.calls(), "ecs", "list-tasks") if "--started-by" not in c.argv]
        self.assertEqual(len(lists), 1)
        return lists[0]

    def test_token_kept_while_another_fetch_task_is_starting(self):
        for other_status in ("PROVISIONING", "PENDING", "ACTIVATING"):
            with self.subTest("ended copy", other_status=other_status), make_repo() as repo:
                stub_ended_copy(repo, other_status=other_status)
                result = repo.run(SCRIPT, "--watch", REPO_ID, env=tty_env(repo))
                self.assertEqual(result.returncode, 1)
                self.assertEqual(aws_calls(repo.calls(), "ssm", "delete-parameter"), [])
                kept = [line for line in result.stderr.splitlines() if "still starting" in line]
                self.assertEqual(len(kept), 1, result.stderr)
                self.assertTrue(
                    kept[0].endswith(f"aws ssm delete-parameter --region us-east-1 --name {PARAMETER}"), kept[0]
                )
                self.assertIn(f"no fetch found for {REPO_ID}", result.stderr)
                self.assertNotIn("Next:", result.stdout)
        for label, args in self.PATHS.items():
            with self.subTest(label, ends="after other starts"), make_repo() as repo:
                stub_beside_other_task(repo, [("RUNNING", None), ("RUNNING", None), ("STOPPED", 0)],
                                       ["PENDING", "RUNNING"])
                result = repo.run(SCRIPT, *args, env=tty_env(repo))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(describe_and_delete(repo.calls()),
                                 ["own", "other", "own", "other", "delete", "own"])
                kept = [line for line in result.stderr.splitlines() if "still starting" in line]
                self.assertEqual(len(kept), 1, result.stderr)
                self.assertIn(f"1 other fetch task(s) in {CLUSTER} are still starting", kept[0])
                self.assertIn(f"so it stays at {PARAMETER}", kept[0])
                self.assertIn("token kept in SSM while another fetch task starts", result.stdout)
                self.assertEqual(sequence(repo.calls(), TOKEN_AND_LEASE),
                                 [("ssm", "delete-parameter"), ("s3api", "delete-object")])
            with self.subTest(label, ends="while other starting"), make_repo() as repo:
                stub_beside_other_task(repo, [("STOPPED", 0)], ["PENDING"])
                result = repo.run(SCRIPT, *args, env=tty_env(repo))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(aws_calls(repo.calls(), "ssm", "delete-parameter"), [])
                self.assertEqual(lease_calls(repo.calls(), "delete-object"), [])
                self.assertIn(f"Next: sagemaker/start.sh {REPO_ID}", result.stdout)
                self.assertIn(f"so it stays at {PARAMETER}", result.stderr)
                self.assertIn(f"aws ssm delete-parameter --region us-east-1 --name {PARAMETER}", result.stderr)

    def test_token_deleted_when_no_fetch_task_is_starting(self):
        for parameter_gone in (False, True):
            with self.subTest("ended copy", parameter_gone=parameter_gone), make_repo() as repo:
                if parameter_gone:
                    repo.stub("aws", "ssm", "delete-parameter", exit=254, stderr=self.NOT_FOUND)
                stub_ended_copy(repo, other_status="RUNNING", status="copying")
                result = repo.run(SCRIPT, "--watch", REPO_ID, env=tty_env(repo))
                self.assertEqual(result.returncode, 1)
                listed = self.cluster_list(repo)
                self.assertEqual(listed[listed.index("--cluster") + 1], CLUSTER)
                self.assertEqual(listed[listed.index("--desired-status") + 1], "RUNNING")
                described = aws_calls(repo.calls(), "ecs", "describe-tasks")
                self.assertEqual([c.argv[c.argv.index("--tasks") + 1:][:2] for c in described],
                                 [[OTHER_TASK_ARN, "--output"]])
                deletes = aws_calls(repo.calls(), "ssm", "delete-parameter")
                self.assertEqual([c.argv for c in deletes],
                                 [["ssm", "delete-parameter", "--region", "us-east-1", "--name", PARAMETER]])
                self.assertIn(f"token parameter {PARAMETER} is deleted", result.stdout)
                self.assertNotIn("could not delete", result.stderr)
                self.assertIn("the copy stopped before finishing (status: copying)", result.stderr)
                self.assertIn(
                    f"Rerunning the same fetch command resumes: shared/fetch-model/fetch-model.sh {REPO_ID}"
                    f" --name {REPO_ID} --revision {SHA}\n",
                    result.stderr,
                )
                self.assertNotIn("Next:", result.stdout)
                self.assertEqual(aws_calls(repo.calls(), "ecs", "run-task"), [])
        for parameter_gone in (False, True):
            # The watched task's own PENDING phase keeps the token; RUNNING lets it go.
            with self.subTest("own task leaves PENDING", parameter_gone=parameter_gone), make_repo() as repo:
                if parameter_gone:
                    repo.stub("aws", "ssm", "delete-parameter", exit=254, stderr=self.NOT_FOUND)
                stub_reattach(repo, [("PENDING", None), ("RUNNING", None), ("STOPPED", 0)])
                result = repo.run(SCRIPT, "--watch", REPO_ID, env=tty_env(repo))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    sequence(repo.calls(), {("ecs", "describe-tasks"), ("ssm", "delete-parameter")}),
                    [("ecs", "describe-tasks"), ("ecs", "describe-tasks"), ("ssm", "delete-parameter"),
                     ("ecs", "describe-tasks")],
                )
                self.assertNotIn("could not delete", result.stderr)
                self.assertNotIn("stays in SSM", result.stderr)
        for label, args in self.PATHS.items():
            with self.subTest(label), make_repo() as repo:
                stub_beside_other_task(repo, [("RUNNING", None), ("STOPPED", 0)], ["RUNNING"])
                result = repo.run(SCRIPT, *args, env=tty_env(repo))
                self.assertEqual(result.returncode, 0, result.stderr)
                # The other task's status is read before the token goes; the watched one is not
                # counted, because it is past PENDING.
                self.assertEqual(describe_and_delete(repo.calls()), ["own", "other", "delete", "own"])
                self.assertEqual(sequence(repo.calls(), TOKEN_AND_LEASE),
                                 [("ssm", "delete-parameter"), ("s3api", "delete-object")])
                self.assertNotIn("still starting", result.stderr)
                self.assertNotIn("token kept", result.stdout)

    def test_watch_without_running_task_reports_complete_copy(self):
        with make_repo() as repo:
            stub_ended_copy(repo, status="complete")
            result = repo.run(SCRIPT, "--watch", REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(aws_calls(repo.calls(), "ecs", "describe-tasks"), [])
            self.assertEqual(len(aws_calls(repo.calls(), "ssm", "delete-parameter")), 1)
            self.assertIn(f"the copy finished: 3 files, 5.00 GB at s3://{BUCKET}/{PREFIX}\n", result.stdout)
            self.assertIn(f"Next: sagemaker/start.sh {REPO_ID}\n", result.stdout)
            self.assertEqual(result.stderr, "")


class OneAtATimeTest(unittest.TestCase):
    """Every fetch shares one token parameter, so another identity's running fetch refuses a new
    one, and a lease covers the window from the token prompt until the parameter is deleted."""

    def test_refuses_other_identity_running_fetch(self):
        project = {"key": "Project", "value": "drip-ai"}
        cases = {"other": ([project, {"key": "StartedBy", "value": OTHER_ARN}], OTHER_ARN),
                 "untagged": ([project], "an identity it does not record")}
        for label, (tags, who) in cases.items():
            with self.subTest(label), make_repo() as repo:
                stub_other_fetch(repo, tags)
                stub_lease(repo)
                result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"another identity's fetch is running in {CLUSTER}: task {OTHER_TASK_ARN} started by {who}",
                              result.stderr)
                self.assertIn("--allow-concurrent", result.stderr)
                calls = repo.calls()
                self.assertEqual(lease_calls(calls, "put-object"), [])
                self.assertEqual([c for c in calls if c.tool == "aws" and c.argv[0] == "ssm"], [])
                self.assertFalse(any("apply" in c.argv for c in calls))
                self.assertNotIn("Hugging Face token", result.stderr)
        with self.subTest("own"), make_repo() as repo:
            stub_other_fetch(repo, [project, {"key": "StartedBy", "value": ARN}])
            stub_lease(repo)
            repo.stub("terraform", chdir(repo, FETCH_ROOT), "apply", exit=1)
            result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
            self.assertIn("apply failed or was declined", result.stderr)
            self.assertNotIn("another identity", result.stderr)

    def test_refuses_own_pending_fetch_for_another_model(self):
        """The caller's own fetch for another model that is still starting reads the token it
        wrote, so a new fetch would overwrite it."""
        for status in ("PROVISIONING", "PENDING", "ACTIVATING"):
            with self.subTest(status=status), make_repo() as repo:
                stub_starting_fetch(repo, status)
                stub_lease(repo)
                result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"task {OTHER_TASK_ARN} for models/{OTHER_NAME}/ is still starting ({status})",
                              result.stderr)
                self.assertIn("wait for it to reach RUNNING", result.stderr)
                self.assertIn(f"shared/fetch-model/fetch-model.sh --watch {OTHER_NAME}", result.stderr)
                self.assertIn("--allow-concurrent", result.stderr)
                self.assertNotIn("another identity", result.stderr)
                calls = repo.calls()
                self.assertEqual(lease_calls(calls, "put-object"), [])
                self.assertEqual([c for c in calls if c.tool == "aws" and c.argv[0] == "ssm"], [])
                self.assertFalse(any("apply" in c.argv for c in calls))
                self.assertNotIn("Hugging Face token", result.stderr)
        with self.subTest("unsafe name not echoed"), make_repo() as repo:
            stub_starting_fetch(repo, "PENDING", name="acme/x;rm")
            stub_lease(repo)
            result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"task {OTHER_TASK_ARN} is still starting (PENDING)", result.stderr)
            self.assertIn("--watch NAME", result.stderr)
            self.assertNotIn(";rm", result.stderr)
        passes = {"running": ("RUNNING", OTHER_NAME, ()), "same name": ("PENDING", REPO_ID, ()),
                  "--allow-concurrent": ("PENDING", OTHER_NAME, ("--allow-concurrent",))}
        for label, (status, name, extra) in passes.items():
            with self.subTest(label), make_repo() as repo:
                stub_starting_fetch(repo, status, started_by=started_by_for(name))
                stub_lease(repo)
                repo.stub("terraform", chdir(repo, FETCH_ROOT), "apply", exit=1)
                result = repo.run(SCRIPT, REPO_ID, *extra, env=tty_env(repo))
                self.assertIn("apply failed or was declined", result.stderr)
                self.assertNotIn("is still starting", result.stderr)
                if extra:
                    self.assertIn(f"--allow-concurrent: copying while another fetch is starting: task {OTHER_TASK_ARN}",
                                  result.stdout)

    def test_allow_concurrent_overrides(self):
        with make_repo() as repo:
            stub_other_fetch(repo, [{"key": "StartedBy", "value": OTHER_ARN}])
            repo.stub("terraform", chdir(repo, FETCH_ROOT), "apply")
            repo.stub("aws", "ssm", "put-parameter", stdin=put_parameter_stdin(TOKEN))
            repo.stub("aws", "ecs", "run-task", stdout=json.dumps({"tasks": [{"taskArn": TASK_ARN}], "failures": []}))
            stub_watch(repo, [("STOPPED", 0)])
            result = repo.run(SCRIPT, REPO_ID, "--allow-concurrent", env=tty_env(repo))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"--allow-concurrent: copying alongside another identity's fetch: task {OTHER_TASK_ARN}"
                          f" started by {OTHER_ARN}", result.stdout)
            calls = repo.calls()
            self.assertEqual(len(aws_calls(calls, "ecs", "run-task")), 1)
            self.assertEqual(len(lease_calls(calls, "delete-object")), 1)

    def test_lease_conflict_refuses_before_token_prompt(self):
        with make_repo() as repo:
            stub_outputs(repo, applied=False)
            stub_lease(repo, holder=OTHER_ARN)
            env = {"DRIP_AI_TTY": str(repo.home / "no-such-tty"), "DRIP_AI_WATCH_SECONDS": "5"}
            result = repo.run(SCRIPT, REPO_ID, env=env)
            self.assertEqual(result.returncode, 1)
            self.assertIn(
                f"fetch-model is in use by {OTHER_ARN} on build-box (pid {OTHER_HOST_PID}) until 2099-01-01T00:00:00Z",
                result.stderr)
            self.assertIn("could not take the fetch-model lease; nothing was started", result.stderr)
            self.assertNotIn("Hugging Face token", result.stderr)
            self.assertNotIn("needs a terminal", result.stderr)
            calls = repo.calls()
            self.assertEqual([c for c in calls if c.tool == "aws" and c.argv[0] in ("ssm", "ecs")], [])
            self.assertFalse(any("apply" in c.argv for c in calls))
            self.assertEqual(lease_calls(calls, "delete-object"), [])

    def test_lease_released_after_token_deleted(self):
        with make_repo() as repo:
            stub_launch(repo, [("RUNNING", None), ("STOPPED", 0)])
            result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = repo.calls()
            self.assertEqual(sequence(calls, TOKEN_AND_LEASE),
                             [("ssm", "put-parameter"), ("ssm", "delete-parameter"), ("s3api", "delete-object")])
            [release] = lease_calls(calls, "delete-object")
            self.assertEqual(release.argv[release.argv.index("--if-match") + 1], '"e1"')
            # Taken before the prompt, refreshed after the apply, before the token is written.
            puts = lease_calls(calls, "put-object")
            self.assertEqual(len(puts), 3)
            takeover = puts[-1]
            self.assertEqual(takeover.argv[takeover.argv.index("--if-match") + 1], '"e1"')
            self.assertNotEqual(json.loads(takeover.body)["pid"], DEAD_PID)
            self.assertLess(calls.index(puts[-1]), calls.index(aws_calls(calls, "ssm", "put-parameter")[0]))
        with make_repo() as repo:
            stub_outputs(repo, applied=False)
            stub_lease(repo)
            repo.stub("terraform", "apply")
            repo.stub("aws", "ssm", "put-parameter", stdin=put_parameter_stdin(TOKEN))
            repo.stub("aws", "ecs", "run-task", exit=255)
            repo.stub("aws", "ssm", "delete-parameter", exit=254, stderr="An error occurred (ThrottlingException)\n")
            result = repo.run(SCRIPT, REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 1)
            self.assertIn("could not delete the token parameter", result.stderr)
            self.assertEqual(lease_calls(repo.calls(), "delete-object"), [])
            self.assertIn("fetch-model lease stays held while the token is in SSM", result.stderr)

    def test_watch_releases_lease_with_token(self):
        with make_repo() as repo:
            stub_reattach(repo, [("PENDING", None), ("RUNNING", None), ("STOPPED", 0)])
            result = repo.run(SCRIPT, "--watch", REPO_ID, env=tty_env(repo))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sequence(repo.calls(), TOKEN_AND_LEASE), [("ssm", "delete-parameter"), ("s3api", "delete-object")])
            self.assertEqual(lease_calls(repo.calls(), "put-object"), [])
        for other_status, released in ((None, True), ("PENDING", False)):
            with self.subTest(other_status=other_status), make_repo() as repo:
                stub_ended_copy(repo, other_status=other_status, status="complete")
                result = repo.run(SCRIPT, "--watch", REPO_ID, env=tty_env(repo))
                expected = [("ssm", "delete-parameter"), ("s3api", "delete-object")] if released else []
                self.assertEqual(sequence(repo.calls(), TOKEN_AND_LEASE), expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
