"""Every AWS call the control core makes: the AWS CLI with a deadline, one error classifier, and an
allowlist of operations split into read-only and mutating.

The CLI is the only way in, so `aws login`, SSO, and profiles work as they do in the shell, and the
tests' PATH stubs answer every call. An operation on neither list is refused before it runs. Inside
`read_only()` a mutating operation raises MutationRefused, so a read-only command cannot spend or
delete by accident. At most four calls run at once. Errors map once, through
shared/catalog/aws_errors.json, to a class: absent, capacity, quota, credentials, throttle, busy,
conflict, transient, denied, invalid, or unknown.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterator, Optional

from drip_ai import load_catalog

DEFAULT_TIMEOUT_SECONDS = 60
MAX_CONCURRENT_CALLS = 4
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

READ_ONLY = frozenset({
    ("sts", "get-caller-identity"),
    ("configure", "export-credentials"),
    ("sagemaker", "list-endpoints"), ("sagemaker", "describe-endpoint"), ("sagemaker", "describe-endpoint-config"),
    ("sagemaker", "describe-model"), ("sagemaker", "list-tags"), ("sagemaker", "list-endpoint-configs"),
    ("sagemaker", "list-models"),
    ("logs", "describe-log-groups"), ("logs", "describe-log-streams"), ("logs", "filter-log-events"),
    ("logs", "get-log-events"), ("logs", "tail"), ("logs", "list-tags-for-resource"),
    ("iam", "get-role"), ("iam", "simulate-principal-policy"),
    ("service-quotas", "list-service-quotas"), ("service-quotas", "get-service-quota"),
    ("s3api", "list-objects-v2"), ("s3api", "head-object"), ("s3api", "head-bucket"), ("s3", "cp"),
    ("ec2", "describe-capacity-block-offerings"), ("ec2", "describe-capacity-reservations"),
    ("ec2", "describe-availability-zones"), ("ec2", "describe-subnets"), ("ec2", "describe-vpcs"),
    ("ec2", "describe-instances"), ("ec2", "describe-regions"), ("ec2", "describe-instance-type-offerings"),
    ("ecs", "list-tasks"), ("ecs", "describe-tasks"), ("ecs", "describe-task-definition"),
    ("ssm", "get-command-invocation"),
    ("cloudwatch", "get-metric-data"),
})
MUTATING = frozenset({
    ("sagemaker", "create-model"), ("sagemaker", "create-endpoint-config"), ("sagemaker", "create-endpoint"),
    ("sagemaker", "delete-endpoint"), ("sagemaker", "delete-endpoint-config"), ("sagemaker", "delete-model"),
    ("logs", "create-log-group"), ("logs", "put-retention-policy"), ("logs", "delete-log-group"),
    ("ec2", "purchase-capacity-block"),
    ("ssm", "send-command"),
    ("ecs", "run-task"), ("ecs", "stop-task"),
})


class MutationRefused(RuntimeError):
    """A mutating AWS operation was attempted inside read_only()."""


class AwsError(Exception):
    """A failed call: `error_class` from the classifier and one printable line."""

    def __init__(self, error_class: str, message: str, operation: str):
        super().__init__(message)
        self.error_class, self.message, self.operation = error_class, message, operation


@dataclass(frozen=True)
class Result:
    ok: bool
    stdout: str
    stderr: str
    error_class: Optional[str]
    operation: str

    def json(self) -> object:
        try:
            return json.loads(self.stdout or "{}")
        except ValueError:
            raise AwsError("unknown", f"aws {self.operation} did not answer JSON", self.operation) from None

    def raise_for_error(self) -> "Result":
        if not self.ok:
            raise AwsError(self.error_class or "unknown", last_line(self.stderr) or f"aws {self.operation} failed",
                           self.operation)
        return self


@lru_cache(maxsize=1)
def _patterns() -> tuple:
    return tuple((p["class"], re.compile(p["regex"]), frozenset(p.get("operations") or ()))
                 for p in load_catalog("aws_errors")["patterns"])


def classify(text: str, operation: Optional[str] = None) -> str:
    """The error class of AWS stderr from OPERATION ("service operation"). A pattern scoped to some
    operations applies only to them, so text with no operation is never absent."""
    for error_class, pattern, operations in _patterns():
        if operations and operation not in operations:
            continue
        if pattern.search(text or ""):
            return error_class
    return "unknown"


def last_line(text: str, limit: int = 300) -> str:
    lines = [line for line in CONTROL.sub("", (text or "").replace("\t", " ")).splitlines() if line.strip()]
    return lines[-1].strip()[:limit] if lines else ""


_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
_read_only_lock = threading.Lock()
_read_only_blocks = 0


@contextmanager
def read_only() -> Iterator[None]:
    """Refuses every mutating operation while the block is open, in every thread of the process,
    so a read-only command's worker threads cannot mutate either. Blocks nest."""
    global _read_only_blocks
    with _read_only_lock:
        _read_only_blocks += 1
    try:
        yield
    finally:
        with _read_only_lock:
            _read_only_blocks -= 1


def is_mutating(service: str, operation: str) -> bool:
    key = (service, operation)
    if key in MUTATING:
        return True
    if key in READ_ONLY:
        return False
    raise ValueError(f"aws {service} {operation} is not on drip-ai's operation allowlist")


def call(service: str, operation: str, *args: str, region: Optional[str] = None,
         timeout: float = DEFAULT_TIMEOUT_SECONDS, output: Optional[str] = "json",
         stdin: Optional[str] = None) -> Result:
    """Runs one AWS CLI call and returns its Result; never raises for an AWS failure."""
    name = f"{service} {operation}"
    if is_mutating(service, operation) and _read_only_blocks > 0:
        raise MutationRefused(f"aws {name} would change AWS, and this command only reads")
    argv = ["aws", service, operation, *args]
    if region:
        argv += ["--region", region]
    if output:
        argv += ["--output", output]
    with _slots:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return Result(False, "", f"aws {name} gave no answer in {timeout:g} s", "transient", name)
        except OSError as error:
            return Result(False, "", f"could not run aws ({error.strerror})", "unknown", name)
    if proc.returncode == 0:
        return Result(True, proc.stdout, proc.stderr, None, name)
    return Result(False, proc.stdout, proc.stderr, classify(proc.stderr, name), name)


def json_call(service: str, operation: str, *args: str, region: Optional[str] = None,
              timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """The call's JSON object; AwsError on a failure or a non-object answer."""
    answer = call(service, operation, *args, region=region, timeout=timeout).raise_for_error().json()
    if not isinstance(answer, dict):
        raise AwsError("unknown", f"aws {service} {operation} did not answer a JSON object", f"{service} {operation}")
    return answer


def base_principal(arn: str) -> str:
    """The identity a session belongs to: an assumed role's ARN without its session name."""
    match = re.fullmatch(r"(arn:aws[a-z-]*:sts::[0-9]{12}:assumed-role/[^/]+)/.+", arn or "")
    return match.group(1) if match else arn


def caller_identity(region: Optional[str] = None) -> tuple:
    """(account, ARN) of the current credentials; AwsError when they do not work."""
    answer = json_call("sts", "get-caller-identity", region=region)
    account, arn = str(answer.get("Account", "")), str(answer.get("Arn", ""))
    if not re.fullmatch(r"[0-9]{12}", account) or not arn.startswith("arn:"):
        raise AwsError("unknown", "aws sts get-caller-identity returned no account", "sts get-caller-identity")
    return account, arn
