"""Pins `bin/drip-ai` routing and setup, and that `shared/check.sh` stops at its first failing step, against
stub CLIs."""

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "testing"))

from cli_stubs import StubRepo  # noqa: E402

ACCOUNT = "111122223333"
REGION = "eu-west-1"
BUCKET = f"drip-ai-tfstate-{ACCOUNT}-{REGION}"
ENV = {"DRIP_AI_REGION": REGION}
STATE_ROOTS = {
    "shared": "shared/terraform",
    "fetch-model": "shared/fetch-model/terraform",
    "sagemaker": "sagemaker/terraform",
    "ec2": "ec2/terraform",
}
ROUTED_SCRIPTS = ("sagemaker/start.sh", "sagemaker/endpoint.sh", "ec2/server.sh", "shared/models.sh",
                  "shared/clients.sh", "shared/fetch-model/fetch-model.sh", "shared/check.sh")


def recorder(name):
    return f'printf "{name}|%s\\n" "$*" >> "$ROUTE_LOG"\n'


# Records "<script> <args>" in $ROUTE_LOG and exits $FAKE_EXIT_<start|endpoint> (default 0). The
# endpoint fake stands in for the tunnel: it prints READY and holds until INT, as the tunnel does.
UP_FAKE = """name="$(basename "$0" .sh)"
echo "$name $*" >> "$ROUTE_LOG"
var="FAKE_EXIT_$name"
[ "${!var:-0}" = 0 ] || exit "${!var}"
if [ "$name" = endpoint ]; then
  trap 'echo "tunnel closed"; exit 130' INT
  echo READY
  while :; do sleep 0.1; done
fi
"""


class DispatchTest(unittest.TestCase):
    def test_routes_each_command(self):
        routes = [
            (["check"], "shared/check.sh|"),
            (["sagemaker", "start", "Org/Model"], "sagemaker/start.sh|Org/Model"),
            (["sagemaker", "watch"], "sagemaker/start.sh|--watch"),
            (["sagemaker", "list"], "sagemaker/start.sh|--list"),
            (["sagemaker", "deploy", "m"], "sagemaker/endpoint.sh|deploy m"),
            (["sagemaker", "start", "m", "--yes"], "sagemaker/start.sh|m --yes"),
            (["sagemaker", "stop", "--yes"], "sagemaker/endpoint.sh|stop --yes"),
            (["ec2", "apply", "--yes"], "ec2/server.sh|apply --yes"),
            (["fetch", "Org/Model", "--yes"], "shared/fetch-model/fetch-model.sh|Org/Model --yes"),
            *[(["sagemaker", c], f"sagemaker/endpoint.sh|{c}") for c in ("stop", "status", "logs", "tunnel")],
            *[(["ec2", c], f"ec2/server.sh|{c}")
              for c in ("apply", "destroy", "start", "stop", "status", "logs", "tunnel")],
            (["fetch", "Org/Model", "--revision", "abc"], "shared/fetch-model/fetch-model.sh|Org/Model --revision abc"),
            (["models", "upload", "m"], "shared/models.sh|upload m"),
            (["models", "list"], "shared/models.sh|list"),
            (["key"], "shared/clients.sh|key"),
            (["smoke"], "shared/clients.sh|smoke"),
            *[(["codex", c], f"shared/clients.sh|codex {c}") for c in ("link", "unlink", "status")],
            *[([c, "claude"], f"shared/clients.sh|{c} claude") for c in ("link", "unlink", "status")],
            (["status"], "shared/clients.sh|status"),
        ]
        files = {script: recorder(script) for script in ROUTED_SCRIPTS}
        with StubRepo(files=files) as repo:
            log = repo.home / "routes.log"
            for args, expected in routes:
                with self.subTest(args):
                    log.write_text("")
                    result = repo.run("bin/drip-ai", *args, env={"ROUTE_LOG": str(log)})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(log.read_text(), expected + "\n")
            self.assertEqual(repo.calls(), [])
            for args in ([], ["bogus"], ["sagemaker", "bogus"], ["ec2", "bogus"], ["models", "bogus"],
                         ["codex", "bogus"], ["init", "--force"], ["setup", "--force"], ["up", "--force"],
                         ["up", "a", "b"]):
                self.assertEqual(repo.run("bin/drip-ai", *args).returncode, 2, args)
            (repo.root / "ec2" / "server.sh").unlink()
            missing = repo.run("bin/drip-ai", "ec2", "status")
            self.assertEqual(missing.returncode, 1)
            self.assertIn("ec2/server.sh not found", missing.stderr)
            self.assertEqual(repo.run("bin/drip-ai", "help").returncode, 0)

    def script_setup(self, repo, bucket_missing, apply_exit=0):
        repo.stub("aws", "--version", stdout="aws-cli/2.37.9 Python/3.12.6 Linux/6.1 exe/x86_64\n")
        repo.stub("terraform", "version", stdout="Terraform v1.16.5\non linux_amd64\n")
        repo.stub("aws", "sts", "get-caller-identity", stdout=f"{ACCOUNT}\tarn:aws:iam::{ACCOUNT}:user/alice\n")
        if bucket_missing:
            repo.stub("aws", "head-bucket", BUCKET, exit=254, times=1,
                      stderr="An error occurred (404) when calling the HeadBucket operation: Not Found\n")
        repo.stub("aws", "head-bucket", BUCKET)
        repo.stub("terraform", "init")
        repo.stub("terraform", "apply", exit=apply_exit)

    def setup_files(self):
        placeholders = {f"{d}/.keep": "" for d in ["bootstrap", *STATE_ROOTS.values()]}
        # The stub init records no backend, so seed what a real init leaves; `tf` then skips a second init.
        placeholders["shared/terraform/.terraform/terraform.tfstate"] = json.dumps(
            {"version": 3, "backend": {"type": "s3", "config": {"bucket": BUCKET}}})
        return placeholders

    def test_setup_applies_the_shared_stack_after_every_init_and_auto_approves_only_with_yes(self):
        for bucket_missing in (True, False):
            for yes in ([], ["--yes"]):
                with self.subTest(bucket_missing=bucket_missing, yes=yes), \
                        StubRepo(files=self.setup_files()) as repo:
                    self.check_setup(repo, bucket_missing, yes)

    def check_setup(self, repo, bucket_missing, yes):
        self.script_setup(repo, bucket_missing)
        result = repo.run("bin/drip-ai", "setup", *yes, env=ENV, input=None if yes else "yes\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        terraform = [c.argv for c in repo.calls() if c.tool == "terraform" and c.argv[0] != "version"]
        # -auto-approve appears only when the user passed --yes.
        approve = ["-auto-approve", "-input=false"] if yes else []
        self.assertEqual(bool([a for argv in terraform for a in argv if "auto-approve" in a]), bool(yes))
        bootstrap_apply = [f"-chdir={repo.root}/bootstrap", "apply", *approve]
        shared_apply = [f"-chdir={repo.root}/shared/terraform", "apply", *approve]
        applies = [argv for argv in terraform if "apply" in argv]
        self.assertEqual(applies, [bootstrap_apply, shared_apply] if bucket_missing else [shared_apply])
        inits = [argv[0] for argv in terraform if "init" in argv and f"-backend-config=bucket={BUCKET}" in argv]
        self.assertEqual(inits, [f"-chdir={repo.root}/{d}" for d in STATE_ROOTS.values()])
        last_init = max(i for i, argv in enumerate(terraform) if "init" in argv)
        self.assertEqual(terraform[last_init + 1:], [shared_apply])
        self.assertIn("Next: bin/drip-ai fetch <Org/Name>", result.stdout)

    def test_setup_fails_without_a_next_step_when_the_shared_apply_is_declined(self):
        with StubRepo(files=self.setup_files()) as repo:
            self.script_setup(repo, bucket_missing=False, apply_exit=1)
            result = repo.run("bin/drip-ai", "setup", env=ENV, input="no\n")
            self.assertEqual(result.returncode, 1)
            self.assertIn("drip-ai: the shared apply failed or was declined", result.stderr)
            self.assertNotIn("Next:", result.stdout)

    def tf_repo(self):
        repo = StubRepo(files={f"{d}/.keep": "" for d in ["bootstrap", *STATE_ROOTS.values()]})
        self.script_setup(repo, bucket_missing=False)
        for verb in ("plan", "show"):
            repo.stub("terraform", verb)
        return repo

    @staticmethod
    def terraform_calls(repo):
        return [c.argv for c in repo.calls() if c.tool == "terraform" and c.argv[0] != "version"]

    def test_tf_passes_arguments_to_the_named_stack(self):
        with self.tf_repo() as repo:
            result = repo.run("bin/drip-ai", "tf", "sagemaker", "plan", "-var", "x=1", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            chdir = f"-chdir={repo.root}/sagemaker/terraform"
            calls = self.terraform_calls(repo)
            self.assertEqual(calls[-1], [chdir, "plan", "-var", "x=1"])
            init = calls[0]
            self.assertEqual(init[:2], [chdir, "init"])
            self.assertIn(f"-backend-config=bucket={BUCKET}", init)
            self.assertEqual(len(calls), 2)
        with self.tf_repo() as repo:
            result = repo.run("bin/drip-ai", "tf", "bootstrap", "plan", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = self.terraform_calls(repo)
            self.assertEqual(calls[-1], [f"-chdir={repo.root}/bootstrap", "plan"])
            self.assertFalse([a for argv in calls for a in argv if a.startswith("-backend-config")])

    def test_tf_rejects_unknown_stack_and_missing_args(self):
        with self.tf_repo() as repo:
            bogus = repo.run("bin/drip-ai", "tf", "bogus", "plan", env=ENV)
            self.assertNotEqual(bogus.returncode, 0)
            self.assertIn("unknown Terraform root 'bogus'", bogus.stderr)
            for args in (["tf"], ["tf", "shared"]):
                self.assertEqual(repo.run("bin/drip-ai", *args, env=ENV).returncode, 2, args)
            self.assertEqual(self.terraform_calls(repo), [])

    def test_tf_never_adds_auto_approve(self):
        with self.tf_repo() as repo:
            result = repo.run("bin/drip-ai", "tf", "shared", "apply", env=ENV, input="yes\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            applies = [argv for argv in self.terraform_calls(repo) if "apply" in argv]
            self.assertEqual(applies, [[f"-chdir={repo.root}/shared/terraform", "apply"]])
            self.assertFalse([a for argv in self.terraform_calls(repo) for a in argv if "auto-approve" in a])


class UpTest(unittest.TestCase):
    FILES = {"sagemaker/start.sh": UP_FAKE, "sagemaker/endpoint.sh": UP_FAKE}

    def test_up_starts_then_holds_the_tunnel_with_a_smoke_until_ctrl_c(self):
        with StubRepo(files=self.FILES) as repo:
            log = repo.home / "routes.log"
            proc = repo.popen("bin/drip-ai", "up", "Org/Model", "--yes", env={"ROUTE_LOG": str(log)},
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True)
            deadline = time.monotonic() + 30
            while "endpoint" not in (log.read_text() if log.exists() else ""):
                self.assertIsNone(proc.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.1)
            time.sleep(0.5)
            os.killpg(proc.pid, signal.SIGINT)  # a terminal's Ctrl-C reaches the whole group
            stdout, stderr = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 130, stderr)
            self.assertEqual(log.read_text().splitlines(), ["start Org/Model --yes --no-watch", "endpoint tunnel --smoke"])
            self.assertTrue(stdout.endswith("tunnel closed\nThe endpoint still bills until bin/drip-ai sagemaker stop\n"),
                            stdout)
            self.assertEqual(repo.calls(), [])

    def test_up_stops_when_start_fails(self):
        with StubRepo(files=self.FILES) as repo:
            log = repo.home / "routes.log"
            result = repo.run("bin/drip-ai", "up", env={"ROUTE_LOG": str(log), "FAKE_EXIT_start": "1"})
            self.assertEqual(result.returncode, 1)
            self.assertEqual(log.read_text().splitlines(), ["start --no-watch"])
            self.assertNotIn("still bills", result.stdout)


class CheckScriptTest(unittest.TestCase):
    ROOTS = ["bootstrap", *STATE_ROOTS.values()]

    def make_repo(self):
        return StubRepo(files={f"{d}/.keep": "" for d in [*self.ROOTS, "shared/modules/conventions"]})

    def test_check_script_stops_at_first_failure(self):
        with self.make_repo() as repo:
            repo.stub("terraform", "fmt", exit=3)
            result = repo.run("shared/check.sh")
            self.assertEqual(result.returncode, 1)
            self.assertIn("check: FAILED at fmt shared/modules/conventions", result.stderr)
            self.assertEqual(len(repo.calls()), 1)


if __name__ == "__main__":
    unittest.main()
