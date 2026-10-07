"""Pins the Terraform, lease, and tunnel functions in `shared/lib/common.sh` against stub CLIs."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "testing"))

from cli_stubs import REAL_SLEEP, StubRepo  # noqa: E402

ACCOUNT = "111122223333"
ARN = f"arn:aws:iam::{ACCOUNT}:user/alice"
OTHER_ARN = "arn:aws:sts::111122223333:assumed-role/Dev/bob"
REGION = "eu-west-1"
BUCKET = f"drip-ai-tfstate-{ACCOUNT}-{REGION}"
HOST = os.uname().nodename
NOW = 1790000000
KEY = "sk-CanaryApiKey0123456789abcdefghijklmnopqrstuvwxyzAB"  # "sk-" and 48 letters and digits, like the real key
CURL_CONFIG = f'header = "Authorization: Bearer {KEY}"\n'
PRECONDITION_FAILED = (
    "An error occurred (PreconditionFailed) when calling the PutObject operation: "
    "At least one of the pre-conditions you specified did not hold\n"
)
LOCK_ERROR = """╷
│ Error: Error acquiring the state lock
│
│ Error message: operation error S3: PutObject, StatusCode: 412
│ Lock Info:
│   ID:        6f1c2d3e-0000-4000-8000-000000000001
│   Path:      drip-ai-tfstate-111122223333-eu-west-1/sagemaker.tfstate
│   Operation: OperationTypeApply
│   Who:       bob@laptop
│   Version:   1.11.0
│   Created:   2026-10-05 10:00:00 +0000 UTC
│   Info:
╵
"""
DRIVER = """set -euo pipefail
repo_root="$(cd "$(dirname "$0")" && pwd)"
source "$repo_root/shared/lib/common.sh"
endpoint_in_service() {
  [ "$(aws sagemaker describe-endpoint --query EndpointStatus --output text)" = InService ]
}
"$@"
"""
# Serves "forever" with the real sleep, because TunnelTest skips the tunnel's own sleeps.
PROXY = f'echo "$$" > "$PROXY_PID_FILE"\nexec {REAL_SLEEP} 300\n'
ENV = {"DRIP_AI_REGION": REGION, "DRIP_AI_NOW_EPOCH": str(NOW)}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backend(bucket=BUCKET):
    return json.dumps({"version": 3, "backend": {"type": "s3", "config": {"bucket": bucket}}})


def lease(owner=OTHER_ARN, host="build-box", expires=NOW + 300, pid=4242):
    return json.dumps({"owner_arn": owner, "host": host, "pid": pid,
                       "acquired_at": iso(NOW - 60), "expires_at": iso(expires)})


def make_repo(**files):
    repo = StubRepo(files={"drive.sh": DRIVER, "fake-proxy.sh": PROXY,
                           "sagemaker/terraform/.keep": "", "shared/terraform/.keep": "", **files})
    repo.stub("aws", "sts", "get-caller-identity", stdout=f"{ACCOUNT}\t{ARN}\n")
    return repo


def calls_of(repo, tool, *tokens):
    return [c for c in repo.calls() if c.tool == tool and all(t in c.argv for t in tokens)]


def initialised(*roots):
    return {f"{root}/.terraform/terraform.tfstate": backend() for root in roots}


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def owner_flag(call):
    return call.argv[call.argv.index("--expected-bucket-owner") + 1] if "--expected-bucket-owner" in call.argv else None


# Above the pid ceiling of Linux (4194304) and macOS (99999), so no process on the test host has it.
DEAD_PID = 2147483647
FORBIDDEN = "An error occurred (403) when calling the HeadBucket operation: Forbidden\n"


class TerraformRootTest(unittest.TestCase):
    def test_init_configures_backend_from_account_and_region(self):
        with make_repo() as repo:
            repo.stub("aws", "s3api", "head-bucket", BUCKET, "--region", REGION)
            repo.stub("terraform", "init")
            result = repo.run("drive.sh", "tf_init", "sagemaker", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            [init] = calls_of(repo, "terraform")
            self.assertEqual(init.argv, [
                f"-chdir={repo.root}/sagemaker/terraform", "init", "-input=false", "-lockfile=readonly",
                f"-backend-config=bucket={BUCKET}", f"-backend-config=region={REGION}",
            ])

    def test_init_uses_readonly_lockfile(self):
        with make_repo(**{"bootstrap/.keep": ""}) as repo:
            repo.stub("aws", "s3api", "head-bucket", BUCKET)
            repo.stub("terraform", "init")
            for root in ("sagemaker", "bootstrap"):
                result = repo.run("drive.sh", "tf_init", root, env=ENV)
                self.assertEqual(result.returncode, 0, result.stderr)
            inits = calls_of(repo, "terraform", "init")
            self.assertEqual([c.argv[0] for c in inits],
                             [f"-chdir={repo.root}/sagemaker/terraform", f"-chdir={repo.root}/bootstrap"])
            for init in inits:
                self.assertIn("-lockfile=readonly", init.argv)

    def test_head_bucket_passes_expected_owner(self):
        with make_repo() as repo:
            repo.stub("aws", "s3api", "head-bucket", BUCKET)
            repo.stub("terraform", "init")
            result = repo.run("drive.sh", "tf_init", "sagemaker", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            [head] = calls_of(repo, "aws", "head-bucket")
            self.assertEqual(owner_flag(head), ACCOUNT)

    def test_foreign_owned_bucket_refuses(self):
        with make_repo() as repo:
            repo.stub("aws", "s3api", "head-bucket", "--expected-bucket-owner", ACCOUNT, exit=254, stderr=FORBIDDEN)
            repo.stub("terraform", "init")
            result = repo.run("drive.sh", "tf_init", "sagemaker", env=ENV)
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"the state bucket name {BUCKET} is taken by another account", result.stderr)
            self.assertIn("set DRIP_AI_STATE_BUCKET_SUFFIX or contact your admin", result.stderr)
            self.assertEqual(calls_of(repo, "terraform"), [])

    def test_state_bucket_suffix_names_the_bucket(self):
        with make_repo() as repo:
            repo.stub("aws", "s3api", "head-bucket", f"{BUCKET}-team2")
            repo.stub("terraform", "init")
            suffixed = {**ENV, "DRIP_AI_STATE_BUCKET_SUFFIX": "team2"}
            named = repo.run("drive.sh", "state_bucket", env=suffixed)
            self.assertEqual((named.returncode, named.stdout.strip()), (0, f"{BUCKET}-team2"), named.stderr)
            result = repo.run("drive.sh", "tf_init", "sagemaker", env=suffixed)
            self.assertEqual(result.returncode, 0, result.stderr)
            [init] = calls_of(repo, "terraform", "init")
            self.assertIn(f"-backend-config=bucket={BUCKET}-team2", init.argv)
            [head] = calls_of(repo, "aws", "head-bucket")
            self.assertEqual(head.argv[head.argv.index("--bucket") + 1], f"{BUCKET}-team2")
            for bad in ("-x", "x-", "Team", "a_b", "abcdefghijklm"):
                result = repo.run("drive.sh", "tf_init", "sagemaker", env={**ENV, "DRIP_AI_STATE_BUCKET_SUFFIX": bad})
                self.assertEqual(result.returncode, 1, bad)
                self.assertIn("DRIP_AI_STATE_BUCKET_SUFFIX must be", result.stderr)
            self.assertEqual(len(calls_of(repo, "terraform")), 1)

    def test_state_bucket_suffix_reaches_bootstrap_as_a_var(self):
        suffixed = {**ENV, "DRIP_AI_STATE_BUCKET_SUFFIX": "team2"}
        with make_repo(**{"bootstrap/.terraform/.keep": ""}) as repo:
            repo.stub("terraform", "apply")
            repo.stub("terraform", "plan")
            repo.stub("terraform", "destroy")
            repo.stub("terraform", "import")
            repo.stub("terraform", "output", stdout="bucket")
            for command in (["apply"], ["plan", "-out=tf.plan"], ["destroy"], ["import", "aws_s3_bucket.state", f"{BUCKET}-team2"]):
                result = repo.run("drive.sh", "tf", "bootstrap", *command, env=suffixed)
                self.assertEqual(result.returncode, 0, (command, result.stderr))
            repo.run("drive.sh", "tf", "bootstrap", "apply", env=ENV)
            repo.run("drive.sh", "tf", "bootstrap", "output", "-raw", "state_bucket", env=suffixed)
            chdir = f"-chdir={repo.root}/bootstrap"
            var = ["-var", "state_bucket_suffix=team2"]
            self.assertEqual([c.argv for c in calls_of(repo, "terraform")], [
                [chdir, "apply", *var],
                [chdir, "plan", *var, "-out=tf.plan"],
                [chdir, "destroy", *var],
                [chdir, "import", *var, "aws_s3_bucket.state", f"{BUCKET}-team2"],
                [chdir, "apply"],
                [chdir, "output", "-raw", "state_bucket"],
            ])

    def test_bootstrap_refuses_a_malformed_suffix_before_terraform_runs(self):
        with make_repo(**{"bootstrap/.terraform/.keep": ""}) as repo:
            repo.stub("terraform", "apply")
            for bad in ("-x", "x-", "Team", "a_b", "abcdefghijklm"):
                result = repo.run("drive.sh", "tf", "bootstrap", "apply", env={**ENV, "DRIP_AI_STATE_BUCKET_SUFFIX": bad})
                self.assertEqual(result.returncode, 1, bad)
                self.assertIn("DRIP_AI_STATE_BUCKET_SUFFIX must be", result.stderr)
            self.assertEqual(calls_of(repo, "terraform"), [])

    def test_bootstrap_init_ignores_the_suffix(self):
        with make_repo(**{"bootstrap/.keep": ""}) as repo:
            repo.stub("terraform", "init")
            result = repo.run("drive.sh", "tf_init", "bootstrap", env={**ENV, "DRIP_AI_STATE_BUCKET_SUFFIX": "team2"})
            self.assertEqual(result.returncode, 0, result.stderr)
            [init] = calls_of(repo, "terraform", "init")
            self.assertEqual(init.argv, [f"-chdir={repo.root}/bootstrap", "init", "-input=false", "-lockfile=readonly"])

    def test_refuses_on_account_change(self):
        stale = {"sagemaker/terraform/.terraform/terraform.tfstate": backend("drip-ai-tfstate-999999999999-eu-west-1")}
        with make_repo(**stale) as repo:
            repo.stub("aws", "s3api", "head-bucket")
            repo.stub("terraform", "init", "-reconfigure")
            for command in (["tf", "sagemaker", "plan"], ["tf_init", "sagemaker"]):
                result = repo.run("drive.sh", *command, env=ENV)
                self.assertEqual(result.returncode, 1, command)
                self.assertIn("initialised for another AWS account/region", result.stderr)
                self.assertIn("run bin/drip-ai init --reconfigure", result.stderr)
            self.assertEqual(calls_of(repo, "terraform"), [])
            result = repo.run("drive.sh", "tf_init", "sagemaker", "--reconfigure", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            [init] = calls_of(repo, "terraform")
            self.assertIn(f"-backend-config=bucket={BUCKET}", init.argv)
            self.assertNotIn("-migrate-state", init.argv)

    def test_missing_state_bucket_points_to_setup(self):
        with make_repo() as repo:
            repo.stub("aws", "s3api", "head-bucket", exit=254, times=1,
                      stderr="An error occurred (404) when calling the HeadBucket operation: Not Found\n")
            repo.stub("aws", "s3api", "head-bucket", exit=254,
                      stderr="An error occurred (403) when calling the HeadBucket operation: Forbidden\n")
            missing = repo.run("drive.sh", "tf_init", "sagemaker", env=ENV)
            forbidden = repo.run("drive.sh", "tf_init", "sagemaker", env=ENV)
            self.assertEqual(missing.returncode, 1)
            self.assertIn("run: bin/drip-ai setup", missing.stderr)
            self.assertEqual(forbidden.returncode, 1)
            self.assertNotIn("bin/drip-ai setup", forbidden.stderr)
            self.assertIn("(403)", forbidden.stderr)
            self.assertEqual(calls_of(repo, "terraform"), [])

    def test_tf_translates_lock_error(self):
        with make_repo(**initialised("sagemaker/terraform")) as repo:
            repo.stub("terraform", "apply", stderr=LOCK_ERROR, exit=3)
            result = repo.run("drive.sh", "tf", "sagemaker", "apply", env=ENV)
            self.assertEqual(result.returncode, 3)
            self.assertIn("Error acquiring the state lock", result.stderr)
            self.assertIn("someone else is applying sagemaker right now (lock info: "
                          "ID=6f1c2d3e-0000-4000-8000-000000000001", result.stderr)
            self.assertIn("Who=bob@laptop", result.stderr)
            [apply] = calls_of(repo, "terraform", "apply")
            self.assertEqual(apply.env["TF_VAR_region"], REGION)


class LeaseTest(unittest.TestCase):
    def put_calls(self, repo):
        return calls_of(repo, "aws", "s3api", "put-object")

    def test_lease_acquire_creates_with_if_none_match(self):
        with make_repo() as repo:
            repo.stub("aws", "put-object", "--if-none-match", stdout='"e1"\n')
            result = repo.run("drive.sh", "lease_acquire", "fetch-model", "600", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            [put] = self.put_calls(repo)
            self.assertEqual(put.argv[put.argv.index("--if-none-match") + 1], "*")
            self.assertEqual(put.argv[put.argv.index("--bucket") + 1], BUCKET)
            self.assertEqual(put.argv[put.argv.index("--key") + 1], "leases/fetch-model.json")
            body = json.loads(put.body)
            self.assertEqual(set(body), {"owner_arn", "host", "pid", "acquired_at", "expires_at"})
            self.assertEqual((body["owner_arn"], body["host"]), (ARN, HOST))
            self.assertEqual((body["acquired_at"], body["expires_at"]), (iso(NOW), iso(NOW + 600)))

    def test_lease_conflict_refuses_naming_owner(self):
        with make_repo() as repo:
            repo.stub("aws", "put-object", "--if-none-match", stderr=PRECONDITION_FAILED, exit=254)
            repo.stub("aws", "get-object", "leases/fetch-model.json", stdout='"e1"\n', last_arg_file=lease())
            result = repo.run("drive.sh", "lease_acquire", "fetch-model", "600", env=ENV)
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"in use by {OTHER_ARN} on build-box (pid 4242) until {iso(NOW + 300)}", result.stderr)
            self.assertEqual(len(self.put_calls(repo)), 1)

    def test_lease_takes_over_expired_with_if_match(self):
        with make_repo() as repo:
            repo.stub("aws", "put-object", "--if-none-match", stderr=PRECONDITION_FAILED, exit=254)
            repo.stub("aws", "get-object", stdout='"e1"\n', last_arg_file=lease(expires=NOW - 1))
            repo.stub("aws", "put-object", "--if-match", stdout='"e2"\n')
            result = repo.run("drive.sh", "lease_acquire", "fetch-model", "600", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            takeover = self.put_calls(repo)[1]
            self.assertEqual(takeover.argv[takeover.argv.index("--if-match") + 1], '"e1"')
            self.assertNotIn("--if-none-match", takeover.argv)
            self.assertEqual(json.loads(takeover.body)["owner_arn"], ARN)

    def test_lease_release_only_when_owned(self):
        cases = {
            "owned": (lease(owner=ARN, host=HOST), [], [["--if-match", '"e1"']]),
            "other": (lease(), [], []),
            "old cli": (lease(owner=ARN, host=HOST), ["Unknown options: --if-match"], [["--if-match"], []]),
        }
        for label, (current, unsupported, expected_deletes) in cases.items():
            with self.subTest(label), make_repo() as repo:
                repo.stub("aws", "get-object", stdout='"e1"\n', last_arg_file=current)
                for message in unsupported:
                    repo.stub("aws", "delete-object", "--if-match", stderr=message, exit=252)
                repo.stub("aws", "delete-object")
                result = repo.run("drive.sh", "lease_release", "fetch-model", env=ENV)
                self.assertEqual(result.returncode, 0, result.stderr)
                deletes = calls_of(repo, "aws", "delete-object")
                self.assertEqual(len(deletes), len(expected_deletes))
                for call, tokens in zip(deletes, expected_deletes):
                    if tokens:
                        self.assertEqual(call.argv[call.argv.index("--if-match") + 1], '"e1"')
                    else:
                        self.assertNotIn("--if-match", call.argv)
                if label == "other":
                    self.assertIn("no longer yours", result.stderr)

    def test_lease_calls_pass_expected_owner(self):
        with make_repo() as repo:
            repo.stub("aws", "put-object", "--if-none-match", stderr=PRECONDITION_FAILED, exit=254)
            repo.stub("aws", "get-object", stdout='"e1"\n', last_arg_file=lease(expires=NOW - 1), times=1)
            repo.stub("aws", "put-object", "--if-match", stdout='"e2"\n')
            repo.stub("aws", "delete-object", "--if-match", stderr="Unknown options: --if-match", exit=252)
            repo.stub("aws", "delete-object")
            self.assertEqual(repo.run("drive.sh", "lease_acquire", "fetch-model", "600", env=ENV).returncode, 0)
            repo.stub("aws", "get-object", stdout='"e2"\n', last_arg_file=lease(owner=ARN, host=HOST))
            result = repo.run("drive.sh", "lease_release", "fetch-model", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = calls_of(repo, "aws", "s3api")
            self.assertEqual([c.argv[1] for c in calls],
                             ["put-object", "get-object", "put-object", "get-object", "delete-object", "delete-object"])
            self.assertEqual([owner_flag(c) for c in calls], [ACCOUNT] * 6)

    def acquire_over_own_lease(self, repo, lease_pid, *pid_arg, env_extra=None):
        repo.stub("aws", "put-object", "--if-none-match", stderr=PRECONDITION_FAILED, exit=254)
        repo.stub("aws", "get-object", stdout='"e1"\n', last_arg_file=lease(owner=ARN, host=HOST, pid=lease_pid))
        repo.stub("aws", "put-object", "--if-match", stdout='"e2"\n')
        return repo.run("drive.sh", "lease_acquire", "fetch-model", "600", *pid_arg, env={**ENV, **(env_extra or {})})

    def test_lease_same_user_other_live_pid_refuses(self):
        live_pid = os.getpid()
        with make_repo() as repo:
            result = self.acquire_over_own_lease(repo, live_pid)
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"another drip-ai process on this machine (pid {live_pid})", result.stderr)
            self.assertIn("wait or stop it", result.stderr)
            self.assertEqual(len(self.put_calls(repo)), 1)

    @unittest.skipIf(os.geteuid() == 0, "root may signal every process, so pid 1 is not another user's here")
    def test_lease_held_by_another_local_users_process_refuses(self):
        # pid 1 is always alive and owned by root, so kill -0 says "not permitted"; a non-English
        # locale must not make that read as dead.
        with make_repo() as repo:
            result = self.acquire_over_own_lease(repo, 1, env_extra={"LANG": "fr_FR.UTF-8", "LC_ALL": "fr_FR.UTF-8"})
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("another drip-ai process on this machine (pid 1)", result.stderr)

    def test_lease_same_user_dead_pid_takes_over(self):
        with make_repo() as repo:
            result = self.acquire_over_own_lease(repo, DEAD_PID, "4243")
            self.assertEqual(result.returncode, 0, result.stderr)
            takeover = self.put_calls(repo)[1]
            self.assertEqual(takeover.argv[takeover.argv.index("--if-match") + 1], '"e1"')
            self.assertEqual(json.loads(takeover.body)["pid"], 4243)

    def test_lease_same_process_refreshes(self):
        live_pid = os.getpid()
        with make_repo() as repo:
            result = self.acquire_over_own_lease(repo, live_pid, str(live_pid))
            self.assertEqual(result.returncode, 0, result.stderr)
            refresh = self.put_calls(repo)[1]
            self.assertIn("--if-match", refresh.argv)
            self.assertEqual(json.loads(refresh.body)["pid"], live_pid)
            for bad in ("0", "abc", "-1"):
                bad_pid = repo.run("drive.sh", "lease_acquire", "fetch-model", "600", bad, env=ENV)
                self.assertEqual(bad_pid.returncode, 1, bad)
                self.assertIn("lease pid must be a process id", bad_pid.stderr)


class TunnelTest(unittest.TestCase):
    def script_tunnel(self, repo, preflight_ok=True, key=KEY):
        """Every client links; a test that wants another answer stubs it first, because rules match
        in the order added."""
        repo.skip_sleeps()
        repo.install_stub("shared/lib/codex_link.py", "codex_link")
        repo.stub("codex_link", "link", stdout="linked Codex to big-model via drip-ai\n")
        repo.stub("codex_link", "unlink", stdout="unlinked Codex: restored model\n")
        repo.install_stub("shared/lib/json_link.py", "json_link")
        repo.stub("json_link", "link", "claude", stdout="linked Claude Code to big-model\n")
        repo.stub("json_link", "link", "opencode", stdin=KEY + "\n", stdout="linked opencode to big-model\n")
        repo.stub("json_link", "unlink", stdout="unlinked: restored 2 keys\n")
        repo.stub("terraform", "output", "-raw", "api_key_parameter", stdout="/drip-ai/api-key")
        repo.stub("aws", "ssm", "get-parameter", stdout=key + "\n", times=None if preflight_ok else 1)
        repo.stub("aws", "ssm", "get-parameter", stderr="Unable to locate credentials\n", exit=255)
        repo.stub("curl", stdin=CURL_CONFIG, stdout="000", exit=7, times=1)
        repo.stub("curl", stdin=CURL_CONFIG, stdout="200")
        repo.stub("aws", "describe-endpoint", stdout="Creating\n", times=1)
        repo.stub("aws", "describe-endpoint", stdout="InService\n")

    def setUp(self):
        self.port = str(free_port())
        self.env = {**ENV, "DRIP_AI_PORT": self.port}

    def run_tunnel(self, repo, wait_for, extra_env=None):
        """Starts the tunnel, waits for `wait_for` in its output, sends SIGINT, and returns
        (returncode, stdout, stderr, proxy_pid, shell_pid). The shell starts with SIGINT at its
        default and in its own session, because a runner that inherited SIGINT ignored would pass
        that on and bash cannot trap a signal ignored at entry; whatever is left is killed."""
        out_path, err_path, pid_path = repo.home / "out", repo.home / "err", repo.home / "proxy.pid"
        env = {**self.env, "DRIP_AI_TUNNEL_POLL_SECONDS": "1", "PROXY_PID_FILE": str(pid_path), **(extra_env or {})}
        with open(out_path, "w") as out, open(err_path, "w") as err:
            proc = repo.popen("drive.sh", "tunnel_with_link", "big-model", "131072", "endpoint_in_service",
                              "bash", str(repo.root / "fake-proxy.sh"), env=env, stdout=out, stderr=err,
                              stdin=subprocess.DEVNULL, start_new_session=True,
                              preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
            try:
                deadline = time.monotonic() + 30
                # The proxy may start after the awaited line; the test needs it running before SIGINT.
                while wait_for not in out_path.read_text() + err_path.read_text() or not (
                        pid_path.exists() and pid_path.read_text().strip()):
                    self.assertIsNone(proc.poll(), err_path.read_text())
                    self.assertLess(time.monotonic(), deadline, "tunnel never reached: " + wait_for)
                    time.sleep(0.1)
                proc.send_signal(signal.SIGINT)
                returncode = proc.wait(timeout=20)
            except BaseException:
                # Only on failure: a passing run must show that the tunnel stopped the proxy itself.
                # The proxy has its own process group (tunnel_with_link's job control).
                proxy = pid_path.read_text().strip() if pid_path.exists() else ""
                groups = [proc.pid] + ([int(proxy)] if proxy else [])
                for group in groups:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                proc.wait(timeout=5)
                raise
        return returncode, out_path.read_text(), err_path.read_text(), int(pid_path.read_text()), proc.pid

    def assert_stopped(self, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail(f"proxy {pid} still running")

    def test_tunnel_links_every_present_client_and_unlinks_them_on_sigint(self):
        with make_repo(**initialised("sagemaker/terraform", "shared/terraform")) as repo:
            self.script_tunnel(repo)
            returncode, out, err, proxy_pid, shell_pid = self.run_tunnel(repo, "opencode: linked")
            self.assertEqual(returncode, 130, err)
            self.assert_stopped(proxy_pid)
            self.assertIn("Context window: 131072 tokens (max_model_len)", out)
            self.assertIn("Codex: linked (restart Codex)\nClaude Code: linked (new session)\n"
                          "opencode: linked (restart opencode)\n", out)
            links = [(c.tool, c.argv) for c in repo.calls() if c.tool in ("codex_link", "json_link")]
            aws_path = str(Path(os.path.realpath(repo.bin_dir)) / "aws")
            auth = ["ssm", "get-parameter", "--region", REGION, "--name", "/drip-ai/api-key",
                    "--with-decryption", "--query", "Parameter.Value", "--output", "text"]
            auth_args = [f"--auth-arg={a}" for a in auth]
            owner = ["--owner-pid", str(shell_pid)]
            self.assertEqual(links, [
                ("codex_link", ["link", "--provider", "drip-ai", "--model", "big-model", "--base-url",
                                f"http://127.0.0.1:{self.port}/v1", "--context-window", "131072",
                                "--auth-command", aws_path, *auth_args, *owner]),
                ("json_link", ["link", "--client", "claude", "--model", "big-model", "--port", self.port,
                               "--context-window", "131072", "--auth-command", aws_path, *auth_args, *owner]),
                ("json_link", ["link", "--client", "opencode", "--model", "big-model", "--port", self.port,
                               "--context-window", "131072", *owner]),
                ("codex_link", ["unlink", "--provider", "drip-ai"]),
                ("json_link", ["unlink", "--client", "claude"]),
                ("json_link", ["unlink", "--client", "opencode"]),
            ])
            # The opencode key went in on stdin.
            self.assertEqual([c.stdin for c in calls_of(repo, "json_link", "opencode", "link")], [KEY + "\n"])
            self.assertEqual(len(calls_of(repo, "aws", "describe-endpoint")), 2)
            # One poll interval after the proxy did not answer, one after the endpoint was Creating.
            self.assertEqual([s for s in repo.sleeps() if s == "1"], ["1", "1"])
            preflight = [c.env for c in calls_of(repo, "aws", "get-parameter") if c.env["PATH"] == "/usr/bin:/bin"]
            self.assertEqual(len(preflight), 1)
            self.assertEqual(preflight[0]["HOME"], str(repo.home))
            self.assertNotIn("DRIP_AI_REGION", preflight[0])

    def test_tunnel_skips_a_missing_client_and_unlinks_only_what_it_linked(self):
        with make_repo(**initialised("sagemaker/terraform", "shared/terraform")) as repo:
            repo.install_stub("shared/lib/json_link.py", "json_link")
            repo.stub("json_link", "link", "claude", stdout="Claude Code config not found; skipping\n")
            self.script_tunnel(repo)
            returncode, out, err, _, _ = self.run_tunnel(repo, "opencode: linked")
            self.assertEqual(returncode, 130, err)
            self.assertNotIn("Claude Code", out + err)
            unlinks = [c.argv for c in repo.calls() if c.tool in ("codex_link", "json_link") and c.argv[0] == "unlink"]
            self.assertEqual(unlinks, [["unlink", "--provider", "drip-ai"], ["unlink", "--client", "opencode"]])

    def test_tunnel_link_failure_leaves_the_other_clients_linked(self):
        with make_repo(**initialised("sagemaker/terraform", "shared/terraform")) as repo:
            repo.install_stub("shared/lib/json_link.py", "json_link")
            repo.stub("json_link", "link", "claude", stderr="json-link: Claude Code is already linked by pid 7\n",
                      exit=1)
            self.script_tunnel(repo)
            returncode, out, err, _, _ = self.run_tunnel(repo, "opencode: linked")
            self.assertEqual(returncode, 130, err)
            self.assertIn("Codex: linked (restart Codex)\nClaude Code: not linked\nopencode: linked", out)
            self.assertIn("already linked by pid 7", err)
            unlinks = [c.argv[-1] for c in repo.calls() if c.tool in ("codex_link", "json_link") and c.argv[0] == "unlink"]
            self.assertEqual(unlinks, ["drip-ai", "opencode"])

    def test_tunnel_links_only_the_clients_named(self):
        cases = {
            "DRIP_AI_LINK_CLIENTS=claude": ({"DRIP_AI_LINK_CLIENTS": "claude"}, ["claude"]),
            "DRIP_AI_LINK_CLIENTS=opencode,codex": ({"DRIP_AI_LINK_CLIENTS": "opencode,codex"}, ["opencode", "codex"]),
            "DRIP_AI_CODEX_LINK=0": ({"DRIP_AI_CODEX_LINK": "0"}, ["claude", "opencode"]),
        }
        for label, (extra_env, expected) in cases.items():
            with self.subTest(label), make_repo(**initialised("sagemaker/terraform", "shared/terraform")) as repo:
                self.script_tunnel(repo)
                last = {"codex": "Codex", "claude": "Claude Code", "opencode": "opencode"}[expected[-1]]
                returncode, out, err, _, _ = self.run_tunnel(repo, f"{last}: linked", extra_env=extra_env)
                self.assertEqual(returncode, 130, err)
                linked = [("codex" if c.tool == "codex_link" else c.argv[2])
                          for c in repo.calls() if c.tool in ("codex_link", "json_link") and c.argv[0] == "link"]
                self.assertEqual(linked, expected)

    def test_tunnel_links_nothing_when_disabled(self):
        for extra_env in ({"DRIP_AI_LINK_CLIENTS": "none"}, {"DRIP_AI_LINK_CLIENTS": "codex", "DRIP_AI_CODEX_LINK": "0"},
                          {"DRIP_AI_LINK_CLIENTS": "codex,cursor"}):
            with self.subTest(extra_env), make_repo() as repo:
                repo.install_stub("shared/lib/codex_link.py", "codex_link")
                repo.install_stub("shared/lib/json_link.py", "json_link")
                result = repo.run("drive.sh", "tunnel_with_link", "big-model", "131072", "endpoint_in_service",
                                  "bash", "-c", "exit 3", env={**self.env, **extra_env})
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("No client links", result.stdout)
                self.assertEqual(repo.calls(), [])
                self.assertEqual("names 'cursor'" in result.stderr, "cursor" in extra_env["DRIP_AI_LINK_CLIENTS"])

    def test_tunnel_skips_codex_and_claude_when_auth_preflight_fails(self):
        with make_repo(**initialised("sagemaker/terraform", "shared/terraform")) as repo:
            self.script_tunnel(repo, preflight_ok=False)
            returncode, out, err, proxy_pid, _ = self.run_tunnel(repo, "opencode: linked")
            self.assertEqual(returncode, 130)
            self.assertIn("Codex desktop app and Claude Code may not see your credentials", err)
            self.assertIn("AWS_PROFILE", err)
            self.assertIn("Codex: not linked\nClaude Code: not linked\nopencode: linked", out)
            self.assertNotIn(KEY, out + err)
            self.assertEqual([c for c in repo.calls() if c.tool == "codex_link"], [])
            self.assertEqual([c.argv[2] for c in calls_of(repo, "json_link", "link")], ["opencode"])
            failed = calls_of(repo, "aws", "get-parameter")[-1]
            self.assertEqual((failed.env["PATH"], failed.env["HOME"]), ("/usr/bin:/bin", str(repo.home)))
            self.assert_stopped(proxy_pid)

    def test_tunnel_skips_link_for_a_key_that_would_break_the_curl_config(self):
        for bad in ('sk-"quoted', "sk-back\\slash", "sk-with space", "sk-two\nlines", "sk-bell\x07"):
            with self.subTest(bad=bad), make_repo(**initialised("sagemaker/terraform", "shared/terraform")) as repo:
                self.script_tunnel(repo, key=bad)
                returncode, _, err, proxy_pid, _ = self.run_tunnel(repo, "no client linked")
                self.assertEqual(returncode, 130, err)
                self.assertIn("API key has an unexpected form", err)
                self.assertEqual([c for c in repo.calls() if c.tool in ("codex_link", "json_link", "curl")], [])
                self.assert_stopped(proxy_pid)

    def test_tunnel_refuses_when_port_in_use(self):
        with make_repo(**initialised("sagemaker/terraform", "shared/terraform")) as repo, socket.socket() as squatter:
            self.script_tunnel(repo)
            squatter.bind(("127.0.0.1", 0))
            squatter.listen()
            port = str(squatter.getsockname()[1])
            started = repo.home / "proxy-started"
            result = repo.run("drive.sh", "tunnel_with_link", "big-model", "131072", "endpoint_in_service",
                              "bash", "-c", f"touch '{started}'", env={**ENV, "DRIP_AI_PORT": port}, timeout=20)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"port {port} is already in use; set DRIP_AI_PORT", result.stderr)
            self.assertFalse(started.exists())
            self.assertEqual([c for c in repo.calls() if c.tool in ("curl", "codex_link", "json_link")], [])
            self.assertEqual(calls_of(repo, "aws", "ssm"), [])

    def test_api_key_never_in_argv(self):
        with make_repo(**initialised("sagemaker/terraform", "shared/terraform")) as repo:
            self.script_tunnel(repo)
            _, out, err, _, _ = self.run_tunnel(repo, "opencode: linked")
            calls = repo.calls()
            self.assertTrue(any(c.tool == "codex_link" for c in calls))
            self.assertTrue(any(c.tool == "json_link" for c in calls))
            for call in calls:
                self.assertFalse(any(KEY in arg for arg in call.argv), call)
                self.assertFalse(any(KEY in value for value in call.env.values()), call.tool)
            self.assertEqual({c.stdin for c in calls if c.tool == "curl"}, {CURL_CONFIG})
            self.assertNotIn(KEY, out + err)


if __name__ == "__main__":
    unittest.main()
