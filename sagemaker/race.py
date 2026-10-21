"""Runs an approved SageMaker race across regions, with a run journal so a killed race resumes.

    race.py run --plan FILE [--rerun CMD] [--dashboard] [--detach]
    race.py status [RUN_ID] [--json] [--last-winner]
    race.py resume RUN_ID [--yes]
    race.py cancel RUN_ID [--yes]

The plan comes from sagemaker/lanes.py, approved by the user. One lane is one endpoint whose
InstancePools hold one GPU class; it moves PLANNED -> ENSURING (model, configuration, and log group,
each created only when SageMaker says it is absent) -> REQUESTED -> PROVISIONING (Creating, no log
stream) -> BOOTING (Creating with a log stream) -> IN_SERVICE. The first lane in service wins, in
lane order, and every other lane is deleted. A Failed lane with a capacity reason and no log stream
is deleted and planned again; any other failure has its log read and recorded, is deleted, and is
never relaunched. "Container started" means a log stream created at or after the endpoint's
CreationTime, because a reused name keeps the streams of earlier endpoints. Someone else's endpoint
under a lane's name is never touched; a Failed one an earlier run of this identity left is deleted
and relaunched only when the lane's configuration has changed since (or it failed for capacity
before any container started), and never relaunched with the configuration that failed.

Every AWS error is classified once (drip_ai.awscli). Expired credentials pause the whole race: no
mutating call until `sts get-caller-identity` works again, a reminder every 15 minutes and through
notify.argv, and the race resumes only for the same base principal; another one stops the race
without deleting anything. A create happens only after a describe answered absent, intent is
journaled before it, and INT and HUP are blocked around it, so a signal never leaves an endpoint
this run does not know of. The cap is wall-clock time, because endpoints bill while a laptop
sleeps. After a win, deleting the losers is capped (DRIP_AI_STOP_WAIT_SECONDS, default 2 h);
cleanup after a signal, the cap, or every lane failing has no limit. The account's race lease in
the home region's state bucket stays in shared/lib/common.sh and is called from here.

Test hooks: DRIP_AI_RACE_POLL_SECONDS (15), DRIP_AI_RACE_LEASE_SECONDS (900),
DRIP_AI_STOP_WAIT_SECONDS (7200), DRIP_AI_RACE_VIRTUAL_CLOCK=1 (sleeps advance a virtual clock).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.append(str(REPO / "shared"))
from drip_ai import awscli, conventions, learn, logsignals, state_dir  # noqa: E402

TEARDOWN = "bin/drip-ai sagemaker stop"
LEASE_NAME = "sagemaker-race"
DELETE_WAIT_STATUSES = frozenset(("Creating", "Updating", "SystemUpdating", "RollingBack"))
LOG_CHECK_SECONDS = 300
PAUSE_POLL_SECONDS = 60
PAUSE_REMINDER_SECONDS = 900
BACKOFF_FIRST_SECONDS = 15
BACKOFF_CAP_SECONDS = 60
# ASSUMPTION: this machine's clock is within a minute of SageMaker's. An endpoint created that long
# before the race began is never taken as one of its own.
CLOCK_SKEW_SECONDS = 60
NOTIFY_TIMEOUT_SECONDS = 10
DETACH_WAIT_SECONDS = 10
FILE_MODE = 0o600
TERMINAL = frozenset(("OUT", "LOST", "PARKED", "ABANDONED", "GONE"))
LIVE = frozenset(("REQUESTED", "PROVISIONING", "BOOTING", "IN_SERVICE"))


def say(text: str, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    try:
        stream.write(text + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass  # a closed terminal; the race goes on unseen


def warn(text: str) -> None:
    say(f"start-sagemaker: {text}", err=True)


def duration_text(seconds: float) -> str:
    seconds = int(seconds)
    count, unit = seconds, "second"
    if seconds > 0 and seconds % 3600 == 0:
        count, unit = seconds // 3600, "hour"
    elif seconds > 0 and seconds % 60 == 0:
        count, unit = seconds // 60, "minute"
    return f"1 {unit}" if count == 1 else f"{count} {unit}s"


def whole_seconds(environ: dict, name: str, default: int) -> int:
    text = environ.get(name, "")
    if text == "":
        return default
    if not text.isdigit() or len(text) > 6:
        raise SystemExit(f"start-sagemaker: {name} must be whole seconds")
    return int(text)


def flags_hash(flags: list) -> str:
    return hashlib.sha256(json.dumps(flags).encode()).hexdigest()[:16]


def parse_time(value: object) -> Optional[float]:
    from datetime import datetime

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Clock:
    """Wall-clock time; with DRIP_AI_RACE_VIRTUAL_CLOCK=1 a sleep advances time instead of waiting."""

    def __init__(self, virtual: bool):
        self.virtual, self.offset = virtual, 0.0

    def now(self) -> float:
        return time.time() + self.offset

    def sleep(self, seconds: float, interrupted) -> None:
        if self.virtual:
            self.offset += seconds
            return
        end = time.monotonic() + seconds
        while time.monotonic() < end and not interrupted():
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))


@dataclass
class Lane:
    name: str
    region: str
    cls: str
    pool: list
    price: Optional[float]
    model: dict
    config: dict
    log_group: str
    state: str = "PLANNED"
    arn: str = ""
    created: str = ""
    attempts: int = 0
    reason: str = ""
    shown: str = "absent"
    unsure: bool = False
    intent: str = ""
    created_at: float = 0.0
    logs_checked_at: float = 0.0
    streams: bool = False
    backoff_until: float = 0.0
    backoff: float = 0.0
    note: str = ""
    retry_after_delete: bool = False

    @property
    def label(self) -> str:
        return f"{self.cls} in {self.region}"


@dataclass
class Seen:
    """What a describe found under a lane's name: absent, mine, other, or unknown."""

    kind: str
    status: str = ""
    reason: str = ""
    arn: str = ""
    created: str = ""
    config: str = ""
    error_class: str = ""


class Journal:
    """~/.local/state/drip-ai/runs/<id>.json, replaced whole on every write."""

    def __init__(self, environ: dict, run_id: str):
        self.path = state_dir(environ, "runs") / f"{run_id}.json"

    def write(self, document: dict) -> None:
        scratch = self.path.with_name(f".{self.path.name}.{os.getpid()}")
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle, indent=2)
        os.replace(scratch, self.path)

    @staticmethod
    def load(environ: dict, run_id: str) -> dict:
        path = state_dir(environ, "runs") / f"{run_id}.json"
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            raise SystemExit(f"start-sagemaker: no race {run_id} on this machine (bin/drip-ai race status lists them)")


def check_names(plan: dict) -> None:
    """Refuses a plan that would create a name outside drip-ai-, at most 63 characters, or a log
    group other than the endpoint's own: the execution role may write logs only under
    /aws/sagemaker/Endpoints/drip-ai-*, so any other name would fail with no log."""
    for lane in plan["lanes"]:
        try:
            for name in (lane["name"], lane["model"]["name"], lane["config"]["name"]):
                conventions.check_name(name)
            if lane["log_group"] != conventions.log_group(lane["name"]):
                raise conventions.BadName(f"log group {lane['log_group']!r} is not {lane['name']}'s own")
        except conventions.BadName as error:
            raise SystemExit(f"start-sagemaker: the race plan names {error}; nothing was created") from None


def process_start(pid: int) -> Optional[str]:
    try:
        proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10,
                              env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


class Race:
    def __init__(self, plan: dict, environ: dict, run_id: str, rerun: str, dashboard: bool):
        check_names(plan)
        self.plan, self.environ, self.run_id, self.rerun, self.dashboard = plan, environ, run_id, rerun, dashboard
        self.home = plan["home_region"]
        self.lanes = [Lane(l["name"], l["region"], l["class"], l["pool"], l["price"], l["model"], l["config"],
                           l["log_group"]) for l in plan["lanes"]]
        self.regions = list(dict.fromkeys(lane.region for lane in self.lanes))
        self.clock = Clock(environ.get("DRIP_AI_RACE_VIRTUAL_CLOCK") == "1")
        self.poll = whole_seconds(environ, "DRIP_AI_RACE_POLL_SECONDS", 15)
        self.lease_seconds = whole_seconds(environ, "DRIP_AI_RACE_LEASE_SECONDS", 900)
        self.lease_refresh = max(1, self.lease_seconds // 5)
        self.stop_wait = whole_seconds(environ, "DRIP_AI_STOP_WAIT_SECONDS", 7200)
        self.cap = int(plan["cap_seconds"])
        self.principal = awscli.base_principal(plan["caller_arn"])
        self.journal = Journal(environ, run_id)
        self.mode, self.result, self.winner = "racing", 0, None
        self.signals, self.signal_exit = 0, 0
        self.started = self.clock.now()
        self.began = int(environ.get("DRIP_AI_NOW_EPOCH") or time.time()) - CLOCK_SKEW_SECONDS
        self.kept_at = 0.0
        self.lease_at = self.lease_ok_at = self.started
        self.lease_held, self.lease_lost = True, ""
        self.paused, self.paused_at, self.reminded_at, self.pause_checked_at = False, 0.0, 0.0, 0.0
        self.last_summary, self.status = "", "racing"
        self.tags = [{"Key": "Project", "Value": "drip-ai"}, {"Key": "ManagedBy", "Value": "drip-ai"},
                     {"Key": "Stack", "Value": "sagemaker"}, {"Key": "StartedBy", "Value": plan["started_by"]},
                     {"Key": "RunId", "Value": run_id}]
        self.learning_warned = False
        self.pid_start: tuple = (None, None)

    # --- Plumbing ----------------------------------------------------------------------------------

    def minutes(self) -> int:
        return int((self.clock.now() - self.started) // 60)

    def save(self) -> None:
        if self.pid_start[0] != os.getpid():  # first save, or the first after --detach forked
            self.pid_start = (os.getpid(), process_start(os.getpid()))
        self.journal.write({
            "version": 1, "run_id": self.run_id, "pid": os.getpid(), "pid_start": self.pid_start[1],
            "host": socket.gethostname(), "status": self.status, "mode": self.mode,
            "winner": self.lanes[self.winner].name if self.winner is not None else None,
            "winner_region": self.lanes[self.winner].region if self.winner is not None else None,
            "updated": time.time(), "began": self.began, "rerun": self.rerun, "plan": self.plan,
            "lanes": [{k: v for k, v in asdict(lane).items() if k not in ("model", "config")} for lane in self.lanes],
        })

    def aws(self, service: str, operation: str, *args: str, region: str) -> awscli.Result:
        result = awscli.call(service, operation, *args, region=region)
        if not result.ok and result.error_class == "credentials" and not self.paused:
            self.pause(awscli.last_line(result.stderr))
        return result

    def learn(self, kind: str, cls: str, fields: dict) -> None:
        if not self.plan.get("learn", True):
            return
        try:
            learn.append(self.environ, kind, cls, fields, account=self.plan["account"], run_id=self.run_id)
        except (learn.LearnError, OSError) as error:
            if not self.learning_warned:
                warn(f"warning: could not record what this race learned ({error})")
                self.learning_warned = True

    def notify(self, message: str) -> None:
        argv = self.plan.get("notify")
        if not argv:
            return
        try:
            subprocess.run([*argv, message], timeout=NOTIFY_TIMEOUT_SECONDS, capture_output=True)
        except (OSError, subprocess.TimeoutExpired) as error:
            warn(f"warning: notify.argv failed ({error})")

    def lease(self, script: str, *words: str) -> subprocess.CompletedProcess:
        """Runs a shared/lib/common.sh lease function with the home region, as the shell does."""
        body = f'source "$1/shared/lib/common.sh"; shift; {script}'
        env = {**self.environ, "DRIP_AI_REGION": self.home}
        return subprocess.run(["bash", "-c", body, "lease", str(REPO), *words], env=env, capture_output=True,
                              text=True, timeout=180)

    def take_lease(self) -> bool:
        proc = self.lease('lease_acquire "$@"', LEASE_NAME, str(self.lease_seconds), str(os.getpid()))
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
        return proc.returncode == 0

    def lease_taken(self) -> Optional[str]:
        proc = self.lease('lease_holder "$1" 2>/dev/null && printf "%s\\t%s\\t%s\\t%s\\n" "$lease_holder_arn"'
                          ' "$lease_holder_host" "$lease_holder_pid" "$lease_holder_expired"', LEASE_NAME)
        fields = proc.stdout.strip().split("\t")
        if proc.returncode != 0 or len(fields) != 4 or not fields[2] or fields[3] != "no":
            return None
        arn, host, pid, _ = fields
        if arn == self.plan["caller_arn"] and host == socket.gethostname() and pid == str(os.getpid()):
            return None
        return f"the {LEASE_NAME} lease is now held by {arn} on {host} (pid {pid})"

    def release_lease(self) -> None:
        if not self.lease_held:
            return
        self.lease_held = False
        if self.lease('lease_release "$@"', LEASE_NAME).returncode != 0:
            warn(f"could not release the {LEASE_NAME} lease; it expires on its own within"
                 f" {self.lease_seconds // 60} minutes")

    def refresh_lease(self) -> None:
        now = self.clock.now()
        if self.lease_lost or self.paused or now - self.lease_at < self.lease_refresh:
            return
        self.lease_at = now
        if self.take_lease():
            self.lease_ok_at = now
            return
        taken = self.lease_taken()
        if taken:
            self.lease_lost = taken
        elif now - self.lease_ok_at >= self.lease_seconds - self.lease_refresh:
            self.lease_lost = (f"the {LEASE_NAME} lease has not been refreshed for {duration_text(now - self.lease_ok_at)},"
                               f" and it lapses after {duration_text(self.lease_seconds)}")
        else:
            warn(f"could not refresh the {LEASE_NAME} lease; racing on and trying again in"
                 f" {duration_text(self.lease_refresh)}")
            return
        self.lease_held = False
        if self.mode == "racing":
            warn(f"{self.lease_lost}, so another race could start; deleting every lane of this run")
            self.mode, self.result = "drain", 1
        elif self.mode == "keep":
            warn(f"{self.lease_lost}; keeping {self.lanes[self.winner].name} and deleting the other lanes")
        else:
            warn(f"{self.lease_lost}; still deleting every lane of this run")

    # --- Credentials -------------------------------------------------------------------------------

    def pause(self, line: str) -> None:
        self.paused, self.paused_at = True, self.clock.now()
        self.reminded_at = self.pause_checked_at = self.paused_at
        self.status = "paused"
        message = (f"AWS credentials stopped working ({line}); the race is paused and changes nothing in AWS"
                   " until they work again. Run: aws login (or aws sso login)")
        warn(message)
        self.notify("drip-ai race paused: run aws login")
        self.learn("credentials", "measured", {"profile": self.environ.get("AWS_PROFILE") or None,
                                               "event": "expired", "expiry_seen": learn.iso(time.time()),
                                               "session_age_s": None})
        self.save()

    def pause_tick(self) -> Optional[int]:
        """While paused: checks the identity every minute. Returns an exit code once the race must
        stop, else None."""
        now = self.clock.now()
        if now - self.reminded_at >= PAUSE_REMINDER_SECONDS:
            self.reminded_at = now
            warn(f"still paused after {duration_text(int(now - self.paused_at) // 60 * 60)}; run: aws login")
            self.notify("drip-ai race still paused: run aws login")
        if now - self.pause_checked_at < PAUSE_POLL_SECONDS:
            return None
        self.pause_checked_at = now
        result = awscli.call("sts", "get-caller-identity", region=self.home)
        if not result.ok:
            return None
        try:
            arn = str(json.loads(result.stdout).get("Arn", ""))
        except ValueError:
            return None
        if awscli.base_principal(arn) != self.principal:
            warn(f"the new credentials belong to {arn}, not {self.plan['caller_arn']}; stopping the race without"
                 f" deleting anything. These endpoints of this run may still exist and bill until {TEARDOWN}:"
                 f"{self.billing()}; once signed in as the race's identity, resume it with:"
                 f" bin/drip-ai race resume {self.run_id}")
            self.status = "stopped"
            self.save()
            return 1
        self.paused, self.status = False, "racing" if self.mode == "racing" else self.mode
        say(f"Credentials work again ({arn}); the race resumes after {duration_text(int(now - self.paused_at))}.")
        self.lease_at = 0.0
        self.save()
        return None

    # --- Reads -------------------------------------------------------------------------------------

    def list_region(self, region: str) -> Optional[dict]:
        """{name: status} of drip-ai endpoints in REGION, or None when the list failed."""
        result = self.aws("sagemaker", "list-endpoints", "--name-contains", "drip-ai", region=region)
        if not result.ok:
            if not self.paused:
                warn(f"could not list the endpoints in {region} ({awscli.last_line(result.stderr)}); asking again")
            return None
        try:
            body = json.loads(result.stdout or "{}")
        except ValueError:
            return None
        return {e.get("EndpointName"): e.get("EndpointStatus") for e in body.get("Endpoints") or []
                if isinstance(e, dict)}

    def describe(self, lane: Lane) -> Seen:
        result = self.aws("sagemaker", "describe-endpoint", "--endpoint-name", lane.name, region=lane.region)
        if not result.ok:
            if result.error_class == "absent":
                return Seen("absent")
            return Seen("unknown", error_class=result.error_class or "unknown")
        try:
            body = json.loads(result.stdout)
        except ValueError:
            return Seen("unknown")
        arn, status = str(body.get("EndpointArn") or ""), str(body.get("EndpointStatus") or "")
        created = body.get("CreationTime")
        if not arn.startswith("arn:") or not status.isalpha() or created in (None, ""):
            return Seen("unknown")
        reason = " ".join(str(body.get("FailureReason") or "").split())[:300]
        seen = Seen("other", status, reason, arn, str(created), str(body.get("EndpointConfigName") or ""))
        if lane.arn and arn == lane.arn and (not lane.created or seen.created == lane.created):
            lane.created = seen.created
            seen.kind = "mine"
        return seen

    def tags_of(self, lane: Lane, arn: str) -> Optional[dict]:
        result = self.aws("sagemaker", "list-tags", "--resource-arn", arn, region=lane.region)
        if not result.ok:
            return None
        try:
            return {t.get("Key"): t.get("Value") for t in json.loads(result.stdout).get("Tags") or []}
        except (ValueError, AttributeError):
            return None

    def adopt(self, lane: Lane) -> str:
        """Takes the endpoint under the lane's name as this run's when its RunId and StartedBy tags
        are this run's, its configuration is the lane's, and it was created after the race began.
        Returns absent, adopted, other, or unknown."""
        seen = self.describe(lane)
        if seen.kind in ("absent", "unknown"):
            return seen.kind
        tags = self.tags_of(lane, seen.arn)
        if tags is None:
            return "unknown"
        created = parse_time(seen.created)
        if (tags.get("RunId") != self.run_id or tags.get("StartedBy") != self.plan["started_by"]
                or seen.config != lane.config["name"] or created is None or created < self.began):
            return "other"
        self.follow(lane, seen.arn, seen.created)
        lane.shown = seen.status
        lane.state = "PROVISIONING" if seen.status == "Creating" else lane.state
        warn(f"{lane.name} exists from this run's earlier create, which reported an error; following it")
        return "adopted"

    def has_streams(self, lane: Lane, created: str) -> Optional[bool]:
        """Whether a container started under the endpoint created at CREATED (its CreationTime): a
        log stream created at or after it. Older streams in the reused log group belong to earlier
        endpoints of the same name and count for nothing. The streams are read newest event first, so
        the endpoint's own, the newest, lead the first page, and a page cut short holds only older
        ones. None when unreadable or CREATED is unknown."""
        since = parse_time(created)
        if since is None:
            return None
        result = self.aws("logs", "describe-log-streams", "--log-group-name", lane.log_group, "--order-by",
                          "LastEventTime", "--descending", "--max-items", str(logsignals.STREAM_LIMIT),
                          region=lane.region)
        if not result.ok:
            return None
        try:
            body = json.loads(result.stdout or "{}")
        except ValueError:
            return None
        return bool(logsignals.created_since(body.get("logStreams"), since)) if isinstance(body, dict) else None

    def read_log(self, lane: Lane, created: str) -> logsignals.Signals:
        """The signals in the log of the endpoint created at CREATED; empty when unreadable."""
        since = parse_time(created)
        if since is None:
            return logsignals.Signals()
        try:
            return logsignals.parse(logsignals.harvest(lane.region, lane.log_group, since))
        except awscli.AwsError:
            return logsignals.Signals()  # the outcome still counts; only its log values are unknown

    # --- Ensuring the model, configuration, and log group --------------------------------------------

    def ensure(self, lane: Lane) -> bool:
        """True once the lane's model, configuration, and log group exist."""
        for kind, describe_op, name_flag, create in (
                ("model", "describe-model", "--model-name", self.create_model),
                ("config", "describe-endpoint-config", "--endpoint-config-name", self.create_config)):
            name = (lane.model if kind == "model" else lane.config)["name"]
            found = self.aws("sagemaker", describe_op, name_flag, name, region=lane.region)
            if found.ok:
                continue
            if found.error_class != "absent":
                return self.create_failed(lane, f"describe the {kind} {name}", found)
            created = create(lane)
            if not created.ok and created.error_class != "conflict":
                return self.create_failed(lane, f"create the {kind} {name}", created)
        return self.ensure_log_group(lane)

    def private_file(self, document: object) -> str:
        folder = state_dir(self.environ, "runs", self.run_id)
        path = folder / f"request-{secrets.token_hex(4)}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle)
        return str(path)

    def create_model(self, lane: Lane) -> awscli.Result:
        path = self.private_file(lane.model["container"])
        try:
            return self.aws("sagemaker", "create-model", "--model-name", lane.model["name"], "--execution-role-arn",
                            lane.model["role_arn"], "--primary-container", f"file://{path}", "--tags",
                            json.dumps(self.tags), region=lane.region)
        finally:
            os.unlink(path)

    def create_config(self, lane: Lane) -> awscli.Result:
        path = self.private_file(lane.config["variants"])
        try:
            return self.aws("sagemaker", "create-endpoint-config", "--endpoint-config-name", lane.config["name"],
                            "--production-variants", f"file://{path}", "--tags", json.dumps(self.tags),
                            region=lane.region)
        finally:
            os.unlink(path)

    def ensure_log_group(self, lane: Lane) -> bool:
        found = self.aws("logs", "describe-log-groups", "--log-group-name-prefix", lane.log_group, region=lane.region)
        if not found.ok:
            return self.create_failed(lane, f"read the log group {lane.log_group}", found)
        groups = {g.get("logGroupName"): g for g in json.loads(found.stdout or "{}").get("logGroups") or []}
        if lane.log_group not in groups:
            tags = {t["Key"]: t["Value"] for t in self.tags}
            made = self.aws("logs", "create-log-group", "--log-group-name", lane.log_group, "--tags",
                            json.dumps(tags), region=lane.region)
            if not made.ok and made.error_class != "conflict":
                return self.create_failed(lane, f"create the log group {lane.log_group}", made)
        if groups.get(lane.log_group, {}).get("retentionInDays") != 14:
            kept = self.aws("logs", "put-retention-policy", "--log-group-name", lane.log_group,
                            "--retention-in-days", "14", region=lane.region)
            if not kept.ok:
                return self.create_failed(lane, f"set the retention of {lane.log_group}", kept)
        return True

    def create_failed(self, lane: Lane, what: str, result: awscli.Result) -> bool:
        """Applies the error-class policy to a failed ensure or create step; always False."""
        cls, line = result.error_class or "unknown", awscli.last_line(result.stderr)
        if cls == "credentials":
            return False
        if cls == "quota":
            lane.state, lane.reason, lane.shown = "PARKED", line, "parked"
            self.note(lane, f"{lane.name}: SageMaker refused to {what} for quota ({line}); parking the lane")
            self.learn("capacity_attempt", "measured", {"region": lane.region, "substrate": "sagemaker",
                                                        "pool": lane.pool, "outcome": "quota", "wait_s": 0,
                                                        "placed_type": None})
        elif cls in ("invalid", "denied"):
            lane.state, lane.reason = "OUT", line
            self.note(lane, f"SageMaker refused to {what} for {lane.name} ({line}); not retrying it")
            if cls == "denied" and result.operation.startswith(("logs ", "iam ")):
                warn(f"the race cannot write {lane.name}'s logs or check its role ({line}); deleting every lane"
                     " of this run")
                self.mode, self.result = "drain", 1
        else:
            lane.unsure = lane.unsure or what.startswith("create the endpoint")
            lane.backoff = min(BACKOFF_CAP_SECONDS, max(BACKOFF_FIRST_SECONDS, lane.backoff * 2)) \
                if cls == "throttle" else 0
            lane.backoff_until = self.clock.now() + lane.backoff
            self.note(lane, f"could not {what} for {lane.name} ({line}); asking again every {self.poll}s")
        self.save()
        return False

    # --- Creating, following, and deleting endpoints -------------------------------------------------

    def note(self, lane: Lane, text: str) -> None:
        if lane.note != text:
            lane.note = text
            warn(text)

    def follow(self, lane: Lane, arn: str, created: str) -> None:
        lane.arn, lane.created, lane.unsure, lane.note, lane.intent = arn, created, False, "", ""
        lane.state, lane.attempts, lane.streams = "REQUESTED", lane.attempts + 1, False
        lane.created_at, lane.logs_checked_at = self.clock.now(), self.clock.now()
        if lane.attempts > 1:
            say(f"After {self.minutes()} min: {lane.name} retry {lane.attempts - 1} (capacity)")
        self.save()

    def launch(self, lane: Lane) -> None:
        """One creation step for a PLANNED lane: ensure its resources, then create the endpoint once a
        describe answers absent."""
        if self.clock.now() < lane.backoff_until:
            return
        lane.state = "ENSURING"
        if not self.ensure(lane):
            if lane.state == "ENSURING":
                lane.state = "PLANNED"
            return
        if lane.unsure:
            found = self.adopt(lane)
        else:
            seen = self.describe(lane)
            found = self.reclaim(lane, seen) if seen.kind == "other" else seen.kind
            if seen.kind == "other" and found == "unknown":
                self.note(lane, f"{lane.name} exists and failed, but its tags or log streams could not be read, so"
                                f" this run cannot tell whether to replace it; leaving it alone and reading again"
                                f" every {self.poll}s")
        if found in ("adopted", "out"):
            return
        if found != "absent":
            lane.state = "PLANNED"
            if found == "other":
                self.note(lane, f"{lane.name} exists, and this run did not create it; leaving it alone and"
                                " creating the lane once it is gone")
            return
        lane.intent = f"create-endpoint at {time.time():.0f}"
        self.save()
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGHUP})
        try:
            result = self.aws("sagemaker", "create-endpoint", "--endpoint-name", lane.name, "--endpoint-config-name",
                              lane.config["name"], "--tags", json.dumps(self.tags), region=lane.region)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        if result.ok:
            try:
                arn = str(json.loads(result.stdout).get("EndpointArn") or "")
            except ValueError:
                arn = result.stdout.strip()
            self.follow(lane, arn, "")
            lane.shown = "Creating"
            self.describe(lane)
            self.save()
            return
        lane.state = "PLANNED"
        if result.error_class == "conflict":
            if self.adopt(lane) != "adopted":
                self.note(lane, f"{lane.name} exists, and this run did not create it; leaving it alone and"
                                " creating the lane once it is gone")
            return
        if result.error_class not in ("quota", "invalid", "denied", "credentials"):
            lane.unsure = True
        self.create_failed(lane, "create the endpoint", result)

    def reclaim(self, lane: Lane, seen: Seen) -> str:
        """An endpoint under the lane's name that this run did not create. When it is Failed, tagged
        ManagedBy=drip-ai and StartedBy this identity, and not in lifecycle.protect, it is deleted so
        the lane can launch, if its configuration differs from the lane's (the content-hashed name
        changed, so the configuration changed) or it failed for capacity before any container started.
        The same configuration that failed otherwise is never relaunched: the lane leaves the race.
        Returns deleting, out, other, or unknown."""
        if seen.status == "Deleting":
            return "deleting"
        if seen.status != "Failed" or lane.name in (self.plan.get("protect") or []):
            return "other"
        tags = self.tags_of(lane, seen.arn)
        if tags is None:
            return "unknown"
        if tags.get("ManagedBy") != "drip-ai" or tags.get("StartedBy") != self.plan["started_by"]:
            return "other"
        reason = seen.reason or "no FailureReason given"
        if seen.config == lane.config["name"]:
            started = self.has_streams(lane, seen.created)
            if started is None:
                return "unknown"
            if started or awscli.classify(seen.reason) != "capacity":
                lane.state, lane.reason, lane.shown = "OUT", reason, "Failed"
                self.note(lane, f"{lane.name} failed in an earlier run with this same configuration ({reason});"
                                " not relaunching it. Change the configuration first: bin/drip-ai plan --explain")
                self.save()
                return "out"
            why = "a capacity failure before any container started"
        else:
            why = f"configuration {seen.config}, which has changed since"
        if not self.delete(lane, seen.status):
            lane.state = "OUT"
            self.save()
            return "out"
        self.note(lane, f"{lane.name} failed in an earlier run ({reason}) with {why}; deleting it and creating the lane"
                        f" with {lane.config['name']}")
        return "deleting"

    def delete(self, lane: Lane, status: str) -> bool:
        """Asks SageMaker to delete the lane's own endpoint; False when refused for good."""
        result = self.aws("sagemaker", "delete-endpoint", "--endpoint-name", lane.name, region=lane.region)
        if result.ok:
            lane.shown = "Deleting"
            return True
        if status in DELETE_WAIT_STATUSES or result.error_class == "busy":
            self.note(lane, f"{lane.name} is {status}, and SageMaker deletes it only after that ends; asking again"
                            f" every {self.poll}s")
            return True
        if result.error_class == "credentials":
            return True
        self.note(lane, f"SageMaker refused to delete {lane.name} ({awscli.last_line(result.stderr)}); it bills"
                        f" until {TEARDOWN}")
        return False

    def lose(self, lane: Lane, seen: Seen) -> None:
        if seen.kind == "absent":
            self.note(lane, f"{lane.name} was deleted outside this run; the lane leaves the race")
        else:
            self.note(lane, f"{lane.name} is now {seen.arn} created {seen.created}, which this run did not create;"
                            " leaving it alone, and the lane leaves the race")
        lane.arn, lane.created, lane.state, lane.shown = "", "", "LOST", "absent"
        self.save()

    def failed(self, lane: Lane, seen: Seen) -> None:
        """A Failed own endpoint: capacity with no log stream is retried; anything else is read,
        recorded, and never relaunched. Either way it is deleted now."""
        lane.reason = seen.reason or "no FailureReason given"
        streams = self.has_streams(lane, seen.created)
        capacity = awscli.classify(seen.reason) == "capacity"
        waited = self.clock.now() - lane.created_at
        if capacity and not streams:
            self.learn("capacity_attempt", "measured", {"region": lane.region, "substrate": "sagemaker",
                                                        "pool": lane.pool, "outcome": "insufficient",
                                                        "wait_s": round(waited), "placed_type": None})
            lane.state, lane.retry_after_delete = "RETIRING", True
        else:
            signals = self.read_log(lane, seen.created)
            outcome = signals.failure or "failed"
            self.learn("startup", "measured", {"image_digest": self.plan["image_digest"],
                                               "vllm_version": signals.vllm_version,
                                               "flags_hash": flags_hash(self.plan["classes"][lane.cls]["flags"]),
                                               "flags": self.plan["classes"][lane.cls]["flags"], "outcome": outcome,
                                               "unknown_flag": signals.unknown_flag, "seconds_to_in_service": None})
            self.record_kv(lane, signals)
            extra = f" [{outcome}]" if outcome != "failed" else ""
            warn(f"{lane.name} ({lane.label}) failed: {lane.reason}{extra} (logs: {lane.log_group}); not retrying it")
            lane.state, lane.retry_after_delete = "RETIRING", False
        if not self.delete(lane, "Failed"):
            lane.state = "OUT"
        self.save()

    def record_kv(self, lane: Lane, signals: logsignals.Signals) -> None:
        if signals.kv_pool_tokens is None and signals.available_kv_gib is None:
            return
        chosen = self.plan["classes"][lane.cls]
        self.learn("kv_capacity", "measured", {
            "model_key": self.plan["model_key"], "instance_type": lane.pool[0], "image_digest": self.plan["image_digest"],
            "dcp": chosen["dcp"], "kv_dtype": chosen["kv_dtype"],
            "gpu_memory_utilization": self.plan["gpu_memory_utilization"],
            "pool_tokens": signals.kv_pool_tokens, "available_kv_gib": signals.available_kv_gib})

    def observe(self, lane: Lane, listed: dict) -> None:
        """Follows one lane through one racing poll."""
        status = listed.get(lane.name) or "absent"
        if lane.state in ("REQUESTED", "PROVISIONING", "BOOTING"):
            if status == "Creating":
                lane.shown = "Creating"
                lane.state = "PROVISIONING" if lane.state == "REQUESTED" else lane.state
                if not lane.streams and self.clock.now() - lane.logs_checked_at >= LOG_CHECK_SECONDS:
                    lane.logs_checked_at = self.clock.now()
                    if self.has_streams(lane, lane.created):
                        lane.streams, lane.state = True, "BOOTING"
                return
            if status not in ("InService", "Failed", "absent"):
                lane.shown = status
                return
            seen = self.describe(lane)
            if seen.kind == "unknown":
                return
            if seen.kind != "mine":
                self.lose(lane, seen)
                return
            lane.shown = seen.status
            if seen.status == "InService":
                lane.state = "IN_SERVICE"
                if self.winner is None:
                    self.winner = self.lanes.index(lane)
            elif seen.status == "Failed":
                self.failed(lane, seen)
        elif lane.state == "RETIRING":
            if status == "Deleting":
                lane.shown = "Deleting"
                return
            seen = self.describe(lane)
            if seen.kind == "unknown":
                return
            if seen.kind == "mine":
                lane.shown = seen.status
                if seen.status != "Deleting" and not self.delete(lane, seen.status):
                    lane.state = "OUT"
                return
            lane.arn, lane.created, lane.shown = "", "", "absent"
            lane.state = "PLANNED" if lane.retry_after_delete else "OUT"
            self.save()

    def print_lanes(self) -> None:
        summary = "".join(f" {lane.name} {lane.shown}" for lane in self.lanes)
        if summary != self.last_summary:
            say(f"After {self.minutes()} min:{summary}")
        self.last_summary = summary

    def race_poll(self, listings: dict) -> None:
        for lane in self.lanes:
            if self.paused:
                return
            if lane.region in listings and listings[lane.region] is not None:
                self.observe(lane, listings[lane.region])
        if self.winner is not None:
            win = self.lanes[self.winner]
            self.print_lanes()
            say(f"{win.name} ({win.label}) is InService and wins; deleting the other lanes.")
            self.mode, self.kept_at, self.status = "keep", self.clock.now(), "keep"
            self.won(win)
            self.save()
            return
        if self.clock.now() - self.started >= self.cap:
            self.print_lanes()
            warn(f"no lane was InService after {duration_text(self.cap)}; deleting every lane of this run")
            self.mode, self.result, self.status = "drain", 1, "drain"
            for lane in self.lanes:
                if lane.state in ("PROVISIONING", "BOOTING", "REQUESTED"):
                    self.learn("capacity_attempt", "measured", {"region": lane.region, "substrate": "sagemaker",
                                                                "pool": lane.pool, "outcome": "timeout",
                                                                "wait_s": round(self.clock.now() - lane.created_at),
                                                                "placed_type": None})
            self.save()
            return
        live = False
        for lane in self.lanes:
            if self.paused or self.mode != "racing":
                break
            if lane.state == "PLANNED" and self.signals == 0 and listings.get(lane.region) is not None:
                self.launch(lane)
            live = live or lane.state not in TERMINAL
        self.print_lanes()
        if self.mode == "racing" and not live and not self.paused:
            warn("every lane is out of the race; deleting every lane of this run")
            self.mode, self.result, self.status = "drain", 1, "drain"
            self.save()

    def won(self, lane: Lane) -> None:
        placed = None
        result = self.aws("sagemaker", "describe-endpoint", "--endpoint-name", lane.name, region=lane.region)
        if result.ok:
            variants = json.loads(result.stdout or "{}").get("ProductionVariants") or [{}]
            placed = variants[0].get("InstanceType") or variants[0].get("CurrentInstanceType")
        waited = round(self.clock.now() - lane.created_at)
        self.learn("capacity_attempt", "measured", {"region": lane.region, "substrate": "sagemaker", "pool": lane.pool,
                                                    "outcome": "placed", "wait_s": waited,
                                                    "placed_type": placed if isinstance(placed, str) else None})
        signals = self.read_log(lane, lane.created)
        flags = self.plan["classes"][lane.cls]["flags"]
        self.learn("startup", "measured", {"image_digest": self.plan["image_digest"], "vllm_version": signals.vllm_version,
                                           "flags_hash": flags_hash(flags), "flags": flags,
                                           "outcome": "ok", "unknown_flag": None, "seconds_to_in_service": waited})
        self.record_kv(lane, signals)
        self.notify(f"drip-ai: {lane.name} serves in {lane.region}")

    def cleanup(self, listings: dict) -> bool:
        """One cleanup poll: deletes every endpoint of this run but the winner. True once none is left."""
        left = False
        for index, lane in enumerate(self.lanes):
            if index == self.winner or lane.state == "STUCK" or self.paused:
                continue
            if not lane.arn:
                if not lane.unsure:
                    continue
                found = self.adopt(lane)
                if found == "unknown":
                    left = True
                    continue
                if found != "adopted":
                    lane.unsure = False
                    if found == "other":
                        self.note(lane, f"{lane.name} exists, and this run did not create it; leaving it alone")
                    continue
            listed = listings.get(lane.region)
            status = (listed or {}).get(lane.name, "absent") if listed is not None else None
            if status is None:
                left = True
                continue
            if status == "Deleting":
                lane.shown, left = "Deleting", True
                continue
            if status in DELETE_WAIT_STATUSES:
                lane.shown, left = status, True
                self.note(lane, f"{lane.name} is {status}, and SageMaker deletes it only after that ends; asking"
                                f" again every {self.poll}s")
                continue
            seen = self.describe(lane)
            if seen.kind == "unknown":
                left = True
            elif seen.kind == "mine":
                lane.shown, left = seen.status, True
                if seen.status != "Deleting" and not self.delete(lane, seen.status):
                    lane.state = "STUCK"
            else:
                lane.arn, lane.created, lane.shown = "", "", "absent"
        self.print_lanes()
        self.save()
        return not left and not self.paused

    def billing(self) -> str:
        text = ""
        for lane in self.lanes:
            if lane.arn:
                text += f" {lane.name} ({lane.shown})"
            elif lane.unsure:
                text += f" {lane.name} (unknown)"
        return text

    # --- The loop ----------------------------------------------------------------------------------

    def on_signal(self, signum: int, _frame) -> None:
        self.signals += 1
        self.signal_exit = 128 + signum

    def run(self) -> int:
        """Races until a winner holds alone, the race drains, or it must stop; releases the lease on
        every way out but a lost lease, which is no longer this run's."""
        try:
            return self.loop()
        finally:
            self.release_lease()

    def loop(self) -> int:
        signal.signal(signal.SIGINT, self.on_signal)
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGHUP, lambda *_: self.closed_terminal())
        self.save()
        say(f"Racing {len(self.lanes)} lanes in {', '.join(self.regions)}; polling every {self.poll}s."
            f" Ctrl-C deletes every lane of this run. Run id: {self.run_id}")
        while True:
            self.refresh_lease()
            if self.mode == "racing" and self.signals:
                say("Interrupted before any lane was InService; deleting every lane of this run.")
                self.mode, self.result, self.status = "drain", self.signal_exit, "drain"
            if self.paused:
                code = self.pause_tick()
                if code is not None:
                    return code
            if not self.paused:
                listings = {region: self.list_region(region) for region in self.regions}
                if not self.paused:
                    if self.mode == "racing":
                        self.race_poll(listings)
                    if self.mode != "racing" and not self.paused and self.cleanup(listings):
                        break
            if self.signals >= 2:
                warn(f"stopped; these endpoints of this run may still exist and bill until {TEARDOWN}:{self.billing()}")
                self.status = "stopped"
                self.save()
                return self.signal_exit
            if self.mode == "keep" and self.clock.now() - self.kept_at >= self.stop_wait:
                win = self.lanes[self.winner]
                warn(f"{win.name} serves, but after {duration_text(self.stop_wait)} these endpoints of this run still"
                     f" exist and bill until {TEARDOWN}:{self.billing()}")
                self.status = "won"
                self.save()
                return 1
            handled = self.signals
            self.clock.sleep(PAUSE_POLL_SECONDS if self.paused else self.poll, lambda: self.signals != handled)
        return self.finish()

    def closed_terminal(self) -> None:
        """A closed terminal would fail every later print; the race goes on unseen."""
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)

    def finish(self) -> int:
        left = self.billing()
        if self.mode == "drain":
            say("Attempts per lane:")
            for lane in self.lanes:
                say(f"  {lane.name} ({lane.label}): {lane.attempts} created; last failure: {lane.reason or 'none'}")
            if left:
                warn(f"SageMaker would not delete these endpoints of this run; each bills until {TEARDOWN}:{left}")
            else:
                say("Every lane of this run is deleted; nothing bills for them.")
            if self.result == 1:
                say(f"Start again: {self.rerun}")
            self.status = "drained"
            self.save()
            self.release_lease()
            self.notify("drip-ai race ended with no winner")
            return self.result
        win = self.lanes[self.winner]
        if left != f" {win.name} ({win.shown})":
            warn(f"{win.name} serves, but SageMaker would not delete these other endpoints of this run; each bills until"
                 f" {TEARDOWN}:{left}")
            self.status = "won"
            self.save()
            return 1
        self.status = "won"
        self.save()
        self.release_lease()
        where = "" if win.region == self.home else f" --region {win.region}"
        say(f"Endpoint {win.name} ({win.label}) serves models/{self.plan['model']}. Next:\n"
            f"  bin/drip-ai sagemaker tunnel{where} --endpoint {win.name}    # in one terminal; the clients link once"
            " the model answers\n"
            "  bin/drip-ai smoke               # in another\n"
            f"Tear down (the endpoint bills until deleted): {TEARDOWN}")
        if self.signals == 0 and self.dashboard:
            os.execvpe("bash", ["bash", str(REPO / "sagemaker" / "start.sh"), "--watch", "--endpoint", win.name],
                       {**self.environ, "DRIP_AI_REGION": win.region})
        return 0


# --- Commands --------------------------------------------------------------------------------------


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3)


def detach(log_path: Path) -> bool:
    """Forks a session leader that keeps racing after this process exits. True in the child."""
    parent = os.getpid()
    if os.fork() > 0:
        return False
    os.setsid()
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    deadline = time.monotonic() + DETACH_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(parent, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    return True


def run_command(args: argparse.Namespace, environ: dict) -> int:
    path = Path(args.plan)
    plan = json.loads(path.read_text())
    # The journal holds the plan from here; start.sh's private folder held only this file.
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass  # not empty: only start.sh's own folder, emptied, goes
    run_id = new_run_id()
    race = Race(plan, environ, run_id, args.rerun or f"bin/drip-ai sagemaker start {plan['model']}", args.dashboard)
    if args.detach:
        log = state_dir(environ, "runs") / f"{run_id}.log"
        if not detach(log):
            say(f"Race {run_id} runs detached; its log is {log}.\n"
                f"  Follow it: bin/drip-ai race status {run_id}\n"
                f"  End it:    bin/drip-ai race cancel {run_id}")
            return 0
        race.dashboard = False
        if not race.take_lease():
            warn(f"could not keep the {LEASE_NAME} lease after detaching; nothing was created")
            return 1
    return race.run()


def journals(environ: dict) -> list:
    folder = state_dir(environ, "runs")
    found = []
    for path in sorted(folder.glob("*.json")):
        try:
            found.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return found


def alive(journal: dict) -> bool:
    pid = journal.get("pid")
    return (isinstance(pid, int) and journal.get("host") == socket.gethostname()
            and process_start(pid) == journal.get("pid_start") and journal.get("pid_start") is not None)


def status_command(args: argparse.Namespace, environ: dict) -> int:
    runs = journals(environ)
    if args.run_id:
        runs = [r for r in runs if r.get("run_id") == args.run_id]
    if args.last_winner:
        won = [r for r in runs if r.get("winner") and r.get("status") == "won"]
        if not won:
            return 1
        print(f"{won[-1]['winner_region']}\t{won[-1]['winner']}")
        return 0
    rows = [{"run_id": r.get("run_id"), "status": r.get("status"), "running": alive(r), "winner": r.get("winner"),
             "model": (r.get("plan") or {}).get("model"),
             "lanes": [{k: lane.get(k) for k in ("name", "region", "state", "shown", "attempts", "arn")}
                       for lane in r.get("lanes") or []]} for r in runs]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No race journals on this machine.")
    for row in rows:
        print(f"{row['run_id']}  {row['status']}{' (running)' if row['running'] else ''}  model {row['model']}"
              f"  winner {row['winner'] or 'none'}")
        for lane in row["lanes"]:
            print(f"  {lane['name']}  {lane['region']}  {lane['state']}  {lane['shown']}  {lane['attempts']} created")
    return 0


def confirm(prompt: str, yes: bool, environ: dict) -> bool:
    if yes:
        say("Approved with --yes.")
        return True
    try:
        with open(environ.get("DRIP_AI_TTY") or "/dev/tty") as tty:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            return tty.readline().strip() == "yes"
    except OSError:
        return False


def restore(journal: dict, environ: dict) -> Race:
    race = Race(journal["plan"], environ, journal["run_id"], journal.get("rerun") or "bin/drip-ai sagemaker start",
                False)
    race.began = journal.get("began", race.began)
    for lane, saved in zip(race.lanes, journal.get("lanes") or []):
        for key in ("state", "arn", "created", "attempts", "reason", "shown", "unsure", "intent", "retry_after_delete"):
            if key in saved:
                setattr(lane, key, saved[key])
        if lane.intent and not lane.arn:
            lane.unsure = True
    return race


def readopt(race: Race) -> None:
    """Re-binds every lane to what SageMaker holds now, by journal ARN or RunId tag; creates nothing."""
    for lane in race.lanes:
        if lane.state in TERMINAL:
            continue
        if lane.arn:
            seen = race.describe(lane)
            if seen.kind == "mine":
                lane.shown = seen.status
                lane.state = {"Creating": "PROVISIONING", "InService": "IN_SERVICE"}.get(seen.status, lane.state)
                continue
            if seen.kind == "unknown":
                lane.unsure = True
                continue
            lane.arn, lane.created, lane.shown = "", "", "absent"
            lane.state = "PLANNED"
        elif lane.unsure:
            race.adopt(lane)
    say("Lanes now: " + ", ".join(f"{lane.name} {lane.shown}" for lane in race.lanes))


def resume_command(args: argparse.Namespace, environ: dict) -> int:
    journal = Journal.load(environ, args.run_id)
    if alive(journal):
        warn(f"race {args.run_id} is still running (pid {journal['pid']}); nothing changed")
        return 1
    _, arn = awscli.caller_identity(journal["plan"]["home_region"])
    if awscli.base_principal(arn) != awscli.base_principal(journal["plan"]["caller_arn"]):
        warn(f"race {args.run_id} belongs to {journal['plan']['caller_arn']}, and you are {arn}; nothing changed")
        return 1
    race = restore(journal, environ)
    readopt(race)
    if not confirm(f"Type yes to resume race {args.run_id}: ", args.yes, environ):
        warn("not approved; nothing was created")
        return 1
    if not race.take_lease():
        warn(f"could not take the {LEASE_NAME} lease; nothing was created")
        return 1
    race.mode = journal.get("mode") if journal.get("mode") in ("racing", "keep", "drain") else "racing"
    if race.mode == "keep":
        race.winner = next((i for i, lane in enumerate(race.lanes) if lane.state == "IN_SERVICE"), None)
        race.mode = "keep" if race.winner is not None else "racing"
        race.kept_at = race.clock.now()
    return race.run()


def cancel_command(args: argparse.Namespace, environ: dict) -> int:
    journal = Journal.load(environ, args.run_id)
    if alive(journal):
        os.kill(journal["pid"], signal.SIGTERM)
        say(f"Ending race {args.run_id} (pid {journal['pid']}); it deletes its own lanes before it exits.")
        return 0
    race = restore(journal, environ)
    readopt(race)
    if not race.billing() and not any(lane.unsure for lane in race.lanes):
        say("Nothing of this run exists any more; nothing bills for it.")
        return 0
    if not confirm(f"Type yes to delete every endpoint of race {args.run_id}: ", args.yes, environ):
        warn("not approved; nothing was deleted")
        return 1
    if not race.take_lease():
        warn(f"could not take the {LEASE_NAME} lease; nothing was deleted")
        return 1
    race.mode, race.result, race.status = "drain", 0, "drain"
    race.rerun = journal.get("rerun") or race.rerun
    return race.run()


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="race.py", description="Runs and follows SageMaker races.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--plan", required=True)
    run.add_argument("--rerun")
    run.add_argument("--dashboard", action="store_true")
    run.add_argument("--detach", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("run_id", nargs="?")
    status.add_argument("--json", action="store_true")
    status.add_argument("--last-winner", action="store_true")
    for name in ("resume", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        command.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    environ = dict(os.environ)
    handlers = {"run": run_command, "status": status_command, "resume": resume_command, "cancel": cancel_command}
    try:
        return handlers[args.command](args, environ)
    except awscli.AwsError as error:
        warn(f"aws {error.operation} failed ({error.message})")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
