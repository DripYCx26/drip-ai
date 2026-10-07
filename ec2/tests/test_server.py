"""Pins `ec2/server.sh` against stub CLIs."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "testing"))

from cli_stubs import REAL_SLEEP, StubRepo  # noqa: E402

SCRIPT = "ec2/server.sh"
ACCOUNT = "111122223333"
REGION = "eu-west-1"
STATE_BUCKET = f"drip-ai-tfstate-{ACCOUNT}-{REGION}"
ENV = {"DRIP_AI_REGION": REGION}
INSTANCE = "i-0123456789abcdef0"
KEY = "sk-CanaryApiKey0123456789abcdefghijklmnopqrstuvwxyz"
TFVARS = 'model_source = "Org/Model"\ninstance_type = "g6e.12xlarge"\n'
BACKEND = json.dumps({"version": 3, "backend": {"type": "s3", "config": {"bucket": STATE_BUCKET}}})
INITIALISED = {f"{root}/.terraform/terraform.tfstate": BACKEND for root in ("ec2/terraform", "shared/terraform")}
# Stands in for `aws ssm start-session`, which runs until the session ends; every other aws call
# goes to the logging stub, by absolute path because the Codex auth preflight clears the environment.
# The session sleeps with the real sleep, because the tunnel test skips the tunnel's own sleeps.
AWS_WRAPPER = """#!/bin/bash
for argument in "$@"; do
  if [ "$argument" = start-session ]; then
    printf '%s\\n' "$*" > "$SESSION_LOG"
    exec {real_sleep} 300
  fi
done
exec "{stub_bin}/aws" "$@"
"""


def make_repo(**files):
    repo = StubRepo(files={**INITIALISED, **files})
    repo.stub("aws", "sts", "get-caller-identity", stdout=f"{ACCOUNT}\tarn:aws:iam::{ACCOUNT}:user/alice\n")
    repo.stub("terraform", "output", "-raw", "instance_id", stdout=INSTANCE)
    repo.install_stub("shared/lib/codex_link.py", "codex_link")
    repo.install_stub("shared/lib/json_link.py", "json_link")
    repo.stub("codex_link", "unlink", stdout="unlinked Codex: restored model\n")
    repo.stub("json_link", "unlink", stdout="not linked\n")
    return repo


def unlinks(repo):
    return [" ".join(c.argv) for c in repo.calls() if c.tool in ("codex_link", "json_link") and c.argv[0] == "unlink"]


ALL_UNLINKS = ["unlink --provider drip-ai", "unlink --client claude", "unlink --client opencode"]


def calls_of_json_link(repo):
    return [c.argv for c in repo.calls() if c.tool == "json_link" and c.argv[0] == "link"]


def chdir(repo):
    return f"-chdir={repo.root}/ec2/terraform"


def free_port():
    """A TCP port nothing listens on, so the tunnel's port-in-use check passes on any machine."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ServerTest(unittest.TestCase):
    def test_ec2_status_uses_tf_output(self):
        with make_repo() as repo:
            repo.stub("aws", "describe-instances", stdout=f"{INSTANCE}\tg6e.12xlarge\tstopped\n")
            result = repo.run(SCRIPT, "status", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"{INSTANCE}\tg6e.12xlarge\tstopped\n")
            calls = [(c.tool, c.argv) for c in repo.calls() if c.argv[0] != "sts"]
            self.assertEqual(calls, [
                ("terraform", [chdir(repo), "output", "-raw", "instance_id"]),
                ("aws", ["ec2", "describe-instances", "--region", REGION, "--instance-ids", INSTANCE,
                         "--query", "Reservations[0].Instances[0].[InstanceId,InstanceType,State.Name]",
                         "--output", "text"]),
            ])

    def test_ec2_commands_refuse_without_an_instance(self):
        for answer in ("", "not-an-id"):
            with self.subTest(answer=answer), StubRepo(files=INITIALISED) as repo:
                repo.stub("aws", "sts", "get-caller-identity",
                          stdout=f"{ACCOUNT}\tarn:aws:iam::{ACCOUNT}:user/alice\n")
                repo.stub("terraform", "output", "-raw", "instance_id", stdout=answer)
                for command in ("start", "stop", "status", "logs", "tunnel", "tunnel --no-link"):
                    result = repo.run(SCRIPT, *command.split(), env=ENV)
                    self.assertEqual(result.returncode, 1, command)
                    self.assertIn("no EC2 instance in the ec2 Terraform state; run bin/drip-ai ec2 apply",
                                  result.stderr)
                self.assertEqual([c for c in repo.calls() if c.tool == "aws" and c.argv[0] != "sts"], [])

    def test_ec2_start_stop_and_logs_target_the_instance(self):
        with make_repo() as repo:
            repo.stub("aws", "start-instances")
            repo.stub("aws", "wait", "instance-running")
            repo.stub("aws", "stop-instances")
            repo.stub("aws", "send-command", stdout="cmd-1\n")
            repo.stub("aws", "wait", "command-executed")
            repo.stub("aws", "get-command-invocation", stdout="Success\tlog lines\t\n")
            for command in ("start", "stop", "logs"):
                self.assertEqual(repo.run(SCRIPT, command, env=ENV).returncode, 0, command)
            # start asks nothing; it takes --yes like every command that spends, and calls the same APIs.
            self.assertEqual(repo.run(SCRIPT, "start", "--yes", env=ENV).returncode, 0)
            # Stop unlinks every client first: the links would point at a stopped server.
            self.assertEqual(unlinks(repo), ALL_UNLINKS)
            tools = [c.tool if c.tool != "aws" else c.argv[1] for c in repo.calls() if c.argv[0] != "sts"]
            self.assertLess(tools.index("json_link"), tools.index("stop-instances"))
            aws = [c.argv for c in repo.calls() if c.tool == "aws" and c.argv[0] != "sts"]
            self.assertEqual([argv[1] for argv in aws], ["start-instances", "wait", "stop-instances",
                                                          "send-command", "wait", "get-command-invocation",
                                                          "start-instances", "wait"])
            for argv in aws:
                self.assertEqual(argv[argv.index("--region") + 1], REGION)
                ids = argv[argv.index("--instance-ids") + 1] if "--instance-ids" in argv else \
                    argv[argv.index("--instance-id") + 1]
                self.assertEqual(ids, INSTANCE)

    def test_ec2_auto_approves_only_with_yes(self):
        with make_repo(**{"ec2/terraform/terraform.tfvars": TFVARS}) as repo:
            repo.stub("terraform", "apply")
            repo.stub("terraform", "destroy")
            self.assertEqual(repo.run(SCRIPT, "apply", env=ENV, input="yes\n").returncode, 0)
            self.assertEqual(repo.run(SCRIPT, "destroy", env=ENV, input="yes\n").returncode, 0)
            terraform = [c.argv for c in repo.calls() if c.tool == "terraform"]
            self.assertEqual(terraform, [[chdir(repo), "apply"], [chdir(repo), "destroy"]])
            self.assertEqual(unlinks(repo), ALL_UNLINKS)  # destroy only
            before = len(repo.calls())
            for args in (["apply", "-auto-approve"], ["destroy", "-auto-approve"], ["apply", "x"], ["stop", "--yes"],
                         [], ["bogus"], ["status", "extra"], ["tunnel", "--bogus"], ["stop", "--no-link"]):
                self.assertEqual(repo.run(SCRIPT, *args, env=ENV).returncode, 2, args)
            # Refused before any CLI call.
            self.assertEqual(len(repo.calls()), before)
            self.assertEqual(repo.run(SCRIPT, "apply", "--yes", env=ENV).returncode, 0)
            self.assertEqual(repo.run(SCRIPT, "destroy", "--yes", env=ENV).returncode, 0)
            terraform = [c.argv for c in repo.calls() if c.tool == "terraform"][2:]
            self.assertEqual(terraform, [[chdir(repo), "apply", "-auto-approve", "-input=false"],
                                         [chdir(repo), "destroy", "-auto-approve", "-input=false"]])

    def test_ec2_apply_refuses_without_model_source_in_tfvars(self):
        for files in ({}, {"ec2/terraform/terraform.tfvars": '# model_source = "x"\ninstance_type = "g6e.xlarge"\n'}):
            with self.subTest(files=bool(files)), make_repo(**files) as repo:
                result = repo.run(SCRIPT, "apply", env=ENV)
                self.assertEqual(result.returncode, 1)
                self.assertIn("ec2/terraform/terraform.tfvars does not set model_source; copy"
                              " ec2/terraform/terraform.tfvars.example", result.stderr)
                self.assertEqual(repo.calls(), [])

    def test_ec2_destroy_without_tfvars_passes_a_valid_model_source(self):
        with make_repo() as repo:
            repo.stub("terraform", "destroy")
            result = repo.run(SCRIPT, "destroy", env=ENV, input="yes\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            terraform = [c.argv for c in repo.calls() if c.tool == "terraform"]
            self.assertEqual(terraform, [[chdir(repo), "destroy", "-var", "model_source=unused-on-destroy"]])

    def test_ec2_declined_apply_exits_non_zero(self):
        with make_repo(**{"ec2/terraform/terraform.tfvars": TFVARS}) as repo:
            repo.stub("terraform", "apply", exit=1)
            result = repo.run(SCRIPT, "apply", env=ENV, input="no\n")
            self.assertEqual(result.returncode, 1)
            self.assertIn("ec2: apply failed or was declined", result.stderr)

    def test_ec2_tunnel_uses_tunnel_with_link(self):
        with make_repo() as repo:
            wrapper_dir = repo.home / "wrapper"
            wrapper_dir.mkdir()
            (wrapper_dir / "aws").write_text(AWS_WRAPPER.format(stub_bin=repo.bin_dir, real_sleep=REAL_SLEEP))
            repo.skip_sleeps()
            (wrapper_dir / "aws").chmod(0o755)
            session_log = repo.home / "session.log"
            repo.stub("codex_link", "link", stdout="linked Codex to drip-coder via drip-ai\n")
            repo.stub("json_link", "link", stdout="linked to drip-coder\n")
            repo.stub("terraform", "output", "-raw", "served_model_name", stdout="drip-coder")
            repo.stub("terraform", "output", "-raw", "max_model_len", stdout="32768")
            repo.stub("terraform", "output", "-raw", "api_key_parameter", stdout="/drip-ai/api-key")
            repo.stub("aws", "ssm", "get-parameter", stdout=KEY + "\n")
            repo.stub("curl", stdout="200")
            repo.stub("aws", "describe-instances", stdout="pending\n", times=1)
            repo.stub("aws", "describe-instances", stdout="running\n")
            port = str(free_port())
            env = {**ENV, "DRIP_AI_PORT": port, "DRIP_AI_TUNNEL_POLL_SECONDS": "1", "SESSION_LOG": str(session_log),
                   "PATH": f"{wrapper_dir}{os.pathsep}{repo.bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"}
            out_path, err_path = repo.home / "out", repo.home / "err"
            with open(out_path, "w") as out, open(err_path, "w") as err:
                proc = repo.popen(SCRIPT, "tunnel", env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL)
                deadline = time.monotonic() + 30
                while "opencode: linked (restart opencode)" not in out_path.read_text():
                    self.assertIsNone(proc.poll(), err_path.read_text())
                    self.assertLess(time.monotonic(), deadline, err_path.read_text())
                    time.sleep(0.1)
                proc.send_signal(signal.SIGINT)
                self.assertEqual(proc.wait(timeout=20), 130)
            self.assertEqual(session_log.read_text().split(), [
                "ssm", "start-session", "--region", REGION, "--target", INSTANCE,
                "--document-name", "AWS-StartPortForwardingSession",
                "--parameters", f"portNumber=8000,localPortNumber={port}",
            ])
            outputs = [(c.argv[0], c.argv[-1]) for c in repo.calls() if c.tool == "terraform"]
            self.assertIn((chdir(repo), "served_model_name"), outputs)
            self.assertIn((chdir(repo), "max_model_len"), outputs)
            # Readiness waited for the instance: one pending answer, one poll interval, then running.
            self.assertEqual(len([c for c in repo.calls() if "describe-instances" in c.argv]), 2)
            self.assertEqual([s for s in repo.sleeps() if s == "1"], ["1"])
            links = [(c.tool, c.argv[0]) for c in repo.calls() if c.tool in ("codex_link", "json_link")]
            self.assertEqual(links, [("codex_link", "link"), ("json_link", "link"), ("json_link", "link"),
                                     ("codex_link", "unlink"), ("json_link", "unlink"), ("json_link", "unlink")])
            link = calls_of_json_link(repo)[0]
            self.assertEqual(link[link.index("--context-window") + 1], "32768")
            self.assertNotIn(KEY, out_path.read_text() + err_path.read_text())


if __name__ == "__main__":
    unittest.main()
