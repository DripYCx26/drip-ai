"""Pins ec2/capacity.py against stub CLIs: the read-only search with zone, subnet, and quota flags,
the gapless chain, and a buy that needs --yes and one offering id."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "testing"))

from cli_stubs import StubRepo  # noqa: E402

NOW = 1790000000  # 2026-09-21T14:13:20Z
MUTATING = {"purchase-capacity-block", "run-instances", "create-tags", "send-command"}


def offering(oid, zone, start, end, itype="p5en.48xlarge", hours=24, fee="1700.00"):
    return {"CapacityBlockOfferingId": oid, "InstanceType": itype, "AvailabilityZone": zone, "InstanceCount": 1,
            "StartDate": start, "EndDate": end, "CapacityBlockDurationHours": hours, "UpfrontFee": fee,
            "CurrencyCode": "USD"}


def script_region(repo, region="us-east-1", offerings=(), held=(), quota=1536.0):
    repo.stub("aws", "ec2", "describe-availability-zones", region, stdout=json.dumps({"AvailabilityZones": [
        {"ZoneName": f"{region}a", "OptInStatus": "opt-in-not-required"},
        {"ZoneName": f"{region}-atl-2a", "OptInStatus": "not-opted-in"}]}))
    repo.stub("aws", "ec2", "describe-vpcs", region, stdout=json.dumps({"Vpcs": [{"VpcId": "vpc-0123456789abcdef0"}]}))
    repo.stub("aws", "ec2", "describe-subnets", region,
              stdout=json.dumps({"Subnets": [{"SubnetId": "subnet-01", "AvailabilityZone": f"{region}a"}]}))
    repo.stub("aws", "service-quotas", "list-service-quotas", "ec2", region, stdout=json.dumps({"Quotas": [
        {"QuotaName": "All P5 Capacity Block vCPUs", "Value": quota}]}))
    repo.stub("aws", "ec2", "describe-capacity-block-offerings", region, "p5en.48xlarge",
              stdout=json.dumps({"CapacityBlockOfferings": list(offerings)}))
    repo.stub("aws", "ec2", "describe-capacity-block-offerings", region,
              stdout=json.dumps({"CapacityBlockOfferings": []}))
    repo.stub("aws", "ec2", "describe-capacity-reservations", region,
              stdout=json.dumps({"CapacityReservations": list(held)}))


class CapacityTest(unittest.TestCase):
    def run_capacity(self, repo, *args):
        return subprocess.run([sys.executable, "-I", str(repo.root / "ec2" / "capacity.py"), *args],
                              capture_output=True, text=True, timeout=60,
                              env=repo._env({"DRIP_AI_NOW_EPOCH": str(NOW)}))

    def test_search_reads_only_and_flags_zones_subnets_and_quota(self):
        with StubRepo() as repo:
            script_region(repo, offerings=[
                offering("cb-0000000000000002", "us-east-1-atl-2a", "2026-09-22T11:30:00Z", "2026-09-23T11:30:00Z"),
                offering("cb-0000000000000001", "us-east-1a", "2026-09-21T11:30:00Z", "2026-09-22T11:30:00Z"),
                offering("cb-0000000000000003", "us-east-1a", "2026-09-21T11:30:00Z", "2026-09-22T11:30:00Z",
                         itype="u-p6e-gb200x72"),
            ], quota=96.0)
            result = self.run_capacity(repo, "search", "--class", "h200", "--regions", "us-east-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [line for line in result.stdout.splitlines() if line.startswith("cb-")]
            self.assertEqual([line.split()[0] for line in lines], ["cb-0000000000000001", "cb-0000000000000002"])
            self.assertIn("[zone opt-in-not-required; quota 96 vCPU does not cover 192]", lines[0])
            self.assertIn("zone not-opted-in; needs subnet", lines[1])
            self.assertIn("1700.00 USD", lines[0])
            self.assertIn("Nothing was bought.", result.stdout)
            self.assertFalse({c.argv[1] for c in repo.calls() if c.tool == "aws"} & MUTATING)
            searched = [c.argv for c in repo.calls() if "describe-capacity-block-offerings" in c.argv]
            self.assertEqual(sorted(a[a.index("--instance-type") + 1] for a in searched),
                             ["p5e.48xlarge", "p5en.48xlarge"])
            [first] = [a for a in searched if "p5en.48xlarge" in a]
            self.assertEqual(first[first.index("--start-date-range") + 1], "2026-09-21T14:13:20Z")
            self.assertEqual(first[first.index("--end-date-range") + 1], "2026-09-29T14:13:20Z")

    def test_the_chain_joins_held_blocks_and_offers_and_names_each_gap(self):
        held = [{"CapacityReservationId": "cr-0123456789abcdef0", "AvailabilityZone": "us-east-1a",
                 "InstanceType": "p5.48xlarge", "State": "active", "StartDate": "2026-09-21T11:30:00Z",
                 "EndDate": "2026-09-22T11:30:00Z", "AvailableInstanceCount": 0}]
        offers = [offering("cb-0000000000000001", "us-east-1a", "2026-09-22T11:30:00Z", "2026-09-23T11:30:00Z"),
                  offering("cb-0000000000000002", "us-east-1a", "2026-09-24T11:30:00Z", "2026-09-25T11:30:00Z")]
        with StubRepo() as repo:
            script_region(repo, offerings=offers, held=held)
            result = self.run_capacity(repo, "chain", "--class", "h200", "--regions", "us-east-1", "--hours", "96")
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = [line.split("  ")[1].split()[0] for line in result.stdout.splitlines() if " to " in line[:45]]
            self.assertEqual(rows, ["held", "offer", "GAP:", "offer", "GAP:"])
            self.assertIn("2026-09-23T11:30:00Z to 2026-09-24T11:30:00Z  GAP", result.stdout)
            self.assertFalse({c.argv[1] for c in repo.calls() if c.tool == "aws"} & MUTATING)

    def test_buy_needs_yes_and_one_offering_id(self):
        with StubRepo() as repo:
            repo.stub("aws", "ec2", "purchase-capacity-block",
                      stdout=json.dumps({"CapacityReservation": {"CapacityReservationId": "cr-0aaa"}}))
            refused = self.run_capacity(repo, "buy", "cb-0000000000000001", "--region", "us-east-1")
            self.assertEqual(refused.returncode, 1)
            self.assertIn("rerun with --yes once you approve that fee", refused.stderr)
            self.assertEqual(self.run_capacity(repo, "buy", "not-an-id", "--region", "us-east-1", "--yes").returncode, 1)
            self.assertEqual([c for c in repo.calls() if c.tool == "aws"], [])
            bought = self.run_capacity(repo, "buy", "cb-0000000000000001", "--region", "us-east-1", "--yes")
            self.assertEqual(bought.returncode, 0, bought.stderr)
            [call] = [c.argv for c in repo.calls() if c.tool == "aws"]
            self.assertEqual(call[:4], ["ec2", "purchase-capacity-block", "--capacity-block-offering-id",
                                        "cb-0000000000000001"])
            self.assertIn("capacity reservation cr-0aaa", bought.stdout)


if __name__ == "__main__":
    unittest.main()
