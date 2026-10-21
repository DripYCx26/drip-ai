"""bin/drip-ai doctor: what drip-ai left in AWS and on this machine, what looks wrong, and the few
fixes it may make.

Every check has a stable finding id. Without --fix, doctor only reads (inside awscli.read_only()).
`--fix R5,R6 --yes` runs only those findings' allowlisted fixes, and only for resources that are
tagged ManagedBy=drip-ai and StartedBy the caller, older than an hour, not named in
lifecycle.protect, and not in use: a configuration an endpoint uses or is moving to
(PendingDeploymentSummary), in any state, is in use, and so is every model a configuration names.
Untagged drip-ai-* resources, someone else's, port listeners drip-ai did not start, and leases are
reported with the command to run, never changed. A process is stopped only when it is registered
and its start time still matches.

    R1  credentials           who you are, and whether the session ends within the hour
    R2  foundation            a configured region without the execution role
    R3  failed endpoints      Failed endpoints of yours that bill nothing        fix: delete them
    R4  unused configurations endpoint configurations nothing references         fix: delete them
    R5  unused models         models no configuration references                 fix: delete them
    R6  log retention         drip-ai log groups that never expire               fix: keep 14 days
    R7  untagged resources    drip-ai-* resources without drip-ai's tags         report only
    R8  leases                expired race or fetch leases, or a holder gone     report only
    R9  orphaned races        race journals whose process is gone                report only
    R10 capacity blocks       held blocks with no instance, or ending alone      report only
    R11 stale tunnels         registered tunnels whose endpoint is gone, or dead fix: stop or forget
    R12 port listeners        listeners on the configured ports drip-ai did not start  report only
    R13 client links          links whose owner is gone                          fix: unlink
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.append(str(Path(__file__).resolve().parents[1]))
from drip_ai import REPO_ROOT, awscli, config, conventions, procs, state_dir  # noqa: E402
from drip_ai.awscli import AwsError  # noqa: E402

MIN_AGE_SECONDS = 3600
IN_FLIGHT = frozenset(("Creating", "Updating", "SystemUpdating", "RollingBack"))
CLIENTS = ("codex", "claude", "opencode")
LEASES = ("sagemaker-race", "fetch-model")


@dataclass
class Item:
    """One resource or process a finding names."""

    name: str
    region: Optional[str] = None
    detail: str = ""
    fixable: bool = False
    data: dict = field(default_factory=dict)


@dataclass
class Finding:
    id: str
    title: str
    level: str  # info, warn, fix
    items: list = field(default_factory=list)
    command: str = ""

    def line(self) -> str:
        return f"{self.id} {self.level}: {self.title} ({len(self.items)})"


def parse_time(value: object) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def epoch_ms(value: object) -> Optional[float]:
    """Epoch seconds from CloudWatch Logs' epoch milliseconds; None when absent or not a number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000
    return None


def now() -> float:
    fixed = os.environ.get("DRIP_AI_NOW_EPOCH", "")
    return float(fixed) if fixed.isdigit() else time.time()


class Inventory:
    """Every read doctor makes, one region at a time, bounded to drip-ai-named resources."""

    def __init__(self, region: str, me: str):
        self.region, self.me = region, me
        self.endpoints: dict = {}
        self.configs: dict = {}
        self.models: dict = {}
        self.log_groups: list = []
        self.errors: list = []

    def read(self) -> "Inventory":
        listed = self.json("sagemaker", "list-endpoints", "--name-contains", "drip-ai") or {}
        for item in listed.get("Endpoints") or []:
            name = item.get("EndpointName", "")
            body = self.json("sagemaker", "describe-endpoint", "--endpoint-name", name) or {}
            body["tags"] = self.tags(body.get("EndpointArn"))
            self.endpoints[name] = body
        configs = self.json("sagemaker", "list-endpoint-configs", "--name-contains", "drip-ai") or {}
        for item in configs.get("EndpointConfigs") or []:
            name = item.get("EndpointConfigName", "")
            body = self.json("sagemaker", "describe-endpoint-config", "--endpoint-config-name", name) or {}
            body["tags"] = self.tags(item.get("EndpointConfigArn") or body.get("EndpointConfigArn"))
            body.setdefault("CreationTime", item.get("CreationTime"))
            self.configs[name] = body
        models = self.json("sagemaker", "list-models", "--name-contains", "drip-ai") or {}
        for item in models.get("Models") or []:
            self.models[item.get("ModelName", "")] = {"tags": self.tags(item.get("ModelArn")),
                                                      "CreationTime": item.get("CreationTime")}
        groups = self.json("logs", "describe-log-groups", "--log-group-name-prefix",
                           conventions.LOG_GROUP_PREFIX + "drip-ai")
        self.log_groups = (groups or {}).get("logGroups") or []
        for group in self.log_groups:
            if group.get("retentionInDays") is None:  # only these can be an R6 fix, so only these are read
                group["tags"] = self.log_group_tags(group)
        return self

    def json(self, service: str, operation: str, *args: str) -> Optional[dict]:
        try:
            return awscli.json_call(service, operation, *args, region=self.region)
        except AwsError as error:
            self.errors.append(f"{self.region}: aws {error.operation}: {error.message}")
            return None

    def tags(self, arn: Optional[str]) -> Optional[dict]:
        if not arn:
            return None
        answer = self.json("sagemaker", "list-tags", "--resource-arn", arn)
        if answer is None:
            return None
        return {t.get("Key"): t.get("Value") for t in answer.get("Tags") or []}

    def log_group_tags(self, group: dict) -> Optional[dict]:
        """A log group's tags, or None when they could not be read."""
        arn = group.get("logGroupArn") or str(group.get("arn") or "").removesuffix(":*")
        if not arn:
            return None
        answer = self.json("logs", "list-tags-for-resource", "--resource-arn", arn)
        tags = None if answer is None else answer.get("tags", {})
        return tags if isinstance(tags, dict) else None

    def in_use_configs(self) -> set:
        used = set()
        for body in self.endpoints.values():
            used.add(body.get("EndpointConfigName"))
            pending = body.get("PendingDeploymentSummary") or {}
            used.add(pending.get("EndpointConfigName"))
        return {name for name in used if name}

    def in_use_models(self) -> set:
        return {variant.get("ModelName") for body in self.configs.values()
                for variant in body.get("ProductionVariants") or [] if isinstance(variant, dict)}

    def ours(self, tags: Optional[dict]) -> bool:
        return bool(tags) and tags.get("ManagedBy") == "drip-ai" and tags.get("StartedBy") == self.me

    def drip_tagged(self, tags: Optional[dict]) -> bool:
        return bool(tags) and tags.get("Project") == "drip-ai"


def fixable(name: str, tags: Optional[dict], created: object, protect: set, inv: Inventory) -> tuple:
    """(may fix, why not)."""
    if name in protect:
        return False, "in lifecycle.protect"
    if not inv.ours(tags):
        return False, "not tagged ManagedBy=drip-ai by you"
    age = now() - (parse_time(created) or now())
    if age < MIN_AGE_SECONDS:
        return False, "younger than an hour"
    return True, ""


def aws_findings(regions: list, me: str, protect: set) -> tuple:
    """Findings R2 to R7 per region, plus read errors."""
    failed = Finding("R3", "Failed endpoints of yours (they bill nothing)", "fix",
                     command="bin/drip-ai doctor --fix R3 --yes")
    configs = Finding("R4", "endpoint configurations nothing references", "fix", command="bin/drip-ai doctor --fix R4 --yes")
    models = Finding("R5", "models no endpoint configuration references", "fix", command="bin/drip-ai doctor --fix R5 --yes")
    retention = Finding("R6", "drip-ai log groups without retention", "fix", command="bin/drip-ai doctor --fix R6 --yes")
    untagged = Finding("R7", "drip-ai-* resources without drip-ai's tags (reported only)", "warn")
    foundation = Finding("R2", "configured regions without the SageMaker foundation", "warn")
    # A log group is protected by its own name or by its endpoint's.
    protected_groups = protect | {conventions.LOG_GROUP_PREFIX + name for name in protect}
    errors = []
    for region in regions:
        role = awscli.call("iam", "get-role", "--role-name", conventions.execution_role_name(region), region=region)
        if not role.ok:
            foundation.items.append(Item(region, region, awscli.last_line(role.stderr)))
            foundation.command = "DRIP_AI_REGION=<region> bin/drip-ai setup"
        inv = Inventory(region, me).read()
        errors += inv.errors
        used_configs, used_models = inv.in_use_configs(), inv.in_use_models()
        for name, body in inv.endpoints.items():
            tags = body.get("tags")
            if not inv.drip_tagged(tags):
                untagged.items.append(Item(name, region, "endpoint", data={"command": f"aws sagemaker delete-endpoint"
                                                                          f" --region {region} --endpoint-name {name}"}))
                continue
            if body.get("EndpointStatus") == "Failed" and inv.ours(tags):
                ok, why = fixable(name, tags, body.get("CreationTime"), protect, inv)
                failed.items.append(Item(name, region, why or str(body.get("FailureReason") or "")[:160], ok))
        for name, body in inv.configs.items():
            tags = body.get("tags")
            if name in used_configs:
                continue
            if not inv.drip_tagged(tags):
                untagged.items.append(Item(name, region, "endpoint configuration", data={
                    "command": f"aws sagemaker delete-endpoint-config --region {region} --endpoint-config-name {name}"}))
                continue
            ok, why = fixable(name, tags, body.get("CreationTime"), protect, inv)
            configs.items.append(Item(name, region, why, ok))
        for name, body in inv.models.items():
            tags = body.get("tags")
            if name in used_models:
                continue
            if not inv.drip_tagged(tags):
                untagged.items.append(Item(name, region, "model", data={
                    "command": f"aws sagemaker delete-model --region {region} --model-name {name}"}))
                continue
            ok, why = fixable(name, tags, body.get("CreationTime"), protect, inv)
            models.items.append(Item(name, region, why, ok))
        for group in inv.log_groups:
            name = group.get("logGroupName", "")
            if group.get("retentionInDays") is not None:
                continue
            tags = group.get("tags")
            if not inv.drip_tagged(tags):
                untagged.items.append(Item(name, region, "log group without retention", data={
                    "command": f"aws logs put-retention-policy --region {region} --log-group-name {name}"
                               " --retention-in-days 14"}))
                continue
            ok, why = fixable(name, tags, epoch_ms(group.get("creationTime")), protected_groups, inv)
            retention.items.append(Item(name, region, why, ok))
    return [foundation, failed, configs, models, retention, untagged], errors


def credentials_finding(home: str) -> Finding:
    finding = Finding("R1", "AWS credentials", "info")
    try:
        account, arn = awscli.caller_identity(home)
    except AwsError as error:
        finding.level, finding.command = "warn", "aws login (or aws sso login)"
        finding.items.append(Item("unusable", home, error.message))
        return finding
    detail = f"account {account}"
    result = awscli.call("configure", "export-credentials", "--format", "process", output=None)
    if result.ok:
        try:
            expiry = parse_time(json.loads(result.stdout).get("Expiration"))
        except ValueError:
            expiry = None
        if expiry is not None:
            left = expiry - now()
            detail += f"; session ends in {int(left // 60)} min"
            if left < 3600:
                finding.level, finding.command = "warn", "aws login (or aws sso login) before a long race"
    finding.items.append(Item(arn, home, detail))
    return finding


def shell_lease(home: str, name: str) -> Optional[list]:
    script = ('source "$1/shared/lib/common.sh"; lease_holder "$2" 2>/dev/null &&'
              ' printf "%s\\t%s\\t%s\\t%s\\t%s\\n" "$lease_holder_arn" "$lease_holder_host" "$lease_holder_pid"'
              ' "$lease_holder_expires" "$lease_holder_expired"')
    proc = subprocess.run(["bash", "-c", script, "lease", str(REPO_ROOT), name], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "DRIP_AI_REGION": home})
    fields = proc.stdout.strip().split("\t")
    return fields if proc.returncode == 0 and len(fields) == 5 and fields[2] else None


def lease_finding(home: str) -> Finding:
    finding = Finding("R8", "leases that block a start or a fetch (reported only)", "warn")
    for name in LEASES:
        holder = shell_lease(home, name)
        if holder is None:
            continue
        arn, host, pid, expires, expired = holder
        local_dead = host == os.uname().nodename and not pid_alive(int(pid))
        if expired == "yes" or local_dead:
            why = "expired" if expired == "yes" else f"held by pid {pid}, which no longer runs"
            finding.items.append(Item(name, home, f"{why}; held by {arn} on {host} until {expires}"))
            finding.command = "the next start or fetch takes it over; nothing to do unless it is unreadable"
    return finding


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def runs_finding(environ: dict) -> Finding:
    finding = Finding("R9", "race journals whose process is gone (reported only)", "warn")
    for path in sorted(state_dir(environ, "runs").glob("*.json")):
        try:
            journal = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if journal.get("status") not in ("racing", "paused", "keep", "drain"):
            continue
        pid = journal.get("pid")
        if isinstance(pid, int) and procs.start_time(pid) == journal.get("pid_start"):
            continue
        live = [lane["name"] for lane in journal.get("lanes") or [] if lane.get("arn")]
        run_id = journal.get("run_id")
        finding.items.append(Item(run_id, journal.get("plan", {}).get("home_region"),
                                  f"status {journal.get('status')}; lanes that may exist: {', '.join(live) or 'none'}"))
        finding.command = f"bin/drip-ai race resume {run_id} or bin/drip-ai race cancel {run_id}"
    return finding


def capacity_finding(regions: list) -> Finding:
    finding = Finding("R10", "capacity blocks to act on (reported only)", "warn")
    sys.path.append(str(REPO_ROOT / "ec2"))
    import capacity  # noqa: E402  (ec2/capacity.py)

    for region in regions:
        try:
            blocks = capacity.held_blocks(region)
        except AwsError:
            continue
        for block in blocks:
            if block.state == "active" and block.available not in (None, 0):
                finding.items.append(Item(block.reservation_id, region, f"active {block.instance_type} in {block.zone}"
                                          f" with no instance until {block.end}: it bills unused"))
            ends = block.end_at().timestamp()
            successor = any(b.start_at().timestamp() <= ends < b.end_at().timestamp() for b in blocks if b is not block)
            if block.state == "active" and 0 < ends - now() < 7200 and not successor:
                finding.items.append(Item(block.reservation_id, region, f"ends {block.end} with no block after it"))
    if finding.items:
        finding.command = "bin/drip-ai capacity chain --hours 48"
    return finding


def tunnel_findings(environ: dict, cfg: config.Resolved, endpoints_by_region: dict) -> list:
    stale = Finding("R11", "registered tunnels whose endpoint is gone, or whose process is dead", "fix",
                    command="bin/drip-ai doctor --fix R11 --yes")
    listeners_finding = Finding("R12", "listeners on the configured ports drip-ai did not start (reported only)", "warn")
    registered = procs.records(environ)
    live_pids = set()
    for record, live in registered:
        name = str(record.get("pid"))
        if not live:
            stale.items.append(Item(name, record.get("region"), "process gone; the record stays", True,
                                    {"path": record.get("path")}))
            continue
        live_pids.add(record["pid"])
        endpoints = endpoints_by_region.get(record.get("region"))
        if record.get("backend") == "sagemaker" and endpoints is not None and record.get("endpoint") not in endpoints:
            stale.items.append(Item(name, record.get("region"), f"serves {record.get('endpoint')}, which is gone",
                                    True, {"path": record.get("path"), "record": record}))
    found = procs.listeners(cfg["ports.base"], cfg["ports.base"] + cfg["ports.range"] - 1)
    if found is None:
        listeners_finding.items.append(Item("lsof", None, "lsof is not available, so the ports were not checked"))
    for listener in found or []:
        if listener.pid not in live_pids:
            listeners_finding.items.append(Item(f"127.0.0.1:{listener.port}", None,
                                                f"pid {listener.pid} ({listener.command})"))
    if listeners_finding.items:
        listeners_finding.command = "stop it yourself if it is yours, or set DRIP_AI_PORT / ports.base"
    return [stale, listeners_finding]


def links_finding(environ: dict) -> Finding:
    finding = Finding("R13", "client links whose owner is gone", "fix", command="bin/drip-ai doctor --fix R13 --yes")
    lib = REPO_ROOT / "shared" / "lib"
    for client in CLIENTS:
        command = ([sys.executable, "-I", str(lib / "codex_link.py"), "status"] if client == "codex" else
                   [sys.executable, "-I", str(lib / "json_link.py"), "status", "--client", client])
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30, env=environ)
        text = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if text.startswith(("linked", "damaged")) and (" dead" in text or text.startswith("damaged")):
            finding.items.append(Item(client, None, text[:200], True))
    root = environ.get("XDG_CONFIG_HOME", "")
    if not root or not os.path.isabs(root):
        root = os.path.join(environ.get("HOME") or str(Path.home()), ".config")
    key = Path(root, "drip-ai", "opencode-key")  # where shared/lib/json_link.py keeps opencode's key
    if key.exists() and not any(i.name == "opencode" for i in finding.items):
        status = subprocess.run([sys.executable, "-I", str(lib / "json_link.py"), "status", "--client", "opencode"],
                                capture_output=True, text=True, timeout=30, env=environ).stdout
        if status.startswith("unlinked"):
            finding.items.append(Item("opencode", None, "the opencode key file is left with no link", True))
    return finding


def inventory(args: argparse.Namespace, environ: dict) -> tuple:
    cfg = config.resolve(environ)
    regions = list(dict.fromkeys([args.home_region, *(cfg["regions"] or [])]))
    findings = [credentials_finding(args.home_region)]
    try:
        _, me_arn = awscli.caller_identity(args.home_region)
    except AwsError:
        return findings, ["could not read your identity, so nothing else was checked"]
    sys.path.append(str(REPO_ROOT / "sagemaker"))
    from lanes import tag_value  # noqa: E402  (the StartedBy rule races tag with)

    aws, errors = aws_findings(regions, tag_value(me_arn), set(cfg["lifecycle.protect"]))
    findings += aws
    findings.append(lease_finding(args.home_region))
    findings.append(runs_finding(environ))
    findings.append(capacity_finding(regions))
    endpoints = {}
    for region in regions:
        try:
            listed = awscli.json_call("sagemaker", "list-endpoints", "--name-contains", "drip-ai", region=region)
            endpoints[region] = {e.get("EndpointName") for e in listed.get("Endpoints") or []}
        except AwsError:
            endpoints[region] = None
    findings += tunnel_findings(environ, cfg, endpoints)
    findings.append(links_finding(environ))
    return findings, errors


def apply_fixes(findings: list, wanted: set) -> int:
    """Runs the allowlisted fix of each wanted finding, item by item; returns 1 when any failed."""
    status = 0
    for finding in findings:
        if finding.id not in wanted:
            continue
        for item in finding.items:
            if not item.fixable:
                print(f"{finding.id}: left {item.name} alone ({item.detail})")
                continue
            ok, text = fix_one(finding.id, item)
            print(f"{finding.id}: {text}")
            status = status or (0 if ok else 1)
    return status


def fix_one(finding_id: str, item: Item) -> tuple:
    if finding_id in ("R3", "R4", "R5"):
        operation, flag = {"R3": ("delete-endpoint", "--endpoint-name"), "R4": ("delete-endpoint-config",
                                                                                "--endpoint-config-name"),
                           "R5": ("delete-model", "--model-name")}[finding_id]
        result = awscli.call("sagemaker", operation, flag, item.name, region=item.region)
    elif finding_id == "R6":
        result = awscli.call("logs", "put-retention-policy", "--log-group-name", item.name, "--retention-in-days", "14",
                             region=item.region)
    elif finding_id == "R11":
        record = item.data.get("record")
        if record and not procs.stop(record):
            return False, f"pid {item.name} is not the registered tunnel any more; left it alone"
        procs.unregister(item.data["path"])
        return True, f"stopped and forgot the tunnel record of pid {item.name}"
    elif finding_id == "R13":
        script = REPO_ROOT / "shared" / "clients.sh"
        proc = subprocess.run(["bash", str(script), "unlink", item.name], capture_output=True, text=True, timeout=60)
        return proc.returncode == 0, f"unlink {item.name}: {(proc.stdout or proc.stderr).strip()[:200]}"
    else:
        return False, f"{finding_id} has no fix"
    if result.ok:
        return True, f"{item.name} ({item.region}) done"
    return False, f"{item.name} ({item.region}) refused: {awscli.last_line(result.stderr)}"


FIXABLE = frozenset(("R3", "R4", "R5", "R6", "R11", "R13"))


def main(args: argparse.Namespace, environ: dict) -> int:
    wanted = {part.strip() for part in (args.fix or "").split(",") if part.strip()}
    if wanted - FIXABLE:
        print(f"drip-ai: doctor fixes only {', '.join(sorted(FIXABLE))}; {', '.join(sorted(wanted - FIXABLE))}"
              " are reported only", file=sys.stderr)
        return 1
    if wanted and not args.yes:
        print("drip-ai: doctor --fix changes AWS or this machine; rerun with --yes once you approve it", file=sys.stderr)
        return 1
    with awscli.read_only():
        findings, errors = inventory(args, environ)
    if args.json:
        print(json.dumps({"findings": [asdict(f) for f in findings], "errors": errors}, indent=2))
    else:
        for finding in findings:
            if not finding.items and finding.id != "R1":
                continue
            print(finding.line())
            for item in finding.items:
                where = f" ({item.region})" if item.region else ""
                extra = item.data.get("command")
                print(f"  {item.name}{where}{': ' + item.detail if item.detail else ''}"
                      f"{'  run: ' + extra if extra else ''}")
            if finding.command and finding.items:
                print(f"  {'fix' if finding.level == 'fix' else 'next'}: {finding.command}")
        for error in errors:
            print(f"doctor: could not read {error}", file=sys.stderr)
        if all(not f.items for f in findings if f.id != "R1"):
            print("Nothing to fix.")
    if not wanted:
        return 0
    return apply_fixes(findings, wanted)
