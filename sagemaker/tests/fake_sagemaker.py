"""A fake SageMaker, CloudWatch Logs, and STS for the race tests, put in front of the aws stub.

    fake_sagemaker.py STATE CALLS_LOG STUB AWS_ARGV...

It answers sagemaker endpoint, endpoint-configuration, model, and tag calls; the logs calls for the
log groups it tracks; and sts get-caller-identity while credentials are scripted. It appends a
cli_stubs Call line to CALLS_LOG for each call it answers, and execs STUB, the rule stub, for every
other call. Each endpoint lives in the region it was created in (--region, default us-east-1), and
its statuses move one step per list-endpoints call of that region. STATE holds:

- "now": the epoch the first creation follows by one second, and each later one by another.
- "scripts": {name: [[step, ...], ...]}: the steps each creation of an endpoint shows, one per poll
  from its creation; the last step, and the last creation's list, repeat. A step is a status, a
  [status, reason] pair, or {"status", "reason", "streams" (true once the container logged),
  "type" (the instance type it was placed on), "log" (the log text)}. An unscripted endpoint is
  Creating, then InService.
- "create_errors": {name: stderr}: the first create of that endpoint makes it, then fails with
  stderr, as a CLI timeout after SageMaker took the request does.
- "create_refusals": {name: [stderr, ...]}: each create of that endpoint fails with the next stderr
  and makes nothing, until the list runs out.
- "describe_errors": {name: [stderr, ...]}: each describe of that endpoint fails with the next
  stderr, until the list runs out.
- "events": {"N": [{"name", "arn", "created", "status", optional "region", "config", "tags",
  "pending_config"}]}: endpoints someone else creates in place of any of the same name at the Nth
  list-endpoints call (counted across regions).
- "expired": {"from_poll": N, "sts_calls": M, optional "arn_after"}: from the Nth list-endpoints
  call every call this fake answers fails with an expired session, until M get-caller-identity
  calls have failed; then get-caller-identity answers "arn_after" when set, else the stub does.
- "endpoints", "models", "configs", "log_groups": what exists; "polls", "creations", and "made"
  (creations per name). A log group is {"region", "retention" (days or null), "tags" ({key: value}),
  optional "created" (ISO time)}; list-tags-for-resource answers its tags by ARN. An endpoint whose
  "config" is "@lane-config" describes with the configuration this fake holds for its lane number
  (the name's last -N), so a test can seed an earlier run's endpoint with the lane's own hash.
- "old_streams": {endpoint name: [{"name", "created" (ISO), optional "log"}]}: log streams an
  earlier endpoint of that name left in its log group, each last written when it was created.
- "log_errors": {endpoint name: [stderr, ...]}: each describe-log-streams of that endpoint's log
  group fails with the next stderr, until the list runs out.

A container that logged writes one stream created, and last written, a second after its endpoint.
describe-log-streams answers as CloudWatch Logs and the CLI do: creationTime and lastEventTimestamp
in epoch milliseconds, ordered by logStreamName unless --order-by LastEventTime, ascending unless
--descending, and at most --max-items streams with a NextToken when more remain.

A delete is refused while an endpoint is Creating or Updating, as SageMaker refuses it; an accepted
one is Deleting until the next poll of its region or a wait endpoint-deleted.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

ACCOUNT = "111122223333"
DEFAULT_REGION = "us-east-1"
UNSCRIPTED = [["Creating", "InService"]]
EXPIRED = "aws: [ERROR]: Your session has expired. Please reauthenticate using 'aws login'.\n"


def option(argv: list, flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


def region_of(argv: list) -> str:
    return option(argv, "--region") or DEFAULT_REGION


def arn(region: str, kind: str, name: str) -> str:
    return f"arn:aws:sagemaker:{region}:{ACCOUNT}:{kind}/{name}"


def step_of(endpoint: dict) -> dict:
    step = endpoint["steps"][min(endpoint["step"], len(endpoint["steps"]) - 1)]
    if isinstance(step, str):
        return {"status": step}
    if isinstance(step, list):
        return {"status": step[0], "reason": step[1]}
    return step


def describe(name: str, endpoint: dict) -> dict:
    step = {"status": "Deleting"} if endpoint.get("deleting") else step_of(endpoint)
    variant = {"VariantName": "main", "CurrentInstanceCount": 1}
    if step.get("type"):
        variant["InstanceType"] = step["type"]
    body = {"EndpointName": name, "EndpointArn": endpoint["arn"], "EndpointConfigName": endpoint["config"],
            "EndpointStatus": step["status"], "FailureReason": step.get("reason"), "CreationTime": endpoint["created"],
            "LastModifiedTime": endpoint["created"], "ProductionVariants": [variant]}
    if endpoint.get("pending_config"):
        body["PendingDeploymentSummary"] = {"EndpointConfigName": endpoint["pending_config"]}
    return body


def has_streams(endpoint: dict) -> bool:
    return any(isinstance(s, dict) and s.get("streams") for s in endpoint["steps"][: endpoint["step"] + 1])


def epoch_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def log_streams_page(streams: list, argv: list) -> dict:
    """One describe-log-streams answer in the order and page size the call asks for."""
    key = "lastEventTimestamp" if option(argv, "--order-by") == "LastEventTime" else "logStreamName"
    ordered = sorted(streams, key=lambda s: s[key], reverse="--descending" in argv)
    limit = option(argv, "--max-items")
    if limit is None or len(ordered) <= int(limit):
        return {"logStreams": ordered}
    return {"logStreams": ordered[: int(limit)], "NextToken": "eyJuZXh0VG9rZW4iOiBudWxsfQ=="}


def lane_config(state: dict, name: str, endpoint: dict) -> None:
    """Resolves the "@lane-config" seed to the configuration held for the lane's number."""
    if endpoint.get("config") != "@lane-config":
        return
    number = "-" + name.rpartition("-")[2]
    endpoint["config"] = next((c for c in state["configs"] if c.endswith(number)), "@lane-config")


def poll(state: dict, region: str) -> list:
    endpoints = state["endpoints"]
    state["polls"] += 1
    for name in [n for n, e in endpoints.items() if e.get("deleting") and e["region"] == region]:
        del endpoints[name]
    for endpoint in endpoints.values():
        if endpoint["region"] == region:
            endpoint["step"] += 1
    for event in state["events"].get(str(state["polls"]), []):
        endpoints[event["name"]] = {"arn": event["arn"], "created": event["created"],
                                    "region": event.get("region", DEFAULT_REGION),
                                    "config": event.get("config", "theirs"), "steps": [event["status"]], "step": 0,
                                    "pending_config": event.get("pending_config"),
                                    **({"tags": event["tags"]} if "tags" in event else {})}
    return [(name, describe(name, e)) for name, e in endpoints.items() if e["region"] == region]


def create(state: dict, argv: list, name: str) -> tuple:
    endpoints, region = state["endpoints"], region_of(argv)
    refusals = state.setdefault("create_refusals", {}).get(name) or []
    if refusals:
        return 254, "", refusals.pop(0)
    if name in endpoints:
        return 254, "", ("An error occurred (ValidationException) when calling the CreateEndpoint operation:"
                         f' Cannot create already existing endpoint "{endpoints[name]["arn"]}".\n')
    made = state["made"].get(name, 0)
    scripts = state["scripts"].get(name) or UNSCRIPTED
    state["made"][name] = made + 1
    state["creations"] += 1
    created = datetime.fromtimestamp(state["now"], timezone.utc) + timedelta(seconds=state["creations"])
    endpoints[name] = {"arn": arn(region, "endpoint", name), "created": created.isoformat(timespec="microseconds"),
                       "region": region, "config": option(argv, "--endpoint-config-name"),
                       "steps": scripts[min(made, len(scripts) - 1)], "step": 0,
                       "tags": json.loads(option(argv, "--tags") or "[]")}
    if made == 0 and name in state.get("create_errors", {}):
        return 255, "", state["create_errors"][name]
    if option(argv, "--output") == "json":
        return 0, json.dumps({"EndpointArn": endpoints[name]["arn"]}), ""
    return 0, endpoints[name]["arn"] + "\n", ""


def read_file_arg(value: str | None) -> object:
    if value and value.startswith("file://"):
        with open(value[len("file://"):]) as handle:
            return json.load(handle)
    return json.loads(value) if value else None


def tags_answer(state: dict, argv: list) -> tuple | None:
    """list-tags of an endpoint, model, or configuration this fake holds; None for any other ARN."""
    target = option(argv, "--resource-arn")
    holders = list(state["endpoints"].values()) + list(state["models"].values()) + list(state["configs"].values())
    for holder in holders:
        if holder["arn"] == target and "tags" in holder:
            if option(argv, "--output") == "text":
                value = next((tag["Value"] for tag in holder["tags"] if tag["Key"] == "StartedBy"), "None")
                return 0, value + "\n", ""
            return 0, json.dumps({"Tags": holder["tags"]}), ""
    return None


def resource_answer(state: dict, argv: list, operation: str) -> tuple | None:
    """Models and endpoint configurations: create, describe, delete, and list."""
    region = region_of(argv)
    kind, store, flag, label = (("model", state["models"], "--model-name", "Model")
                                if "model" in operation and "endpoint-config" not in operation
                                else ("endpoint-config", state["configs"], "--endpoint-config-name", "EndpointConfig"))
    if operation.startswith("list-"):
        key = "Models" if kind == "model" else "EndpointConfigs"
        items = [{f"{label}Name": n, f"{label}Arn": r["arn"], "CreationTime": r["created"]}
                 for n, r in store.items() if r["region"] == region]
        return 0, json.dumps({key: items}), ""
    name = option(argv, flag)
    missing = (254, "", f"An error occurred (ValidationException) when calling the Describe{label} operation:"
                        f" Could not find {'model' if kind == 'model' else 'endpoint configuration'}"
                        f' "{arn(region, kind, name)}".\n')
    if operation.startswith("create-"):
        if name in store:
            return 254, "", (f"An error occurred (ValidationException) when calling the Create{label} operation:"
                             f' Cannot create already existing {kind} "{store[name]["arn"]}".\n')
        body = read_file_arg(option(argv, "--primary-container") or option(argv, "--production-variants"))
        store[name] = {"arn": arn(region, kind, name), "region": region, "body": body,
                       "created": datetime.fromtimestamp(state["now"], timezone.utc).isoformat(),
                       "tags": json.loads(option(argv, "--tags") or "[]"),
                       "role": option(argv, "--execution-role-arn")}
        return 0, json.dumps({f"{label}Arn": store[name]["arn"]}), ""
    if name not in store or store[name]["region"] != region:
        return missing
    if operation.startswith("delete-"):
        del store[name]
        return 0, "", ""
    record = store[name]
    if kind == "model":
        return 0, json.dumps({"ModelName": name, "ModelArn": record["arn"], "PrimaryContainer": record["body"],
                              "ExecutionRoleArn": record["role"], "CreationTime": record["created"]}), ""
    return 0, json.dumps({"EndpointConfigName": name, "EndpointConfigArn": record["arn"],
                          "ProductionVariants": record["body"], "CreationTime": record["created"]}), ""


def log_group_arn(region: str, name: str) -> str:
    return f"arn:aws:logs:{region}:{ACCOUNT}:log-group:{name}"


def describe_log_group(name: str, group: dict) -> dict:
    """A describe-log-groups entry; creationTime is epoch milliseconds, as CloudWatch Logs answers."""
    body = {"logGroupName": name, "arn": log_group_arn(group["region"], name) + ":*",
            "logGroupArn": log_group_arn(group["region"], name)}
    if group.get("created"):
        body["creationTime"] = int(datetime.fromisoformat(group["created"]).timestamp() * 1000)
    if group["retention"]:
        body["retentionInDays"] = group["retention"]
    return body


def logs_answer(state: dict, argv: list) -> tuple | None:
    operation, region = argv[1], region_of(argv)
    groups = state["log_groups"]
    if operation == "create-log-group":
        name = option(argv, "--log-group-name")
        if name in groups:
            return 254, "", ("An error occurred (ResourceAlreadyExistsException) when calling the CreateLogGroup"
                             " operation: The specified log group already exists\n")
        groups[name] = {"region": region, "retention": None, "tags": json.loads(option(argv, "--tags") or "{}"),
                        "created": datetime.fromtimestamp(state["now"], timezone.utc).isoformat()}
        return 0, "", ""
    if operation == "put-retention-policy":
        groups[option(argv, "--log-group-name")]["retention"] = int(option(argv, "--retention-in-days"))
        return 0, "", ""
    if operation == "describe-log-groups":
        prefix = option(argv, "--log-group-name-prefix") or ""
        found = [describe_log_group(n, g) for n, g in groups.items() if n.startswith(prefix) and g["region"] == region]
        return 0, json.dumps({"logGroups": found}), ""
    if operation == "list-tags-for-resource":
        target = option(argv, "--resource-arn")
        for name, group in groups.items():
            if log_group_arn(group["region"], name) == target:
                return 0, json.dumps({"tags": group.get("tags") or {}}), ""
        return 254, "", ("An error occurred (ResourceNotFoundException) when calling the ListTagsForResource"
                         " operation: The specified log group does not exist.\n")
    group = option(argv, "--log-group-name") or ""
    endpoint = state["endpoints"].get(group.rpartition("/")[2])
    old = state.get("old_streams", {}).get(group.rpartition("/")[2], [])
    if operation == "describe-log-streams":
        errors = state.setdefault("log_errors", {}).get(group.rpartition("/")[2]) or []
        if errors:
            return 254, "", errors.pop(0)
        streams = [{"logStreamName": s["name"], "creationTime": epoch_ms(s["created"]),
                    "lastEventTimestamp": epoch_ms(s["created"])} for s in old]
        if endpoint and has_streams(endpoint):
            born = epoch_ms(endpoint["created"]) + 1000
            streams.append({"logStreamName": "main/i-0abc/AllTraffic", "creationTime": born, "lastEventTimestamp": born})
        return 0, json.dumps(log_streams_page(streams, argv)), ""
    if operation == "get-log-events":
        earlier = next((s for s in old if s["name"] == option(argv, "--log-stream-name")), None)
        text = str(earlier.get("log", "")) if earlier else "\n".join(
            str(s.get("log", "")) for s in (endpoint or {"steps": []})["steps"] if isinstance(s, dict) and s.get("log"))
        events = [{"message": line} for line in text.splitlines()]
        return 0, json.dumps({"events": events}), ""
    return None


def sts_answer(state: dict, argv: list) -> tuple | None:
    expired = state.get("expired")
    if not expired or argv[1] != "get-caller-identity":
        return None
    if not expired.get("after"):
        return None
    if expired.get("arn_after"):
        if option(argv, "--output") == "text":
            return 0, f"{ACCOUNT}\t{expired['arn_after']}\n", ""
        return 0, json.dumps({"Account": ACCOUNT, "Arn": expired["arn_after"], "UserId": "AIDAEXAMPLE"}), ""
    return None


def credentials_window(state: dict, argv: list) -> tuple | None:
    """The expired-session answer while the scripted window is open."""
    expired = state.get("expired")
    if not expired or expired.get("after") or state["polls"] < expired["from_poll"]:
        return None
    if argv[:2] == ["sts", "get-caller-identity"]:
        expired["sts_seen"] = expired.get("sts_seen", 0) + 1
        if expired["sts_seen"] > expired["sts_calls"]:
            expired["after"] = True
            return None
    return 255, "", EXPIRED


def answer(state: dict, argv: list) -> tuple | None:
    """(exit code, stdout, stderr) for a call this fake answers, or None for the rule stub."""
    service = argv[0] if argv else ""
    if service not in ("sagemaker", "logs", "sts"):
        return None
    window = credentials_window(state, argv)
    if window is not None:
        return window
    if service == "sts":
        return sts_answer(state, argv)
    if service == "logs":
        return logs_answer(state, argv)
    operation, name = argv[1], option(argv, "--endpoint-name")
    endpoints, region = state["endpoints"], region_of(argv)
    if operation == "list-endpoints":
        listed = poll(state, region)
        if option(argv, "--output") == "json":
            return 0, json.dumps({"Endpoints": [{"EndpointName": n, "EndpointStatus": d["EndpointStatus"],
                                                  "EndpointArn": d["EndpointArn"], "CreationTime": d["CreationTime"]}
                                                 for n, d in listed]}), ""
        return 0, "".join(f"{n}\t{d['EndpointStatus']}\t{d['EndpointArn']}\n" for n, d in listed), ""
    if operation == "create-endpoint":
        return create(state, argv, name)
    if operation == "list-tags":
        return tags_answer(state, argv)
    if operation in ("create-model", "describe-model", "delete-model", "list-models", "create-endpoint-config",
                     "describe-endpoint-config", "delete-endpoint-config", "list-endpoint-configs"):
        return resource_answer(state, argv, operation)
    if operation == "wait" and "endpoint-deleted" in argv:
        if name in endpoints and endpoints[name].get("deleting"):
            del endpoints[name]
        return (255, "", "Waiter EndpointDeleted failed: Max attempts exceeded\n") if name in endpoints else (0, "", "")
    if operation not in ("describe-endpoint", "delete-endpoint"):
        return None
    errors = state.setdefault("describe_errors", {}).get(name) or []
    if operation == "describe-endpoint" and errors:
        return 254, "", errors.pop(0)
    if name not in endpoints or endpoints[name]["region"] != region:
        return 254, "", ("An error occurred (ValidationException) when calling the DescribeEndpoint operation:"
                         f' Could not find endpoint "{name}".\n')
    lane_config(state, name, endpoints[name])
    described = describe(name, endpoints[name])
    if operation == "delete-endpoint":
        if described["EndpointStatus"] in ("Creating", "Updating"):
            return 254, "", ("An error occurred (ValidationException) when calling the DeleteEndpoint operation:"
                             f' Cannot update in-progress endpoint "{described["EndpointArn"]}".\n')
        endpoints[name]["deleting"] = True
        return 0, "", ""
    if option(argv, "--output") == "json" and not option(argv, "--query"):
        return 0, json.dumps(described), ""
    fields = [described.get(key) for key in (option(argv, "--query") or "").strip("[]").split(",")]
    return 0, "\t".join("None" if value is None else str(value) for value in fields) + "\n", ""


def main(argv: list) -> int:
    state_path, calls_log, stub, aws_argv = argv[0], argv[1], argv[2], argv[3:]
    with open(state_path) as handle:
        state = json.load(handle)
    for key in ("models", "configs", "log_groups"):
        state.setdefault(key, {})
    for endpoint in state["endpoints"].values():
        endpoint.setdefault("region", DEFAULT_REGION)
    result = answer(state, aws_argv)
    # Saved before a call goes on to the stub too: a credentials window counts the calls it lets by.
    scratch = f"{state_path}.{os.getpid()}"
    with open(scratch, "w") as handle:
        json.dump(state, handle)
    os.replace(scratch, state_path)
    if result is None:
        os.execv(stub, [stub, *aws_argv])
    with open(calls_log, "a") as handle:
        handle.write(json.dumps({"tool": "aws", "argv": aws_argv, "stdin": None, "tfvars": None}) + "\n")
    code, stdout, stderr = result
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
