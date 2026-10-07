"""Point the user's Codex config at the drip-ai model while it is up, and put it back afterwards.

`link` sets the top-level `model`, `model_provider`, `model_context_window`, and
`model_auto_compact_token_limit` keys and appends a `[model_providers.<id>]` table whose `auth` command reads the API key at use time, so the key never
touches disk. `unlink` restores only what still equals what `link` wrote; `unlink --force` without
a record removes the provider tables and the top-level keys that point Codex at them. Every config
write is a compare-and-swap that keeps all other bytes. It stays one stdlib-only file so
`python3 -I` runs it with nothing to install.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# `python3 -I` leaves this file's folder off sys.path; appended, so it never shadows the stdlib.
sys.path.append(str(Path(__file__).resolve().parent))
from link_files import (  # noqa: E402
    ConfigPlan,
    LinkError as CodexLinkError,
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

MANAGED_KEYS = ("model", "model_provider", "model_context_window", "model_auto_compact_token_limit")
# Codex compacts at this share of the window; it is also Codex's own default (openai/codex,
# ModelInfo::auto_compact_token_limit), written out so the config shows the number.
AUTO_COMPACT_PERCENT = 90
DEFAULT_PROVIDER = "drip-ai"
DEFAULT_MODEL = "drip-coder"  # what drip-ai links; `unlink --force` must guess it without a record
BUILT_IN_MODEL_HINT = (
    "Codex will use its built-in default model; set your own model in config.toml if needed."
)
# ASSUMPTION: a cold `aws ssm get-parameter` returns within 15 s; Codex's own default is 5 s.
DEFAULT_TIMEOUT_MS = 15000
DEFAULT_REFRESH_INTERVAL_MS = 300000  # Codex's default
CONFIG_NAME = "config.toml"
SIDECAR_NAME = "drip-ai-link.json"
LOCK_NAME = "drip-ai-link.lock"
BACKUP_NAME = "config.toml.drip-ai.bak"
HAS_TOMLLIB = sys.version_info >= (3, 11)  # an extra check only; the runtime floor is 3.9

BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
BASE_URL = re.compile(r"https?://[^\s@/]+(/\S*)?")
LOOPBACK_BASE_URL = re.compile(r"http://(127\.0\.0\.1|localhost)(:[0-9]+)?(/\S*)?")
SIMPLE_VALUE = re.compile(
    r"[ \t]*(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)'|([+-]?[0-9](?:_?[0-9])*))[ \t]*(?:#.*)?"
)
EOL_SEPARATORS = frozenset({"", "\n", "\n\n", "\r\n", "\r\n\r\n"})


# --- TOML layout scanner ------------------------------------------------------------------------
# Not a TOML parser: it finds statement boundaries (tracking strings, multi-line strings, and
# bracket depth) so edits touch whole lines and never mistake a '[' inside a value for a header.


@dataclass(frozen=True)
class Statement:
    start: int
    end: int  # after the final line ending, or EOF
    content_end: int  # before the final line ending
    kind: str  # "trivia", "header", or "keyval"
    path: tuple  # header path, or the key path as written
    is_bare_key: bool
    is_multiline: bool
    value_start: int


class _LexState:
    def __init__(self) -> None:
        self.open_delimiter = ""  # '"""' or "'''" while inside a multi-line string
        self.depth = 0

    def is_neutral(self) -> bool:
        return not self.open_delimiter and self.depth == 0


def scan_statements(text: str) -> list:
    statements = []
    pos = 0
    while pos < len(text):
        start = pos
        state = _LexState()
        line_count = 0
        while True:
            newline = text.find("\n", pos)
            line_end = len(text) if newline < 0 else newline + 1
            _lex_line(text, pos, line_end, state)
            pos = line_end
            line_count += 1
            if state.is_neutral() or pos >= len(text):
                break
        if not state.is_neutral():
            raise CodexLinkError("config.toml ends inside a string or array")
        statements.append(_classify(text, start, pos, line_count > 1))
    return statements


def _lex_line(text: str, i: int, line_end: int, state: _LexState) -> None:
    while i < line_end:
        if state.open_delimiter:
            close = _find_multiline_close(text, i, line_end, state.open_delimiter)
            if close < 0:
                return
            state.open_delimiter = ""
            i = close
            continue
        char = text[i]
        if char == "#":
            return
        if char in "\"'":
            if text.startswith(char * 3, i):
                state.open_delimiter = char * 3
                i += 3
            else:
                i = _skip_string(text, i, line_end)
            continue
        if char in "[{":
            state.depth += 1
        elif char in "]}":
            state.depth -= 1
            if state.depth < 0:
                raise CodexLinkError("config.toml has an unbalanced bracket")
        i += 1


def _find_multiline_close(text: str, i: int, end: int, delimiter: str) -> int:
    while i < end:
        if delimiter == '"""' and text[i] == "\\":
            i += 2
            continue
        if text.startswith(delimiter, i):
            close = i + 3
            while close < end and close - i < 5 and text[close] == delimiter[0]:
                close += 1  # TOML allows one or two quotes just before the closing delimiter
            return close
        i += 1
    return -1


def _skip_string(text: str, i: int, end: int) -> int:
    quote = text[i]
    j = i + 1
    while j < end and text[j] != "\n":
        if quote == '"' and text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        j += 1
    raise CodexLinkError("config.toml has an unterminated string")


def _content_end(text: str, start: int, end: int) -> int:
    content_end = end
    if content_end > start and text[content_end - 1] == "\n":
        content_end -= 1
        if content_end > start and text[content_end - 1] == "\r":
            content_end -= 1
    return content_end


def _skip_blank(text: str, i: int, end: int) -> int:
    while i < end and text[i] in " \t":
        i += 1
    return i


def _classify(text: str, start: int, end: int, is_multiline: bool) -> Statement:
    content_end = _content_end(text, start, end)
    i = _skip_blank(text, start, content_end)
    if i >= content_end or text[i] == "#":
        return Statement(start, end, content_end, "trivia", (), False, is_multiline, -1)
    if text[i] == "[":
        closer = "]]" if text.startswith("[[", i) else "]"
        path, j = _parse_key_path(text, i + len(closer), content_end)
        if not text.startswith(closer, _skip_blank(text, j, content_end)):
            raise CodexLinkError("config.toml has a malformed table header")
        return Statement(start, end, content_end, "header", path, False, is_multiline, -1)
    path, j = _parse_key_path(text, i, content_end)
    is_bare_key = BARE_KEY.fullmatch(text[i:j]) is not None
    j = _skip_blank(text, j, content_end)
    if j >= content_end or text[j] != "=":
        raise CodexLinkError("config.toml has a line that is not a key, table, or comment")
    return Statement(start, end, content_end, "keyval", path, is_bare_key, is_multiline, j + 1)


def _parse_key_path(text: str, i: int, end: int) -> tuple:
    parts = []
    while True:
        i = _skip_blank(text, i, end)
        if i < end and text[i] == '"':
            close = _skip_string(text, i, end)
            parts.append(_decode_basic_string(text[i:close]))
            i = close
        elif i < end and text[i] == "'":
            close = text.find("'", i + 1, end)
            if close < 0:
                raise CodexLinkError("config.toml has an unterminated string")
            parts.append(text[i + 1 : close])
            i = close + 1
        else:
            match = BARE_KEY.match(text, i, end)
            if match is None:
                raise CodexLinkError("config.toml has a malformed key")
            parts.append(match.group())
            i = match.end()
        dot = _skip_blank(text, i, end)
        if dot < end and text[dot] == ".":
            i = dot + 1
            continue
        return tuple(parts), i


def _decode_basic_string(quoted: str) -> str | None:
    try:
        return json.loads(quoted, strict=False)
    except ValueError:
        return None  # TOML-only escapes such as \U or \e: never equal to a value we wrote


def simple_value(text: str, statement: Statement) -> tuple | None:
    """("str", s) or ("int", n) for a one-line bare `key = value`; None for anything else."""
    if statement.kind != "keyval" or statement.is_multiline or not statement.is_bare_key:
        return None
    match = SIMPLE_VALUE.fullmatch(text, statement.value_start, statement.content_end)
    if match is None:
        return None
    basic, literal, integer = match.groups()
    if basic is not None:
        decoded = _decode_basic_string(f'"{basic}"')
        return None if decoded is None else ("str", decoded)
    if literal is not None:
        return ("str", literal)
    return ("int", int(integer.replace("_", "")))


def top_level_keys(statements: list) -> dict:
    """Map each managed key to the top-level statements that set it (by first key segment)."""
    found = {key: [] for key in MANAGED_KEYS}
    for statement in statements:
        if statement.kind == "header":
            break
        if statement.kind == "keyval" and statement.path[0] in found:
            found[statement.path[0]].append(statement)
    return found


def full_paths(statements: list) -> list:
    """(statement, absolute key path) for every header and key."""
    table = ()
    paths = []
    for statement in statements:
        if statement.kind == "header":
            table = statement.path
            paths.append((statement, table))
        elif statement.kind == "keyval":
            paths.append((statement, table + statement.path))
    return paths


def headers_named(statements: list, path: tuple) -> list:
    return [s for s in statements if s.kind == "header" and s.path == path]


def table_span_end(statements: list, header: Statement, text_length: int) -> int:
    after = [s.start for s in statements if s.kind == "header" and s.start > header.start]
    return min(after) if after else text_length


def detect_eol(text: str) -> str:
    newline = text.find("\n")
    return "\r\n" if newline > 0 and text[newline - 1] == "\r" else "\n"


def apply_edits(text: str, edits: list) -> str:
    """Apply (start, end, replacement) edits; ties at one offset keep their list order."""
    pieces = []
    cursor = 0
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1])):
        if start < cursor:
            raise CodexLinkError("internal error: overlapping config edits")
        pieces.append(text[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


# --- Link and unlink plans (pure text in, text out) ---------------------------------------------


@dataclass(frozen=True)
class LinkRequest:
    provider: str
    model: str
    base_url: str
    context_window: int
    auth_command: str
    auth_args: tuple
    timeout_ms: int
    refresh_interval_ms: int
    owner_pid: int

    def written(self) -> dict:
        return {
            "model": self.model,
            "model_provider": self.provider,
            "model_context_window": self.context_window,
            "model_auto_compact_token_limit": self.context_window * AUTO_COMPACT_PERCENT // 100,
        }


def toml_value(value: object) -> str:
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)  # inputs carry no control characters


def provider_tables(request: LinkRequest, eol: str) -> str:
    args = ", ".join(toml_value(arg) for arg in request.auth_args)
    lines = [
        f"[model_providers.{request.provider}]",
        f"name = {toml_value(request.provider)}",
        f"base_url = {toml_value(request.base_url)}",
        'wire_api = "responses"',
        "",
        f"[model_providers.{request.provider}.auth]",
        f"command = {toml_value(request.auth_command)}",
        f"args = [{args}]",
        f"timeout_ms = {request.timeout_ms}",
        f"refresh_interval_ms = {request.refresh_interval_ms}",
    ]
    return eol.join(lines) + eol


def _refuse_existing_provider(statements: list, provider: str) -> None:
    for statement, path in full_paths(statements):
        is_inline_providers = statement.kind == "keyval" and path == ("model_providers",)
        if is_inline_providers or path[:2] == ("model_providers", provider):
            raise CodexLinkError(
                f"config.toml already defines model_providers.{provider} and drip-ai did not "
                "write it; remove it by hand first"
            )


def _insertion_offset(text: str, statements: list) -> int:
    for statement in statements:
        if statement.kind == "header":
            return statement.start
    return len(text) if not text or text.endswith("\n") else 0


def plan_link(text: str, request: LinkRequest) -> tuple:
    """Return (new text, link record fields) or refuse."""
    statements = scan_statements(text)
    _refuse_existing_provider(statements, request.provider)
    eol = detect_eol(text)
    found = top_level_keys(statements)
    written = request.written()
    edits = []
    previous = {}
    inserted = []
    for key in MANAGED_KEYS:
        line = f"{key} = {toml_value(written[key])}"
        if len(found[key]) > 1:
            raise CodexLinkError(f"config.toml sets {key} more than once")
        if not found[key]:
            previous[key] = None
            inserted.append(line + eol)
            continue
        statement = found[key][0]
        if len(statement.path) != 1 or simple_value(text, statement) is None:
            raise CodexLinkError(
                f"{key} in config.toml is not a simple one-line value; simplify it by hand first"
            )
        previous[key] = text[statement.start : statement.content_end]
        edits.append((statement.start, statement.content_end, line))
    if inserted:
        offset = _insertion_offset(text, statements)
        edits.append((offset, offset, "".join(inserted)))
    tables = provider_tables(request, eol)
    separator = "" if not text else (eol if text.endswith("\n") else eol + eol)
    edits.append((len(text), len(text), separator + tables))
    fields = {"previous": previous, "written": written, "tables": tables, "separator": separator}
    return apply_edits(text, edits), fields


def _typed(value: object) -> tuple:
    return ("int", value) if isinstance(value, int) else ("str", value)


def _plan_key_restores(text: str, statements: list, record: dict) -> tuple:
    edits, restored, notes = [], [], []
    found = top_level_keys(statements)
    for key in MANAGED_KEYS:
        previous = record["previous"][key]
        current = found[key]
        if not current:
            if previous is not None:
                notes.append(f"kept your change to {key}")
            continue
        if len(current) != 1 or simple_value(text, current[0]) != _typed(record["written"][key]):
            notes.append(f"kept your change to {key}")
            continue
        statement = current[0]
        if previous is None:
            edits.append((statement.start, statement.end, ""))
        else:
            edits.append((statement.start, statement.content_end, previous))
        restored.append(key)
    return edits, restored, notes


def _split_recorded_tables(record: dict) -> list:
    """[(header path, exact text)] for the two tables link appended."""
    tables = record["tables"]
    provider = record["provider"]
    statements = scan_statements(tables)
    headers = [s for s in statements if s.kind == "header"]
    expected = [("model_providers", provider), ("model_providers", provider, "auth")]
    if [h.path for h in headers] != expected or headers[0].start != 0:
        raise CodexLinkError(f"{SIDECAR_NAME} is damaged; run unlink --force")
    return [
        (header.path, tables[header.start : table_span_end(statements, header, len(tables))])
        for header in headers
    ]


def _plan_table_removal(text: str, statements: list, record: dict) -> tuple:
    """Return (edits, note). Both tables go together, or neither, so Codex never sees half."""
    label = f"[model_providers.{record['provider']}]"
    spans = []
    for path, expected in _split_recorded_tables(record):
        matches = headers_named(statements, path)
        if not matches:
            spans.append(None)
            continue
        header = matches[0]
        segment = text[header.start : table_span_end(statements, header, len(text))]
        rest = segment[len(expected) :]
        is_ours = (
            len(matches) == 1
            and segment.startswith(expected)
            and all(s.kind == "trivia" for s in scan_statements(rest))
        )
        if not is_ours:
            return [], f"kept {label} because it changed"
        spans.append((header.start, header.start + len(expected)))
    if spans == [None, None]:
        return [], ""
    if None in spans:
        return [], f"kept {label} because it changed"
    edits = [(start, end, "") for start, end in spans]
    first_start = spans[0][0]
    separator = record["separator"]
    sep_start = first_start - len(separator)
    is_tail = spans[1][0] == spans[0][1] and spans[1][1] == len(text)
    if (
        separator
        and sep_start >= 0
        and text[sep_start:first_start] == separator
        and (sep_start == 0 or text[sep_start - 1] == "\n" or is_tail)
    ):
        edits.append((sep_start, first_start, ""))
    return edits, f"removed {label}"


def plan_unlink(text: str, record: dict) -> tuple:
    """Return (new text, restored keys, notes)."""
    statements = scan_statements(text)
    key_edits, restored, notes = _plan_key_restores(text, statements, record)
    table_edits, table_note = _plan_table_removal(text, statements, record)
    if table_note:
        notes.append(table_note)
    return apply_edits(text, key_edits + table_edits), restored, notes


def _body(statements: list, header: Statement, text_length: int) -> list:
    end = table_span_end(statements, header, text_length)
    return [s for s in statements if header.start < s.start < end and s.kind == "keyval"]


def _is_str(value: tuple | None) -> bool:
    return value is not None and value[0] == "str"


def _is_int(value: tuple | None) -> bool:
    return value is not None and value[0] == "int"


# The exact shape link writes: key -> rule(simple value, raw value text).
FORCE_PROVIDER_RULES = {
    "name": lambda value, raw: _is_str(value),
    "base_url": lambda value, raw: _is_str(value) and bool(LOOPBACK_BASE_URL.fullmatch(value[1])),
    "wire_api": lambda value, raw: value == ("str", "responses"),
}
FORCE_AUTH_RULES = {
    "command": lambda value, raw: _is_str(value) and os.path.isabs(value[1]),
    "args": lambda value, raw: raw.startswith("[") and raw.endswith("]"),
    "timeout_ms": lambda value, raw: _is_int(value),
    "refresh_interval_ms": lambda value, raw: _is_int(value),
}


def _has_force_shape(text: str, body: list, rules: dict) -> bool:
    if sorted(s.path for s in body) != sorted((key,) for key in rules):
        return False
    for statement in body:
        if statement.is_multiline or not statement.is_bare_key:
            return False
        raw = text[statement.value_start : statement.content_end].strip()
        if not rules[statement.path[0]](simple_value(text, statement), raw):
            return False
    return True


def _sole_plain_value(text: str, found: dict, key: str) -> tuple | None:
    """The simple value of the one plain top-level `key = value` line; None otherwise."""
    statements = found[key]
    if len(statements) != 1 or len(statements[0].path) != 1:
        return None
    return simple_value(text, statements[0])


def _whole_line(statement: Statement) -> tuple:
    return (statement.start, statement.end, "")


def _plan_dangling_key_removal(text: str, statements: list, provider: str, model: str) -> tuple:
    """Return (edits, notes) for the top-level keys that make Codex use a provider being removed.

    Without a record only `model_provider` proves the keys are ours; `model` also has to equal the
    name `link` was given, and the two window keys go with a matched `model_provider`.
    """
    found = top_level_keys(statements)
    provider_value = toml_value(provider)
    if _sole_plain_value(text, found, "model_provider") != ("str", provider):
        if not found["model_provider"]:
            return [], []
        return [], [f"kept model_provider: it is not exactly {provider_value}"]
    edits = [_whole_line(found["model_provider"][0])]
    notes = [f"removed model_provider = {provider_value}"]
    is_model_kept = False
    if _sole_plain_value(text, found, "model") == ("str", model):
        edits.append(_whole_line(found["model"][0]))
        notes.append(f"removed model = {toml_value(model)}")
    elif found["model"]:
        is_model_kept = True
        notes.append(f"kept model: it is not exactly {toml_value(model)}")
    for key in ("model_context_window", "model_auto_compact_token_limit"):
        window = _sole_plain_value(text, found, key)
        if _is_int(window):
            edits.append(_whole_line(found[key][0]))
            notes.append(f"removed {key} = {window[1]}")
        elif found[key]:
            notes.append(f"kept {key}: it is not a plain number")
    if not is_model_kept:
        notes.append(BUILT_IN_MODEL_HINT)
    return edits, notes


def plan_force_unlink(text: str, provider: str, model: str) -> tuple:
    """Remove the provider tables only when they have exactly the shape link writes, and with them
    the top-level keys that would point Codex at a provider that no longer exists.

    Return (new text, one note per reported line).
    """
    statements = scan_statements(text)
    label = f"[model_providers.{provider}]"
    provider_path = ("model_providers", provider)
    owned = [s for s, path in full_paths(statements) if path[:2] == provider_path]
    if not owned:
        return text, [f"no {label} in config.toml"]
    providers = headers_named(statements, provider_path)
    auths = headers_named(statements, provider_path + ("auth",))
    not_ours = CodexLinkError(f"left {label}: it is not the shape drip-ai writes")
    if len(providers) != 1 or len(auths) != 1:
        raise not_ours
    tables = [
        (providers[0], _body(statements, providers[0], len(text)), FORCE_PROVIDER_RULES),
        (auths[0], _body(statements, auths[0], len(text)), FORCE_AUTH_RULES),
    ]
    if len(owned) != sum(1 + len(body) for _, body, _ in tables):
        raise not_ours  # another subtable or a dotted key also writes into this provider
    edits = []
    for header, body, rules in tables:
        if not _has_force_shape(text, body, rules):
            raise not_ours
        edits.append((header.start, body[-1].end, ""))
    key_edits, key_notes = _plan_dangling_key_removal(text, statements, provider, model)
    return apply_edits(text, edits + key_edits), [f"removed {label}"] + key_notes


# --- Files: paths, link record, and parsing helpers ---------------------------------------


@dataclass(frozen=True)
class CodexPaths:
    home: Path

    @property
    def config(self) -> Path:
        return self.home / CONFIG_NAME

    @property
    def sidecar(self) -> Path:
        return self.home / SIDECAR_NAME

    @property
    def lock(self) -> Path:
        return self.home / LOCK_NAME

    @property
    def backup(self) -> Path:
        return self.home / BACKUP_NAME

    @property
    def link_lock(self) -> LinkLock:
        return LinkLock(self.lock, "Codex")


def codex_paths(environ: dict) -> CodexPaths:
    """$CODEX_HOME when set and absolute, else $HOME/.codex, as Codex resolves it."""
    configured = environ.get("CODEX_HOME", "")
    if configured and os.path.isabs(configured):
        return CodexPaths(Path(configured))
    return CodexPaths(Path(environ.get("HOME") or str(Path.home())) / ".codex")


def parse_toml(text: str) -> dict | None:
    """Parsed TOML, or None when it does not parse. Call only when HAS_TOMLLIB."""
    import tomllib

    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _read_json(path: Path, label: str) -> object:
    data = read_config_bytes(path)
    if data is None:
        return None
    try:
        return json.loads(data)
    except ValueError as error:
        raise CodexLinkError(f"{label} is damaged; run unlink --force") from error


def read_sidecar(paths: CodexPaths) -> dict | None:
    """The link record, None when absent; refuses a damaged one."""
    record = _read_json(paths.sidecar, SIDECAR_NAME)
    if record is None:
        return None
    if not _is_valid_record(record):
        raise CodexLinkError(f"{SIDECAR_NAME} is damaged; run unlink --force")
    return record


def _is_valid_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    previous, written = record.get("previous"), record.get("written")
    if not isinstance(previous, dict) or not isinstance(written, dict):
        return False
    return (
        isinstance(record.get("pid"), int)
        and isinstance(record.get("host"), str)
        and isinstance(record.get("provider"), str)
        and BARE_KEY.fullmatch(record["provider"]) is not None
        and isinstance(record.get("tables"), str)
        and record.get("separator") in EOL_SEPARATORS
        and all(
            previous.get(key) is None
            or (isinstance(previous[key], str) and not set(previous[key]) & {"\n", "\r"})
            for key in MANAGED_KEYS
        )
        and set(previous) == set(MANAGED_KEYS)
        and isinstance(written.get("model"), str)
        and isinstance(written.get("model_provider"), str)
        and type(written.get("model_context_window")) is int
        and type(written.get("model_auto_compact_token_limit")) is int
    )


# --- Commands ------------------------------------------------------------------------------------


def restore_config(paths: CodexPaths, record: dict) -> str:
    """Undo a link record under compare-and-swap; return a one-line summary."""
    outcome = {"restored": [], "notes": []}

    def plan(text: str | None) -> ConfigPlan:
        if text is None:
            outcome["notes"] = ["config.toml is gone"]
            return ConfigPlan(None)
        new_text, restored, notes = plan_unlink(text, record)
        _refuse_breaking(text, new_text)
        outcome["restored"], outcome["notes"] = restored, notes
        return ConfigPlan(new_text if new_text != text else None)

    swap_config(paths.config, plan)
    parts = []
    if outcome["restored"]:
        parts.append("restored " + ", ".join(outcome["restored"]))
    parts.extend(outcome["notes"])
    return "; ".join(parts) or "nothing to restore"


def _refuse_breaking(text: str, new_text: str) -> None:
    """On Python 3.11+, refuse an edit that would turn parseable TOML into unparseable TOML."""
    if HAS_TOMLLIB and parse_toml(text) is not None and parse_toml(new_text) is None:
        raise CodexLinkError("the edit would leave config.toml unparseable; nothing written")


def link_request(args: argparse.Namespace) -> LinkRequest:
    """Validate the link arguments; refuse before touching any file."""
    if BARE_KEY.fullmatch(args.provider) is None:
        raise CodexLinkError("--provider must be letters, digits, '-' or '_'")
    for name, value in (("--model", args.model), ("--base-url", args.base_url)):
        validate_text(name, value)
        if not value:
            raise CodexLinkError(f"{name} is empty")
    if BASE_URL.fullmatch(args.base_url) is None:
        raise CodexLinkError("--base-url must be an http(s) URL without credentials")
    if not os.path.isabs(args.auth_command):
        raise CodexLinkError("--auth-command must be an absolute path; GUI apps do not get PATH")
    validate_text("--auth-command", args.auth_command)
    if not (os.path.isfile(args.auth_command) and os.access(args.auth_command, os.X_OK)):
        raise CodexLinkError("--auth-command is not an executable file")
    for arg in args.auth_arg:
        validate_text("--auth-arg", arg)
        if looks_like_secret(arg):
            raise CodexLinkError("an --auth-arg looks like a secret; pass a command that reads it")
    if looks_like_secret(args.base_url) or looks_like_secret(args.model):
        raise CodexLinkError("--model or --base-url looks like a secret")
    if args.context_window <= 0 or args.timeout_ms <= 0 or args.refresh_interval_ms < 0:
        raise CodexLinkError("--context-window and --timeout-ms must be > 0, refresh >= 0")
    owner_pid = os.getppid() if args.owner_pid is None else args.owner_pid
    if owner_pid <= 0:
        raise CodexLinkError("--owner-pid must be > 0")
    return LinkRequest(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        context_window=args.context_window,
        auth_command=args.auth_command,
        auth_args=tuple(args.auth_arg),
        timeout_ms=args.timeout_ms,
        refresh_interval_ms=args.refresh_interval_ms,
        owner_pid=owner_pid,
    )


def _expected_provider(request: LinkRequest) -> dict:
    return {
        "name": request.provider,
        "base_url": request.base_url,
        "wire_api": "responses",
        "auth": {
            "command": request.auth_command,
            "args": list(request.auth_args),
            "timeout_ms": request.timeout_ms,
            "refresh_interval_ms": request.refresh_interval_ms,
        },
    }


def run_link(args: argparse.Namespace, paths: CodexPaths) -> str:
    request = link_request(args)
    if not paths.home.is_dir():
        return "Codex config not found; skipping"
    notes = [paths.link_lock.acquire(request.owner_pid)]
    try:
        orphan = read_sidecar(paths)
        if orphan is not None:
            notes.append("repaired the previous link: " + restore_config(paths, orphan))
            remove_if_present(paths.sidecar)
    except BaseException:
        remove_if_present(paths.lock)  # keep the orphan record so unlink can still restore
        raise
    try:
        swap_config(paths.config, lambda text: _link_plan(paths, request, text))
    except BaseException:
        remove_if_present(paths.sidecar)  # config.toml was not swapped, so the record is moot
        remove_if_present(paths.lock)
        raise
    summary = f"linked Codex to {request.model} via {request.provider}"
    summary += f" (owner pid {request.owner_pid})"
    return "; ".join([summary] + [note for note in notes if note])


def _link_plan(paths: CodexPaths, request: LinkRequest, text: str | None) -> ConfigPlan:
    new_text, fields = plan_link(text or "", request)
    if HAS_TOMLLIB:
        if text is not None and parse_toml(text) is None:
            raise CodexLinkError("config.toml does not parse as TOML; fix it first")
        data = parse_toml(new_text) or {}
        providers = data.get("model_providers", {})
        landed = {key: data.get(key) for key in MANAGED_KEYS} == request.written()
        if not landed or providers.get(request.provider) != _expected_provider(request):
            raise CodexLinkError("the linked config.toml did not check out; nothing written")
    record = {
        "pid": request.owner_pid,
        "host": socket.gethostname(),
        "linked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": request.provider,
        **fields,
    }

    def before_swap() -> None:
        if text is not None:
            write_private_file(paths.backup, text.encode("utf-8"))
        write_private_file(paths.sidecar, json.dumps(record, indent=2).encode("utf-8"))

    return ConfigPlan(new_text, before_swap)


def run_unlink(args: argparse.Namespace, paths: CodexPaths) -> str:
    if not paths.home.is_dir():
        return "Codex config not found; nothing to unlink"
    try:
        record = read_sidecar(paths)
    except CodexLinkError:
        if not args.force:
            raise
        record = None
    if record is not None:
        summary = restore_config(paths, record)
        remove_if_present(paths.sidecar)
        remove_if_present(paths.lock)
        return f"unlinked Codex: {summary}"
    if not args.force:
        paths.link_lock.release_if_stale()
        return "Codex is not linked to drip-ai; nothing to restore"
    outcome = {"notes": []}

    def plan(text: str | None) -> ConfigPlan:
        if text is None:
            outcome["notes"] = ["config.toml is gone"]
            return ConfigPlan(None)
        new_text, outcome["notes"] = plan_force_unlink(text, args.provider, args.model)
        _refuse_breaking(text, new_text)
        return ConfigPlan(new_text if new_text != text else None)

    swap_config(paths.config, plan)
    remove_if_present(paths.sidecar)
    remove_if_present(paths.lock)
    first, *rest = outcome["notes"]
    return "\n".join([f"unlinked Codex by force: {first}", *rest])


def _describe_current(paths: CodexPaths) -> str:
    try:
        data = read_config_bytes(paths.config)
        if data is None:
            return "model unset; model_provider unset"
        text = decode(data, CONFIG_NAME)
        found = top_level_keys(scan_statements(text))
    except CodexLinkError:
        return "model unknown; model_provider unknown"
    described = []
    for key in ("model", "model_provider"):
        statements = found[key]
        value = simple_value(text, statements[0]) if len(statements) == 1 else None
        shown = "unset" if not statements else ("unknown" if value is None else str(value[1]))
        described.append(f"{key} {shown}")
    return "; ".join(described)


def run_status(args: argparse.Namespace, paths: CodexPaths) -> str:
    if not paths.home.is_dir():
        return "unlinked; Codex config not found"
    try:
        record = read_sidecar(paths)
        state = "linked" if record is not None else "unlinked"
    except CodexLinkError:
        record, state = None, "damaged link record"
    try:
        owner = paths.link_lock.read_owner()
    except CodexLinkError:
        return f"{state}; owner unreadable; {_describe_current(paths)}"
    if owner is None and record is None:
        owner_text = "no owner"
    else:
        pid, host = owner if owner is not None else (record["pid"], record["host"])
        if host != socket.gethostname():
            owner_text = f"owner pid {pid} on host {host}"
        else:
            owner_text = f"owner pid {pid} {'alive' if is_alive(pid) else 'dead'}"
    return f"{state}; {owner_text}; {_describe_current(paths)}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex_link.py",
        description="Point Codex at the drip-ai model while it is up; restore it afterwards.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    link = commands.add_parser("link", help="make Codex default to the drip-ai provider")
    link.add_argument("--provider", default=DEFAULT_PROVIDER)
    link.add_argument("--model", required=True)
    link.add_argument("--base-url", required=True)
    link.add_argument("--context-window", type=int, required=True)
    link.add_argument("--auth-command", required=True, help="absolute path; prints the key")
    link.add_argument(
        "--auth-arg", action="append", default=[], help="once per argument; --auth-arg=--flag"
    )
    link.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    link.add_argument("--refresh-interval-ms", type=int, default=DEFAULT_REFRESH_INTERVAL_MS)
    link.add_argument("--owner-pid", type=int, help="process that owns the link (default: parent)")
    link.set_defaults(run=run_link)

    unlink = commands.add_parser("unlink", help="restore what link changed")
    unlink.add_argument("--provider", default=DEFAULT_PROVIDER)
    unlink.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="with --force and no link record, remove `model` only when it equals this name",
    )
    unlink.add_argument(
        "--force",
        action="store_true",
        help="without a link record, remove drip-ai's tables and the keys that point at them",
    )
    unlink.set_defaults(run=run_unlink)

    status = commands.add_parser("status", help="print whether Codex is linked")
    status.set_defaults(run=run_status)
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = codex_paths(dict(os.environ))
    try:
        message = args.run(args, paths)
    except CodexLinkError as error:
        print(f"codex-link: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"codex-link: could not update the Codex config: {error.strerror}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
