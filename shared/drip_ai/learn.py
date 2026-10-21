"""The local learning store: what races and startups observed, so later plans order and size better.

One JSON line per observation in ${XDG_STATE_HOME:-~/.local/state}/drip-ai/observations.jsonl
(0600, in a 0700 folder), appended under an fcntl lock and rotated at 5 MB with one old file kept.
Every line is validated on read; a bad line is skipped and counted, never guessed at. Learning is
advisory: it orders choices and fills derived values, and never overrides explicit configuration
or excludes a region. DRIP_AI_LEARN=off (learn.enabled false) reads and writes nothing, so a plan
equals the empty-store plan. summary.json is a cache rebuilt whenever the log's size or mtime
changes.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import statistics
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional

from drip_ai import REPO_ROOT, state_dir

# The link programs' library, appended as they append it, so it never shadows the stdlib.
if str(REPO_ROOT / "shared" / "lib") not in sys.path:
    sys.path.append(str(REPO_ROOT / "shared" / "lib"))
import link_files  # noqa: E402

STORE_NAME = "observations.jsonl"
ROTATED_NAME = "observations.jsonl.1"
LOCK_NAME = "observations.lock"
SUMMARY_NAME = "summary.json"
ROTATE_BYTES = 5 * 1024 * 1024
LINE_MAX_BYTES = 16 * 1024
FILE_MODE = 0o600
HALF_LIFE_SECONDS = 7 * 24 * 3600
OFFERING_TTL_SECONDS = 14 * 24 * 3600
SCHEMA_VERSION = 1
CLASSES = ("measured", "reported")
OUTCOMES = {
    "capacity_attempt": ("placed", "insufficient", "quota", "timeout"),
    "startup": ("ok", "oom", "kv_too_small", "download", "health", "unknown_flag", "failed"),
}
STR, INT, NUM, LIST, OPT_STR, OPT_INT, OPT_NUM = "str", "int", "num", "list", "str?", "int?", "num?"
# Required fields per kind, beyond v, ts, account, kind, class, and run_id.
KINDS = {
    "capacity_attempt": {"region": STR, "substrate": STR, "pool": LIST, "outcome": STR, "wait_s": NUM,
                         "placed_type": OPT_STR},
    "startup": {"image_digest": STR, "vllm_version": OPT_STR, "flags_hash": STR, "flags": LIST, "outcome": STR,
                "unknown_flag": OPT_STR, "seconds_to_in_service": OPT_NUM},
    "kv_capacity": {"model_key": STR, "instance_type": STR, "image_digest": STR, "dcp": INT, "kv_dtype": STR,
                    "gpu_memory_utilization": NUM, "pool_tokens": OPT_INT, "available_kv_gib": OPT_NUM},
    "credentials": {"profile": OPT_STR, "event": STR, "expiry_seen": OPT_STR, "session_age_s": OPT_NUM},
    "offering_seen": {"zone": STR, "instance_type": STR, "start": STR, "duration_hours": NUM, "usd": OPT_NUM},
}
ACCOUNT = re.compile(r"[0-9]{12}")


class LearnError(ValueError):
    """An observation was refused; one line."""


def enabled(environ: Mapping[str, str]) -> bool:
    return environ.get("DRIP_AI_LEARN", "").strip().lower() not in ("off", "0", "false", "no")


def looks_like_secret(text: str) -> bool:
    """The rule shared/lib/link_files.py applies to every link argument."""
    return link_files.looks_like_secret(text)


def _type_ok(kind: str, value: object) -> bool:
    if kind.endswith("?") and value is None:
        return True
    kind = kind.rstrip("?")
    if kind == STR:
        return isinstance(value, str) and 0 < len(value) <= 512
    if kind == INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == NUM:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, list) and len(value) <= 256 and all(isinstance(v, str) for v in value)


def validate(record: Mapping) -> Optional[str]:
    """Why a record is malformed, or None."""
    if not isinstance(record, dict):
        return "not an object"
    if record.get("v") != SCHEMA_VERSION:
        return "unknown version"
    kind = record.get("kind")
    if kind not in KINDS:
        return "unknown kind"
    if not isinstance(record.get("ts"), (int, float)) or isinstance(record.get("ts"), bool):
        return "no ts"
    if record.get("class") not in CLASSES:
        return "no class"
    if record.get("account") is not None and not ACCOUNT.fullmatch(str(record.get("account"))):
        return "bad account"
    if record.get("run_id") is not None and not _type_ok(STR, record.get("run_id")):
        return "bad run_id"
    for name, kind_of in KINDS[kind].items():
        if not _type_ok(kind_of, record.get(name)):
            return f"bad {name}"
    if kind in OUTCOMES and record["outcome"] not in OUTCOMES[kind]:
        return "bad outcome"
    return None


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _store(environ: Mapping[str, str]) -> Path:
    return state_dir(environ)


@contextmanager
def _locked(folder: Path) -> Iterator[None]:
    fd = os.open(folder / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def append(environ: Mapping[str, str], kind: str, cls: str, fields: Mapping, account: Optional[str] = None,
           run_id: Optional[str] = None, now: Optional[float] = None) -> bool:
    """Writes one observation; False when learning is off. Refuses a malformed record and any value
    that looks like a secret."""
    if not enabled(environ):
        return False
    record = {"v": SCHEMA_VERSION, "ts": now if now is not None else time.time(), "account": account,
              "kind": kind, "class": cls, "run_id": run_id, **fields}
    problem = validate(record)
    if problem:
        raise LearnError(f"refused a {kind} observation: {problem}")
    if any(looks_like_secret(text) for text in _strings(record)):
        raise LearnError(f"refused a {kind} observation: a value looks like a secret")
    line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) > LINE_MAX_BYTES:
        raise LearnError(f"refused a {kind} observation: longer than {LINE_MAX_BYTES} bytes")
    folder = _store(environ)
    with _locked(folder):
        path = folder / STORE_NAME
        if path.exists() and path.stat().st_size + len(line) > ROTATE_BYTES:
            os.replace(path, folder / ROTATED_NAME)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE)
        try:
            os.fchmod(fd, FILE_MODE)
            os.write(fd, line)
        finally:
            os.close(fd)
    return True


@dataclass(frozen=True)
class Store:
    records: tuple
    bad_lines: int


def read(environ: Mapping[str, str]) -> Store:
    """Every valid observation, oldest file first; an empty store when learning is off."""
    if not enabled(environ):
        return Store((), 0)
    folder = _store(environ)
    records, bad = [], 0
    for name in (ROTATED_NAME, STORE_NAME):
        path = folder / name
        if not path.exists():
            continue
        with open(path, "rb") as handle:
            for raw in handle:
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    bad += 1
                    continue
                if validate(record) is None:
                    records.append(record)
                else:
                    bad += 1
    return Store(tuple(records), bad)


def _weight(record: Mapping, now: float) -> float:
    return 0.5 ** (max(0.0, now - record["ts"]) / HALF_LIFE_SECONDS)


@dataclass(frozen=True)
class Summary:
    """What plans read: placement chances, flag evidence per image, and KV measurements."""

    placement: Mapping = field(default_factory=dict)  # "region|type" -> [placed weight, failed weight]
    flags_accepted: Mapping = field(default_factory=dict)  # digest -> [flag, ...]
    flags_rejected: Mapping = field(default_factory=dict)
    vllm_versions: Mapping = field(default_factory=dict)  # digest -> version
    kv: Mapping = field(default_factory=dict)  # "model|type|digest|dtype|util" -> {dcp, pool_tokens, available_kv_gib}
    placement_seconds: tuple = ()
    bad_lines: int = 0

    def chance(self, region: str, instance_type: str) -> float:
        """Beta(1, 1) placement probability with decayed counts."""
        placed, failed = self.placement.get(f"{region}|{instance_type}", (0.0, 0.0))
        return (1.0 + placed) / (2.0 + placed + failed)

    def counts(self, region: str, instance_type: str) -> tuple:
        return tuple(self.placement.get(f"{region}|{instance_type}", (0.0, 0.0)))

    def p50_placement_seconds(self) -> Optional[float]:
        return statistics.median(self.placement_seconds) if self.placement_seconds else None


def kv_key(model_key: str, instance_type: str, digest: str, dtype: str, util: float) -> str:
    return f"{model_key}|{instance_type}|{digest}|{dtype}|{util:g}"


def summarize(store: Store, now: float) -> Summary:
    placement: dict = {}
    accepted: dict = {}
    rejected: dict = {}
    versions: dict = {}
    kv: dict = {}
    seconds = []
    for record in store.records:
        weight = _weight(record, now)
        if record["kind"] == "capacity_attempt":
            if record["outcome"] == "placed":
                types = [record["placed_type"]] if record["placed_type"] else record["pool"]
                for instance_type in types:
                    placement.setdefault(f"{record['region']}|{instance_type}", [0.0, 0.0])[0] += weight
                seconds.append(float(record["wait_s"]))
            elif record["outcome"] in ("insufficient", "timeout"):
                for instance_type in record["pool"]:
                    placement.setdefault(f"{record['region']}|{instance_type}", [0.0, 0.0])[1] += weight
        elif record["kind"] == "startup":
            digest = record["image_digest"]
            if record["vllm_version"]:
                versions[digest] = record["vllm_version"]
            if record["outcome"] == "ok":
                accepted.setdefault(digest, set()).update(record["flags"])
            elif record["outcome"] == "unknown_flag" and record["unknown_flag"]:
                rejected.setdefault(digest, set()).add(record["unknown_flag"])
        elif record["kind"] == "kv_capacity":
            key = kv_key(record["model_key"], record["instance_type"], record["image_digest"], record["kv_dtype"],
                         record["gpu_memory_utilization"])
            kv[key] = {"dcp": record["dcp"], "pool_tokens": record["pool_tokens"],
                       "available_kv_gib": record["available_kv_gib"]}
    for digest in rejected:
        accepted.get(digest, set()).difference_update(rejected[digest])
    return Summary({k: tuple(v) for k, v in placement.items()},
                   {k: sorted(v) for k, v in accepted.items()}, {k: sorted(v) for k, v in rejected.items()},
                   versions, kv, tuple(seconds), store.bad_lines)


def summary(environ: Mapping[str, str], now: Optional[float] = None) -> Summary:
    """The summary of the store, from summary.json while the log is unchanged since it was built."""
    now = time.time() if now is None else now
    if not enabled(environ):
        return Summary()
    folder = _store(environ)
    signature = []
    for name in (ROTATED_NAME, STORE_NAME):
        path = folder / name
        signature.append([path.stat().st_size, path.stat().st_mtime_ns] if path.exists() else None)
    cache = folder / SUMMARY_NAME
    # The cache holds counts decayed to its build time; a day's drift is within the decay's own noise.
    try:
        cached = json.loads(cache.read_text())
        if cached.get("signature") == signature and now - cached.get("built", 0) < 24 * 3600:
            body = cached["summary"]
            return Summary(body["placement"], body["flags_accepted"], body["flags_rejected"],
                           body["vllm_versions"], body["kv"], tuple(body["placement_seconds"]), body["bad_lines"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    built = summarize(read(environ), now)
    body = {"placement": built.placement, "flags_accepted": built.flags_accepted,
            "flags_rejected": built.flags_rejected, "vllm_versions": built.vllm_versions, "kv": built.kv,
            "placement_seconds": list(built.placement_seconds), "bad_lines": built.bad_lines}
    scratch = cache.with_name(f".{SUMMARY_NAME}.{os.getpid()}")
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        json.dump({"signature": signature, "built": now, "summary": body}, handle)
    os.replace(scratch, cache)
    return built


def forget(environ: Mapping[str, str], kind: Optional[str] = None, before: Optional[float] = None) -> int:
    """Removes the observations matching every given filter; returns how many went."""
    if kind is not None and kind not in KINDS:
        raise LearnError(f"no observation kind {kind!r}; kinds: {', '.join(KINDS)}")
    folder = _store(environ)
    removed = 0
    with _locked(folder):
        for name in (ROTATED_NAME, STORE_NAME):
            path = folder / name
            if not path.exists():
                continue
            kept = []
            for raw in path.read_bytes().splitlines(keepends=True):
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    record = None
                match = (isinstance(record, dict) and (kind is None or record.get("kind") == kind)
                         and (before is None or (isinstance(record.get("ts"), (int, float)) and record["ts"] < before)))
                if match:
                    removed += 1
                else:
                    kept.append(raw)
            scratch = path.with_name(f".{name}.{os.getpid()}")
            fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
            with os.fdopen(fd, "wb") as handle:
                handle.writelines(kept)
            os.replace(scratch, path)
    (folder / SUMMARY_NAME).unlink(missing_ok=True)
    return removed


def export(environ: Mapping[str, str], redact: bool) -> list:
    """Every valid observation; redact replaces the account and run ids."""
    records = [dict(r) for r in read(environ).records]
    if redact:
        for record in records:
            record["account"] = None
            record["run_id"] = None
    return records


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
