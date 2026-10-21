"""Pins `sagemaker/endpoint.sh` (stop, tunnel, deploy) against stub CLIs."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "testing"))

from cli_stubs import REAL_SLEEP, StubRepo  # noqa: E402

SCRIPT = "sagemaker/endpoint.sh"
ACCOUNT = "111122223333"
ARN = f"arn:aws:iam::{ACCOUNT}:user/alice"
OTHER_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/Dev/bob"
HOST = os.uname().nodename
NOW = 1790000000
# Lanes start names now, the per-type name of the previous version, and the first version's single one.
P5EN, P5E, OLD_P5EN, LEGACY = "drip-ai-p5en-48xlarge-1", "drip-ai-p5e-48xlarge-1", "drip-ai-p5en-48xlarge", "drip-ai"
LEASE_KEY = "leases/sagemaker-race.json"


def lane_arn(name):
    return f"arn:aws:sagemaker:us-east-1:{ACCOUNT}:endpoint/{name}"


ENDPOINT_ARN = lane_arn(P5EN)
STATE_BUCKET = f"drip-ai-tfstate-{ACCOUNT}-us-east-1"
KEY = "CanaryApiKey0123456789abcdefghijklmnopqrstuvwxyzAB"
CURL_CONFIG = f'header = "Authorization: Bearer {KEY}"\n'
INITIALISED = {
    f"{root}/.terraform/terraform.tfstate": json.dumps(
        {"version": 3, "backend": {"type": "s3", "config": {"bucket": STATE_BUCKET}}})
    for root in ("sagemaker/terraform", "shared/terraform")
}
# Records its argv and stdin line 1 in $HOME/proxy.json, then serves "forever" like the real proxy.
FAKE_PROXY = """import json, os, sys, time
record = {"argv": sys.argv[1:], "stdin": sys.stdin.readline()}
with open(os.path.join(os.environ["HOME"], "proxy.json"), "w") as f:
    json.dump(record, f)
time.sleep(300)
"""
# Appends "<name> <args>" to $HOME/order.log and exits $FAKE_EXIT_<name> (default 0).
FAKE_SCRIPT = """name="$(basename "$0")"
echo "$name $*" >>"$HOME/order.log"
var="FAKE_EXIT_${name%%.*}"
exit "${!var:-0}"
"""


def make_repo(files=None):
    repo = StubRepo(files={**INITIALISED, **(files or {})})
    repo.stub("aws", "sts", "get-caller-identity", stdout=f"{ACCOUNT}\t{ARN}\n")
    repo.install_stub("shared/lib/codex_link.py", "codex_link")
    repo.install_stub("shared/lib/json_link.py", "json_link")
    return repo


IN_PROGRESS = ("An error occurred (ValidationException) when calling the DeleteEndpoint operation:"
               " Cannot update in-progress endpoint\n")
NOT_FOUND = ('An error occurred (ValidationException) when calling the DescribeEndpoint operation:'
             ' Could not find endpoint.\n')


def script_lists(repo, *answers):
    """Answers ListEndpoints with each of `answers` ({name: status}) once, in order; the last repeats."""
    for index, states in enumerate(answers):
        repo.stub("aws", "sagemaker", "list-endpoints", times=None if index == len(answers) - 1 else 1,
                  stdout="".join(f"{name}\t{status}\t{lane_arn(name)}\n" for name, status in states.items()))


def script_owner(repo, owner, status="InService", times=None, created="2026-10-05T10:00:00+00:00", name=P5EN):
    """The owner reads of `name`: tagged `owner` ("None" prints for a missing tag) and created at
    `created`; `times` retires both answers after that many reads."""
    repo.stub("aws", "sagemaker", "describe-endpoint", name, "text", times=times,
              stdout=f"{lane_arn(name)}\t{status}\t{created}\tdrip-ai-20261005\n")
    repo.stub("aws", "sagemaker", "list-tags", lane_arn(name), stdout=f"{owner}\n", times=times)


def script_absent(repo, name=P5EN):
    repo.stub("aws", "sagemaker", "describe-endpoint", name, "text", stderr=NOT_FOUND, exit=254)


def race_lease(owner, host, pid):
    """The race lease of a start running as `pid` on `host`, held until ten minutes after NOW."""
    def iso(epoch):
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return json.dumps({"owner_arn": owner, "host": host, "pid": pid,
                       "acquired_at": iso(NOW - 60), "expires_at": iso(NOW + 600)})


def script_no_race(repo):
    """No race lease, so no race runs."""
    repo.stub("aws", "s3api", "get-object", LEASE_KEY, exit=254,
              stderr="An error occurred (NoSuchKey) when calling the GetObject operation: The specified key does not exist.\n")


def calls_of(repo, tool, *tokens):
    return [c for c in repo.calls() if c.tool == tool and all(t in c.argv for t in tokens)]


def free_port():
    """A TCP port nothing listens on, so the tunnel's port-in-use check passes on any machine."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class StopTest(unittest.TestCase):
    def stop(self, repo, *args, typed=None, unlink_exit=0, env=None):
        """Runs stop; a lease the test scripted comes before the default of no race."""
        repo.stub("codex_link", "unlink", stdout="Codex is not linked to drip-ai; nothing to restore\n",
                  exit=unlink_exit, stderr="codex-link: config.toml is locked\n" if unlink_exit else "")
        repo.stub("json_link", "unlink", stdout="not linked to drip-ai; nothing to restore\n")
        script_no_race(repo)
        env = {"DRIP_AI_NOW_EPOCH": str(NOW), **(env or {})}
        if typed is not None:
            tty = repo.home / "tty"
            tty.write_text(typed)
            env["DRIP_AI_TTY"] = str(tty)
        return repo.run(SCRIPT, "stop", *args, env=env)

    def test_stop_deletes_your_endpoints_and_runs_no_terraform(self):
        with make_repo() as repo:
            script_lists(repo, {P5EN: "InService"})
            script_owner(repo, ARN, times=2)
            script_absent(repo)
            repo.stub("aws", "sagemaker", "delete-endpoint")
            repo.stub("aws", "sagemaker", "wait", "endpoint-deleted")
            result = self.stop(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([c for c in repo.calls() if c.tool == "terraform"], [])
            self.assertEqual(len(calls_of(repo, "aws", "delete-endpoint", P5EN)), 1)
            self.assertIn("No drip-ai endpoint of yours exists in us-east-1", result.stdout)
            self.assertIn("bin/drip-ai doctor lists the free models", result.stdout)
            self.assertEqual(list(repo.root.rglob("*.tfvars")), [])

    def test_someone_elses_endpoint_is_left_alone_unless_forced_with_its_typed_name(self):
        for owner in (OTHER_ARN, "None"):
            with self.subTest(owner=owner), make_repo() as repo:
                script_lists(repo, {P5EN: "InService"})
                script_owner(repo, owner)
                result = self.stop(repo)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Leaving endpoint {P5EN} alone: it was", result.stdout)
                self.assertEqual(calls_of(repo, "aws", "delete-endpoint"), [])
            for typed in ("drip\n", ""):
                with self.subTest(owner=owner, typed=typed), make_repo() as repo:
                    script_lists(repo, {P5EN: "InService"})
                    script_owner(repo, owner)
                    result = self.stop(repo, "--force", typed=typed)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(f"the typed name did not match {P5EN}; nothing more was deleted", result.stderr)
                    self.assertEqual(calls_of(repo, "aws", "delete-endpoint"), [])
        with make_repo() as repo:
            script_lists(repo, {P5EN: "InService"})
            script_owner(repo, OTHER_ARN, times=3)
            script_absent(repo)
            repo.stub("aws", "sagemaker", "delete-endpoint")
            repo.stub("aws", "sagemaker", "wait", "endpoint-deleted")
            result = self.stop(repo, "--force", typed=f"{P5EN}\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"started by {OTHER_ARN} at 2026-10-05T10:00:00+00:00", result.stdout)
            self.assertEqual(len(calls_of(repo, "aws", "delete-endpoint")), 1)

    def test_yes_never_types_an_endpoint_name(self):
        with make_repo() as repo:
            script_lists(repo, {P5EN: "InService"})
            script_owner(repo, OTHER_ARN)
            result = self.stop(repo, "--force", "--yes", env={"DRIP_AI_TTY": str(repo.home / "no-such-tty")})
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"the typed name did not match {P5EN}; nothing more was deleted", result.stderr)
            self.assertEqual(calls_of(repo, "aws", "delete-endpoint"), [])

    def test_stop_unlinks_every_client_before_any_delete(self):
        for unlink_exit in (0, 1):
            with self.subTest(unlink_exit=unlink_exit), make_repo() as repo:
                script_lists(repo, {P5EN: "InService"})
                script_owner(repo, ARN, times=2)
                script_absent(repo)
                repo.stub("aws", "sagemaker", "delete-endpoint")
                repo.stub("aws", "sagemaker", "wait", "endpoint-deleted")
                result = self.stop(repo, unlink_exit=unlink_exit)
                self.assertEqual(result.returncode, 0, result.stderr)
                order = [(c.tool, " ".join(c.argv) if c.tool != "aws" else c.argv[1])
                         for c in repo.calls() if c.tool in ("codex_link", "json_link")
                         or (c.tool == "aws" and c.argv[1] == "delete-endpoint")]
                self.assertEqual(order, [("codex_link", "unlink --provider drip-ai"),
                                         ("json_link", "unlink --client claude"),
                                         ("json_link", "unlink --client opencode --provider drip-ai"),
                                         ("aws", "delete-endpoint")])
                self.assertEqual("could not restore the Codex config" in result.stderr, bool(unlink_exit))

    def test_stop_deletes_every_lane_and_the_earlier_names(self):
        lanes = {P5EN: "InService", P5E: "Creating", OLD_P5EN: "Failed", LEGACY: "Failed"}
        with make_repo() as repo:
            script_lists(repo, lanes)
            for name, status in lanes.items():
                script_owner(repo, ARN, status=status, times=2, name=name)
                script_absent(repo, name)
            repo.stub("aws", "sagemaker", "delete-endpoint")
            repo.stub("aws", "sagemaker", "wait", "endpoint-deleted")
            result = self.stop(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            deleted = [c.argv[c.argv.index("--endpoint-name") + 1] for c in calls_of(repo, "aws", "delete-endpoint")]
            self.assertEqual(deleted, list(lanes))
            self.assertIn("No drip-ai endpoint of yours exists", result.stdout)

    def test_stop_covers_every_configured_region(self):
        with make_repo() as repo:
            config = repo.home / ".config" / "drip-ai" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"regions": ["us-east-2", "us-east-1"]}))
            config.chmod(0o600)
            use2 = "drip-ai-h200-us-east-2-1"
            repo.stub("aws", "sagemaker", "list-endpoints", "us-east-2", times=2,
                      stdout=f"{use2}\tInService\tarn:aws:sagemaker:us-east-2:{ACCOUNT}:endpoint/{use2}\n")
            repo.stub("aws", "sagemaker", "list-endpoints", "us-east-2", stdout="")
            repo.stub("aws", "sagemaker", "describe-endpoint", use2, "text", times=2,
                      stdout=f"arn:aws:sagemaker:us-east-2:{ACCOUNT}:endpoint/{use2}\tInService\t2026-10-05T10:00:00+00:00\tc\n")
            repo.stub("aws", "sagemaker", "describe-endpoint", use2, "text", stderr=NOT_FOUND, exit=254)
            repo.stub("aws", "sagemaker", "list-tags", f"arn:aws:sagemaker:us-east-2:{ACCOUNT}:endpoint/{use2}",
                      stdout=f"{ARN}\n")
            script_lists(repo, {})
            repo.stub("aws", "sagemaker", "delete-endpoint")
            repo.stub("aws", "sagemaker", "wait", "endpoint-deleted")
            result = self.stop(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            [delete] = calls_of(repo, "aws", "delete-endpoint")
            self.assertEqual(delete.argv[delete.argv.index("--region") + 1], "us-east-2")
            self.assertIn("No drip-ai endpoint in us-east-1.", result.stdout)
            self.assertIn("No drip-ai endpoint of yours exists in us-east-1 us-east-2", result.stdout)

    def test_stop_retries_a_creating_lane_until_it_is_gone(self):
        with make_repo() as repo:
            repo.skip_sleeps()
            script_lists(repo, {P5EN: "Creating"})
            # read_lanes and two refused tries see Creating; the third try is accepted.
            script_owner(repo, ARN, status="Creating", times=3)
            script_owner(repo, ARN, status="InService", times=1)
            script_absent(repo)
            repo.stub("aws", "sagemaker", "delete-endpoint", stderr=IN_PROGRESS, exit=254, times=2)
            repo.stub("aws", "sagemaker", "delete-endpoint")
            repo.stub("aws", "sagemaker", "wait", "endpoint-deleted")
            result = self.stop(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls_of(repo, "aws", "delete-endpoint")), 3)
            self.assertEqual(repo.sleeps(), ["15", "15"])
            self.assertEqual(result.stdout.count("asking again every 15s for up to 120 minutes"), 1)
            self.assertIn(f"Endpoint {P5EN} is Creating, and SageMaker deletes it only after that ends", result.stdout)

    def test_stop_never_says_done_while_an_endpoint_exists(self):
        refusals = {
            "Creating past the cap": ("Creating", IN_PROGRESS, "is still Creating after 0 minutes"),
            "refused for another reason": ("Failed", "An error occurred (AccessDeniedException)\n",
                                           f"SageMaker refused to delete endpoint {P5EN} (An error occurred"),
        }
        for label, (status, refusal, message) in refusals.items():
            with self.subTest(label), make_repo() as repo:
                script_lists(repo, {P5EN: status})
                script_owner(repo, ARN, status=status)
                repo.stub("aws", "sagemaker", "delete-endpoint", stderr=refusal, exit=254)
                result = self.stop(repo, env={"DRIP_AI_STOP_WAIT_SECONDS": "0"})
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertIn("it bills until deleted", result.stderr)
                self.assertEqual(len(calls_of(repo, "aws", "delete-endpoint")), 1)
                self.assertNotIn("No drip-ai endpoint of yours exists", result.stdout)

    def test_ctrl_c_while_waiting_names_what_still_bills_and_the_retry(self):
        with make_repo() as repo:
            script_lists(repo, {P5EN: "Creating", LEGACY: "Failed"})
            for name in (P5EN, LEGACY):
                script_owner(repo, ARN, status="Creating", name=name)
            repo.stub("aws", "sagemaker", "delete-endpoint", stderr=IN_PROGRESS, exit=254)
            repo.stub("codex_link", "unlink", stdout="")
            repo.stub("json_link", "unlink", stdout="")
            script_no_race(repo)
            out_path, err_path = repo.home / "out", repo.home / "err"
            with open(out_path, "w") as out, open(err_path, "w") as err:
                # Its own session, so the signal reaches the sleep as a terminal's Ctrl-C would.
                proc = repo.popen(SCRIPT, "stop", env={"DRIP_AI_STOP_POLL_SECONDS": "60"}, stdout=out,
                                  stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
                try:
                    deadline = time.monotonic() + 30
                    while "Ctrl-C stops waiting" not in out_path.read_text():
                        self.assertIsNone(proc.poll(), err_path.read_text())
                        self.assertLess(time.monotonic(), deadline, err_path.read_text())
                        time.sleep(0.1)
                    os.killpg(proc.pid, signal.SIGINT)
                    self.assertEqual(proc.wait(timeout=20), 130)
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
            self.assertIn(f"may still exist and bill until deleted: {P5EN} {LEGACY}. Finish with:"
                          " bin/drip-ai sagemaker stop\n", err_path.read_text())

    def test_stop_force_deletes_only_the_endpoint_confirmed_by_arn_and_creation_time(self):
        # Theirs, taken over after stop listed it as yours: no delete.
        with make_repo() as repo:
            script_lists(repo, {P5EN: "Failed"})
            script_owner(repo, ARN, status="Failed", times=1)
            script_owner(repo, OTHER_ARN, status="Failed")
            result = self.stop(repo)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"was started by {OTHER_ARN}", result.stderr)
            self.assertIn("not deleting it", result.stderr)
            self.assertEqual(calls_of(repo, "aws", "delete-endpoint"), [])
        # --force and the typed name: deleted.
        with make_repo() as repo:
            script_lists(repo, {P5EN: "Failed"})
            script_owner(repo, OTHER_ARN, status="Failed", times=3)
            script_absent(repo)
            repo.stub("aws", "sagemaker", "delete-endpoint", P5EN)
            repo.stub("aws", "sagemaker", "wait", "endpoint-deleted", P5EN)
            result = self.stop(repo, "--force", typed=f"{P5EN}\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls_of(repo, "aws", "delete-endpoint")), 1)
        # Someone recreated the lane after the typed name: refused, because the confirmation names
        # the endpoint by ARN and CreationTime, not by name.
        with make_repo() as repo:
            script_lists(repo, {P5EN: "Failed"})
            script_owner(repo, OTHER_ARN, status="Failed", times=2)
            script_owner(repo, OTHER_ARN, status="Creating", created="2026-10-05T11:30:00+00:00")
            repo.stub("aws", "sagemaker", "delete-endpoint", P5EN)
            result = self.stop(repo, "--force", typed=f"{P5EN}\n")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"was started by {OTHER_ARN} at 2026-10-05T11:30:00+00:00", result.stderr)
            self.assertIn("not deleting it", result.stderr)
            self.assertEqual(calls_of(repo, "aws", "delete-endpoint"), [])

    def test_stop_ends_a_race_on_this_machine_then_deletes_its_lanes(self):
        with make_repo() as repo:
            # Stands in for the start racing on this machine; TERM ends it.
            race = subprocess.Popen([REAL_SLEEP, "60"])
            threading.Thread(target=race.wait, daemon=True).start()  # reaped, so its pid goes
            try:
                repo.stub("aws", "s3api", "get-object", LEASE_KEY, stdout='"e1"\n',
                          last_arg_file=race_lease(ARN, HOST, race.pid))
                script_lists(repo, {P5EN: "Failed"})
                script_owner(repo, ARN, status="Failed", times=2)
                script_absent(repo)
                repo.stub("aws", "sagemaker", "delete-endpoint")
                repo.stub("aws", "sagemaker", "wait", "endpoint-deleted")
                result = self.stop(repo, "--yes")
            finally:
                race.kill()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(race.wait(timeout=10), -signal.SIGTERM)
            self.assertIn(f"Ending the race (pid {race.pid}); it deletes its own lanes before it exits.", result.stdout)
            operations = [c.argv[1] for c in repo.calls() if c.tool == "aws" and c.argv[1] != "sts"]
            self.assertLess(operations.index("get-object"), operations.index("list-endpoints"))
            self.assertEqual(len(calls_of(repo, "aws", "delete-endpoint", P5EN)), 1)

    def test_stop_refuses_a_race_on_another_machine(self):
        with make_repo() as repo:
            repo.stub("aws", "s3api", "get-object", LEASE_KEY, stdout='"e1"\n',
                      last_arg_file=race_lease(OTHER_ARN, "build-box", 4242))
            result = self.stop(repo)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"a race started by {OTHER_ARN} is running on build-box (pid 4242, lease until", result.stderr)
            self.assertIn("nothing was deleted", result.stderr)
            self.assertEqual([c for c in repo.calls() if c.tool != "aws" or "list-endpoints" in c.argv], [])


class TunnelTest(unittest.TestCase):
    def setUp(self):
        self.port = str(free_port())

    def script_tunnel(self, repo, owner, readiness):
        """`readiness` is a list of (stdout, exit) answers to the InService check; the last repeats."""
        repo.skip_sleeps()
        repo.stub("codex_link", "link", stdout="linked Codex to big-model via drip-ai\n")
        repo.stub("codex_link", "unlink", stdout="unlinked Codex: restored model\n")
        repo.stub("json_link", "link", stdout="Claude Code config not found; skipping\n")
        repo.stub("json_link", "unlink", stdout="not linked\n")
        for index, (stdout, exit_code) in enumerate(readiness):
            last = index == len(readiness) - 1
            repo.stub("aws", "describe-endpoint", "EndpointStatus", stdout=stdout, exit=exit_code,
                      stderr="An error occurred (ValidationException)\n" if exit_code else "",
                      times=None if last else 1)
        script_lists(repo, {P5E: "Deleting", P5EN: "InService"})
        script_owner(repo, owner)
        # The race leaves the sagemaker outputs missing; the endpoint's own model has the values.
        repo.stub("terraform", f"-chdir={repo.root}/sagemaker/terraform", "output", exit=1,
                  stderr="Error: Output not found\n")
        repo.stub("aws", "describe-endpoint-config", "drip-ai-20261005", "ProductionVariants[0].ModelName",
                  stdout="drip-ai-0a1b2c3d\n")
        repo.stub("aws", "describe-model", "drip-ai-0a1b2c3d", stdout="big-model\t65536\n")
        repo.stub("aws", "ssm", "get-parameter", "/drip-ai/api-key", stdout=KEY + "\n")
        repo.stub("curl", stdin=CURL_CONFIG, stdout="200")

    def run_tunnel(self, repo, wait_for, *args, extra_env=None):
        """Starts the tunnel, waits for `wait_for` in its output, sends SIGINT, and returns
        (returncode, stdout, stderr, proxy record)."""
        out_path, err_path = repo.home / "out", repo.home / "err"
        env = {"DRIP_AI_PORT": self.port, "DRIP_AI_TUNNEL_POLL_SECONDS": "1", "DRIP_AI_LINK_CLIENTS": "all",
               **(extra_env or {})}
        with open(out_path, "w") as out, open(err_path, "w") as err:
            proc = repo.popen(SCRIPT, "tunnel", *args, env=env, stdout=out, stderr=err,
                              stdin=subprocess.DEVNULL)
            deadline = time.monotonic() + 30
            while wait_for not in out_path.read_text() + err_path.read_text() or not (
                    repo.home / "proxy.json").exists():
                self.assertIsNone(proc.poll(), err_path.read_text())
                self.assertLess(time.monotonic(), deadline, "tunnel never reached: " + wait_for)
                time.sleep(0.1)
            proc.send_signal(signal.SIGINT)
            returncode = proc.wait(timeout=20)
        record = json.loads((repo.home / "proxy.json").read_text())
        return returncode, out_path.read_text(), err_path.read_text(), record

    def test_tunnel_links_the_endpoints_own_model_and_window_and_keeps_the_key_out_of_argv(self):
        with make_repo(files={"sagemaker/proxy.py": FAKE_PROXY}) as repo:
            self.script_tunnel(repo, OTHER_ARN, [("Creating\n", 0), ("InService\n", 0)])
            returncode, out, err, record = self.run_tunnel(repo, "Codex: linked (restart Codex)")
            self.assertEqual(returncode, 130, err)
            self.assertIn(f"warning: endpoint {P5EN} was started by {OTHER_ARN}", err)
            self.assertEqual([c.argv[0] for c in calls_of(repo, "codex_link")], ["link", "unlink"])
            link = calls_of(repo, "codex_link", "link")[0].argv
            self.assertEqual(link[link.index("--context-window") + 1], "65536")
            self.assertGreaterEqual(len(calls_of(repo, "aws", "describe-endpoint", "EndpointStatus")), 2)
            self.assertEqual(record["argv"], ["--region", "us-east-1", "--endpoint", P5EN,
                                              "--model", "big-model", "--port", self.port])
            # The key reaches the proxy on stdin only.
            self.assertEqual(record["stdin"], KEY + "\n")
            for call in repo.calls():
                self.assertFalse(any(KEY in arg for arg in call.argv), call.argv)
                self.assertFalse(any(KEY in value for value in call.env.values()), call.tool)
            self.assertNotIn(KEY, out + err)

    def test_tunnel_smoke_runs_once_the_clients_link_and_keeps_the_tunnel_open(self):
        smoke = 'echo "smoke ran: $*"; exit "${FAKE_SMOKE_EXIT:-0}"\n'
        for smoke_exit, wait_for in (("0", "smoke ran: smoke"), ("1", "the smoke test failed; the tunnel stays open")):
            with self.subTest(smoke_exit=smoke_exit), make_repo(
                    files={"sagemaker/proxy.py": FAKE_PROXY, "shared/clients.sh": smoke}) as repo:
                self.script_tunnel(repo, ARN, [("InService\n", 0)])
                returncode, out, err, _ = self.run_tunnel(repo, wait_for, "--smoke",
                                                          extra_env={"FAKE_SMOKE_EXIT": smoke_exit})
                self.assertEqual(returncode, 130, err)
                self.assertLess(out.index("Codex: linked (restart Codex)"), out.index("smoke ran: smoke"))
                self.assertEqual(out.count("smoke ran"), 1)
                self.assertEqual([c.argv[0] for c in calls_of(repo, "codex_link")], ["link", "unlink"])

    def test_tunnel_smoke_says_so_when_no_client_will_link(self):
        smoke = 'echo "smoke ran: $*"\n'
        with make_repo(files={"sagemaker/proxy.py": FAKE_PROXY, "shared/clients.sh": smoke}) as repo:
            self.script_tunnel(repo, ARN, [("InService\n", 0)])
            returncode, out, err, _ = self.run_tunnel(repo, "No smoke test", "--smoke",
                                                      extra_env={"DRIP_AI_LINK_CLIENTS": "none"})
            self.assertEqual(returncode, 130, err)
            self.assertIn("No smoke test: it runs once the clients link, and none will;", out)
            self.assertNotIn("smoke ran", out)

    def test_tunnel_never_links_on_failed(self):
        cases = {
            "Failed": ([("Failed\n", 0)], (), "was not ready after"),
            "Deleting": ([("Deleting\n", 0)], (), "was not ready after"),
            "missing": ([("", 254)], (), "was not ready after"),
            "--no-link": ([("InService\n", 0)], ("--no-link",), "No client links"),
        }
        for label, (readiness, args, wait_for) in cases.items():
            with self.subTest(label), make_repo(files={"sagemaker/proxy.py": FAKE_PROXY}) as repo:
                self.script_tunnel(repo, ARN, readiness)
                returncode, out, err, _ = self.run_tunnel(
                    repo, wait_for, *args, extra_env={"DRIP_AI_TUNNEL_WAIT_SECONDS": "0"})
                self.assertEqual(returncode, 130, err)
                self.assertEqual(calls_of(repo, "codex_link"), [])
                self.assertEqual(calls_of(repo, "json_link"), [])
                self.assertNotIn("warning: endpoint", err)

    def test_tunnel_registers_itself_and_a_second_one_for_the_same_endpoint_reuses_it(self):
        with make_repo(files={"sagemaker/proxy.py": FAKE_PROXY}) as repo:
            self.script_tunnel(repo, ARN, [("Creating\n", 0)])
            out_path, err_path = repo.home / "out", repo.home / "err"
            env = {"DRIP_AI_PORT": self.port, "DRIP_AI_TUNNEL_POLL_SECONDS": "1"}
            with open(out_path, "w") as out, open(err_path, "w") as err:
                proc = repo.popen(SCRIPT, "tunnel", env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL)
                try:
                    registry = repo.home / ".local" / "state" / "drip-ai" / "procs"
                    deadline = time.monotonic() + 30
                    while not list(registry.glob("*.json")) if registry.exists() else True:
                        self.assertIsNone(proc.poll(), err_path.read_text())
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.1)
                    [record_path] = registry.glob("*.json")
                    record = json.loads(record_path.read_text())
                    self.assertEqual((record["pid"], record["port"], record["endpoint"], record["provider"]),
                                     (proc.pid, int(self.port), P5EN, "drip-ai"))
                    again = repo.run(SCRIPT, "tunnel", env=env)
                    self.assertEqual(again.returncode, 0, again.stderr)
                    self.assertIn(f"A drip-ai tunnel already serves {P5EN} (pid {proc.pid} on 127.0.0.1:{self.port})",
                                  again.stdout)
                finally:
                    proc.send_signal(signal.SIGINT)
                    proc.wait(timeout=20)
            self.assertFalse(record_path.exists())

    def test_tunnel_follows_a_named_endpoint_in_another_region_with_that_regions_key(self):
        name = "drip-ai-h200-us-east-2-1"
        with make_repo(files={"sagemaker/proxy.py": FAKE_PROXY}) as repo:
            self.script_tunnel(repo, ARN, [("InService\n", 0)])
            repo.stub("aws", "sagemaker", "describe-endpoint", name, "text", "us-east-2",
                      stdout=f"arn:aws:sagemaker:us-east-2:{ACCOUNT}:endpoint/{name}\tInService\t2026-10-05T10:00:00+00:00\tdrip-ai-20261005\n")
            repo.stub("aws", "sagemaker", "list-tags", f"arn:aws:sagemaker:us-east-2:{ACCOUNT}:endpoint/{name}",
                      stdout=f"{ARN}\n")
            returncode, out, err, record = self.run_tunnel(repo, "Codex: linked", "--region", "us-east-2",
                                                           "--endpoint", name)
            self.assertEqual(returncode, 130, err)
            self.assertEqual(record["argv"][:4], ["--region", "us-east-2", "--endpoint", name])
            keys = [c.argv for c in calls_of(repo, "aws", "ssm", "get-parameter")]
            self.assertTrue(keys and all(argv[argv.index("--region") + 1] == "us-east-2" for argv in keys))
            self.assertEqual(calls_of(repo, "aws", "list-endpoints"), [])

    def test_tunnel_refuses_no_endpoint_and_a_race_in_progress(self):
        cases = {
            "none": ({}, "no drip-ai endpoint in us-east-1"),
            "racing": ({P5EN: "Creating", P5E: "Creating"}, "race in progress"),
        }
        for label, (states, message) in cases.items():
            with self.subTest(label), make_repo() as repo:
                script_lists(repo, states)
                result = repo.run(SCRIPT, "tunnel")
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual([c for c in repo.calls() if c.tool != "aws"], [])


class DeployTest(unittest.TestCase):
    FILES = {"shared/models.sh": FAKE_SCRIPT, "sagemaker/start.sh": FAKE_SCRIPT}

    def script_no_endpoint(self, repo):
        script_lists(repo, {})

    def script_unreadable_endpoint(self, repo):
        script_lists(repo, {P5EN: "InService"})
        repo.stub("aws", "describe-endpoint", "text", exit=255,
                  stderr="An error occurred (AccessDeniedException) when calling the DescribeEndpoint"
                         " operation: not authorized\n")

    def order(self, repo):
        path = repo.home / "order.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_deploy_when_no_endpoint_uploads_then_starts(self):
        with make_repo(files=self.FILES) as repo:
            self.script_no_endpoint(repo)
            result = repo.run(SCRIPT, "deploy", "my-model")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.order(repo),
                             ["models.sh upload my-model", "start.sh my-model --no-manifest"])
            self.assertEqual([c.tool for c in repo.calls()], ["aws", "aws"])
            self.assertEqual(calls_of(repo, "aws", "s3"), [])

    def test_deploy_stops_when_the_upload_fails(self):
        with make_repo(files=self.FILES) as repo:
            self.script_no_endpoint(repo)
            result = repo.run(SCRIPT, "deploy", "my-model", env={"FAKE_EXIT_models": "3"})
            self.assertEqual(result.returncode, 1)
            self.assertIn("nothing was started", result.stderr)
            self.assertEqual(self.order(repo), ["models.sh upload my-model"])

    def test_deploy_checks_arguments_before_any_aws_call(self):
        with make_repo(files=self.FILES) as repo:
            for folder in ("-x", "Org/Name", "--unknown"):
                with self.subTest(folder=folder):
                    result = repo.run(SCRIPT, "deploy", folder)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("folder must be one name under models/", result.stderr)
            for args in ((), ("--take-over",), ("a", "b"), ("a", "--take-over", "b")):
                with self.subTest(args=args):
                    result = repo.run(SCRIPT, "deploy", *args)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("Usage: sagemaker/endpoint.sh status", result.stderr)
            self.assertEqual(self.order(repo), [])
            self.assertEqual(repo.calls(), [])

    def test_deploy_refuses_other_owner_before_upload(self):
        def held(owner):
            return lambda repo: (script_lists(repo, {P5EN: "InService"}), script_owner(repo, owner))

        holders = {
            "someone else": (held(OTHER_ARN), f"started by {OTHER_ARN}"),
            "no StartedBy tag": (held("None"), "with no StartedBy tag"),
            "unreadable": (self.script_unreadable_endpoint, "of unknown owner (An error occurred"),
        }
        for label, (script, holder_text) in holders.items():
            with self.subTest(label), make_repo(files=self.FILES) as repo:
                script(repo)
                result = repo.run(SCRIPT, "deploy", "my-model")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(holder_text, result.stderr)
                self.assertIn("nothing was uploaded", result.stderr)
                self.assertIn("rerun with --take-over", result.stderr)
                self.assertEqual(self.order(repo), [])
                self.assertEqual(calls_of(repo, "aws", "s3"), [])
                self.assertEqual([c for c in repo.calls() if c.tool != "aws"], [])

    def test_deploy_refuses_to_upload_over_own_running_endpoint(self):
        with make_repo(files=self.FILES) as repo:
            script_lists(repo, {P5EN: "InService"})
            script_owner(repo, ARN)
            result = repo.run(SCRIPT, "deploy", "my-model")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("is already running (yours)", result.stderr)
            self.assertIn("Stop it first or start a different model", result.stderr)
            self.assertIn("nothing was uploaded", result.stderr)
            self.assertEqual(self.order(repo), [])
            self.assertEqual(calls_of(repo, "aws", "s3"), [])

    def test_deploy_passes_yes_to_start(self):
        with make_repo(files=self.FILES) as repo:
            self.script_no_endpoint(repo)
            result = repo.run(SCRIPT, "deploy", "--yes", "my-model")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.order(repo), ["models.sh upload my-model", "start.sh my-model --no-manifest --yes"])

    def test_deploy_with_take_over_passes_flag_to_start(self):
        holders = {
            "someone else": lambda repo: script_owner(repo, OTHER_ARN),
            "no StartedBy tag": lambda repo: script_owner(repo, "None"),
            "unreadable": self.script_unreadable_endpoint,
            "yours": lambda repo: script_owner(repo, ARN),
        }
        for label, script in holders.items():
            for args in (("my-model", "--take-over"), ("--take-over", "my-model")):
                with self.subTest(label, args=args), make_repo(files=self.FILES) as repo:
                    script(repo)
                    result = repo.run(SCRIPT, "deploy", *args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.order(repo), [
                        "models.sh upload my-model", "start.sh my-model --no-manifest --take-over"])


if __name__ == "__main__":
    unittest.main()
