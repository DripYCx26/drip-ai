"""Pins `bin/drip-ai status --json` (shared/status.py) against stub CLIs: the documented keys, null
plus a reason for a section that cannot be read, and no key-shaped value anywhere."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "testing"))

from cli_stubs import StubRepo  # noqa: E402

ACCOUNT = "111122223333"
REGION = "eu-west-1"
ENV = {"DRIP_AI_REGION": REGION}
BUCKET = f"drip-ai-models-{ACCOUNT}-{REGION}"  # every region but us-east-1 suffixes the bucket
KEY = "sk-CanaryApiKey0123456789abcdefghijklmnopqrstuvwxyz"
LANE = "drip-ai-p5en-48xlarge"
LANE_ARN = f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:endpoint/{LANE}"
ALICE = f"arn:aws:iam::{ACCOUNT}:user/alice"
TASK_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/drip-ai-fetch-model/0123456789abcdef"
KEYS = {"region", "endpoints", "ec2", "fetches", "models", "clients", "runs", "procs", "capacity_blocks", "errors"}


def listing(prefix, files=(), folders=()):
    return json.dumps({"Contents": [{"Key": prefix + name, "Size": 1} for name in files],
                       "CommonPrefixes": [{"Prefix": prefix + name + "/"} for name in folders]})


# Failing answers for the second test; stub rules match in the order added, so these go first.
FAILURES = (
    (("list-objects-v2", "models/"), "An error occurred (NoSuchBucket) when calling the ListObjectsV2 operation\n"),
    (("describe-model", "drip-ai-model"), "An error occurred (Throttling)\n"),
    (("ecs", "list-tasks"), "An error occurred (ClusterNotFoundException) when calling the ListTasks operation\n"),
)


def make_repo(failing=False):
    repo = StubRepo()
    for tokens, stderr in FAILURES if failing else ():
        repo.stub("aws", *tokens, exit=254, stderr=stderr)
    repo.install_stub("shared/lib/codex_link.py", "codex_link")
    repo.install_stub("shared/lib/json_link.py", "json_link")
    repo.stub("codex_link", "status", stdout="linked; owner pid 1 alive; model example-model\n")
    repo.stub("json_link", "status", stdout="unlinked; no owner; model unset\n")
    repo.stub("aws", "ssm", "get-parameter", stdout=KEY + "\n")
    repo.stub("aws", "sts", "get-caller-identity", stdout=json.dumps({"Account": ACCOUNT, "Arn": ALICE}))
    repo.stub("aws", "list-endpoints", stdout=json.dumps({"Endpoints": [
        {"EndpointName": LANE, "EndpointArn": LANE_ARN, "EndpointStatus": "InService",
         "CreationTime": "2026-10-06T10:00:00+00:00"},
        {"EndpointName": "someone-elses-endpoint", "EndpointArn": "x", "EndpointStatus": "InService"}]}))
    repo.stub("aws", "list-tags", LANE_ARN, stdout=json.dumps({"Tags": [{"Key": "StartedBy", "Value": ALICE}]}))
    repo.stub("aws", "describe-endpoint", LANE, stdout=json.dumps({"EndpointConfigName": "drip-ai-cfg"}))
    repo.stub("aws", "describe-endpoint-config", "drip-ai-cfg", stdout=json.dumps(
        {"ProductionVariants": [{"InstanceType": "ml.p5en.48xlarge", "ModelName": "drip-ai-model"}]}))
    repo.stub("aws", "describe-model", "drip-ai-model", stdout=json.dumps({"PrimaryContainer": {"Environment": {
        "SM_VLLM_SERVED_MODEL_NAME": "example-model", "SM_VLLM_MAX_MODEL_LEN": "131072", "CANARY": KEY}}}))
    repo.stub("aws", "ec2", "describe-instances", stdout=json.dumps({"Reservations": [{"Instances": [
        {"InstanceId": "i-0123456789abcdef0", "InstanceType": "g6e.12xlarge", "State": {"Name": "stopped"}}]}]}))
    repo.stub("aws", "ecs", "list-tasks", stdout=json.dumps({"taskArns": [TASK_ARN]}))
    repo.stub("aws", "ecs", "describe-tasks", stdout=json.dumps({"tasks": [{
        "taskArn": TASK_ARN, "lastStatus": "RUNNING", "createdAt": "2026-10-06T09:00:00+00:00",
        "tags": [{"key": "StartedBy", "value": ALICE}],
        "overrides": {"containerOverrides": [{"name": "fetch", "command": ["copy", "--name", "Org/Big"]}]}}]}))
    repo.stub("aws", "list-objects-v2", "models/", stdout=listing("models/", folders=("Org", "mine")))
    repo.stub("aws", "list-objects-v2", "models/Org/", stdout=listing("models/Org/", folders=("Big",)))
    repo.stub("aws", "list-objects-v2", "models/Org/Big/",
              stdout=listing("models/Org/Big/", files=("drip-ai-fetch.json", "config.json")))
    repo.stub("aws", "list-objects-v2", "models/mine/", stdout=listing("models/mine/", files=("config.json",)))
    repo.stub("aws", "s3", "cp", f"s3://{BUCKET}/models/Org/Big/drip-ai-fetch.json",
              stdout=json.dumps({"status": "copying", "files": []}))
    repo.stub("aws", "ec2", "describe-capacity-reservations", stdout=json.dumps({"CapacityReservations": [
        {"CapacityReservationId": "cr-0123456789abcdef0", "InstanceType": "p5en.48xlarge", "AvailabilityZone":
         f"{REGION}a", "State": "active", "StartDate": "2026-10-06T11:30:00Z", "EndDate": "2026-10-07T11:30:00Z",
         "AvailableInstanceCount": 1}]}))
    return repo


class StatusJsonTest(unittest.TestCase):
    def status(self, repo):
        result = repo.run("bin/drip-ai", "status", "--json", env=ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotRegex(result.stdout + result.stderr, r"sk-[A-Za-z0-9]{8}")
        self.assertEqual([c for c in repo.calls() if "ssm" in c.argv or c.tool == "terraform"], [])
        for call in repo.calls():
            if "--region" in call.argv:
                self.assertEqual(call.argv[call.argv.index("--region") + 1], REGION)
        report = json.loads(result.stdout)
        self.assertEqual(set(report), KEYS)
        mutating = {"create-endpoint", "delete-endpoint", "create-model", "delete-model", "put-object", "delete-object",
                    "purchase-capacity-block", "send-command", "run-task", "stop-task"}
        self.assertFalse({c.argv[1] for c in repo.calls() if c.tool == "aws"} & mutating)
        return report

    def test_prints_every_section_and_no_key(self):
        with make_repo() as repo:
            report = self.status(repo)
            self.assertEqual(report["region"], REGION)
            self.assertEqual(report["endpoints"], [{
                "name": LANE, "status": "InService", "instance_type": "ml.p5en.48xlarge", "started_by": ALICE,
                "created": "2026-10-06T10:00:00+00:00", "served_model": "example-model", "max_model_len": 131072}])
            self.assertEqual(report["ec2"], [{"instance_id": "i-0123456789abcdef0", "instance_type": "g6e.12xlarge",
                                              "state": "stopped"}])
            self.assertEqual(report["fetches"], [{"task_id": "0123456789abcdef", "model": "Org/Big",
                                                  "status": "RUNNING", "started_by": ALICE,
                                                  "created": "2026-10-06T09:00:00+00:00"}])
            self.assertEqual(report["models"], [{"name": "Org/Big", "source": "fetched", "fetch": "copying"},
                                                {"name": "mine", "source": "uploaded", "fetch": None}])
            self.assertEqual(report["clients"], {"codex": "linked", "claude": "unlinked", "opencode": "unlinked"})
            self.assertEqual(report["capacity_blocks"], [{
                "reservation_id": "cr-0123456789abcdef0", "instance_type": "p5en.48xlarge", "zone": f"{REGION}a",
                "state": "active", "start": "2026-10-06T11:30:00Z", "end": "2026-10-07T11:30:00Z", "available": 1}])
            self.assertEqual((report["runs"], report["procs"]), ([], []))
            self.assertEqual(report["errors"], {})

    def test_the_local_sections_show_this_machines_races_and_tunnels(self):
        with make_repo() as repo:
            runs = repo.home / ".local" / "state" / "drip-ai" / "runs"
            runs.mkdir(parents=True)
            (runs / "r1.json").write_text(json.dumps({
                "run_id": "20261009T170203Z-a1b2c3", "status": "won", "pid": 2147483647, "pid_start": "x",
                "winner": "drip-ai-h200-us-east-2-1", "winner_region": "us-east-2",
                "lanes": [{"name": "drip-ai-h200-us-east-2-1", "region": "us-east-2", "state": "IN_SERVICE",
                           "shown": "InService"}]}))
            procs_dir = repo.home / ".local" / "state" / "drip-ai" / "procs"
            procs_dir.mkdir(parents=True)
            (procs_dir / "2147483647.json").write_text(json.dumps({
                "pid": 2147483647, "start_time": "x", "port": 8002, "endpoint": "drip-ai-h200-us-east-2-1",
                "region": "us-east-2", "provider": "drip-ai-us-east-2"}))
            report = self.status(repo)
            self.assertEqual(report["runs"], [{
                "run_id": "20261009T170203Z-a1b2c3", "status": "won", "running": False,
                "winner": "drip-ai-h200-us-east-2-1", "winner_region": "us-east-2",
                "lanes": [{"name": "drip-ai-h200-us-east-2-1", "region": "us-east-2", "state": "IN_SERVICE",
                           "status": "InService"}]}])
            self.assertEqual(report["procs"], [{"pid": 2147483647, "port": 8002, "endpoint": "drip-ai-h200-us-east-2-1",
                                                "region": "us-east-2", "provider": "drip-ai-us-east-2",
                                                "running": False}])

    def test_an_unreadable_section_is_null_with_its_reason(self):
        with make_repo(failing=True) as repo:
            report = self.status(repo)
            self.assertIsNone(report["models"])
            self.assertIn("NoSuchBucket", report["errors"]["models"])
            self.assertEqual(report["fetches"], [])  # the fetch stack was never applied
            self.assertNotIn("fetches", report["errors"])
            # A failed lane read leaves its later fields null and names the lane.
            [lane] = report["endpoints"]
            self.assertEqual((lane["instance_type"], lane["served_model"]), ("ml.p5en.48xlarge", None))
            self.assertTrue(report["errors"]["endpoints"].startswith(f"{LANE}: "))
            self.assertEqual(report["clients"]["codex"], "linked")


if __name__ == "__main__":
    unittest.main()
