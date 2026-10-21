"""Pins `bin/drip-ai plan` (shared/drip_ai/cli.py) against stub CLIs: the serving plan with each
value's layer and reason, the capacity options before a race, and no mutating call."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "testing"))

from cli_stubs import StubRepo  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ACCOUNT = "111122223333"
BUCKET = f"drip-ai-models-{ACCOUNT}"
NAME = "Org/Example"
MUTATING = {"create-model", "create-endpoint-config", "create-endpoint", "delete-endpoint", "purchase-capacity-block",
            "put-object", "delete-object", "create-log-group", "put-retention-policy"}


def make_repo(user=None):
    config = json.loads((FIXTURES / "example_mla_dsa.json").read_text())
    profile = {config["model_type"]: {"vllm_args": ["--enable-auto-tool-choice", "--tool-call-parser", "example"],
                                      "source": "https://example.com/card"}}
    architectures = {"vllm_ref": "v0.30.0", "architectures": [config["architectures"][0]]}
    repo = StubRepo(files={"shared/model-profiles/profiles.json": json.dumps(profile),
                           "shared/model-profiles/vllm-architectures.json": json.dumps(architectures)})
    if user:
        path = repo.home / ".config" / "drip-ai" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(user))
        path.chmod(0o600)
    repo.stub("aws", "sts", "get-caller-identity", stdout=json.dumps({"Account": ACCOUNT, "Arn": "arn:aws:iam::x:user/a"}))
    prefix = f"models/{NAME}/"
    repo.stub("aws", "s3api", "list-objects-v2", BUCKET, stdout=json.dumps({"Contents": [
        {"Key": prefix + "config.json", "Size": 1000},
        {"Key": prefix + "model-00001.safetensors", "Size": 756 * 10**9}]}))
    repo.stub("aws", "s3", "cp", f"s3://{BUCKET}/{prefix}config.json", stdout=json.dumps(config))
    return repo


class PlanTest(unittest.TestCase):
    def plan(self, repo, *args):
        return subprocess.run([sys.executable, "-I", str(repo.root / "shared" / "drip_ai" / "cli.py"), "plan", NAME,
                               "--home-region", "us-east-1", *args], capture_output=True, text=True, timeout=120,
                              env=repo._env({}))

    def test_plan_explains_each_value_and_reads_only(self):
        with make_repo() as repo:
            result = self.plan(repo, "--explain")
            self.assertEqual(result.returncode, 0, result.stderr)
            out = result.stdout
            self.assertIn("serve.dcp = 8  derived  (smallest DCP with pool >= 2x1048576x1.10", out)
            self.assertIn("Skipping h100 (ml.p5.48xlarge): 756.0 GB of weights leave no KV-cache memory", out)
            self.assertIn("builtin (shared/config/defaults.json) < derived", out)
            self.assertIn("serve.max_model_len = 1048576  derived  (config.json max_position_embeddings)", out)
            self.assertIn("race.order us-east-1 ml.p5en.48xlarge chance 0.50  builtin", out)
            self.assertIn(f"SageMaker race: bin/drip-ai sagemaker start {NAME}", out)
            self.assertFalse({c.argv[1] for c in repo.calls() if c.tool == "aws"} & MUTATING)

    def test_a_set_value_wins_and_says_where_it_came_from(self):
        with make_repo() as repo:
            result = self.plan(repo, "--set", "serve.max_model_len=262144", "--explain")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("serve.max_model_len = 262144  cli  (set explicitly)", result.stdout)

    def test_capacity_blocks_come_first_when_the_substrate_is_configured(self):
        user = {"substrates": ["ec2-capacity-block", "sagemaker"], "race": {"classes": ["h200"]}}
        with make_repo(user) as repo:
            repo.stub("aws", "ec2", "describe-capacity-reservations", stdout=json.dumps({"CapacityReservations": []}))
            repo.stub("aws", "ec2", "describe-availability-zones", stdout=json.dumps({"AvailabilityZones": [
                {"ZoneName": "us-east-1a", "OptInStatus": "opt-in-not-required"}]}))
            repo.stub("aws", "ec2", "describe-vpcs", stdout=json.dumps({"Vpcs": []}))
            repo.stub("aws", "service-quotas", "list-service-quotas", stdout=json.dumps({"Quotas": []}))
            repo.stub("aws", "ec2", "describe-capacity-block-offerings", "p5en.48xlarge", stdout=json.dumps({
                "CapacityBlockOfferings": [{"CapacityBlockOfferingId": "cb-0000000000000001", "InstanceType":
                                            "p5en.48xlarge", "AvailabilityZone": "us-east-1a",
                                            "StartDate": "2026-10-10T11:30:00Z", "EndDate": "2026-10-11T11:30:00Z",
                                            "CapacityBlockDurationHours": 24, "UpfrontFee": "1700.00",
                                            "CurrencyCode": "USD"}]}))
            repo.stub("aws", "ec2", "describe-capacity-block-offerings", stdout=json.dumps({"CapacityBlockOfferings": []}))
            result = self.plan(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = result.stdout
            self.assertLess(out.index("1) Held capacity blocks: none active"), out.index("2) Capacity blocks on offer"))
            self.assertLess(out.index("cb-0000000000000001"), out.index("SageMaker race:"))
            self.assertRegex(out, r"EC2 on p5en\.48xlarge: max_model_len 1048576, DCP 8; vllm_extra_args: .*--async-scheduling")
            self.assertFalse({c.argv[1] for c in repo.calls() if c.tool == "aws"} & MUTATING)


if __name__ == "__main__":
    unittest.main()
