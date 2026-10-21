"""Layered drip-ai configuration with the provenance of every value.

Layers, lowest first: built-in (shared/config/defaults.json) < derived (the sizing engine and
learning, only for keys whose built-in value is null) < model profile < user file < environment
< command line. defaults.json is also the schema: a key it does not list, or a value of the wrong
type, is refused at the layer that sets it. An explicit value (profile and above) always beats a
derived one.

The user file is $DRIP_AI_CONFIG, else ${XDG_CONFIG_HOME:-~/.config}/drip-ai/config.json: nested
JSON objects whose dotted paths are keys, at most 256 KB, written 0600, never group- or
world-writable. The environment layer reads only the DRIP_AI_* variables defaults.json declares.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional

from drip_ai import REPO_ROOT
from drip_ai.conventions import REGION

DEFAULTS_PATH = REPO_ROOT / "shared" / "config" / "defaults.json"
LAYERS = ("builtin", "derived", "profile", "user", "env", "cli")
EXPLICIT_LAYERS = frozenset(("profile", "user", "env", "cli"))
LAYER_LINE = ("Layers, lowest first: builtin (shared/config/defaults.json) < derived (sizing and learning,"
              " only where builtin is null) < profile (shared/model-profiles) < user (your config file)"
              " < env (DRIP_AI_*) < cli (flags and --set)")
USER_FILE_MAX_BYTES = 256 * 1024
USER_FILE_MODE = 0o600
CLIENT_NAMES = ("codex", "claude", "opencode")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_LIST = re.compile(r"list\[(.+)\]")


class ConfigError(ValueError):
    """A refused key or value; the message is one line naming where it came from."""


@dataclass(frozen=True)
class Key:
    name: str
    type: str
    value: object
    doc: str
    nullable: bool = False
    env: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[tuple] = None

    @property
    def derived(self) -> bool:
        """The engine fills this key when no explicit layer sets it."""
        return self.value is None and not self.nullable


@lru_cache(maxsize=1)
def schema() -> dict:
    with open(DEFAULTS_PATH) as handle:
        document = json.load(handle)
    keys = {}
    for name, entry in document["keys"].items():
        keys[name] = Key(name, entry["type"], entry.get("value"), entry.get("doc", ""),
                         bool(entry.get("nullable")), entry.get("env"), entry.get("min"), entry.get("max"),
                         tuple(entry["choices"]) if "choices" in entry else None)
    return keys


def _check_scalar(key: Key, kind: str, value: object, origin: str) -> object:
    where = f"{key.name} from {origin}"
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be a whole number, not {value!r}")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where} must be a number, not {value!r}")
        value = float(value)
    elif kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false, not {value!r}")
    elif kind == "string":
        if not isinstance(value, str) or not value or any(ord(c) < 0x20 for c in value):
            raise ConfigError(f"{where} must be a one-line string")
    elif kind == "region":
        if not isinstance(value, str) or not REGION.fullmatch(value):
            raise ConfigError(f"{where} names {value!r}, which is not an AWS region")
    elif kind.startswith("enum:"):
        allowed = kind[len("enum:"):].split("|")
        if value not in allowed:
            raise ConfigError(f"{where} names {value!r}; use {', '.join(allowed)}")
    elif kind == "json":
        json.dumps(value)
    else:
        raise ConfigError(f"{key.name} has an unknown type {kind!r} in shared/config/defaults.json")
    if kind in ("int", "number"):
        if key.minimum is not None and value < key.minimum:
            raise ConfigError(f"{where} must be at least {key.minimum:g}")
        if key.maximum is not None and value > key.maximum:
            raise ConfigError(f"{where} must be at most {key.maximum:g}")
        if key.choices is not None and value not in key.choices:
            raise ConfigError(f"{where} must be one of {', '.join(map(str, key.choices))}")
    return value


def check_value(key: Key, value: object, origin: str) -> object:
    """The value, normalised, or ConfigError when it does not fit the key's type."""
    if value is None:
        if key.nullable:
            return None
        raise ConfigError(f"{key.name} from {origin} cannot be null")
    listed = _LIST.fullmatch(key.type)
    if not listed:
        return _check_scalar(key, key.type, value, origin)
    if not isinstance(value, list):
        raise ConfigError(f"{key.name} from {origin} must be a list")
    items = [_check_scalar(key, listed.group(1), item, origin) for item in value]
    if len(set(map(json.dumps, items))) != len(items):
        raise ConfigError(f"{key.name} from {origin} repeats an entry")
    return items


def lookup(name: str, origin: str) -> Key:
    try:
        return schema()[name]
    except KeyError:
        raise ConfigError(f"{origin} sets {name!r}, which is not a drip-ai config key"
                          " (see bin/drip-ai config show)") from None


def parse_text(key: Key, text: str, origin: str) -> object:
    """A value typed as text, from the environment or `--set key=value`."""
    text = text.strip()
    if key.nullable and text == "null":
        return None
    listed = _LIST.fullmatch(key.type)
    kind = listed.group(1) if listed else key.type
    if listed:
        if text.startswith("["):
            return check_value(key, _json(text, key, origin), origin)
        items = [item.strip() for item in text.split(",") if item.strip()]
        return check_value(key, [_scalar_text(key, kind, item, origin) for item in items], origin)
    return check_value(key, _scalar_text(key, kind, text, origin), origin)


def _scalar_text(key: Key, kind: str, text: str, origin: str) -> object:
    try:
        if kind == "int":
            return int(text)
        if kind == "number":
            return float(text)
    except ValueError:
        raise ConfigError(f"{key.name} from {origin} must be a number, not {text!r}") from None
    if kind == "bool":
        if text.lower() in _TRUE:
            return True
        if text.lower() in _FALSE:
            return False
        raise ConfigError(f"{key.name} from {origin} must be true or false, not {text!r}")
    if kind == "json":
        return _json(text, key, origin)
    return text


def _json(text: str, key: Key, origin: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        raise ConfigError(f"{key.name} from {origin} is not valid JSON") from None


def flatten(document: object, origin: str) -> dict:
    """Dotted keys from a nested JSON object; descends until a path names a schema key."""
    if not isinstance(document, dict):
        raise ConfigError(f"{origin} is not a JSON object")
    keys, flat = schema(), {}

    def walk(node: dict, prefix: str) -> None:
        for part, value in node.items():
            name = f"{prefix}{part}"
            if name in keys:
                flat[name] = check_value(keys[name], value, origin)
            elif isinstance(value, dict) and any(k.startswith(name + ".") for k in keys):
                walk(value, name + ".")
            else:
                lookup(name, origin)

    body = dict(document)
    version = body.pop("version", 1)
    if version != 1:
        raise ConfigError(f"{origin} has version {version!r}; this drip-ai reads version 1")
    walk(body, "")
    return flat


def nest(flat: Mapping[str, object]) -> dict:
    """The nested JSON form of dotted keys, as the user file stores them."""
    document: dict = {"version": 1}
    for name in sorted(flat):
        node = document
        parts = name.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = flat[name]
    return document


def user_path(environ: Mapping[str, str]) -> Path:
    explicit = environ.get("DRIP_AI_CONFIG", "")
    if explicit:
        if not os.path.isabs(explicit):
            raise ConfigError("DRIP_AI_CONFIG must be an absolute path")
        return Path(explicit)
    root = environ.get("XDG_CONFIG_HOME", "")
    if not root or not os.path.isabs(root):
        root = os.path.join(environ.get("HOME") or str(Path.home()), ".config")
    return Path(root, "drip-ai", "config.json")


def load_user(environ: Mapping[str, str]) -> dict:
    """The user file's keys; {} when it does not exist."""
    path = user_path(environ)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ConfigError(f"could not open {path} ({error.strerror}); it must be a regular file") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"{path} is not a regular file")
        if info.st_mode & 0o022:
            raise ConfigError(f"{path} is writable by other users; run: chmod 600 {path}")
        data = handle.read(USER_FILE_MAX_BYTES + 1)
    if len(data) > USER_FILE_MAX_BYTES:
        raise ConfigError(f"{path} is larger than {USER_FILE_MAX_BYTES // 1024} KB")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ConfigError(f"{path} is not plain JSON") from None
    return flatten(document, str(path))


def _env_value(key: Key, text: str) -> object:
    origin = key.env
    if key.name == "clients.link":
        lowered = text.strip().lower()
        if lowered == "all":
            return list(CLIENT_NAMES)
        if lowered == "none":
            return []
    return parse_text(key, text, origin)


def load_env(environ: Mapping[str, str]) -> dict:
    found = {}
    for key in schema().values():
        if key.env and environ.get(key.env, "") != "":
            found[key.name] = _env_value(key, environ[key.env])
    return found


def parse_sets(pairs: list) -> dict:
    """`--set key=value` pairs from the command line."""
    found = {}
    for pair in pairs:
        name, sep, text = pair.partition("=")
        if not sep:
            raise ConfigError(f"--set takes key=value, not {pair!r}")
        key = lookup(name.strip(), "--set")
        found[key.name] = parse_text(key, text, "--set")
    return found


@dataclass(frozen=True)
class Resolved:
    values: dict
    sources: dict
    reasons: dict = field(default_factory=dict)

    def __getitem__(self, name: str) -> object:
        return self.values[name]

    def source(self, name: str) -> str:
        return self.sources[name]

    def reason(self, name: str) -> str:
        return self.reasons.get(name, "")

    def is_explicit(self, name: str) -> bool:
        return self.sources[name] in EXPLICIT_LAYERS

    def with_derived(self, derived: Mapping[str, object], reasons: Mapping[str, str]) -> "Resolved":
        """This resolution with the engine's values filled in where no explicit layer set the key."""
        values, sources, why = dict(self.values), dict(self.sources), dict(self.reasons)
        for name, value in derived.items():
            key = lookup(name, "the sizing engine")
            if not key.derived or self.is_explicit(name):
                continue
            values[name] = value if value is None else check_value(key, value, "the sizing engine")
            sources[name] = "derived"
            why[name] = reasons.get(name, "")
        return Resolved(values, sources, why)


def resolve(environ: Mapping[str, str], profile: Optional[Mapping[str, object]] = None,
            cli: Optional[Mapping[str, object]] = None) -> Resolved:
    """Every key's value and layer, without the derived layer (Resolved.with_derived adds it)."""
    values, sources, reasons = {}, {}, {}
    for key in schema().values():
        values[key.name], sources[key.name] = key.value, "builtin"
    layers = (("profile", dict(profile or {})), ("user", load_user(environ)), ("env", load_env(environ)),
              ("cli", dict(cli or {})))
    for layer, found in layers:
        for name, value in found.items():
            key = lookup(name, layer)
            values[name], sources[name] = check_value(key, value, layer), layer
            reasons.pop(name, None)
    return Resolved(values, sources, reasons)


def explain_lines(resolved: Resolved, names: Optional[list] = None) -> list:
    lines = [LAYER_LINE]
    for name in names or sorted(resolved.values):
        value = resolved.values[name]
        shown = "derived later" if value is None and schema()[name].derived else json.dumps(value)
        why = resolved.reason(name) or schema()[name].doc
        lines.append(f"{name} = {shown}  {resolved.source(name)}  ({why})")
    return lines


def write_user(environ: Mapping[str, str], flat: Mapping[str, object]) -> Path:
    """Replaces the user file with these keys, 0600, in a folder made 0700 when created."""
    path = user_path(environ)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(nest(flat), indent=2) + "\n").encode("utf-8")
    if len(data) > USER_FILE_MAX_BYTES:
        raise ConfigError(f"the config would exceed {USER_FILE_MAX_BYTES // 1024} KB")
    fd, temp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), USER_FILE_MODE)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return path


def set_user_values(environ: Mapping[str, str], pairs: list) -> Path:
    """`config set key=value ...`: merges into the user file; `key=` with nothing removes the key."""
    flat = load_user(environ)
    for pair in pairs:
        name, sep, text = pair.partition("=")
        if not sep:
            raise ConfigError(f"config set takes key=value, not {pair!r}")
        key = lookup(name.strip(), "config set")
        if text.strip() == "":
            flat.pop(key.name, None)
        else:
            flat[key.name] = parse_text(key, text, "config set")
    return write_user(environ, flat)


# Legacy sagemaker/terraform/terraform.tfvars tuning and the config key each moves to.
TFVARS_KEYS = {
    "instance_types": "race.instance_types",
    "served_model_name": "serve.served_model_name",
    "max_model_len": "serve.max_model_len",
    "vllm_extra_args": "serve.extra_args",
    "tensor_parallel_size": "serve.tensor_parallel_size",
    "inference_ami_version": "sagemaker.inference_ami_version",
    "startup_timeout_seconds": "sagemaker.startup_timeout_seconds",
    "download_timeout_seconds": "sagemaker.download_timeout_seconds",
    "volume_size_gb": "sagemaker.volume_size_gb",
}
# Still declared by the foundation-only sagemaker root, so they stay in the tfvars file.
TFVARS_KEPT = ("region", "project")
_TFVARS_LINE = re.compile(r"^\s*([a-z_]+)\s*=\s*(.*?)\s*(#.*)?$")


def import_tfvars(environ: Mapping[str, str], tfvars: Path) -> list:
    """Moves the legacy tuning lines of TFVARS into the user file; refuses to overwrite a key the
    user file already sets. Rewrites TFVARS with only TFVARS_KEPT lines, keeping the original as
    terraform.tfvars.pre-import. Returns (tfvars name, config key, value) per moved line."""
    lines = tfvars.read_text().splitlines()
    flat, moved, kept = load_user(environ), [], []
    for line in lines:
        match = _TFVARS_LINE.match(line)
        if not match or match.group(1) not in TFVARS_KEYS:
            if not match or match.group(1) in TFVARS_KEPT:
                kept.append(line)
            elif match.group(1) != "lanes_per_type":
                raise ConfigError(f"{tfvars.name} sets {match.group(1)}, which has no config key; remove it first")
            continue
        name = TFVARS_KEYS[match.group(1)]
        if name in flat:
            raise ConfigError(f"your config already sets {name}; nothing was imported")
        text = match.group(2)
        value = None if text == "null" else _json(text, schema()[name], tfvars.name)
        flat[name] = check_value(schema()[name], value, tfvars.name)
        moved.append((match.group(1), name, flat[name]))
    if not moved:
        return []
    write_user(environ, flat)
    backup = tfvars.with_name(tfvars.name + ".pre-import")
    backup.write_bytes(tfvars.read_bytes())
    os.chmod(backup, USER_FILE_MODE)
    tfvars.write_text("\n".join(kept) + "\n")
    return moved


# --- Model profiles: the profile layer -------------------------------------------------------------

PROFILES_PATH = REPO_ROOT / "shared" / "model-profiles" / "profiles.json"
PROFILES_NAME = "shared/model-profiles/profiles.json"
PROFILE_KEYS = frozenset(("vllm_args", "source", "serve", "weights"))
PROFILE_FLAG = re.compile(r"--[a-z0-9][a-z0-9-]*")
PROFILE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*")
PROFILE_SHAPE = ('{"vllm_args": [plain flags and values], "source": "https://...", optional "serve": {...},'
                 ' optional "weights": "s3" or "hf"}')


def load_profiles(path: Path = PROFILES_PATH) -> dict:
    """{key: entry}, keyed by model_type or by an exact Org/Name@revision; refuses a malformed entry."""
    try:
        with open(path) as handle:
            profiles = json.load(handle)
    except (OSError, ValueError) as error:
        raise ConfigError(f"{PROFILES_NAME} is unreadable ({error})") from None
    if not isinstance(profiles, dict):
        raise ConfigError(f"{PROFILES_NAME} is not a JSON object")
    for name, entry in profiles.items():
        args = entry.get("vllm_args") if isinstance(entry, dict) else None
        source = entry.get("source") if isinstance(entry, dict) else None
        if (not isinstance(args, list) or not isinstance(source, str) or not source.startswith("https://")
                or not {"vllm_args", "source"} <= set(entry) <= PROFILE_KEYS
                or not all(isinstance(a, str) and (PROFILE_FLAG.fullmatch(a) or PROFILE_VALUE.fullmatch(a))
                           for a in args)
                or entry.get("weights", "s3") not in ("s3", "hf")
                or not isinstance(entry.get("serve", {}), dict)):
            raise ConfigError(f"{PROFILES_NAME} entry {name!r} must be {PROFILE_SHAPE}")
        profile_layer(entry, name)
    return profiles


def find_profile(profiles: Mapping, model_type: str, repo_revision: Optional[str]) -> Optional[tuple]:
    """(key, entry): the exact repo@revision entry first, then the model_type entry."""
    for key in (repo_revision, model_type):
        if key and key in profiles:
            return key, profiles[key]
    return None


def profile_layer(entry: Mapping, name: str = "profile") -> dict:
    """The config keys a profile sets: serve.<k> for each "serve" field, and serve.weights."""
    origin = f"{PROFILES_NAME} entry {name!r}"
    layer = {}
    for part, value in (entry.get("serve") or {}).items():
        key = lookup(f"serve.{part}", origin)
        layer[key.name] = check_value(key, value, origin)
    if "weights" in entry:
        layer["serve.weights"] = check_value(lookup("serve.weights", origin), entry["weights"], origin)
    return layer
