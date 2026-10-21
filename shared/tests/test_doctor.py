"""Pins `bin/drip-ai doctor` (shared/drip_ai/doctor.py) against stub CLIs and the fake SageMaker
API: it reads only without --fix, never touches anything in use, young, protected, untagged, or
not its own, and stops only a registered process whose start time still matches."""

import json
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "testing"))
sys.path.append(str(Path(__file__).resolve().parents[1]))

from cli_stubs import StubRepo  # noqa: E402
from drip_ai import procs  # noqa: E402

FAKE = Path(__file__).resolve().parents[2] / "sagemaker" / "tests" / "fake_sagemaker.py"
ACCOUNT = "111122223333"
ARN = f"arn:aws:iam::{ACCOUNT}:user/alice"
BOB = f"arn:aws:iam::{ACCOUNT}:user/bob"
NOW = 1790000000
OLD = "2026-01-01T00:00:00+00:00"
YOUNG = "2026-09-21T14:00:00+00:00"  # 13 minutes before NOW
MUTATING = {"delete-endpoint", "delete-endpoint-config", "delete-model", "put-retention-policy", "create-endpoint",
            "create-model", "create-endpoint-config", "create-log-group", "put-object", "delete-object"}
LOGS = "/aws/sagemaker/Endpoints/"


def arn(kind, name, region="us-east-1"):
    return f"arn:aws:sagemaker:{region}:{ACCOUNT}:{kind}/{name}"


def ours(run="20260101T000000Z-aaaaaa"):
    return [{"Key": "Project", "Value": "drip-ai"}, {"Key": "ManagedBy", "Value": "drip-ai"},
            {"Key": "Stack", "Value": "sagemaker"}, {"Key": "StartedBy", "Value": ARN}, {"Key": "RunId", "Value": run}]


def log_group(created=OLD, tags=None, retention=None):
    """A log group as race.py creates it: tags as a {key: value} map."""
    return {"region": "us-east-1", "retention": retention, "created": created,
            "tags": {t["Key"]: t["Value"] for t in ours()} if tags is None else tags}


def config(name, model, created=OLD, tags=None):
    return {"arn": arn("endpoint-config", name), "region": "us-east-1", "created": created,
            "tags": ours() if tags is None else tags,
            "body": [{"VariantName": "main", "ModelName": model, "InitialInstanceCount": 1}]}


def model(name, created=OLD, tags=None):
    return {"arn": arn("model", name), "region": "us-east-1", "created": created, "role": None, "body": {},
            "tags": ours() if tags is None else tags}


def endpoint(name, config_name, status, tags=None, pending=None):
    return {"arn": arn("endpoint", name), "created": OLD, "region": "us-east-1", "config": config_name,
            "steps": [status], "step": 0, "tags": ours() if tags is None else tags, "pending_config": pending}


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.repo = StubRepo()
        self.addCleanup(self.repo.__exit__)
        repo = self.repo
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        user = repo.home / ".config" / "drip-ai" / "config.json"
        user.parent.mkdir(parents=True)
        user.write_text(json.dumps({"ports": {"base": self.port, "range": 3},
                                    "lifecycle": {"protect": ["drip-ai-c-kept-1", "drip-ai-h200-us-east-1-9"]}}))
        user.chmod(0o600)
        repo.stub("aws", "sts", "get-caller-identity", "text", stdout=f"{ACCOUNT}\t{ARN}\n")
        repo.stub("aws", "sts", "get-caller-identity", stdout=json.dumps({"Account": ACCOUNT, "Arn": ARN}))
        repo.stub("aws", "configure", "export-credentials", stdout=json.dumps({"Expiration": "2026-09-21T14:30:00Z"}))
        repo.stub("aws", "iam", "get-role", stdout=json.dumps({"Role": {"Arn": "x"}}))
        repo.stub("aws", "s3api", "get-object", exit=254,
                  stderr="An error occurred (NoSuchKey) when calling the GetObject operation: nope\n")
        repo.stub("aws", "ec2", "describe-capacity-reservations", stdout=json.dumps({"CapacityReservations": []}))
        repo.install_stub("shared/lib/codex_link.py", "codex_link")
        repo.install_stub("shared/lib/json_link.py", "json_link")
        repo.stub("codex_link", "status", stdout="unlinked; no owner; model unset\n")
        repo.stub("json_link", "status", "claude", stdout="unlinked; no owner; model unset\n")
        repo.stub("json_link", "status", "opencode", stdout="linked; owner pid 2147483647 dead; model x\n")
        repo.stub("json_link", "unlink", stdout="unlinked opencode: restored 1 key\n")
        state = {
            "now": NOW, "scripts": {}, "create_errors": {}, "events": {}, "polls": 0, "creations": 0, "made": {},
            "endpoints": {
                # Mid blue/green: serving on one configuration, moving to another. Untagged, as an
                # endpoint made by hand outside drip-ai is.
                "drip-ai-adhoc-pool-use2-1": endpoint("drip-ai-adhoc-pool-use2-1", "drip-ai-adhoc-1", "Updating",
                                                      tags=[], pending="drip-ai-adhoc-2"),
                "drip-ai-h200-us-east-1-1": endpoint("drip-ai-h200-us-east-1-1", "drip-ai-c-live-1", "InService"),
                "drip-ai-h200-us-east-1-2": endpoint("drip-ai-h200-us-east-1-2", "drip-ai-c-dead-1",
                                                     ["Failed", "CUDA out of memory"]),
            },
            "configs": {
                "drip-ai-adhoc-1": config("drip-ai-adhoc-1", "drip-ai-adhoc-model", tags=[]),
                "drip-ai-adhoc-2": config("drip-ai-adhoc-2", "drip-ai-adhoc-model", tags=[]),
                "drip-ai-adhoc-3": config("drip-ai-adhoc-3", "drip-ai-adhoc-model", tags=[]),
                "drip-ai-c-live-1": config("drip-ai-c-live-1", "drip-ai-m-live"),
                "drip-ai-c-dead-1": config("drip-ai-c-dead-1", "drip-ai-m-live"),
                "drip-ai-c-orphan-1": config("drip-ai-c-orphan-1", "drip-ai-m-live"),
                "drip-ai-c-young-1": config("drip-ai-c-young-1", "drip-ai-m-live", created=YOUNG),
                "drip-ai-c-kept-1": config("drip-ai-c-kept-1", "drip-ai-m-live"),
            },
            "models": {
                "drip-ai-adhoc-model": model("drip-ai-adhoc-model", tags=[]),
                "drip-ai-m-live": model("drip-ai-m-live"),
                "drip-ai-m-orphan": model("drip-ai-m-orphan"),
            },
            "log_groups": {
                # The hand-made endpoint's group: untagged, so it is reported and never given a retention.
                LOGS + "drip-ai-adhoc-pool-use2-1": log_group(tags={}),
                LOGS + "drip-ai-h200-us-east-1-1": log_group(),
                LOGS + "drip-ai-h200-us-east-1-3": log_group(created=YOUNG),
                LOGS + "drip-ai-h200-us-east-1-4": log_group(tags={**log_group()["tags"], "StartedBy": BOB}),
                LOGS + "drip-ai-h200-us-east-1-5": log_group(retention=14),
                LOGS + "drip-ai-h200-us-east-1-9": log_group(),  # its endpoint is in lifecycle.protect
            },
        }
        self.state = repo.home / "sagemaker.json"
        self.state.write_text(json.dumps(state))
        repo.put_in_front("aws", ("sagemaker", "logs", "sts"), FAKE, str(self.state))

    def doctor(self, *args):
        return subprocess.run([sys.executable, "-I", str(self.repo.root / "shared" / "drip_ai" / "cli.py"), "doctor",
                               "--home-region", "us-east-1", "--json", *args], capture_output=True, text=True,
                              timeout=120, env=self.repo._env({"DRIP_AI_NOW_EPOCH": str(NOW)}))

    def findings(self, result):
        """The findings of the JSON report doctor prints first, before any fix lines."""
        self.assertEqual(result.returncode, 0, result.stderr)
        report, _ = json.JSONDecoder().raw_decode(result.stdout)
        return {f["id"]: f for f in report["findings"]}

    def names(self, finding):
        return {item["name"]: item for item in finding["items"]}

    def test_without_fix_doctor_only_reads_and_names_each_finding(self):
        found = self.findings(self.doctor())
        self.assertFalse({c.argv[1] for c in self.repo.calls() if c.tool == "aws"} & MUTATING)
        self.assertIn("session ends in 16 min", found["R1"]["items"][0]["detail"])
        self.assertEqual(found["R1"]["level"], "warn")
        self.assertEqual(set(self.names(found["R3"])), {"drip-ai-h200-us-east-1-2"})
        configs = self.names(found["R4"])
        # The live endpoint's, the Failed endpoint's, and the blue/green swap's are in use.
        self.assertEqual(set(configs), {"drip-ai-c-orphan-1", "drip-ai-c-young-1", "drip-ai-c-kept-1"})
        self.assertTrue(configs["drip-ai-c-orphan-1"]["fixable"])
        self.assertEqual((configs["drip-ai-c-young-1"]["fixable"], configs["drip-ai-c-young-1"]["detail"]),
                         (False, "younger than an hour"))
        self.assertEqual(configs["drip-ai-c-kept-1"]["detail"], "in lifecycle.protect")
        self.assertEqual(set(self.names(found["R5"])), {"drip-ai-m-orphan"})
        untagged = self.names(found["R7"])
        self.assertEqual(set(untagged), {"drip-ai-adhoc-pool-use2-1", "drip-ai-adhoc-3", LOGS + "drip-ai-adhoc-pool-use2-1"})
        self.assertIn("aws sagemaker delete-endpoint-config --region us-east-1 --endpoint-config-name drip-ai-adhoc-3",
                      untagged["drip-ai-adhoc-3"]["data"]["command"])
        self.assertEqual(untagged[LOGS + "drip-ai-adhoc-pool-use2-1"]["data"]["command"],
                         "aws logs put-retention-policy --region us-east-1 --log-group-name"
                         f" {LOGS}drip-ai-adhoc-pool-use2-1 --retention-in-days 14")
        retention = {name: (item["fixable"], item["detail"]) for name, item in self.names(found["R6"]).items()}
        self.assertEqual(retention, {LOGS + "drip-ai-h200-us-east-1-1": (True, ""),
                                     LOGS + "drip-ai-h200-us-east-1-3": (False, "younger than an hour"),
                                     LOGS + "drip-ai-h200-us-east-1-4": (False, "not tagged ManagedBy=drip-ai by you"),
                                     LOGS + "drip-ai-h200-us-east-1-9": (False, "in lifecycle.protect")})
        self.assertEqual(set(self.names(found["R13"])), {"opencode"})

    def test_fix_deletes_only_its_own_unused_old_resources(self):
        result = self.doctor("--fix", "R3,R4,R5", "--yes")
        self.findings(result)
        deleted = [(c.argv[1], c.argv[c.argv.index(next(a for a in c.argv if a.endswith("-name"))) + 1])
                   for c in self.repo.calls() if c.tool == "aws" and c.argv[1].startswith("delete-")]
        self.assertEqual(sorted(deleted), sorted([("delete-endpoint", "drip-ai-h200-us-east-1-2"),
                                                  ("delete-endpoint-config", "drip-ai-c-orphan-1"),
                                                  ("delete-model", "drip-ai-m-orphan")]))
        left = json.loads(self.state.read_text())
        for name in ("drip-ai-adhoc-1", "drip-ai-adhoc-2", "drip-ai-adhoc-3", "drip-ai-c-young-1", "drip-ai-c-kept-1",
                     "drip-ai-c-live-1", "drip-ai-c-dead-1"):
            self.assertIn(name, left["configs"])
        self.assertIn("drip-ai-adhoc-pool-use2-1", left["endpoints"])
        self.assertIn("drip-ai-adhoc-model", left["models"])

    def test_fix_r6_keeps_14_days_only_on_its_own_old_log_groups(self):
        self.findings(self.doctor("--fix", "R6", "--yes"))
        kept = [c.argv[c.argv.index("--log-group-name") + 1] for c in self.repo.calls()
                if c.tool == "aws" and c.argv[:2] == ["logs", "put-retention-policy"]]
        self.assertEqual(kept, [LOGS + "drip-ai-h200-us-east-1-1"])
        groups = json.loads(self.state.read_text())["log_groups"]
        self.assertIsNone(groups[LOGS + "drip-ai-adhoc-pool-use2-1"]["retention"])
        self.assertEqual(groups[LOGS + "drip-ai-h200-us-east-1-1"]["retention"], 14)

    def test_fix_needs_yes_and_a_fixable_id(self):
        for args, message in ((("--fix", "R4"), "rerun with --yes"), (("--fix", "R7", "--yes"), "are reported only")):
            result = self.doctor(*args)
            self.assertEqual(result.returncode, 1)
            self.assertIn(message, result.stderr)
        self.assertEqual([c for c in self.repo.calls() if c.tool == "aws"], [])

    def test_a_listener_drip_ai_did_not_start_is_reported_and_left_running(self):
        with socket.socket() as squatter:
            squatter.bind(("127.0.0.1", self.port))
            squatter.listen()
            found = self.findings(self.doctor("--fix", "R11", "--yes"))
            import shutil

            if shutil.which("lsof"):
                [item] = found["R12"]["items"]
                self.assertEqual(item["name"], f"127.0.0.1:{self.port}")
                self.assertIn("python", item["detail"].lower())
            squatter.getsockname()  # still open: doctor stopped nothing

    def test_a_stale_tunnel_is_stopped_only_while_its_start_time_matches(self):
        env = {"HOME": str(self.repo.home)}
        gone = subprocess.Popen(["sleep", "60"])
        reused = subprocess.Popen(["sleep", "60"])
        for proc in (gone, reused):
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
        deadline = time.monotonic() + 5
        while (procs.start_time(gone.pid) is None or procs.start_time(reused.pid) is None) and time.monotonic() < deadline:
            time.sleep(0.05)
        procs.register(env, gone.pid, ["tunnel"], port=self.port, endpoint="drip-ai-gone-1", region="us-east-1",
                       backend="sagemaker", provider="drip-ai")
        path = procs.register(env, reused.pid, ["tunnel"], port=self.port + 1, endpoint="drip-ai-gone-2",
                              region="us-east-1", backend="sagemaker", provider="drip-ai-x")
        record = json.loads(path.read_text())
        record["start_time"] = "Thu Jan  1 00:00:00 1970"  # the pid now belongs to another process
        path.write_text(json.dumps(record))
        found = self.findings(self.doctor("--fix", "R11", "--yes"))
        self.assertEqual(set(self.names(found["R11"])), {str(gone.pid), str(reused.pid)})
        self.assertIsNotNone(gone.wait(timeout=10))
        self.assertIsNone(reused.poll())


if __name__ == "__main__":
    unittest.main()
