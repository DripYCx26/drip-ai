"""The registry of tunnels drip-ai started, and who listens on the local ports.

Each running tunnel has one record, ${XDG_STATE_HOME:-~/.local/state}/drip-ai/procs/<pid>.json
(0600): pid, the process start time `ps` reports, a hash of its argv, port, endpoint, region, and
opencode provider. A record is live only while that pid runs with the same start time, so a pid
the system reused never counts as ours. drip-ai stops only a live registered process; anything
else on a port is reported, never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from drip_ai import state_dir

FILE_MODE = 0o600
PS_TIMEOUT_SECONDS = 10
RECORD_NAME = re.compile(r"[0-9]{1,10}\.json")


@dataclass(frozen=True)
class Listener:
    port: int
    pid: int
    command: str


def registry(environ: Mapping[str, str]) -> Path:
    return state_dir(environ, "procs")


def _ps(pid: int, field: str) -> Optional[str]:
    try:
        proc = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=PS_TIMEOUT_SECONDS, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else None


def start_time(pid: int) -> Optional[str]:
    return _ps(pid, "lstart")


def command_of(pid: int) -> Optional[str]:
    text = _ps(pid, "command")
    return text[:200] if text else None


def argv_hash(argv: list) -> str:
    return hashlib.sha256(json.dumps(argv).encode()).hexdigest()[:16]


def register(environ: Mapping[str, str], pid: int, argv: list, **fields: object) -> Path:
    """Writes the record for a running `pid`; refuses a pid with no start time."""
    started = start_time(pid)
    if started is None:
        raise OSError(f"process {pid} is not running")
    record = {"pid": pid, "start_time": started, "argv_hash": argv_hash(argv), "registered_at": time.time(),
              **fields}
    path = registry(environ) / f"{pid}.json"
    scratch = path.with_name(f".{pid}.json.tmp")
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle)
    os.replace(scratch, path)
    return path


def records(environ: Mapping[str, str]) -> list:
    """(record, live) for every readable record; an unreadable one is (None, False) with its path."""
    found = []
    for path in sorted(registry(environ).iterdir()):
        if not RECORD_NAME.fullmatch(path.name):
            continue
        try:
            record = json.loads(path.read_text())
            pid = record["pid"]
            live = isinstance(pid, int) and start_time(pid) == record.get("start_time")
        except (OSError, ValueError, KeyError, TypeError):
            found.append(({"path": str(path)}, False))
            continue
        record["path"] = str(path)
        found.append((record, live))
    return found


def unregister(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass  # already gone is the goal


def stop(record: Mapping, grace_seconds: float = 5.0) -> bool:
    """TERM, then KILL after the grace period, only while the pid still has the recorded start
    time. Returns False when the process was not the registered one, or is already gone."""
    pid = record.get("pid")
    if not isinstance(pid, int) or start_time(pid) != record.get("start_time"):
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if start_time(pid) != record.get("start_time"):
            return True
        time.sleep(0.1)
    if start_time(pid) == record.get("start_time"):
        os.kill(pid, signal.SIGKILL)
    return True


def listeners(first: int, last: int) -> Optional[list]:
    """Every TCP listener on ports first..last, from lsof; None when lsof cannot run."""
    try:
        proc = subprocess.run(["lsof", "-nP", f"-iTCP:{first}-{last}", "-sTCP:LISTEN", "-Fpcn"],
                              capture_output=True, text=True, timeout=PS_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    found, pid, command = [], None, ""
    for line in proc.stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid, command = (int(value) if value.isdigit() else None), ""
        elif tag == "c":
            command = value[:200]
        elif tag == "n" and pid is not None:
            port = value.rpartition(":")[2]
            if port.isdigit() and first <= int(port) <= last:
                listener = Listener(int(port), pid, command)
                if listener not in found:
                    found.append(listener)
    return found
