"""Point Claude Code or opencode at the drip-ai model while a tunnel is open, and put it back.

`link` sets a fixed set of keys in the client's JSON config (Claude Code's `settings.json`,
opencode's `opencode.json`) and records the previous values, the values it wrote, and the exact
original text in a 0600 sidecar. `unlink` puts the original bytes back while the file is still
exactly what `link` wrote; after any other edit it restores only the keys that still equal what
`link` wrote, so a change made in the meantime survives. A file that is not strict JSON (comments,
trailing commas, duplicate keys) is refused with the keys to add by hand.

Claude Code reads the key through `apiKeyHelper`, the same SSM command Codex runs, so it never
touches disk. opencode has no command hook: `link` writes the key, read from stdin, to a 0600 file
in a 0700 folder and points `{file:...}` at it; `unlink` deletes it. One stdlib-only file, run with
`python3 -I`.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shlex
import shutil
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# `python3 -I` leaves this file's folder off sys.path; appended, so it never shadows the stdlib.
sys.path.append(str(Path(__file__).resolve().parent))
from link_files import (  # noqa: E402
    ConfigPlan,
    LinkError,
    LinkLock,
    decode,
    is_alive,
    looks_like_secret,
    read_config_bytes,
    remove_if_present,
    swap_config,
    validate_text,
    write_private_file,
)

PROVIDER = "drip-ai"
SIDECAR_NAME = "drip-ai-link.json"
LOCK_NAME = "drip-ai-link.lock"
KEY_DIR_MODE = 0o700
# Claude Code compacts at this share of the window, the share Codex uses by default.
AUTO_COMPACT_PERCENT = 90
# CLAUDE_CODE_AUTO_COMPACT_WINDOW takes 100000 to 1000000 tokens (code.claude.com/docs/en/env-vars).
AUTO_COMPACT_FLOOR = 100000
# ASSUMPTION: 16384 output tokens, or a quarter of a small window, leaves room to think and answer.
OUTPUT_TOKEN_CAP = 16384
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]*")
API_KEY = re.compile(r"[!-~]+")


@dataclass(frozen=True)
class LinkRequest:
    model: str
    port: int
    context_window: int
    owner_pid: int
    api_key_command: str  # Claude Code's apiKeyHelper; empty for opencode
    key_file: Path | None  # opencode's key file; None for Claude Code

    @property
    def output_tokens(self) -> int:
        return max(1, min(OUTPUT_TOKEN_CAP, self.context_window // 4))


# --- Clients: where each config lives and what link writes ---------------------------------------


@dataclass(frozen=True)
class ClientPaths:
    home: Path
    config_name: str

    @property
    def config(self) -> Path:
        return self.home / self.config_name

    @property
    def sidecar(self) -> Path:
        return self.home / SIDECAR_NAME

    @property
    def lock(self) -> Path:
        return self.home / LOCK_NAME


def _absolute_env(environ: dict, name: str) -> Path | None:
    value = environ.get(name, "")
    if not value:
        return None
    if not os.path.isabs(value):
        raise LinkError(f"{name} is not an absolute path")
    return Path(value)


def _home(environ: dict) -> Path:
    return Path(environ.get("HOME") or str(Path.home()))


def _config_root(environ: dict) -> Path:
    """$XDG_CONFIG_HOME when set and absolute, else $HOME/.config, as opencode resolves it."""
    value = environ.get("XDG_CONFIG_HOME", "")
    return Path(value) if value and os.path.isabs(value) else _home(environ) / ".config"


def claude_paths(environ: dict) -> ClientPaths:
    """$CLAUDE_CONFIG_DIR when set, else $HOME/.claude (code.claude.com/docs/en/env-vars)."""
    return ClientPaths(_absolute_env(environ, "CLAUDE_CONFIG_DIR") or _home(environ) / ".claude",
                       "settings.json")


def opencode_paths(environ: dict) -> ClientPaths:
    return ClientPaths(_config_root(environ) / "opencode", "opencode.json")


def opencode_key_file(environ: dict) -> Path:
    return _config_root(environ) / "drip-ai" / "opencode-key"


def claude_entries(request: LinkRequest) -> list:
    """(key path, value) pairs for settings.json; every name is in code.claude.com/docs/en/env-vars
    or /settings-reference."""
    model = request.model
    env = {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{request.port}",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(request.context_window),
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(request.output_tokens),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # Keeps Anthropic's pre-release request fields away from vLLM (docs/en/llm-gateway-connect).
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }
    compact_window = request.context_window * AUTO_COMPACT_PERCENT // 100
    if compact_window >= AUTO_COMPACT_FLOOR:  # below it, Claude Code would raise it to the floor
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(compact_window)
    entries = [(("env", name), value) for name, value in env.items()]
    return entries + [(("apiKeyHelper",), request.api_key_command)]


def opencode_entries(request: LinkRequest) -> list:
    """(key path, value) pairs for opencode.json (opencode.ai/docs/providers and /config.json)."""
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": PROVIDER,
        "options": {
            "baseURL": f"http://127.0.0.1:{request.port}/v1",
            "apiKey": f"{{file:{request.key_file}}}",
        },
        "models": {
            request.model: {
                "name": request.model,
                "tool_call": True,
                "reasoning": True,
                "limit": {"context": request.context_window, "output": request.output_tokens},
            }
        },
    }
    return [(("provider", PROVIDER), provider), (("model",), f"{PROVIDER}/{request.model}")]


@dataclass(frozen=True)
class Client:
    name: str
    label: str
    paths: Callable[[dict], ClientPaths]
    entries: Callable[[LinkRequest], list]
    model_path: tuple  # the key status reports
    owned_paths: tuple  # keys that refuse to link when present before link wrote them
    uses_key_file: bool
    overriding_files: tuple  # sibling configs the client reads after this one, so they win


CLIENTS = {
    "claude": Client("claude", "Claude Code", claude_paths, claude_entries, ("env", "ANTHROPIC_MODEL"),
                     (), False, ()),
    "opencode": Client("opencode", "opencode", opencode_paths, opencode_entries, ("model",),
                       (("provider", PROVIDER),), True, ("opencode.jsonc",)),
}


def is_installed(client: Client, paths: ClientPaths, environ: dict) -> bool:
    if paths.home.is_dir():
        return True
    on_path = shutil.which("opencode", path=environ.get("PATH")) is not None
    return client.name == "opencode" and on_path


# --- JSON documents: strict parse, key paths, plans (pure data in, data out) --------------------


class NotStrictJson(LinkError):
    pass


def parse_strict(text: str, name: str) -> dict:
    """The top-level object; whitespace-only is {}. Refuses JSONC, duplicate keys, NaN."""
    if not text.strip():
        return {}

    def unique_pairs(pairs: list) -> dict:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(pairs)

    def no_constant(token: str) -> None:
        raise ValueError(token)

    try:
        data = json.loads(text, object_pairs_hook=unique_pairs, parse_constant=no_constant)
    except ValueError as error:
        raise NotStrictJson(
            f"{name} is not plain JSON (comments, trailing commas, or duplicate keys)"
        ) from error
    if not isinstance(data, dict):
        raise LinkError(f"{name} is not a JSON object")
    return data


def render(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def same(a: object, b: object) -> bool:
    """JSON equality: unlike ==, true is not 1 and 1 is not 1.0."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


_ABSENT = object()


def get_path(data: dict, path: tuple) -> object:
    node: object = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return _ABSENT
        node = node[key]
    return node


def dotted(path: tuple) -> str:
    return ".".join(path)


def plan_link(data: dict, entries: list, client: Client, name: str) -> tuple:
    """Return (new data, record entries, created container paths) or refuse."""
    new = json.loads(json.dumps(data))  # a deep copy; the caller's object stays as read
    record_entries, created = [], []
    for path, value in entries:
        node = new
        for depth, key in enumerate(path[:-1]):
            if key not in node:
                node[key] = {}
                created.append(list(path[: depth + 1]))
            node = node[key]
            if not isinstance(node, dict):
                raise LinkError(f"{dotted(path[: depth + 1])} in {name} is not an object")
        entry = {"path": list(path), "written": value}
        if path[-1] in node:
            if path in client.owned_paths:
                raise LinkError(
                    f"{name} already defines {dotted(path)} and drip-ai did not write it; "
                    "remove it by hand first"
                )
            entry["previous"] = node[path[-1]]
        node[path[-1]] = value
        record_entries.append(entry)
    return new, record_entries, created


def plan_unlink(data: dict, record: dict) -> tuple:
    """Return (new data, restored keys, notes) for a file edited since link."""
    new = json.loads(json.dumps(data))
    restored, notes = [], []
    for entry in record["entries"]:
        path = tuple(entry["path"])
        current = get_path(new, path)
        if current is _ABSENT or not same(current, entry["written"]):
            if current is not _ABSENT or "previous" in entry:
                notes.append(f"kept your change to {dotted(path)}")
            continue
        parent = get_path(new, path[:-1]) if len(path) > 1 else new
        if "previous" in entry:
            parent[path[-1]] = entry["previous"]
        else:
            del parent[path[-1]]
        restored.append(dotted(path))
    for container in sorted(record["created"], key=len, reverse=True):
        path = tuple(container)
        if same(get_path(new, path), {}):
            parent = get_path(new, path[:-1]) if len(path) > 1 else new
            del parent[path[-1]]
    return new, restored, notes


def hand_snippet(entries: list, client: Client) -> str:
    """The keys link would write, as JSON to paste; opencode reads the key from the environment."""
    snippet: dict = {}
    for path, value in entries:
        node = snippet
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = json.loads(json.dumps(value))
    if client.uses_key_file:
        snippet["provider"][PROVIDER]["options"]["apiKey"] = "{env:DRIP_AI_API_KEY}"
        return render(snippet) + 'Then run: export DRIP_AI_API_KEY="$(bin/drip-ai key)"'
    return render(snippet).rstrip("\n")


# --- Files: link record, key file ---------------------------------------------------------------


def _is_valid_record(record: object, client: Client) -> bool:
    if not isinstance(record, dict):
        return False
    entries, created = record.get("entries"), record.get("created")
    return (
        record.get("client") == client.name
        and type(record.get("pid")) is int
        and isinstance(record.get("host"), str)
        and (record.get("original") is None or isinstance(record.get("original"), str))
        and isinstance(record.get("written_text"), str)
        and isinstance(record.get("created_home"), bool)
        and isinstance(entries, list)
        and all(
            isinstance(entry, dict)
            and "written" in entry
            and isinstance(entry.get("path"), list)
            and entry["path"]
            and all(isinstance(key, str) for key in entry["path"])
            for entry in entries
        )
        and isinstance(created, list)
        and all(
            isinstance(path, list) and path and all(isinstance(key, str) for key in path)
            for path in created
        )
    )


def read_sidecar(paths: ClientPaths, client: Client) -> dict | None:
    """The link record, None when absent; refuses a damaged one."""
    data = read_config_bytes(paths.sidecar)
    if data is None:
        return None
    try:
        record = json.loads(data)
    except ValueError:
        record = None
    if not _is_valid_record(record, client):
        raise LinkError(
            f"{SIDECAR_NAME} is damaged; run unlink --force and check {paths.config_name}"
        )
    return record


def read_api_key(stream) -> str:
    """One line of stdin: printable ASCII without quotes or backslashes, as the shell checks it."""
    if stream.isatty():
        raise LinkError("opencode needs the API key on stdin; the tunnel pipes it in")
    key = stream.readline().rstrip("\n")
    if not API_KEY.fullmatch(key) or '"' in key or "\\" in key:
        raise LinkError("the API key on stdin has an unexpected form")
    return key


def write_key_file(path: Path, key: str) -> None:
    """Writes the key with no newline into a private folder this user owns."""
    folder = path.parent
    folder.mkdir(mode=KEY_DIR_MODE, parents=True, exist_ok=True)
    info = os.lstat(folder)
    if not os.path.isdir(folder) or os.path.islink(folder) or info.st_uid != os.getuid():
        raise LinkError(f"{folder} is not a folder you own; the key stays off disk")
    os.chmod(folder, KEY_DIR_MODE)
    write_private_file(path, key.encode("ascii"))


def remove_key_file(path: Path) -> None:
    remove_if_present(path)
    remove_dir_if_empty(path.parent)


def remove_dir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
            raise


# --- Commands -----------------------------------------------------------------------------------


def restore_config(paths: ClientPaths, record: dict) -> str:
    """Undo a link record under compare-and-swap; return a one-line summary."""
    outcome = {"restored": [], "notes": []}
    name = paths.config_name

    def plan(text: str | None) -> ConfigPlan:
        if text is None:
            outcome["notes"] = [f"{name} is gone"]
            return ConfigPlan(None)
        if text == record["written_text"]:
            outcome["restored"] = [dotted(entry["path"]) for entry in record["entries"]]
            if record["original"] is None:
                return ConfigPlan(None, delete=True)
            return ConfigPlan(record["original"])
        try:
            data = parse_strict(text, name)
        except LinkError as error:
            raise LinkError(f"{error}; restore it by hand from {SIDECAR_NAME}") from error
        new, outcome["restored"], outcome["notes"] = plan_unlink(data, record)
        if record["original"] is None and not new:
            return ConfigPlan(None, delete=True)
        return ConfigPlan(None if same(new, data) else render(new))

    swap_config(paths.config, plan)
    parts = []
    count = len(outcome["restored"])
    if count:
        parts.append(f"restored {count} key{'s' if count > 1 else ''}")
    parts.extend(outcome["notes"])
    return "; ".join(parts) or "nothing to restore"


def link_request(args: argparse.Namespace, client: Client, environ: dict) -> LinkRequest:
    """Validate the link arguments; refuse before touching any file."""
    if not MODEL_NAME.fullmatch(args.model) or looks_like_secret(args.model):
        raise LinkError("--model must be a model name")
    if not 0 < args.port <= 65535 or args.context_window <= 0:
        raise LinkError("--port must be a TCP port and --context-window > 0")
    owner_pid = os.getppid() if args.owner_pid is None else args.owner_pid
    if owner_pid <= 0:
        raise LinkError("--owner-pid must be > 0")
    command, key_file = "", None
    if client.uses_key_file:
        key_file = opencode_key_file(environ)
        validate_text("the opencode key file path", str(key_file))
        if "}" in str(key_file):
            raise LinkError("the opencode key file path has a '}', which opencode cannot read")
    else:
        command = _api_key_command(args.auth_command, args.auth_arg)
    return LinkRequest(args.model, args.port, args.context_window, owner_pid, command, key_file)


def _api_key_command(auth_command: str | None, auth_args: list) -> str:
    """The shell command Claude Code's apiKeyHelper runs: quoted, absolute, holding no secret."""
    if not auth_command or not os.path.isabs(auth_command):
        raise LinkError("--auth-command must be an absolute path; GUI apps do not get PATH")
    validate_text("--auth-command", auth_command)
    if not (os.path.isfile(auth_command) and os.access(auth_command, os.X_OK)):
        raise LinkError("--auth-command is not an executable file")
    for arg in auth_args:
        validate_text("--auth-arg", arg)
        if looks_like_secret(arg):
            raise LinkError("an --auth-arg looks like a secret; pass a command that reads it")
    return " ".join(shlex.quote(part) for part in [auth_command, *auth_args])


def run_link(args: argparse.Namespace, client: Client, paths: ClientPaths, environ: dict) -> str:
    request = link_request(args, client, environ)
    if not is_installed(client, paths, environ):
        return f"{client.label} config not found; skipping"
    for sibling in client.overriding_files:
        if (paths.home / sibling).exists():
            raise LinkError(f"{sibling} exists and wins over {paths.config_name}; drip-ai edits "
                            "only plain JSON, so add the keys by hand")
    key = read_api_key(sys.stdin) if client.uses_key_file else ""
    created_home = not paths.home.is_dir()
    paths.home.mkdir(parents=True, exist_ok=True)
    lock = LinkLock(paths.lock, client.label)
    notes = [lock.acquire(request.owner_pid)]
    try:
        orphan = read_sidecar(paths, client)
        if orphan is not None:
            notes.append("repaired the previous link: " + restore_config(paths, orphan))
            created_home = created_home or orphan["created_home"]
            remove_if_present(paths.sidecar)
    except BaseException:
        remove_if_present(paths.lock)  # keep the orphan record so unlink can still restore
        raise
    warnings: list = []
    try:
        swap_config(paths.config, lambda text: _link_plan(paths, client, request, key, text,
                                                          created_home, warnings))
    except BaseException:
        remove_if_present(paths.sidecar)  # the config was not swapped, so the record is moot
        if request.key_file is not None:
            remove_key_file(request.key_file)
        remove_if_present(paths.lock)
        if created_home:
            remove_dir_if_empty(paths.home)
        raise
    for warning in warnings:
        print(f"json-link: warning: {warning}", file=sys.stderr)
    summary = f"linked {client.label} to {request.model} (owner pid {request.owner_pid})"
    return "; ".join([summary] + [note for note in notes if note])


def _link_plan(paths: ClientPaths, client: Client, request: LinkRequest, key: str,
               text: str | None, created_home: bool, warnings: list) -> ConfigPlan:
    entries = client.entries(request)
    try:
        data = parse_strict(text or "", paths.config_name)
    except NotStrictJson as error:
        raise LinkError(f"{error}; drip-ai edits only plain JSON. Add this by hand, and remove it "
                        f"when the tunnel closes:\n{hand_snippet(entries, client)}") from error
    warnings[:] = overriding_credentials(client, data)
    new, record_entries, created = plan_link(data, entries, client, paths.config_name)
    new_text = render(new)
    record = {
        "client": client.name,
        "pid": request.owner_pid,
        "host": socket.gethostname(),
        "linked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "original": text,
        "written_text": new_text,
        "created_home": created_home,
        "entries": record_entries,
        "created": created,
    }

    def before_swap() -> None:
        if request.key_file is not None:
            write_key_file(request.key_file, key)
        write_private_file(paths.sidecar, json.dumps(record, indent=2).encode("utf-8"))

    return ConfigPlan(new_text, before_swap)


def overriding_credentials(client: Client, data: dict) -> list:
    """Claude Code sends ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY instead of apiKeyHelper's key."""
    env = get_path(data, ("env",)) if client.name == "claude" else _ABSENT
    return [
        f"settings.json env sets {name}, which Claude Code sends instead of the drip-ai key; "
        "remove it while the tunnel is open"
        for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
        if isinstance(env, dict) and name in env
    ]


def run_unlink(args: argparse.Namespace, client: Client, paths: ClientPaths, environ: dict) -> str:
    if client.uses_key_file:
        remove_key_file(opencode_key_file(environ))  # whatever happens next, the key goes
    if not paths.home.is_dir():
        return f"{client.label} config not found; nothing to unlink"
    try:
        record = read_sidecar(paths, client)
    except LinkError:
        if not args.force:
            raise
        remove_if_present(paths.sidecar)
        remove_if_present(paths.lock)
        return (f"unlinked {client.label} by force: removed the damaged link record without "
                f"restoring; check {paths.config_name} by hand")
    if record is None:
        if args.force:
            remove_if_present(paths.lock)
        else:
            LinkLock(paths.lock, client.label).release_if_stale()
        return f"{client.label} is not linked to drip-ai; nothing to restore"
    summary = restore_config(paths, record)
    remove_if_present(paths.sidecar)
    remove_if_present(paths.lock)
    if record["created_home"]:
        remove_dir_if_empty(paths.home)
    return f"unlinked {client.label}: {summary}"


def _describe_model(client: Client, paths: ClientPaths) -> str:
    try:
        data = read_config_bytes(paths.config)
        if data is None:
            return "model unset"
        value = get_path(parse_strict(decode(data, paths.config_name), paths.config_name),
                         client.model_path)
    except LinkError:
        return "model unknown"
    if value is _ABSENT:
        return "model unset"
    return f"model {value}" if isinstance(value, str) else "model unknown"


def run_status(args: argparse.Namespace, client: Client, paths: ClientPaths, environ: dict) -> str:
    if not paths.home.is_dir():
        return f"unlinked; {client.label} config not found"
    try:
        record = read_sidecar(paths, client)
        state = "linked" if record is not None else "unlinked"
    except LinkError:
        record, state = None, "damaged link record"
    try:
        owner = LinkLock(paths.lock, client.label).read_owner()
    except LinkError:
        return f"{state}; owner unreadable; {_describe_model(client, paths)}"
    if owner is None and record is None:
        owner_text = "no owner"
    else:
        pid, host = owner if owner is not None else (record["pid"], record["host"])
        if host != socket.gethostname():
            owner_text = f"owner pid {pid} on host {host}"
        else:
            owner_text = f"owner pid {pid} {'alive' if is_alive(pid) else 'dead'}"
    return f"{state}; {owner_text}; {_describe_model(client, paths)}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="json_link.py",
        description="Point Claude Code or opencode at the drip-ai model; restore it afterwards.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    link = commands.add_parser("link", help="make the client default to the drip-ai model")
    link.add_argument("--client", choices=sorted(CLIENTS), required=True)
    link.add_argument("--model", required=True)
    link.add_argument("--port", type=int, required=True)
    link.add_argument("--context-window", type=int, required=True)
    link.add_argument("--auth-command", help="Claude Code: absolute path; prints the key")
    link.add_argument(
        "--auth-arg", action="append", default=[], help="once per argument; --auth-arg=--flag"
    )
    link.add_argument("--owner-pid", type=int, help="process that owns the link (default: parent)")
    link.set_defaults(run=run_link)

    unlink = commands.add_parser("unlink", help="restore what link changed")
    unlink.add_argument("--client", choices=sorted(CLIENTS), required=True)
    unlink.add_argument(
        "--force", action="store_true", help="drop a damaged link record or a stuck lock"
    )
    unlink.set_defaults(run=run_unlink)

    status = commands.add_parser("status", help="print whether the client is linked")
    status.add_argument("--client", choices=sorted(CLIENTS), required=True)
    status.set_defaults(run=run_status)
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = CLIENTS[args.client]
    environ = dict(os.environ)
    try:
        paths = client.paths(environ)
        message = args.run(args, client, paths, environ)
    except LinkError as error:
        print(f"json-link: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"json-link: could not update the {client.label} config: {error.strerror}",
              file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
