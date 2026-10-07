"""The files a client link owns: symlink-refusing reads, private writes, a compare-and-swap config
write, and the owner lock. Shared by `codex_link.py` and `json_link.py`; stdlib only.

Every config write is a compare-and-swap: the file is replaced only while it still hashes as it did
when it was read, so a concurrent edit by the app (or the user) is replanned, never overwritten.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import socket
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CAS_ATTEMPTS = 3
PRIVATE_MODE = 0o600
SECRET_PREFIXES = re.compile(
    r"(?<![A-Za-z0-9])((sk|hf|ghp|gho|github_pat)[-_][A-Za-z0-9_-]{8,}|A[KS]IA[0-9A-Z]{16})"
)
TOKEN_RUN = re.compile(r"[A-Za-z0-9+/=_]{32,}")  # random keys, such as the 48-character API key


class LinkError(Exception):
    """An expected refusal; the message is one line safe to print."""


def read_config_bytes(path: Path) -> bytes | None:
    """The file's bytes, None when missing; refuses a symlink or a non-regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise LinkError(f"{path.name} is a symlink; drip-ai edits only a regular file")
        raise
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise LinkError(f"{path.name} is not a regular file")
        return handle.read()


def decode(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LinkError(f"{name} is not UTF-8") from error


def validate_text(name: str, value: str) -> None:
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise LinkError(f"{name} has a control character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise LinkError(f"{name} is not valid text") from error


def looks_like_secret(value: str) -> bool:
    if SECRET_PREFIXES.search(value):
        return True
    return any(
        re.search(r"[0-9]", run) and re.search(r"[a-z]", run) and re.search(r"[A-Z]", run)
        for run in TOKEN_RUN.findall(value)
    )


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def write_private_file(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), PRIVATE_MODE)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        remove_if_present(Path(temp))


def remove_if_present(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass  # already gone is the goal


def _swap_if_unchanged(path: Path, original: bytes | None, new_text: str | None) -> bool:
    """Write new_text (None deletes) only if the file still hashes as `original`; False on a
    concurrent edit."""
    if new_text is None:
        if _digest(read_config_bytes(path)) != _digest(original):
            return False
        remove_if_present(path)
        return True
    mode = PRIVATE_MODE if original is None else stat.S_IMODE(os.lstat(path).st_mode)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".drip-ai.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(new_text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if _digest(read_config_bytes(path)) != _digest(original):
            return False
        if original is None:
            try:
                os.link(temp, path)  # creates without replacing a file that appeared meanwhile
            except FileExistsError:
                return False
        else:
            os.replace(temp, path)
        return True
    finally:
        remove_if_present(Path(temp))


@dataclass(frozen=True)
class ConfigPlan:
    new_text: str | None  # None: leave the file alone
    before_swap: Callable[[], None] | None = None
    delete: bool = False  # remove the file instead of writing new_text


def swap_config(path: Path, plan: Callable[[str | None], ConfigPlan]) -> None:
    """Read, plan, and compare-and-swap; replans on a concurrent edit, up to CAS_ATTEMPTS."""
    for _ in range(CAS_ATTEMPTS):
        original = read_config_bytes(path)
        planned = plan(None if original is None else decode(original, path.name))
        if planned.new_text is None and not planned.delete:
            return
        if planned.before_swap is not None:
            planned.before_swap()
        if _swap_if_unchanged(path, original, None if planned.delete else planned.new_text):
            return
    raise LinkError(f"{path.name} kept changing while drip-ai edited it; try again")


def is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


@dataclass(frozen=True)
class LinkLock:
    """The lock file naming the process (pid and host) that owns a client's link."""

    path: Path
    label: str  # the client, as messages name it: "Codex"

    def owner(self, data: bytes | None) -> tuple | None:
        """(pid, host) from lock bytes, None when there is no lock; refuses an unreadable one."""
        if data is None:
            return None
        try:
            holder = json.loads(data)
        except ValueError:
            holder = None
        if (
            not isinstance(holder, dict)
            or type(holder.get("pid")) is not int
            or not isinstance(holder.get("host"), str)
        ):
            raise LinkError(f"{self.path.name} is unreadable; run unlink --force if no tunnel runs")
        return holder["pid"], holder["host"]

    def read_owner(self) -> tuple | None:
        return self.owner(read_config_bytes(self.path))

    def acquire(self, owner_pid: int) -> str:
        """Create the lock for owner_pid; take over a dead owner's lock; refuse a live one."""
        content = json.dumps({"pid": owner_pid, "host": socket.gethostname()}).encode()
        note = ""
        for _ in range(CAS_ATTEMPTS):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                note = self._take_over_stale()
                continue
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return note
        raise LinkError("another drip-ai link is starting; try again")

    def _take_over_stale(self) -> str:
        stale = read_config_bytes(self.path)
        if stale is None:
            return ""
        pid, host = self.owner(stale)
        if host != socket.gethostname():
            raise LinkError(
                f"{self.label} is linked from host {host}; unlink there or unlink --force"
            )
        if is_alive(pid):
            raise LinkError(f"{self.label} is already linked by pid {pid}")
        claimed = self.path.with_name(f"{self.path.name}.{os.getpid()}.stale")
        try:
            os.rename(self.path, claimed)
        except FileNotFoundError:
            return ""
        if read_config_bytes(claimed) == stale:
            remove_if_present(claimed)
            return f"took over a stale link from pid {pid}"
        try:
            os.link(claimed, self.path)  # we moved a lock another link just took; put it back
        except FileExistsError:
            pass  # a third link holds the lock now; either way it is not ours
        remove_if_present(claimed)
        raise LinkError("another drip-ai link is starting; try again")

    def release_if_stale(self) -> None:
        """Remove a lock whose owner on this host no longer runs."""
        owner = self.read_owner()
        if owner is not None and owner[1] == socket.gethostname() and not is_alive(owner[0]):
            remove_if_present(self.path)
