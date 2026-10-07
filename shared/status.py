"""Prints one JSON object of what drip-ai runs and bills in a Region, for an app to poll.

`bin/drip-ai status --json` runs it with the Region drip_region chose. It makes only read-only AWS
calls and runs the link programs' `status`; it runs no Terraform and never reads the API key or a
Hugging Face token. The sections are read in parallel. A section that could not be read is null,
with its reason under "errors"; an empty list means the read worked and found nothing. It exits 0
whenever it prints the object. The shape is documented in docs/architecture.md, "Where an app plugs
in".
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

AWS_TIMEOUT_SECONDS = 60
LINK_TIMEOUT_SECONDS = 30
LIB = Path(__file__).resolve().parent / "lib"
# The same names as lane_name_pattern in sagemaker/endpoint.sh, the models bucket in
# shared/modules/conventions, and the cluster in shared/fetch-model/terraform.
LANE_NAME = re.compile(r"drip-ai(-[a-z0-9-]+)?")
FETCH_CLUSTER = "drip-ai-fetch-model"
STATUS_OBJECT = "drip-ai-fetch.json"
CLIENTS = ("codex", "claude", "opencode")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# An EC2 instance in any of these states can still bill (its volume at least).
EC2_STATES = "pending,running,stopping,stopped,shutting-down"


class ReadError(Exception):
    """A read failed; the message is one printable line."""


def last_line(text: str, limit: int = 200) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return CONTROL.sub("", lines[-1]).strip()[:limit] if lines else ""


def run(command: list, timeout: int) -> str:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ReadError(f"{command[0]} {command[1]} gave no answer in {timeout} s") from None
    except OSError as error:
        raise ReadError(f"could not run {command[0]} ({error.strerror})") from None
    if proc.returncode != 0:
        raise ReadError(last_line(proc.stderr) or f"{command[0]} {command[1]} exited {proc.returncode}")
    return proc.stdout


def aws(region: str, *args: str) -> dict:
    """One AWS CLI call's JSON answer; the CLI follows every page itself."""
    text = run(["aws", *args, "--region", region, "--output", "json"], AWS_TIMEOUT_SECONDS)
    try:
        answer = json.loads(text or "{}")
    except ValueError:
        raise ReadError(f"aws {args[0]} {args[1]} did not answer JSON") from None
    if not isinstance(answer, dict):
        raise ReadError(f"aws {args[0]} {args[1]} did not answer a JSON object")
    return answer


def text_or_none(value: object) -> str | None:
    return CONTROL.sub("", value) if isinstance(value, str) else None


def fill_endpoint(region: str, entry: dict, arn: str) -> None:
    """Sets the rest of one lane's fields in order; a read that fails leaves the later ones null."""
    tags = aws(region, "sagemaker", "list-tags", "--resource-arn", arn)
    entry["started_by"] = next((text_or_none(tag.get("Value")) for tag in tags.get("Tags") or []
                                if tag.get("Key") == "StartedBy"), None)
    described = aws(region, "sagemaker", "describe-endpoint", "--endpoint-name", entry["name"])
    config = aws(region, "sagemaker", "describe-endpoint-config", "--endpoint-config-name",
                 described["EndpointConfigName"])
    variant = (config.get("ProductionVariants") or [{}])[0]
    entry["instance_type"] = text_or_none(variant.get("InstanceType"))
    model = aws(region, "sagemaker", "describe-model", "--model-name", variant["ModelName"])
    # Only these two keys leave the model's environment.
    environment = (model.get("PrimaryContainer") or {}).get("Environment") or {}
    entry["served_model"] = text_or_none(environment.get("SM_VLLM_SERVED_MODEL_NAME"))
    window = environment.get("SM_VLLM_MAX_MODEL_LEN")
    entry["max_model_len"] = int(window) if isinstance(window, str) and window.isdigit() else None


def endpoints(region: str) -> tuple:
    listed = aws(region, "sagemaker", "list-endpoints", "--name-contains", "drip-ai")
    found, error = [], None
    for item in listed.get("Endpoints") or []:
        name = item.get("EndpointName")
        if not isinstance(name, str) or not LANE_NAME.fullmatch(name):
            continue
        entry = {"name": name, "status": text_or_none(item.get("EndpointStatus")), "instance_type": None,
                 "started_by": None, "created": text_or_none(item.get("CreationTime")),
                 "served_model": None, "max_model_len": None}
        found.append(entry)
        try:
            fill_endpoint(region, entry, str(item.get("EndpointArn")))
        except ReadError as failure:
            error = error or f"{name}: {failure}"
        except (KeyError, IndexError, TypeError, AttributeError):
            error = error or f"{name}: unexpected answer"
    return found, error


def ec2(region: str) -> tuple:
    answer = aws(region, "ec2", "describe-instances", "--filters", "Name=tag:Project,Values=drip-ai",
                 "Name=tag:Stack,Values=ec2", f"Name=instance-state-name,Values={EC2_STATES}")
    instances = [instance for reservation in answer.get("Reservations") or []
                 for instance in reservation.get("Instances") or []]
    return [{"instance_id": text_or_none(i.get("InstanceId")),
             "instance_type": text_or_none(i.get("InstanceType")),
             "state": text_or_none((i.get("State") or {}).get("Name"))} for i in instances], None


def copied_model(task: dict) -> str | None:
    """The --name a fetch task copies to, from its command override."""
    for override in (task.get("overrides") or {}).get("containerOverrides") or []:
        command = [str(part) for part in override.get("command") or []]
        if "--name" in command[:-1]:
            return text_or_none(command[command.index("--name") + 1])
    return None


def fetches(region: str) -> tuple:
    try:
        listed = aws(region, "ecs", "list-tasks", "--cluster", FETCH_CLUSTER, "--desired-status", "RUNNING")
    except ReadError as error:
        if "ClusterNotFoundException" in str(error):
            return [], None  # the fetch stack was never applied, so no fetch runs
        raise
    arns, tasks = listed.get("taskArns") or [], []
    for start in range(0, len(arns), 100):  # describe-tasks takes at most 100
        answer = aws(region, "ecs", "describe-tasks", "--cluster", FETCH_CLUSTER, "--include", "TAGS",
                     "--tasks", *arns[start:start + 100])
        tasks += answer.get("tasks") or []
    return [{"task_id": text_or_none(str(task.get("taskArn", "")).rpartition("/")[2]),
             "model": copied_model(task), "status": text_or_none(task.get("lastStatus")),
             "started_by": next((text_or_none(tag.get("value")) for tag in task.get("tags") or []
                                 if tag.get("key") == "StartedBy"), None),
             "created": text_or_none(task.get("createdAt"))} for task in tasks], None


def models(region: str) -> tuple:
    """Folders one or two levels under models/ holding drip-ai-fetch.json (fetched) or config.json
    (uploaded), the rule shared/models.sh list uses. `fetch` is the status object's status."""
    bucket = f"drip-ai-models-{account_id(region)}"

    def level(prefix: str) -> tuple:
        answer = aws(region, "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix,
                     "--delimiter", "/")
        files = {str(item.get("Key"))[len(prefix):] for item in answer.get("Contents") or []}
        return files, [str(item.get("Prefix")) for item in answer.get("CommonPrefixes") or []]

    folders = []
    for first in level("models/")[1]:
        files, seconds = level(first)
        if STATUS_OBJECT in files or "config.json" in files:
            folders.append((first, files))
            continue
        for second in seconds:
            files = level(second)[0]
            if STATUS_OBJECT in files or "config.json" in files:
                folders.append((second, files))
    found, error = [], None
    for prefix, files in folders:
        entry = {"name": prefix[len("models/"):-1], "source": "uploaded", "fetch": None}
        if STATUS_OBJECT in files:
            entry["source"] = "fetched"
            try:
                status = json.loads(run(["aws", "s3", "cp", f"s3://{bucket}/{prefix}{STATUS_OBJECT}", "-",
                                         "--region", region], AWS_TIMEOUT_SECONDS))
                entry["fetch"] = text_or_none(status.get("status")) if isinstance(status, dict) else None
            except ReadError as failure:
                error = error or f"{entry['name']}: {failure}"
            except ValueError:
                error = error or f"{entry['name']}: the fetch status object is not JSON"
        found.append(entry)
    return found, error


def clients(region: str) -> tuple:
    """Each client's link state: linked, unlinked, or damaged link record."""
    states, error = {}, None
    for client in CLIENTS:
        command = ([sys.executable, "-I", str(LIB / "codex_link.py"), "status"] if client == "codex" else
                   [sys.executable, "-I", str(LIB / "json_link.py"), "status", "--client", client])
        try:
            states[client] = last_line(run(command, LINK_TIMEOUT_SECONDS)).partition(";")[0] or None
        except ReadError as failure:
            states[client] = None
            error = error or f"{client}: {failure}"
    return states, error


def account_id(region: str) -> str:
    account = str(aws(region, "sts", "get-caller-identity").get("Account", ""))
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ReadError("aws sts get-caller-identity returned no account")
    return account


SECTIONS = {"endpoints": endpoints, "ec2": ec2, "fetches": fetches, "models": models, "clients": clients}


def read(section, region: str) -> tuple:
    """(value, first error) of one section; an unreadable section is (None, why)."""
    try:
        return section(region)
    except ReadError as error:
        return None, str(error)
    except (KeyError, IndexError, TypeError, AttributeError):
        return None, "unexpected answer"


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="status.py", description=__doc__.splitlines()[0])
    parser.add_argument("--region", required=True)
    region = parser.parse_args(argv).region
    with ThreadPoolExecutor(max_workers=len(SECTIONS)) as pool:
        futures = {name: pool.submit(read, section, region) for name, section in SECTIONS.items()}
    report, errors = {"region": region}, {}
    for name, future in futures.items():
        report[name], error = future.result()
        if error:
            errors[name] = error
    report["errors"] = errors
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
