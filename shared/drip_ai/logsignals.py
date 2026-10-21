"""Reads what a vLLM server log says about one startup: its version, KV pool, and why it failed.

A harvest is bounded: the newest 500 events of the newest log stream (of an endpoint's own, created
at or after its CreationTime, when the race reads it), at most 1 MB, from
CloudWatch (SageMaker) or from text a caller already holds (docker logs on EC2). The patterns are
data, in shared/catalog/log_signals.json.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from drip_ai import awscli, load_catalog

EVENT_LIMIT = 500
BYTE_LIMIT = 1024 * 1024
# Streams read when looking for one an endpoint created; each instance launch writes one.
STREAM_LIMIT = 50


@dataclass(frozen=True)
class Signals:
    vllm_version: Optional[str] = None
    kv_pool_tokens: Optional[int] = None
    available_kv_gib: Optional[float] = None
    estimated_max_model_len: Optional[int] = None
    failure: Optional[str] = None
    unknown_flag: Optional[str] = None


@lru_cache(maxsize=1)
def _compiled() -> tuple:
    catalog = load_catalog("log_signals")
    values = {name: [re.compile(p) for p in patterns] for name, patterns in catalog["values"].items()}
    failures = [(entry["outcome"], re.compile(entry["regex"])) for entry in catalog["failures"]]
    return values, failures


def parse(text: str) -> Signals:
    """The signals in the last BYTE_LIMIT bytes of `text`; the last match of each value wins."""
    text = text[-BYTE_LIMIT:]
    values, failures = _compiled()
    found: dict = {}
    for name, patterns in values.items():
        for pattern in patterns:
            matches = pattern.findall(text)
            if matches:
                found[name] = matches[-1]
    failure = unknown_flag = None
    for outcome, pattern in failures:
        match = pattern.search(text)
        if match:
            failure = outcome
            if outcome == "unknown_flag" and match.groups():
                unknown_flag = match.group(1).strip()[:100]
            break

    def number(name: str, kind):
        raw = found.get(name)
        try:
            return kind(raw.replace(",", "")) if raw is not None else None
        except ValueError:
            return None

    return Signals(found.get("vllm_version"), number("kv_pool_tokens", int), number("available_kv_gib", float),
                   number("estimated_max_model_len", int), failure, unknown_flag)


def created_since(streams: object, since: float) -> list:
    """Names of the log streams created at or after SINCE (epoch seconds), in the order given. An
    endpoint name that is reused keeps its log group and the streams of every earlier endpoint, so
    only a stream created at or after the endpoint's CreationTime is that endpoint's."""
    names = []
    for stream in streams if isinstance(streams, list) else []:
        if not isinstance(stream, dict):
            continue
        created, name = stream.get("creationTime"), stream.get("logStreamName")
        if (isinstance(created, (int, float)) and not isinstance(created, bool) and created >= since * 1000
                and isinstance(name, str) and name):
            names.append(name)
    return names


def newest_stream(region: str, log_group: str, since: Optional[float] = None) -> Optional[str]:
    """The log stream written last, created at or after SINCE when given; None when the group holds
    none; AwsError when unreadable."""
    answer = awscli.json_call("logs", "describe-log-streams", "--log-group-name", log_group, "--order-by",
                              "LastEventTime", "--descending", "--max-items",
                              str(1 if since is None else STREAM_LIMIT), region=region)
    streams = answer.get("logStreams") or []
    if since is not None:
        found = created_since(streams, since)
        return found[0] if found else None
    name = streams[0].get("logStreamName") if streams and isinstance(streams[0], dict) else None
    return name if isinstance(name, str) and name else None


def harvest(region: str, log_group: str, since: Optional[float] = None) -> str:
    """The newest EVENT_LIMIT events of the newest stream, oldest first, at most BYTE_LIMIT bytes.
    With SINCE, an endpoint's CreationTime in epoch seconds, only that endpoint's own streams count."""
    stream = newest_stream(region, log_group, since)
    if stream is None:
        return ""
    window = [] if since is None else ["--start-time", str(int(since * 1000))]
    answer = awscli.json_call("logs", "get-log-events", "--log-group-name", log_group, "--log-stream-name", stream,
                              "--limit", str(EVENT_LIMIT), "--no-start-from-head", *window, region=region)
    lines = [str(event.get("message", "")) for event in answer.get("events") or [] if isinstance(event, dict)]
    return "\n".join(lines)[-BYTE_LIMIT:]
