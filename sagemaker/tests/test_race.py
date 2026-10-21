"""Pins sagemaker/race.py, the SageMaker race state machine, through `sagemaker/start.sh` against stub
CLIs and the fake SageMaker API: lane life cycles, error classes, credentials, regions, the lease,
signals, and resume."""

import json
import re
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

from drip_ai.logsignals import STREAM_LIMIT  # noqa: E402

from start_fixtures import (  # noqa: E402
    ACCOUNT, ARN, BUCKET, CAPACITY, DASHBOARD_ENV, EXPIRED, H1, H2, H3, H4, LANES, LEASE_KEY, MUTATING_AWS, NAME, NOW,
    OTHER_ARN, P5E_TYPE, P5EN_TYPE, PINNED, PRECONDITION_FAILED, SCRIPT, TEARDOWN, WEIGHTS, creates, deletes,
    endpoints_left, lane, lane_arn, lane_operations, lease, make_repo, models_made, operations, option, run_start,
    script_foundation, script_start_cli, status_object)

TIMEOUT = 'Read timeout on endpoint URL: "https://api.sagemaker.us-east-1.amazonaws.com/"\n'
QUOTA = ("An error occurred (ResourceLimitExceeded) when calling the CreateEndpoint operation: The account-level"
         " service limit 'ml.p5en.48xlarge for endpoint usage' is 2 Instances, with current utilization of 2"
         " Instances\n")
UNKNOWN = "An error occurred (InternalFailure) when calling the DescribeEndpoint operation (reached max retries: 2)\n"


def iso(epoch):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds")


def learned(repo, kind):
    path = repo.home / ".local" / "state" / "drip-ai" / "observations.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if json.loads(line)["kind"] == kind]


def journals(repo):
    folder = repo.home / ".local" / "state" / "drip-ai" / "runs"
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


class RaceTest(unittest.TestCase):
    """The race loop on a virtual clock (one second per poll), approved with yes, and --no-watch."""

    def race(self, repo, *args, env=None, **race):
        script_start_cli(repo, status=status_object(WEIGHTS), **race)
        return run_start(repo, NAME, "--no-watch", *args, **(env or {}))

    def test_creates_every_lane_once_with_the_five_tags_and_keeps_the_first_in_service(self):
        with make_repo() as repo:
            result = self.race(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Racing 4 lanes in us-east-1; polling every 1s. Ctrl-C deletes every lane of this run.",
                          result.stdout)
            found = creates(repo)
            self.assertEqual([name for name, _, _ in found], list(LANES))
            run_ids = {tags.pop("RunId") for _, _, tags in found}
            self.assertEqual(len(run_ids), 1)
            self.assertEqual([tags for _, _, tags in found], [{"Project": "drip-ai", "ManagedBy": "drip-ai",
                                                               "Stack": "sagemaker", "StartedBy": ARN}] * 4)
            self.assertIn(f"{H1} (h200 in us-east-1) is InService and wins; deleting the other lanes.", result.stdout)
            self.assertEqual(deletes(repo), [H2, H3, H4])
            self.assertEqual(list(endpoints_left(repo)), [H1])
            self.assertIn(f"Endpoint {H1} (h200 in us-east-1) serves models/{NAME}.", result.stdout)
            [journal] = journals(repo)
            self.assertEqual((journal["status"], journal["winner"]), ("won", H1))
            self.assertIn(("aws", "delete-object"), operations(repo))  # the lease goes back

    def test_a_capacity_failure_without_a_log_stream_is_deleted_and_created_again(self):
        scripts = {H1: [["Creating", ["Failed", CAPACITY]], ["Creating", "Creating", "InService"]],
                   **{name: [["Creating"] * 6 + ["InService"]] for name in LANES[1:]}}
        with make_repo() as repo:
            result = self.race(repo, scripts=scripts)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(lane_operations(repo, H1), ["create-endpoint", "delete-endpoint", "create-endpoint"])
            self.assertIn(f"After 0 min: {H1} retry 1 (capacity)\n", result.stdout)
            self.assertIn(f"{H1} (h200 in us-east-1) is InService and wins", result.stdout)
            self.assertEqual(list(endpoints_left(repo)), [H1])
            attempts = learned(repo, "capacity_attempt")
            self.assertEqual([a["outcome"] for a in attempts if a["outcome"] != "placed"], ["insufficient"])
            self.assertEqual(attempts[0]["pool"], [P5EN_TYPE, P5E_TYPE])

    def test_a_failure_after_the_container_logged_is_read_recorded_and_never_relaunched(self):
        log = "INFO vLLM API server version 0.11.2\ntorch.OutOfMemoryError: CUDA out of memory. Tried to allocate"
        cases = {
            "a container error": {"status": "Failed", "reason": "CUDA out of memory", "streams": True, "log": log},
            "a capacity reason with a log stream": {"status": "Failed", "reason": CAPACITY, "streams": True, "log": log},
        }
        for label, failed in cases.items():
            scripts = {H1: [["Creating", failed]], **{name: [["Creating", "Creating", "InService"]] for name in LANES[1:]}}
            with self.subTest(label), make_repo() as repo:
                result = self.race(repo, scripts=scripts)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"{H1} (h200 in us-east-1) failed: {failed['reason']} [oom] (logs:"
                              f" /aws/sagemaker/Endpoints/{H1}); not retrying it", result.stderr)
                self.assertEqual(lane_operations(repo, H1), ["create-endpoint", "delete-endpoint"])
                self.assertNotIn("retry", result.stdout)
                self.assertIn(f"{H2} (h200 in us-east-1) is InService and wins", result.stdout)
                startups = learned(repo, "startup")
                self.assertEqual([(s["outcome"], s["vllm_version"]) for s in startups if s["outcome"] != "ok"],
                                 [("oom", "0.11.2")])
                self.assertEqual({s["image_digest"] for s in startups}, {PINNED})

    def test_a_stream_an_earlier_endpoint_left_does_not_make_a_capacity_failure_a_crash(self):
        old = [{"name": "main/i-0old/AllTraffic", "created": iso(NOW - 86400),
                "log": "api_server.py: error: unrecognized arguments: --bogus"}]
        scripts = {H1: [["Creating", ["Failed", CAPACITY]], ["Creating", "Creating", "InService"]],
                   **{name: [["Creating"] * 6 + ["InService"]] for name in LANES[1:]}}
        with make_repo() as repo:
            result = self.race(repo, scripts=scripts, old_streams={H1: old})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(lane_operations(repo, H1), ["create-endpoint", "delete-endpoint", "create-endpoint"])
            self.assertIn(f"After 0 min: {H1} retry 1 (capacity)\n", result.stdout)
            self.assertNotIn("not retrying it", result.stderr)
            self.assertEqual([s["outcome"] for s in learned(repo, "startup")], ["ok"])

    def test_a_failed_lane_is_read_from_its_own_stream_not_an_earlier_one(self):
        old = [{"name": "main/i-0old/AllTraffic", "created": iso(NOW - 86400),
                "log": "api_server.py: error: unrecognized arguments: --bogus"}]
        failed = {"status": "Failed", "reason": "CUDA out of memory", "streams": True,
                  "log": "torch.OutOfMemoryError: CUDA out of memory."}
        scripts = {H1: [["Creating", failed]], **{name: [["Creating", "Creating", "InService"]] for name in LANES[1:]}}
        with make_repo() as repo:
            result = self.race(repo, scripts=scripts, old_streams={H1: old})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"{H1} (h200 in us-east-1) failed: CUDA out of memory [oom]", result.stderr)
            [read] = [c for c in repo.calls() if c.argv[1:2] == ["get-log-events"]
                      and H1 in (option(c.argv, "--log-group-name") or "")]
            self.assertEqual(option(read.argv, "--log-stream-name"), "main/i-0abc/AllTraffic")

    def test_every_lane_failing_for_another_reason_deletes_them_and_exits_1(self):
        scripts = {name: [["Creating", ["Failed", "CUDA out of memory"]]] for name in LANES}
        with make_repo() as repo:
            result = self.race(repo, scripts=scripts)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertEqual(sorted(deletes(repo)), sorted(LANES))
            self.assertEqual(endpoints_left(repo), {})
            self.assertIn("every lane is out of the race; deleting every lane of this run", result.stderr)
            for name in LANES:
                self.assertIn(f"  {name} (h200 in us-east-1): 1 created; last failure: CUDA out of memory\n",
                              result.stdout)
            self.assertIn("Every lane of this run is deleted; nothing bills for them.", result.stdout)
            self.assertIn(f"Start again: bin/drip-ai sagemaker start {NAME}", result.stdout)

    def test_the_first_in_service_lane_wins_and_a_creating_loser_is_deleted_once_it_can_be(self):
        with make_repo() as repo:
            result = self.race(repo, scripts={H2: [["Creating", "Creating", "Creating", "InService"]]})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"{H1} (h200 in us-east-1) is InService and wins", result.stdout)
            self.assertEqual(result.stderr.count(f"{H2} is Creating, and SageMaker deletes it only after that ends;"
                                                 " asking again every 1s"), 1)
            self.assertEqual(deletes(repo), [H3, H4, H2])
            self.assertEqual(list(endpoints_left(repo)), [H1])

    def test_a_same_named_endpoint_this_run_did_not_create_is_never_deleted_or_chosen(self):
        replacements = {"another ARN": f"arn:aws:sagemaker:us-east-1:444455556666:endpoint/{H1}",
                        "the same ARN, created later": f"arn:aws:sagemaker:us-east-1:{ACCOUNT}:endpoint/{H1}"}
        for label, arn in replacements.items():
            with self.subTest(label), make_repo() as repo:
                theirs = {"name": H1, "arn": arn, "created": "2026-10-05T11:30:00.000000+00:00", "status": "InService"}
                scripts = {H1: [["Creating"]], **{name: [["Creating", "Creating", "InService"]] for name in LANES[1:]}}
                result = self.race(repo, scripts=scripts, events={3: [theirs]})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"{H1} is now {arn} created 2026-10-05T11:30:00.000000+00:00, which this run did"
                              " not create; leaving it alone", result.stderr)
                self.assertEqual(lane_operations(repo, H1), ["create-endpoint"])
                self.assertIn(f"{H2} (h200 in us-east-1) is InService and wins", result.stdout)
                self.assertEqual({n: e["arn"] for n, e in endpoints_left(repo).items()},
                                 {H1: arn, H2: f"arn:aws:sagemaker:us-east-1:{ACCOUNT}:endpoint/{H2}"})

    def test_no_winner_by_the_cap_deletes_every_lane_and_sums_up_each(self):
        scripts = {name: [["Creating", ["Failed", CAPACITY]]] for name in LANES}
        with make_repo() as repo:
            result = self.race(repo, env={"DRIP_AI_RACE_CAP_SECONDS": "2"}, scripts=scripts)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("no lane was InService after 2 seconds; deleting every lane of this run", result.stderr)
            self.assertEqual(endpoints_left(repo), {})
            for name in LANES:
                self.assertRegex(result.stdout, rf"  {name} \(h200 in us-east-1\): [1-9][0-9]* created; last"
                                                 f" failure: {re.escape(CAPACITY)}\n")
            self.assertIn("Every lane of this run is deleted; nothing bills for them.", result.stdout)

    def test_a_create_that_reported_an_error_but_went_through_is_adopted_not_made_again(self):
        slow = {name: [["Creating"] * 4 + ["InService"]] for name in LANES[1:]}
        cases = {"followed to the win": (slow, H1, [H1]), "deleted once another lane won first": (None, H2, [H2])}
        for label, (scripts, winner, left) in cases.items():
            with self.subTest(label), make_repo() as repo:
                result = self.race(repo, scripts=scripts, create_errors={H1: TIMEOUT})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"could not create the endpoint for {H1} (Read timeout on endpoint URL", result.stderr)
                self.assertIn(f"{H1} exists from this run's earlier create, which reported an error; following it",
                              result.stderr)
                self.assertIn(f"{winner} (h200 in us-east-1) is InService and wins", result.stdout)
                self.assertEqual(list(endpoints_left(repo)), left)
                self.assertEqual(lane_operations(repo, H1).count("create-endpoint"), 1)

    def test_a_taken_name_is_followed_only_when_this_run_created_it(self):
        from datetime import datetime, timezone
        from start_fixtures import NOW, lane_arn

        def iso(epoch):
            return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds")

        cases = {
            "no RunId tag": {"config": "drip-ai-c-00000000-1", "created": iso(NOW + 30),
                             "tags": [{"Key": "StartedBy", "Value": ARN}]},
            "another identity's": {"config": "drip-ai-c-00000000-1", "created": iso(NOW + 30),
                                   "tags": [{"Key": "StartedBy", "Value": OTHER_ARN}]},
        }
        for label, fields in cases.items():
            with self.subTest(label), make_repo() as repo:
                theirs = {"name": H1, "arn": lane_arn(H1), "status": "InService", **fields}
                result = self.race(repo, events={2: [theirs]})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"{H1} exists, and this run did not create it; leaving it alone", result.stderr)
                self.assertNotIn("following it", result.stderr)
                self.assertEqual(lane_operations(repo, H1), [])
                self.assertEqual(endpoints_left(repo)[H1]["created"], fields["created"])
                self.assertIn(f"{H2} (h200 in us-east-1) is InService and wins", result.stdout)

    def earlier_failure(self, config, tags):
        return {H1: {"arn": lane_arn(H1), "created": iso(NOW - 7200), "region": "us-east-1", "config": config,
                     "steps": [{"status": "Failed", "streams": True,
                                "reason": "The primary container for production variant main did not pass the ping"
                                          " health check."}], "step": 0, "tags": tags}}

    def test_an_earlier_runs_failed_endpoint_is_deleted_and_relaunched_once_its_configuration_changed(self):
        tags = [{"Key": "Project", "Value": "drip-ai"}, {"Key": "ManagedBy", "Value": "drip-ai"},
                {"Key": "StartedBy", "Value": ARN}, {"Key": "RunId", "Value": "20261009T000000Z-abcdef"}]
        slow = {name: [["Creating"] * 6 + ["InService"]] for name in LANES[1:]}
        with make_repo() as repo:
            result = self.race(repo, scripts=slow, endpoints=self.earlier_failure("drip-ai-c-00000000-1", tags))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(lane_operations(repo, H1), ["delete-endpoint", "create-endpoint"])
            self.assertIn(f"{H1} failed in an earlier run (The primary container for production variant main did not"
                          " pass the ping health check.) with configuration drip-ai-c-00000000-1, which has changed"
                          " since; deleting it and creating the lane with drip-ai-c-", result.stderr)
            self.assertIn(f"{H1} (h200 in us-east-1) is InService and wins", result.stdout)
            config = endpoints_left(repo)[H1]["config"]
            self.assertTrue(config.startswith("drip-ai-c-") and config != "drip-ai-c-00000000-1", config)

    def test_a_failed_endpoint_is_never_relaunched_with_the_same_configuration_or_when_made_by_hand(self):
        ours = [{"Key": "Project", "Value": "drip-ai"}, {"Key": "ManagedBy", "Value": "drip-ai"},
                {"Key": "StartedBy", "Value": ARN}, {"Key": "RunId", "Value": "20261009T000000Z-abcdef"}]
        cases = {
            "the same configuration": ("@lane-config", ours,
                                       f"{H1} failed in an earlier run with this same configuration (The primary"
                                       " container for production variant main did not pass the ping health check.);"
                                       " not relaunching it"),
            "made by hand": ("drip-ai-c-00000000-1", [{"Key": "StartedBy", "Value": ARN}],
                             f"{H1} exists, and this run did not create it; leaving it alone"),
        }
        for label, (config, tags, message) in cases.items():
            with self.subTest(label), make_repo() as repo:
                result = self.race(repo, endpoints=self.earlier_failure(config, tags))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(lane_operations(repo, H1), [])
                self.assertEqual(endpoints_left(repo)[H1]["arn"], lane_arn(H1))
                self.assertIn(f"{H2} (h200 in us-east-1) is InService and wins", result.stdout)

    def crowded_log_group(self, before):
        """More streams than one page holds, all older than BEFORE, named to sort ahead of the
        endpoint's own stream by name, as AllTraffic/i-... streams of earlier boots do."""
        return [{"name": f"AllTraffic/i-{n:017x}", "created": iso(before - 3600 - n)}
                for n in range(STREAM_LIMIT + 10)]

    def test_a_crowded_log_group_still_finds_the_lanes_own_stream_after_a_failure(self):
        log = "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate"
        cases = {
            "a capacity reason with its own stream": ({"status": "Failed", "reason": CAPACITY, "streams": True,
                                                       "log": log}, ["create-endpoint", "delete-endpoint"]),
            "a capacity reason with no stream of its own": (["Failed", CAPACITY],
                                                            ["create-endpoint", "delete-endpoint", "create-endpoint"]),
        }
        for label, (failed, expected) in cases.items():
            scripts = {H1: [["Creating", failed], ["Creating", "Creating", "InService"]],
                       **{name: [["Creating"] * 6 + ["InService"]] for name in LANES[1:]}}
            with self.subTest(label), make_repo() as repo:
                result = self.race(repo, scripts=scripts, old_streams={H1: self.crowded_log_group(NOW)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(lane_operations(repo, H1), expected)
                reads = [c for c in repo.calls() if c.argv[1:2] == ["describe-log-streams"]
                         and H1 in (option(c.argv, "--log-group-name") or "")]
                self.assertTrue(reads)
                self.assertTrue(all(option(c.argv, "--order-by") == "LastEventTime" and "--descending" in c.argv
                                    for c in reads), [c.argv for c in reads])
                if len(expected) == 2:
                    self.assertIn(f"{H1} (h200 in us-east-1) failed: {CAPACITY} [oom]", result.stderr)
                    self.assertNotIn("retry", result.stdout)
                else:
                    self.assertIn(f"After 0 min: {H1} retry 1 (capacity)\n", result.stdout)
                    self.assertNotIn("not retrying it", result.stderr)

    def test_an_earlier_runs_failed_endpoint_in_a_crowded_log_group_is_classified_not_left_waiting(self):
        ours = [{"Key": "Project", "Value": "drip-ai"}, {"Key": "ManagedBy", "Value": "drip-ai"},
                {"Key": "StartedBy", "Value": ARN}, {"Key": "RunId", "Value": "20261009T000000Z-abcdef"}]
        slow = {name: [["Creating"] * 6 + ["InService"]] for name in LANES[1:]}
        crash = self.earlier_failure("@lane-config", ours)
        capacity = self.earlier_failure("@lane-config", ours)
        capacity[H1]["steps"] = [{"status": "Failed", "reason": CAPACITY}]
        cases = {
            "it started a container": (crash, [], f"{H1} failed in an earlier run with this same configuration"),
            "it failed for capacity before any container": (
                capacity, ["delete-endpoint", "create-endpoint"],
                f"{H1} failed in an earlier run ({CAPACITY}) with a capacity failure before any container started;"),
        }
        for label, (endpoints, expected, message) in cases.items():
            with self.subTest(label), make_repo() as repo:
                result = self.race(repo, scripts=slow, endpoints=endpoints,
                                   old_streams={H1: self.crowded_log_group(NOW - 7200)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(lane_operations(repo, H1), expected)

    def test_an_earlier_runs_failed_endpoint_that_cannot_be_read_is_named_once_and_left_alone(self):
        ours = [{"Key": "Project", "Value": "drip-ai"}, {"Key": "ManagedBy", "Value": "drip-ai"},
                {"Key": "StartedBy", "Value": ARN}, {"Key": "RunId", "Value": "20261009T000000Z-abcdef"}]
        unreadable = ("An error occurred (ServiceUnavailableException) when calling the DescribeLogStreams"
                      " operation (reached max retries: 2)\n")
        slow = {name: [["Creating"] * 6 + ["InService"]] for name in LANES[1:]}
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS), scripts=slow,
                             endpoints=self.earlier_failure("@lane-config", ours))
            state = repo.home / "sagemaker.json"
            state.write_text(json.dumps({**json.loads(state.read_text()), "log_errors": {H1: [unreadable] * 50}}))
            result = run_start(repo, NAME, "--no-watch")
            self.assertEqual(result.returncode, 0, result.stderr)
            message = (f"{H1} exists and failed, but its tags or log streams could not be read, so this run cannot"
                       " tell whether to replace it; leaving it alone and reading again every 1s")
            self.assertEqual(result.stderr.count(message), 1, result.stderr)
            self.assertEqual(lane_operations(repo, H1), [])
            self.assertEqual(endpoints_left(repo)[H1]["arn"], lane_arn(H1))
            self.assertIn(f"{H2} (h200 in us-east-1) is InService and wins", result.stdout)

    def test_an_unknown_describe_never_creates(self):
        with make_repo() as repo:
            result = self.race(repo, describe_errors={H1: [UNKNOWN] * 50})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(lane_operations(repo, H1), [])
            self.assertIn(f"{H2} (h200 in us-east-1) is InService and wins", result.stdout)

    def test_a_quota_refusal_parks_the_lane(self):
        with make_repo() as repo:
            result = self.race(repo, create_refusals={H1: [QUOTA]})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"{H1}: SageMaker refused to create the endpoint for quota (An error occurred"
                          " (ResourceLimitExceeded)", result.stderr)
            self.assertIn("parking the lane", result.stderr)
            self.assertEqual(lane_operations(repo, H1), ["create-endpoint"])
            self.assertIn(f"{H2} (h200 in us-east-1) is InService and wins", result.stdout)
            self.assertEqual([a["outcome"] for a in learned(repo, "capacity_attempt")][:1], ["quota"])

    def test_a_loser_still_creating_after_the_wait_is_named_with_the_winner(self):
        with make_repo() as repo:
            result = self.race(repo, env={"DRIP_AI_STOP_WAIT_SECONDS": "2"}, scripts={H2: [["Creating"]]})
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"{H1} serves, but after 2 seconds these endpoints of this run still exist and bill"
                          f" until {TEARDOWN}: {H1} (InService) {H2} (Creating)\n", result.stderr)
            self.assertEqual(deletes(repo), [H3, H4])
            self.assertIn(("aws", "delete-object"), operations(repo))  # the lease goes back

    def test_the_resources_are_created_only_when_absent_and_reused_by_hash(self):
        with make_repo() as repo:
            result = self.race(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            model_creates = [c for c in repo.calls() if c.argv[1:2] == ["create-model"]]
            self.assertEqual(len(model_creates), 1)
            describes = [c for c in repo.calls() if c.argv[1:2] == ["describe-model"]]
            self.assertEqual(len(describes), 4)  # every lane checks; only the first finds it absent
            [path] = [c.argv[c.argv.index("--primary-container") + 1] for c in model_creates]
            self.assertTrue(path.startswith("file://"))
            self.assertFalse(Path(path[len("file://"):]).exists())  # the 0600 request file is gone


class RegionTest(unittest.TestCase):
    """Two regions: lanes in both, models per region, the first winner kept."""

    def test_two_regions_race_together_and_the_first_winner_stays(self):
        use2 = "us-east-2"
        user = {"regions": [use2, "us-east-1"], "race": {"classes": ["h200"], "max_lanes": 4}}
        u2 = [lane(region=use2, number=n) for n in (1, 2)]
        u1 = [lane(region="us-east-1", number=n) for n in (1, 2)]
        scripts = {**{name: [["Creating", "Creating", "Creating", "InService"]] for name in u2},
                   **{name: [["Creating", "InService"]] for name in u1}}
        with make_repo(user_config=user) as repo:
            script_foundation(repo, region=use2)
            script_start_cli(repo, status=status_object(WEIGHTS), scripts=scripts)
            bucket2 = f"drip-ai-models-{ACCOUNT}-{use2}"
            repo.stub("aws", "s3", "cp", f"s3://{bucket2}/models/{NAME}/drip-ai-fetch.json",
                      stdout=json.dumps(status_object(WEIGHTS)))
            result = run_start(repo, NAME, "--no-watch", "--yes")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([name for name, _, _ in creates(repo)], [u2[0], u1[0], u2[1], u1[1]])
            self.assertIn(f"{u1[0]} (h200 in us-east-1) is InService and wins", result.stdout)
            self.assertEqual(sorted(deletes(repo)), sorted([u2[0], u2[1], u1[1]]))
            self.assertEqual(list(endpoints_left(repo)), [u1[0]])
            regions = {m["region"]: m for m in models_made(repo).values()}
            self.assertEqual(set(regions), {use2, "us-east-1"})
            self.assertEqual(regions[use2]["body"]["Image"],
                             f"763104351884.dkr.ecr.{use2}.amazonaws.com/vllm@{PINNED}")
            self.assertEqual(regions[use2]["role"], f"arn:aws:iam::{ACCOUNT}:role/drip-ai-sagemaker-execution-{use2}")
            self.assertEqual(regions[use2]["body"]["ModelDataSource"]["S3DataSource"]["S3Uri"],
                             f"s3://{bucket2}/models/{NAME}/")

    def test_a_region_without_the_foundation_or_the_weights_is_skipped(self):
        user = {"regions": ["us-east-2", "us-west-2", "us-east-1"], "race": {"classes": ["h200"]}}
        with make_repo(user_config=user) as repo:
            script_foundation(repo, region="us-west-2", missing=True)
            script_start_cli(repo, status=status_object(WEIGHTS))
            repo.stub("aws", "s3", "cp", f"s3://drip-ai-models-{ACCOUNT}-us-east-2/models/{NAME}/drip-ai-fetch.json",
                      stderr="fatal error: An error occurred (404) when calling the HeadObject operation\n", exit=1)
            result = run_start(repo, NAME, "--no-watch", "--yes")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("us-west-2: skipped; the execution role drip-ai-sagemaker-execution-us-west-2 does not exist;"
                          " apply the foundation there with: DRIP_AI_REGION=us-west-2 bin/drip-ai setup", result.stdout)
            self.assertIn(f"us-east-2: skipped; no complete copy in s3://drip-ai-models-{ACCOUNT}-us-east-2/models/{NAME}/",
                          result.stdout)
            self.assertEqual({name.split("h200-")[1].rsplit("-", 1)[0] for name, _, _ in creates(repo)}, {"us-east-1"})

    def test_a_denied_log_simulation_refuses_before_anything_is_created(self):
        with make_repo() as repo:
            script_foundation(repo, log_decision="implicitDeny")
            script_start_cli(repo, status=status_object(WEIGHTS))
            result = run_start(repo, NAME, "--no-watch", "--yes")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("the execution role in us-east-1 may not logs:CreateLogStream or logs:PutLogEvents on its"
                          " lanes' log groups", result.stderr)
            self.assertEqual(creates(repo), [])
            self.assertEqual(models_made(repo), {})


class CredentialsTest(unittest.TestCase):
    """Expired credentials pause the race; it resumes only for the same base principal."""

    def run_expiring(self, repo, arn_after=None, arn=ARN):
        expired = {"from_poll": 3, "sts_calls": 2, **({"arn_after": arn_after} if arn_after else {})}
        scripts = {name: [["Creating"] * 3 + ["InService"]] for name in LANES}
        script_start_cli(repo, status=status_object(WEIGHTS), scripts=scripts, expired=expired)
        return run_start(repo, NAME, "--no-watch", "--yes")

    def test_expired_credentials_pause_every_change_then_the_race_resumes(self):
        with make_repo() as repo:
            result = self.run_expiring(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr.count("Run: aws login"), 1)
            self.assertIn(EXPIRED, result.stderr)
            self.assertIn("Credentials work again", result.stdout)
            calls = repo.calls()
            lists = [i for i, c in enumerate(calls) if c.argv[:2] == ["sagemaker", "list-endpoints"]]
            paused = lists[2]  # the third list call is the first that fails
            resumed = next(i for i, c in enumerate(calls) if i > paused and c.argv[:2] == ["sts", "get-caller-identity"]
                           and c.tool == "aws" and "text" not in c.argv and i > paused + 2)
            window = [c.argv[1] for c in calls[paused:resumed]]
            self.assertFalse(set(window) & MUTATING_AWS, window)
            self.assertIn(f"{H1} (h200 in us-east-1) is InService and wins", result.stdout)
            self.assertEqual([e["event"] for e in learned(repo, "credentials")], ["expired"])

    def test_a_new_session_of_the_same_role_resumes(self):
        alice = f"arn:aws:sts::{ACCOUNT}:assumed-role/Dev/alice-1"
        with make_repo(arn=alice) as repo:
            result = self.run_expiring(repo, arn_after=f"arn:aws:sts::{ACCOUNT}:assumed-role/Dev/alice-2", arn=alice)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Credentials work again", result.stdout)

    def test_another_identity_stops_the_race_without_deleting_anything(self):
        with make_repo() as repo:
            result = self.run_expiring(repo, arn_after=OTHER_ARN)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"the new credentials belong to {OTHER_ARN}, not {ARN}; stopping the race without deleting"
                          " anything", result.stderr)
            self.assertEqual(deletes(repo), [])
            self.assertEqual(sorted(endpoints_left(repo)), sorted(LANES))
            self.assertIn("bin/drip-ai race resume", result.stderr)


class RaceLeaseTest(unittest.TestCase):
    """A race whose lease refresh fails, with a 5-second lease on the virtual clock."""

    def test_a_race_that_cannot_keep_its_lease_stops_creating_and_deletes_its_lanes(self):
        internal = "An error occurred (InternalError) when calling the {} operation: try again\n"
        cases = {
            "refresh failing": (
                dict(exit=254, stderr=internal.format("GetObject")),
                r"the sagemaker-race lease has not been refreshed for [4-9] seconds, and it lapses after 5"
                r" seconds, so another race could start; deleting every lane of this run"),
            "lease taken": (
                dict(stdout='"e2"\n', last_arg_file=lease(OTHER_ARN, "build-box")),
                rf"the sagemaker-race lease is now held by {re.escape(OTHER_ARN)} on build-box \(pid 4242\),"
                " so another race could start; deleting every lane of this run"),
        }
        for label, (get_object, message) in cases.items():
            with self.subTest(label), make_repo() as repo:
                repo.stub("aws", "s3api", "put-object", LEASE_KEY, stdout='"e1"\n', times=2)
                repo.stub("aws", "s3api", "put-object", LEASE_KEY, exit=254,
                          stderr=PRECONDITION_FAILED if label == "lease taken" else internal.format("PutObject"))
                repo.stub("aws", "s3api", "get-object", LEASE_KEY, **get_object)
                script_start_cli(repo, status=status_object(WEIGHTS),
                                 scripts={name: [["Creating"] * 10 + [["Failed", "boot timed out"]]] for name in LANES})
                result = run_start(repo, NAME, "--no-watch", "--yes", DRIP_AI_RACE_LEASE_SECONDS="5")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertRegex(result.stderr, message)
                self.assertNotIn("every lane is out of the race", result.stderr)
                self.assertEqual(len(creates(repo)), 4)
                self.assertEqual(endpoints_left(repo), {})
                self.assertIn("Every lane of this run is deleted; nothing bills for them.", result.stdout)
                self.assertNotIn(("aws", "delete-object"), operations(repo))


class RaceSignalTest(unittest.TestCase):
    """Signals, a closed terminal, a kill, and resume during a race, polling every real second."""

    def start(self, repo, scripts=None):
        script_start_cli(repo, status=status_object(WEIGHTS), scripts=scripts)
        out_path, err_path = repo.home / "out", repo.home / "err"
        out, err = open(out_path, "w"), open(err_path, "w")
        proc = repo.popen(SCRIPT, NAME, "--no-watch", stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                          env=dict(DASHBOARD_ENV, DRIP_AI_TTY=str(repo.home / "tty"), DRIP_AI_RACE_POLL_SECONDS="1"))
        return proc, out_path, err_path, (out, err)

    def wait_for(self, proc, paths, text):
        deadline = time.monotonic() + 60
        while text not in paths[0].read_text() + paths[1].read_text():
            self.assertIsNone(proc.poll(), paths[1].read_text())
            self.assertLess(time.monotonic(), deadline, "never printed: " + text)
            time.sleep(0.1)

    def run_race(self, repo, *signals, scripts=None):
        proc, out_path, err_path, files = self.start(repo, scripts)
        try:
            for text, signal_number in signals:
                self.wait_for(proc, (out_path, err_path), text)
                proc.send_signal(signal_number)
            returncode = proc.wait(timeout=120)
        finally:
            for handle in files:
                handle.close()
        return returncode, out_path.read_text(), err_path.read_text()

    def test_ctrl_c_or_term_before_a_winner_deletes_every_lane_of_this_run(self):
        scripts = {name: [["Creating", "Creating", ["Failed", "boot timed out"]]] for name in LANES}
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal_number.name), make_repo() as repo:
                returncode, out, err = self.run_race(repo, ("After 0 min:", signal_number), scripts=scripts)
                self.assertEqual(returncode, code, err)
                self.assertIn("Interrupted before any lane was InService; deleting every lane of this run.", out)
                self.assertEqual([name for name, _, _ in creates(repo)], list(LANES))
                self.assertEqual(endpoints_left(repo), {})
                self.assertIn("Every lane of this run is deleted; nothing bills for them.", out)

    def test_a_second_ctrl_c_names_what_may_still_bill(self):
        with make_repo() as repo:
            returncode, out, err = self.run_race(
                repo, ("After 0 min:", signal.SIGINT), ("Interrupted before any lane", signal.SIGINT),
                scripts={name: [["Creating"]] for name in LANES})
            self.assertEqual(returncode, 130, err)
            self.assertIn(f"stopped; these endpoints of this run may still exist and bill until {TEARDOWN}:"
                          f" {H1} (Creating) {H2} (Creating) {H3} (Creating) {H4} (Creating)\n", err)

    def test_a_closed_terminal_keeps_racing_unseen(self):
        scripts = {H1: [["Creating", "Creating", "InService"]],
                   **{name: [["Creating", "Creating", "Creating", ["Failed", "boot timed out"]]] for name in LANES[1:]}}
        with make_repo() as repo:
            returncode, out, err = self.run_race(repo, ("After 0 min:", signal.SIGHUP), scripts=scripts)
            self.assertEqual(returncode, 0, err)
            self.assertEqual(list(endpoints_left(repo)), [H1])
            self.assertEqual(sorted(deletes(repo)), sorted(LANES[1:]))
            self.assertNotIn("wins", out)

    def test_a_killed_race_resumes_by_adopting_its_lanes_and_creates_nothing(self):
        scripts = {name: [["Creating"] * 12 + ["InService"]] for name in LANES}
        with make_repo() as repo:
            proc, out_path, err_path, files = self.start(repo, scripts)
            try:
                self.wait_for(proc, (out_path, err_path), f"After 0 min: {H1} Creating")
                proc.kill()
                proc.wait(timeout=10)
            finally:
                for handle in files:
                    handle.close()
            [journal] = journals(repo)
            made = len(creates(repo))
            self.assertEqual(made, 4)
            repo.skip_sleeps()
            result = subprocess.run(
                [sys.executable, "-I", str(repo.root / "sagemaker" / "race.py"), "resume", journal["run_id"], "--yes"],
                capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                env=repo._env(dict(DASHBOARD_ENV, DRIP_AI_RACE_POLL_SECONDS="1", DRIP_AI_RACE_VIRTUAL_CLOCK="1")))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(creates(repo)), made)
            self.assertIn(f"{H1} (h200 in us-east-1) is InService and wins", result.stdout)
            self.assertEqual(list(endpoints_left(repo)), [H1])


class PlanReadsOnlyTest(unittest.TestCase):
    def test_the_planner_makes_no_mutating_call(self):
        with make_repo() as repo:
            script_start_cli(repo, status=status_object(WEIGHTS))
            out = repo.home / "plan.json"
            result = subprocess.run(
                [sys.executable, "-I", str(repo.root / "sagemaker" / "lanes.py"), "plan", "--model", NAME, "--mode",
                 "manifest", "--home-region", "us-east-1", "--bucket", BUCKET, "--weight-bytes", str(800 * 10**9),
                 "--out", str(out)], capture_output=True, text=True, timeout=120,
                env={**repo._env(DASHBOARD_ENV)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse({c.argv[1] for c in repo.calls() if c.tool == "aws"} & MUTATING_AWS)
            self.assertEqual(len(json.loads(out.read_text())["lanes"]), 4)
            self.assertEqual(oct(out.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
