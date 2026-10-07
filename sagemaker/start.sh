#!/usr/bin/env bash
# Starts the SageMaker endpoint from a model already in the S3 models bucket by racing one endpoint
# per instance_types entry, then shows a live, read-only dashboard of the winner. Design:
# docs/DECISIONS.md, 2026-10-05 "Folders per backend, Terraform roots per stack", and 2026-10-06
# "Race instance types". Bash 3.2 and 5.
#
# Before any Terraform call it reads the StartedBy tag of every drip-ai endpoint: the caller's own
# opens the dashboard, and anyone else's is refused unless --take-over, each typed endpoint name,
# and the stop path. The model and the instance types it fits go to `terraform plan` as -var on
# each run; no tfvars file is written. The user types yes to the saved plan, or passed --yes, and
# it is applied in the background while the race loop below decides the winner. --no-watch returns
# instead of opening the dashboard, so `bin/drip-ai up` can open the tunnel next.
#
# Test hooks: DRIP_AI_NOW_EPOCH fixes "now", DRIP_AI_WATCH_ITERATIONS stops the dashboard after
# that many refreshes, DRIP_AI_TTY replaces /dev/tty as the picker's, take-over's, and approval's
# input, and DRIP_AI_RACE_POLL_SECONDS (15), DRIP_AI_RACE_CAP_SECONDS (10800), and
# DRIP_AI_RACE_APPLY_WAIT_SECONDS (300) shorten the race's poll, cap, and wait for Terraform.
#
# The file holds one embedded Python program (start_py) beside the shell flow, because the
# operator scripts ship as stdlib-only files that `python3 -I` runs with nothing to install.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../shared/lib/common.sh
source "$repo_root/shared/lib/common.sh"
# The endpoint-owner functions (read_endpoint_owner, sanitize_tag_value, open_tty, ...).
# shellcheck source=./endpoint.sh
source "$repo_root/sagemaker/endpoint.sh"

local_port="${DRIP_AI_PORT:-8000}"
bucket_pattern='^[a-z0-9.-]+$'
instance_type_pattern='^ml\.[a-z0-9.-]+$'
list_pattern='^\[(.*)\]$'
# shared/terraform/main.tf stores "sk-" plus a random_password with special = false; a bare
# alphanumeric key still passes. Anything else is refused rather than quoted into a curl config line.
api_key_pattern='^(sk-)?[A-Za-z0-9]+$'
price_pattern='\$([0-9]+(\.[0-9]+)?)/h'
teardown='bin/drip-ai sagemaker stop'
profiles_file="$repo_root/shared/model-profiles/profiles.json"
architectures_file="$repo_root/shared/model-profiles/vllm-architectures.json"
race_poll_seconds="${DRIP_AI_RACE_POLL_SECONDS:-15}"
race_cap_seconds="${DRIP_AI_RACE_CAP_SECONDS:-10800}"
race_apply_wait_seconds="${DRIP_AI_RACE_APPLY_WAIT_SECONDS:-300}"
# Race state the signal traps and the loop share.
race_signals=0 race_exit=0 apply_pid="" apply_signalled=no plan_dir="" take_over_confirmed=no
# --no-watch sets this to no. --yes sets endpoint.sh's assume_yes, which answers the race's own
# question and the take-over's destroy.
open_dashboard=yes

usage() {
  cat <<'EOF_USAGE'
Usage: sagemaker/start.sh [--take-over] [--unchecked-model] [--yes] [--no-watch]
                                                               pick a model from the S3 bucket, then start it
       sagemaker/start.sh <name> [--no-manifest] [--take-over] [--unchecked-model] [--yes] [--no-watch]
                                                               start models/<name>/ from the S3 bucket
       sagemaker/start.sh --list                               list the models in the S3 bucket
       sagemaker/start.sh --watch                              show the live endpoint dashboard

<name> is a folder under s3://<models bucket>/models/ (name or Org/Name). Without --no-manifest
it must be a complete copy made by shared/fetch-model/fetch-model.sh. --no-manifest accepts a folder uploaded
with shared/models.sh upload (config.json and *.safetensors at the top).
The model's config.json must name an architecture the pinned vLLM registers and a model_type
with a profile in shared/model-profiles/profiles.json, whose vLLM flags go to Terraform;
--unchecked-model skips both checks and uses only vllm_extra_args.
Starting plans one endpoint per instance_types entry whose GPU memory holds the weights at 90%,
skipping the others, with the model as -var, shows each one's price, and creates them all once you
type yes (--yes answers that question; it still prints the plan and prices first). The first
InService is kept and the others are deleted; Ctrl-C before that deletes them all. Your own
running endpoint opens the dashboard instead; someone else's is refused unless --take-over and
you type each endpoint name, which stops theirs first; --yes never types a name. --no-watch
returns instead of opening the dashboard and exits 0 only when your endpoint exists. Tuning
(instance_types and the rest) lives in the optional sagemaker/terraform/terraform.tfvars. The
dashboard is read-only; DRIP_AI_WATCH_SECONDS sets its refresh (default 15, min 5).
EOF_USAGE
}

fail() { echo "start-sagemaker: $*" >&2; exit 1; }

# GPU memory per instance in GB: 8x H200 (141 GB) and 8x H100 (80 GB). Unlisted types are not checked.
gpu_memory_gb() {
  case "$1" in
    ml.p5en.48xlarge | ml.p5e.48xlarge) echo 1128 ;;
    ml.p5.48xlarge) echo 640 ;;
    *) return 1 ;;
  esac
}

# For the model table: sets fit_instance to the lane with the most GPU memory and fit_limit to the
# weight bytes it allows (90% of that memory), because start races every lane that holds the
# model and skips the rest. When a lane's memory is not recorded, fit_instance names it and
# fit_limit is empty, so fit is not checked. Needs instance_types.
set_fit() {
  local type gb most=""
  fit_instance="" fit_limit=""
  for type in $instance_types; do
    gb="$(gpu_memory_gb "$type")" || { fit_instance="$type"; return 0; }
    [ -n "$most" ] && [ "$gb" -le "$most" ] || { most="$gb"; fit_instance="$type"; }
  done
  fit_limit=$((most * 900000000))
}

# Prints the hourly list price as a number, parsed from the one price table in common.sh.
hourly_price() {
  local line
  line="$(endpoint_hourly_price "$1")"
  [[ "$line" =~ $price_pattern ]] || return 1
  echo "${BASH_REMATCH[1]}"
}

# Prints the configured instance_types, space-separated, without Terraform: a one-line
# `instance_types = [...]` in sagemaker/terraform/terraform.tfvars, else TF_VAR_instance_types, else
# the default in variables.tf (Terraform's order). The plan gets the ones the model fits as -var,
# which outranks every tfvars file.
configured_instance_types() {
  local dir="$repo_root/sagemaker/terraform" value="" types
  if [ -f "$dir/terraform.tfvars" ]; then
    value="$(awk '/^[[:space:]]*instance_types[[:space:]]*=/ {
      sub(/^[^=]*=[[:space:]]*/, ""); sub(/[[:space:]]*#.*$/, ""); print; exit }' "$dir/terraform.tfvars")"
  fi
  [ -n "$value" ] || value="${TF_VAR_instance_types:-}"
  [ -n "$value" ] || value="$(awk '/^variable "instance_types"/ { inside = 1 }
    inside && /^[[:space:]]*default[[:space:]]*=/ { sub(/^[^=]*=[[:space:]]*/, ""); print; exit }
    inside && /^}/ { exit }' "$dir/variables.tf")"
  [[ "$value" =~ $list_pattern ]] || return 1
  types="$(printf '%s' "${BASH_REMATCH[1]}" | tr -s ',"[:space:]' '\n' | sed '/^$/d')"
  [ -n "$types" ] && ! printf '%s\n' "$types" | grep -Evq "$instance_type_pattern" || return 1
  echo $types
}

# Sets bucket, instance_types, fit_instance, and fit_limit. Refuses the instance_type key that
# instance_types replaced, because Terraform only warns about it and would race the default list.
# Needs region_name and the caller identity loaded in this shell.
resolve_model_store() {
  ! grep -Eqs '^[[:space:]]*instance_type[[:space:]]*=' "$repo_root/sagemaker/terraform/terraform.tfvars" ||
    fail "sagemaker/terraform/terraform.tfvars sets instance_type, which is now a list raced in order; replace that line with: instance_types = [\"ml.p5en.48xlarge\", \"ml.p5e.48xlarge\"]"
  bucket="$(tf_output shared models_bucket)" ||
    fail "no models_bucket output in the shared Terraform root; apply shared/terraform first"
  [[ "$bucket" =~ $bucket_pattern ]] || fail "the models_bucket output is not a bucket name"
  instance_types="$(configured_instance_types)" ||
    fail "no valid one-line instance_types list in sagemaker/terraform/terraform.tfvars, TF_VAR_instance_types, or its default"
  set_fit
}

start_py() {
  python3 -I - "$@" <<'PY'
"""Read-only S3 checks, the model table, and the dashboard frame for sagemaker/start.sh."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

STATUS_OBJECT = "drip-ai-fetch.json"
AWS_TIMEOUT_SECONDS = 120
DOWNLOAD_CAP_SECONDS = 3600  # SageMaker's maximum model_data_download_timeout_in_seconds
# ASSUMPTION: below this S3 read rate a load has never been seen to fail the cap; above it, warn.
DOWNLOAD_WARN_BYTES_PER_SECOND = 150 * 10**6
IDLE_SECONDS = 30 * 60
TEARDOWN = "bin/drip-ai sagemaker stop"
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
GB = 10**9
PROFILES_NAME = "shared/model-profiles/profiles.json"
MODEL_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
# Profile args reach Terraform as an HCL list, so a value carries no quote, backslash, '$', or '%'.
PROFILE_FLAG = re.compile(r"--[a-z0-9][a-z0-9-]*")
PROFILE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*")
PROFILE_TEMPLATE = ('"{model_type}": {{"vllm_args": ["--enable-auto-tool-choice", "--tool-call-parser",'
                    ' "<parser>", "--reasoning-parser", "<parser>"], "source": "<model card URL>"}},')


class Refusal(Exception):
    """A start-sagemaker refusal; the message is one user-facing line."""


def clean(text, limit=160):
    """Last non-empty line of untrusted text, without control characters, at most `limit` chars."""
    lines = [line for line in CONTROL.sub("", (text or "").replace("\t", " ")).splitlines() if line.strip()]
    return lines[-1].strip()[:limit] if lines else ""


def gigabytes(size):
    return f"{size / GB:.1f} GB"


def aws(*args):
    proc = subprocess.run(["aws", *args], capture_output=True, text=True, timeout=AWS_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise Refusal(clean(proc.stderr) or f"aws exited {proc.returncode}")
    return proc.stdout


def list_prefix(region, bucket, prefix, delimited=False):
    """Returns ({relative key: size}, [sub-prefixes]). The CLI follows every page itself."""
    args = ["s3api", "list-objects-v2", "--region", region, "--bucket", bucket, "--prefix", prefix]
    if delimited:
        args += ["--delimiter", "/"]
    body = json.loads(aws(*args, "--output", "json") or "{}") or {}
    sizes = {item["Key"][len(prefix):]: item["Size"] for item in body.get("Contents") or []}
    return sizes, [item["Prefix"] for item in body.get("CommonPrefixes") or []]


def read_status(region, bucket, prefix):
    text = aws("s3", "cp", f"s3://{bucket}/{prefix}{STATUS_OBJECT}", "-", "--region", region)
    try:
        status = json.loads(text)
    except ValueError:
        raise Refusal(f"s3://{bucket}/{prefix}{STATUS_OBJECT} is not JSON") from None
    if not isinstance(status, dict):
        raise Refusal(f"s3://{bucket}/{prefix}{STATUS_OBJECT} is not a JSON object")
    return status


def manifest_files(status):
    """The {path: size} list of a complete status object; raises Refusal when malformed."""
    files = status.get("files")
    if not isinstance(files, list) or not files:
        raise Refusal("the status object has no file list")
    expected = {}
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        size = entry.get("size") if isinstance(entry, dict) else None
        if (not isinstance(path, str) or not path or path == STATUS_OBJECT or path in expected
                or isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise Refusal("the status object has a malformed file entry")
        expected[path] = size
    total = status.get("total_bytes")
    if total is not None and total != sum(expected.values()):
        raise Refusal("the status object's total_bytes is not the sum of its file sizes")
    return expected


def mismatch(expected, actual):
    """One line naming how the S3 listing differs from the status object, or None."""
    if expected == actual:
        return None
    first = sorted(set(expected.items()) ^ set(actual.items()))[0][0]
    return (f"S3 holds {len(actual)} files ({gigabytes(sum(actual.values()))}), the status object"
            f" lists {len(expected)} ({gigabytes(sum(expected.values()))}); first difference: {first}")


def uploaded_problem(sizes):
    if "config.json" not in sizes:
        return "no config.json at the top of the folder"
    if not any("/" not in path and path.endswith(".safetensors") for path in sizes):
        return "no *.safetensors at the top of the folder"
    return None


def nested_model(name, sizes, own=None):
    """The first sub-folder directly under one-segment models/<name>/ that holds another model, or
    None. SageMaker's S3Prefix loads everything under the prefix, so a nested model would load with
    this one. `own` is a fetched model's manifest paths: a sub-folder file it lists is this model's,
    and an unlisted drip-ai-fetch.json or config.json marks another model. `own` is None for an
    uploaded folder, where a sub-folder is another model only when it holds drip-ai-fetch.json or a
    config.json beside *.safetensors; a lone config.json (original/config.json) is this model's."""
    if "/" in name:
        return None
    children = {}
    for path in sizes:
        sub, _, base = path.partition("/")
        if base and "/" not in base:
            children.setdefault(sub, []).append(base)
    for sub in sorted(children):
        files = children[sub]
        if own is None:
            has_weights = any(f.endswith(".safetensors") for f in files)
            holds_model = STATUS_OBJECT in files or ("config.json" in files and has_weights)
        else:
            holds_model = any(marker in files and f"{sub}/{marker}" not in own
                              for marker in (STATUS_OBJECT, "config.json"))
        if holds_model:
            return f"{name}/{sub}"
    return None


def listed_paths(status):
    """The paths a status object lists, tolerating a malformed list."""
    files = status.get("files") if isinstance(status.get("files"), list) else []
    return {entry.get("path") for entry in files if isinstance(entry, dict)}


def over_limit(size, instance, limit):
    if limit is None or size <= limit:
        return None
    return f"{gigabytes(size)} is over 90% of {instance} GPU memory ({limit / GB:.0f} GB)"


def fitting_lanes(total, lanes):
    """The instance types, in order, whose GPU memory holds `total` bytes at 90%, from TYPE:GB
    arguments (GB empty when not recorded, which is not checked). Prints a line per skipped type."""
    kept = []
    for lane in lanes:
        instance, _, gb = lane.partition(":")
        if not gb:
            print(f"start-sagemaker: warning: no GPU memory recorded for {instance}; not checking that"
                  " the weights fit", file=sys.stderr)
        elif total > int(gb) * 9 * 10**8:
            print(f"Skipping {instance}: {gigabytes(total)} is over 90% of its {gb} GB GPU memory")
            continue
        kept.append(instance)
    return kept


def verify(mode, name, region, bucket, *lanes):
    """Exits 1 with a refusal unless models/<name>/ in S3 is a complete model that fits at least one
    of `lanes` (TYPE:GB). Prints the types it fits, space-separated, as the last line."""
    prefix = f"models/{name}/"
    where = f"s3://{bucket}/{prefix}"
    if mode == "manifest":
        try:
            status = read_status(region, bucket, prefix)
        except Refusal as error:
            raise Refusal(f"no readable fetch status object at {where}{STATUS_OBJECT} ({error}); copy"
                          " the model with shared/fetch-model/fetch-model.sh, or pass --no-manifest for a folder"
                          " uploaded with shared/models.sh upload") from None
        if status.get("status") != "complete":
            raise Refusal(f"{where} is not a complete copy (status {status.get('status')!r}); wait for"
                          " shared/fetch-model/fetch-model.sh --watch to finish")
        expected = manifest_files(status)
    sizes, _ = list_prefix(region, bucket, prefix)
    nested = nested_model(name, sizes, expected if mode == "manifest" else None)
    if nested:
        raise Refusal(f"{where} contains nested model {nested} (s3://{bucket}/models/{nested}/), and"
                      f" SageMaker would load both; move {nested} out of models/{name}/, or this"
                      " model to a folder of its own")
    if mode == "manifest":
        sizes.pop(STATUS_OBJECT, None)
        difference = mismatch(expected, sizes)
        if difference:
            raise Refusal(f"S3 does not match the fetch status object at {where}: {difference};"
                          " rerun shared/fetch-model/fetch-model.sh")
    else:
        if STATUS_OBJECT in sizes:
            raise Refusal(f"{where} holds a fetch status object; run without --no-manifest")
        problem = uploaded_problem(sizes)
        if problem:
            raise Refusal(f"{where} has {problem}")
    total = sum(sizes.values())
    print(f"{where}: {len(sizes)} files, {gigabytes(total)}, checked")
    kept = fitting_lanes(total, lanes)
    if not kept:
        raise Refusal(f"{where} holds {gigabytes(total)}, over 90% of the GPU memory of every type in"
                      " instance_types; pick a smaller model or add a larger type to instance_types in"
                      " sagemaker/terraform/terraform.tfvars; nothing was created")
    if total / DOWNLOAD_CAP_SECONDS > DOWNLOAD_WARN_BYTES_PER_SECOND:
        print(f"start-sagemaker: warning: loading {gigabytes(total)} within SageMaker's"
              f" {DOWNLOAD_CAP_SECONDS} s download limit needs about"
              f" {total / DOWNLOAD_CAP_SECONDS / 10**6:.0f} MB/s from S3", file=sys.stderr)
    print(" ".join(kept))


def load_profiles(path):
    """{model_type: (args, source)} from the committed profiles file; refuses a malformed entry."""
    try:
        with open(path) as handle:
            profiles = json.load(handle)
    except (OSError, ValueError) as error:
        raise Refusal(f"{PROFILES_NAME} is unreadable ({clean(str(error))})") from None
    if not isinstance(profiles, dict):
        raise Refusal(f"{PROFILES_NAME} is not a JSON object")
    loaded = {}
    for model_type, entry in profiles.items():
        args = entry.get("vllm_args") if isinstance(entry, dict) else None
        source = entry.get("source") if isinstance(entry, dict) else None
        if (not isinstance(args, list) or not isinstance(source, str) or not source.startswith("https://")
                or set(entry) != {"vllm_args", "source"}
                or not all(isinstance(a, str) and (PROFILE_FLAG.fullmatch(a) or PROFILE_VALUE.fullmatch(a))
                           for a in args)):
            raise Refusal(f"{PROFILES_NAME} entry {model_type!r} must be"
                          ' {"vllm_args": [plain flags and values], "source": "https://..."}')
        loaded[model_type] = (args, source)
    return loaded


def load_architectures(path):
    """(vLLM ref, set of architecture names) from the generated list."""
    try:
        with open(path) as handle:
            document = json.load(handle)
        ref, names = document["vllm_ref"], document["architectures"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Refusal(f"the vLLM architecture list is unreadable ({clean(str(error))}); rerun"
                      " shared/model-profiles/regenerate_architectures.py") from None
    if not isinstance(ref, str) or not isinstance(names, list) or not names:
        raise Refusal("the vLLM architecture list is malformed; rerun"
                      " shared/model-profiles/regenerate_architectures.py")
    return ref, set(names)


def profile(region, bucket, name, profiles_path, architectures_path):
    """Prints the profile's vLLM args as a JSON list (valid HCL), then one display line. Refuses a
    model whose config.json names an architecture the pinned vLLM does not register, or a
    model_type with no profile."""
    where = f"s3://{bucket}/models/{name}/config.json"
    try:
        config = json.loads(aws("s3", "cp", where, "-", "--region", region))
    except Refusal as error:
        raise Refusal(f"could not read {where} ({error})") from None
    except ValueError:
        raise Refusal(f"{where} is not JSON") from None
    model_type = config.get("model_type") if isinstance(config, dict) else None
    architectures = config.get("architectures") if isinstance(config, dict) else None
    if not isinstance(model_type, str) or not MODEL_TYPE.fullmatch(model_type):
        raise Refusal(f"{where} has no plain model_type")
    if not isinstance(architectures, list) or not architectures or not isinstance(architectures[0], str):
        raise Refusal(f"{where} has no architectures list")
    architecture = clean(architectures[0], 100)
    ref, known = load_architectures(architectures_path)
    if architectures[0] not in known:
        raise Refusal(f"{where} names architecture {architecture!r}, which vLLM {ref} in the pinned"
                      " image does not register for text generation, so the endpoint would not boot;"
                      " pick another model, or pass --unchecked-model to try anyway")
    profiles = load_profiles(profiles_path)
    if model_type not in profiles:
        raise Refusal(f"no vLLM profile for model_type {model_type!r} ({architecture}). Add one line to"
                      f" {PROFILES_NAME} with the tool-call and reasoning parsers from the model card"
                      " and the vLLM docs, then start again:\n  "
                      + PROFILE_TEMPLATE.format(model_type=model_type))
    args, source = profiles[model_type]
    print(json.dumps(args))
    print(f"{model_type} ({architecture}): {' '.join(args) or 'no flags'}  (source: {clean(source)})")


def plan_lanes(path, name_pattern, *types):
    """Prints NAME and TYPE per lane, in TYPES order, from a saved plan's JSON. Refuses a plan that
    would do anything to an endpoint but create it, or whose endpoints are not one per TYPES entry
    with a name NAME_PATTERN matches, which the race loop's ListEndpoints filter needs."""
    try:
        with open(path) as handle:
            changes = json.load(handle).get("resource_changes") or []
    except (OSError, ValueError, AttributeError) as error:
        raise Refusal(f"the saved plan is unreadable ({clean(str(error))}); nothing was created") from None
    names = {}
    for change in changes:
        if not isinstance(change, dict) or change.get("type") != "aws_sagemaker_endpoint":
            continue
        planned = change.get("change") or {}
        if planned.get("actions") != ["create"]:
            raise Refusal(f"the plan would {'/'.join(map(str, planned.get('actions') or ['?']))}"
                          f" {clean(str(change.get('address')))}, not only create it; nothing was"
                          " created. Check: bin/drip-ai sagemaker status")
        names[str(change.get("index"))] = (planned.get("after") or {}).get("name")
    if sorted(names) != sorted(types) or not all(
            isinstance(name, str) and re.search(name_pattern, name) for name in names.values()):
        planned = ", ".join(f"{clean(str(name), 63)} on {clean(key, 40)}" for key, name in names.items())
        raise Refusal(f"the plan's endpoints ({planned or 'none'}) are not one drip-ai-<type> endpoint"
                      f" per instance_types entry ({' '.join(types)}); keep instance_types on one"
                      " line in sagemaker/terraform/terraform.tfvars; nothing was created")
    for instance_type in types:
        print(f"{names[instance_type]}\t{instance_type}")


def describe_model(region, bucket, prefix, top_files, has_subfolders, instance, limit, current):
    """One table row (name, mode, blocker, cells) for a model prefix."""
    name = prefix[len("models/"):-1]
    blocker = None
    # Only a one-segment model with sub-folders can hold a nested model; list it whole once.
    whole = list_prefix(region, bucket, prefix)[0] if has_subfolders and "/" not in name else None
    own = None
    if STATUS_OBJECT in top_files:
        mode, source = "manifest", "fetched"
        own = set()
        origin, state, size, count = "-", "unreadable", None, None
        try:
            status = read_status(region, bucket, prefix)
            own = listed_paths(status)
            origin = f"{clean(str(status.get('repo')), 60)}@{clean(str(status.get('revision')), 7)}"
            state = clean(str(status.get("status")), 20)
            if state != "complete":
                blocker = "still copying" if state == "copying" else f"status {state}"
                files = status.get("files") if isinstance(status.get("files"), list) else []
                size, count = status.get("total_bytes"), len(files)
            else:
                expected = manifest_files(status)
                sizes = dict(whole) if whole is not None else list_prefix(region, bucket, prefix)[0]
                sizes.pop(STATUS_OBJECT, None)
                if mismatch(expected, sizes):
                    blocker = "S3 does not match its fetch status object"
                size, count = sum(expected.values()), len(expected)
        except Refusal as error:
            blocker = f"status object unreadable ({error})"
    else:
        mode, source, origin, state = "no-manifest", "uploaded", "-", "-"
        sizes = whole if whole is not None else list_prefix(region, bucket, prefix)[0]
        size, count = sum(sizes.values()), len(sizes)
    nested = nested_model(name, whole or {}, own)
    if nested:
        blocker = f"contains nested model {nested}"
    if not isinstance(size, int) or isinstance(size, bool):
        size = None
    fit = "fit unknown" if limit is None or size is None else ("fits" if size <= limit else "too big")
    if blocker is None and size is not None:
        blocker = over_limit(size, instance, limit)
    cells = [name, source, origin, state, gigabytes(size) if size is not None else "size unknown",
             f"{count} files" if count is not None else "files unknown", fit,
             "[current]" if name == current else ""]
    return name, mode, blocker, cells


def is_model(top_files):
    return STATUS_OBJECT in top_files or uploaded_problem(top_files) is None


def discover(region, bucket, instance, limit_text, current):
    """Prints NAME, MODE, BLOCKER ("-" when selectable), and the display line per model, joined by
    the unit separator. Names are one level (name) or two (Org/Name) deep."""
    limit = int(limit_text) if limit_text else None
    rows = []
    _, firsts = list_prefix(region, bucket, "models/", delimited=True)
    for first in firsts:
        top_files, seconds = list_prefix(region, bucket, first, delimited=True)
        candidates = [(first, top_files, bool(seconds))] if is_model(top_files) else []
        if not candidates:
            for second in seconds:
                files, _ = list_prefix(region, bucket, second, delimited=True)
                if is_model(files):
                    candidates.append((second, files, False))
        for prefix, files, has_subfolders in candidates:
            rows.append(describe_model(region, bucket, prefix, files, has_subfolders, instance, limit,
                                       current))
    widths = [max(len(row[3][i]) for row in rows) for i in range(8)] if rows else []
    for name, mode, blocker, cells in rows:
        line = "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()
        if blocker:
            line += f"  not selectable: {blocker}"
        print("\x1f".join([name, mode, blocker or "-", line]))


def parse_time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def clock(epoch):
    if epoch is None:
        return "unknown"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def money(amount):
    return f"${amount:,.2f}"


def load_json(text):
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def series(results, metric_id):
    """[(epoch, value)] for one query; [] when it returned no datapoints."""
    for result in results:
        if result.get("Id") == metric_id:
            times = [parse_time(t) for t in result.get("Timestamps") or []]
            return [(t, v) for t, v in zip(times, result.get("Values") or []) if t is not None]
    return []


def total(points):
    return f"{sum(v for _, v in points):.0f}" if points else "no data"


def latest(points, unit):
    if not points:
        return "no data"
    value = max(points)[1]
    if unit == "us":
        return f"{value / 1000:.0f} ms" if value < 10**6 else f"{value / 10**6:.2f} s"
    return f"{value:.0f}%"


def endpoint_lines(env, now, endpoint):
    """Endpoint, cost, and idle lines; returns (lines, InService and idle-checkable uptime)."""
    instance = env.get("DASH_INSTANCE_TYPE") or "instance type unavailable"
    if endpoint is None:
        return [f"Endpoint: unavailable ({clean(env.get('DASH_ENDPOINT_ERROR')) or 'no answer'})",
                "Cost: unknown (no creation time)"], None, None
    variants = endpoint.get("ProductionVariants") or [{}]
    count = variants[0].get("CurrentInstanceCount") if isinstance(variants[0], dict) else None
    if not isinstance(count, int):
        configured = env.get("DASH_INSTANCE_COUNT", "")
        count = int(configured) if configured.isdigit() else None
    status = clean(str(endpoint.get("EndpointStatus", "unknown")), 40)
    created = parse_time(endpoint.get("CreationTime"))
    lines = [f"Endpoint: {status}  {instance} x{count if count is not None else '?'}",
             f"  created {clock(created)}  modified {clock(parse_time(endpoint.get('LastModifiedTime')))}"]
    if endpoint.get("FailureReason"):
        lines.append(f"  failure: {clean(str(endpoint['FailureReason']), 300)}")
    if created is None:
        lines.append("Cost: unknown (no creation time)")
        return lines, status, None
    uptime = max(0, now - created)
    minutes = int(uptime // 60)
    running = f"running {minutes // 60}h{minutes % 60:02d}m"
    price = env.get("DASH_PRICE")
    if not price:
        known = env.get("DASH_INSTANCE_TYPE")
        lines.append(f"Cost: {running}  price unknown" + (f" for {known}" if known else ""))
        return lines, status, uptime
    hourly = float(price) * (count if count is not None else 1)
    assumed = "" if count is not None else "  (instance count unknown; 1 assumed)"
    lines.append(f"Cost: {running}  ≈ {money(hourly * uptime / 3600)} so far  ({money(hourly)}/h,"
                 f" ≈ ${hourly * 24:,.0f}/day)  estimate from list price since creation{assumed}")
    return lines, status, uptime


def render(env):
    now = int(env["DASH_NOW"])
    port, color = env["DASH_PORT"], env.get("DASH_COLOR") == "1"
    owner = clean(env.get("DASH_OWNER"), 256) or "unknown"
    lines = [f"== drip-ai SageMaker endpoint {env['DASH_ENDPOINT']}  started by {owner}  {clock(now)}"
             f"  (refreshes every {env['DASH_INTERVAL']}s)"]
    endpoint_part, status, uptime = endpoint_lines(env, now, load_json(env.get("DASH_ENDPOINT_JSON")))
    lines += endpoint_part
    metrics = load_json(env.get("DASH_METRICS_JSON"))
    if metrics is None:
        lines.append(f"Usage: unavailable ({clean(env.get('DASH_METRICS_ERROR')) or 'no answer'})")
    else:
        results = [r for r in metrics.get("MetricDataResults") or [] if isinstance(r, dict)]
        invocations = series(results, "inv")
        recent = [(t, v) for t, v in invocations if t >= now - IDLE_SECONDS]
        # CloudWatch has no datapoints for minutes without calls, so an empty answer is zero calls.
        if status == "InService" and uptime is not None and uptime > IDLE_SECONDS and not sum(
                v for _, v in recent):
            price = env.get("DASH_PRICE")
            billing = f"≈ {money(float(price))}/h" if price else "(price unknown)"
            warning = f"!! idle 30+ min: still billing {billing}. Stop: {TEARDOWN}"
            lines.append(f"\x1b[1;31m{warning}\x1b[0m" if color else warning)
        if not any(series(results, r.get("Id")) for r in results):
            lines.append("Usage (last 60 min): no data yet")
        else:
            lines += [
                f"Usage (last 60 min): requests {total(invocations)}  4xx {total(series(results, 'err4'))}"
                f"  5xx {total(series(results, 'err5'))}  (last 30 min: {total(recent) if recent else 0})",
                f"  model latency (latest minute): avg {latest(series(results, 'lat'), 'us')}"
                f"  p90 {latest(series(results, 'latp90'), 'us')}",
                f"  latest: GPU {latest(series(results, 'gpu'), '%')}"
                f"  GPU mem {latest(series(results, 'gpumem'), '%')}"
                f"  CPU {latest(series(results, 'cpu'), '%')}"
                f"  memory {latest(series(results, 'mem'), '%')}"
                f"  disk {latest(series(results, 'disk'), '%')}  (GPU and CPU are summed over devices)",
            ]
    proxy = env.get("DASH_PROXY", "")
    if env.get("DASH_KEY_ERROR"):
        lines.append(f"Proxy: unavailable (could not read the API key: {clean(env['DASH_KEY_ERROR'])})")
    elif proxy == "200":
        lines.append(f"Proxy: up on localhost:{port} (GET /v1/models 200)")
    elif proxy in ("", "000"):
        lines.append(f"Proxy: not running on localhost:{port}; start it: bin/drip-ai sagemaker tunnel")
    else:
        lines.append(f"Proxy: localhost:{port} answers GET /v1/models with HTTP {clean(proxy, 3)}")
    if env.get("DASH_LOGS_ERROR"):
        lines.append(f"Logs: unavailable ({clean(env['DASH_LOGS_ERROR'])})")
    else:
        log_lines = [clean(line, 200) for line in (env.get("DASH_LOGS") or "").splitlines()]
        log_lines = [line for line in log_lines if line][-3:]
        lines.append("Logs (last 5 min): " + ("" if log_lines else "no lines yet"))
        lines += [f"  {line}" for line in log_lines]
    lines += [
        f"Connect: http://localhost:{port}/v1  model {clean(env.get('DASH_MODEL'), 80) or 'unknown'}"
        '  key: export DRIP_AI_API_KEY="$(bin/drip-ai key)"',
        "Smoke:   bin/drip-ai smoke   (with the tunnel open)",
        f"Stop:    {TEARDOWN}",
        "Ctrl-C closes this view; the endpoint keeps billing.",
    ]
    print("\n".join(line.rstrip() for line in lines))


def metric_queries(endpoint):
    dims = [{"Name": "EndpointName", "Value": endpoint}, {"Name": "VariantName", "Value": "main"}]
    queries = [
        ("inv", "AWS/SageMaker", "Invocations", "Sum"),
        ("err4", "AWS/SageMaker", "Invocation4XXErrors", "Sum"),
        ("err5", "AWS/SageMaker", "Invocation5XXErrors", "Sum"),
        ("lat", "AWS/SageMaker", "ModelLatency", "Average"),
        ("latp90", "AWS/SageMaker", "ModelLatency", "p90"),
        ("gpu", "/aws/sagemaker/Endpoints", "GPUUtilization", "Average"),
        ("gpumem", "/aws/sagemaker/Endpoints", "GPUMemoryUtilization", "Average"),
        ("cpu", "/aws/sagemaker/Endpoints", "CPUUtilization", "Average"),
        ("mem", "/aws/sagemaker/Endpoints", "MemoryUtilization", "Average"),
        ("disk", "/aws/sagemaker/Endpoints", "DiskUtilization", "Average"),
    ]
    print(json.dumps([
        {"Id": metric_id, "MetricStat": {
            "Metric": {"Namespace": namespace, "MetricName": metric, "Dimensions": dims},
            "Period": 60, "Stat": stat}}
        for metric_id, namespace, metric, stat in queries
    ]))


def main(argv):
    command, args = argv[0], argv[1:]
    try:
        if command == "verify":
            verify(*args)
        elif command == "profile":
            profile(*args)
        elif command == "plan-lanes":
            plan_lanes(*args)
        elif command == "discover":
            discover(*args)
        elif command == "render":
            render(os.environ)
        elif command == "metric-queries":
            metric_queries(*args)
        elif command == "field":
            value = (load_json(os.environ.get("DASH_JSON")) or {}).get(args[0])
            print(value if isinstance(value, str) else "")
    except Refusal as error:
        print(f"start-sagemaker: {error}", file=sys.stderr)
        return 1
    return 0


sys.exit(main(sys.argv[1:]))
PY
}

# Prints the models/<name> folder the running endpoint serves, or nothing when no endpoint runs or a
# read fails, so the table simply has no [current] mark. Needs endpoint_config from the last
# read_endpoint_owner (every lane serves one model), region_name, and bucket.
current_model() {
  local model uri
  [ -n "${endpoint_config:-}" ] || return 0
  model="$(aws sagemaker describe-endpoint-config --region "$region_name" \
    --endpoint-config-name "$endpoint_config" --query 'ProductionVariants[0].ModelName' \
    --output text 2>/dev/null)" || return 0
  uri="$(aws sagemaker describe-model --region "$region_name" --model-name "$model" \
    --query 'PrimaryContainer.ModelDataSource.S3DataSource.S3Uri' --output text 2>/dev/null)" || return 0
  case "$uri" in
    "s3://$bucket/models/"*/) uri="${uri#"s3://$bucket/models/"}" ;;
    *) return 0 ;;
  esac
  uri="${uri%/}"
  [[ "$uri" =~ $model_name_pattern ]] || return 0
  echo "$uri"
}

# Sets model_names, model_modes, and model_blockers, and prints the numbered table. Returns 1 when
# the bucket holds no model.
list_models() {
  local table name mode blocker display current fit_rule
  current="$(current_model)"
  table="$(start_py discover "$region_name" "$bucket" "$fit_instance" "$fit_limit" "$current")" || exit 1
  if [ -z "$table" ]; then
    echo "No models in s3://$bucket/models/. Copy one with shared/fetch-model/fetch-model.sh," \
      "or upload one with shared/models.sh upload."
    return 1
  fi
  fit_rule="weights must fit in 90% of the largest lane's GPU memory, $(gpu_memory_gb "$fit_instance" || echo '?') GB on $fit_instance; smaller lanes are skipped"
  [ -n "$fit_limit" ] || fit_rule="GPU memory of $fit_instance is not recorded, so fit is not checked"
  echo "Models in s3://$bucket/models/ (racing $instance_types; $fit_rule):"
  model_names=() model_modes=() model_blockers=()
  while IFS=$'\x1f' read -r name mode blocker display; do
    model_names+=("$name") model_modes+=("$mode") model_blockers+=("$blocker")
    printf '  %d) %s\n' "${#model_names[@]}" "$display"
  done <<<"$table"
}

# Sets picked_name and picked_mode from a number typed on the terminal; Enter, q, or EOF cancels.
pick_model() {
  local choice="" index
  open_tty || fail "no terminal to read a choice from; run sagemaker/start.sh <name>"
  while :; do
    printf 'Pick a number (Enter or q cancels): '
    read -r -u 5 choice || true
    case "$choice" in
      "" | q | Q)
        echo
        echo "Cancelled; nothing changed."
        # --no-watch promises exit 0 only when an endpoint of yours exists.
        [ "$open_dashboard" = yes ] || exit 1
        exit 0
        ;;
    esac
    if ! [[ "$choice" =~ ^[0-9]{1,4}$ ]] || [ "$choice" -lt 1 ] ||
      [ "$choice" -gt "${#model_names[@]}" ]; then
      echo "Type a number from 1 to ${#model_names[@]}, or q."
      continue
    fi
    index=$((choice - 1))
    if [ "${model_blockers[$index]}" != - ]; then
      echo "$choice) ${model_names[$index]} is not selectable: ${model_blockers[$index]}"
      continue
    fi
    picked_name="${model_names[$index]}" picked_mode="${model_modes[$index]}"
    return 0
  done
}

check_model_name() {
  [[ "$1" =~ $model_name_pattern ]] ||
    fail "model name must be name or Org/Name: letters, digits, '.', '_', '-', each part starting" \
      "with a letter or digit"
}

# Sets region_name and my_started_by, loading the caller identity in this shell so later $(...)
# Terraform reads reuse it.
load_start_context() {
  region_name="$(drip_region)" || exit 1
  load_caller_identity || exit 1
  my_started_by="$(sanitize_tag_value "$drip_caller_arn_value")"
}

# For --no-watch, whose exit 0 tells `up` to open the tunnel: refuses unless one of the caller's
# lanes (lanes_mine, statuses from read_lanes) is InService, Creating, or Updating, because a
# Failed or Deleting endpoint never serves.
require_live_own_lane() {
  local name status first=""
  for name in $lanes_mine; do
    status="$(awk -F'\t' -v name="$name" '$1 == name { print $2; exit }' <<<"$lanes_listed")"
    case "$status" in InService | Creating | Updating) return 0 ;; esac
    [ -n "$first" ] || first="endpoint $name is ${status:-not listed}"
  done
  fail "$first; run bin/drip-ai sagemaker stop, then up"
}

# Runs before any Terraform call, so a plan happens only when no drip-ai endpoint exists. None:
# returns. Only the caller's own: opens the dashboard and exits. Anyone else's, or one whose owner
# cannot be read: refuses unless TAKE_OVER is yes and the user types each of their names; the stop
# path then runs right before the plan.
gate_endpoint_owner() {
  local take_over="$1"
  read_lanes "$region_name" "$my_started_by" ||
    fail "could not list the SageMaker endpoints in $region_name; nothing changed"
  [ -n "$lanes_mine$lanes_other" ] || return 0
  if [ -z "$lanes_other" ]; then
    echo "Endpoint$lanes_mine already exists (yours)."
    echo "Watch: bin/drip-ai sagemaker watch   Tunnel: bin/drip-ai sagemaker tunnel"
    echo "To serve another model: bin/drip-ai sagemaker stop, then start again."
    if [ "$open_dashboard" = yes ]; then dashboard; else require_live_own_lane; fi
    exit 0
  fi
  [ "$take_over" = yes ] ||
    fail "$lanes_other_text; it is theirs to stop. To replace it, rerun with --take-over and type the endpoint name"
  echo "Taking over deletes every drip-ai endpoint (Terraform asks first), then races new ones recorded as yours."
  confirm_other_lanes || fail "the typed name did not match $endpoint_name; nothing changed"
  take_over_confirmed=yes
}

# Prints MODEL_CHECK's vLLM args for models/NAME/ as an HCL list: the model profile's, or [] with
# --unchecked-model. Refuses, before Terraform, a model whose architecture the pinned vLLM does not
# register or whose model_type has no profile. Needs region_name and bucket.
model_profile_args() {
  local name="$1" model_check="$2" out args display
  if [ "$model_check" = no ]; then
    echo "start-sagemaker: warning: --unchecked-model: models/$name/ is not checked against vLLM's architectures or shared/model-profiles/profiles.json; only vllm_extra_args in sagemaker/terraform/terraform.tfvars apply, so the boot can fail or tool calls can go unparsed" >&2
    echo '[]'
    return 0
  fi
  out="$(start_py profile "$region_name" "$bucket" "$name" "$profiles_file" "$architectures_file")" || return 1
  { IFS= read -r args; IFS= read -r display; } <<<"$out"
  echo "Model profile: $display" >&2
  echo "$args"
}

# The race's INT, TERM, and HUP handler: counts the signal for the loop, which acts on it within a
# second, and keeps the exit status the signal would have given.
race_signal() {
  race_signals=$((race_signals + 1))
  race_exit="$1"
}

# Sends INT once to the apply's process group, so Terraform stops waiting and saves its state.
stop_apply() {
  [ -n "$apply_pid" ] && [ "$apply_signalled" = no ] || return 0
  apply_signalled=yes
  echo "Stopping Terraform (INT); it keeps in state what it created."
  kill -INT -- "-$apply_pid" 2>/dev/null || kill -INT "$apply_pid" 2>/dev/null || true
}

# Sets lane_states to one ListEndpoints answer (NAME STATUS ARN lines). On a failed call it keeps
# the last answer, says why, and returns 1.
poll_lanes() {
  local listed
  if listed="$(list_lanes "$region_name" 2>"$plan_dir/list.err")"; then
    lane_states="$listed"
    return 0
  fi
  echo "start-sagemaker: could not list the endpoints ($(awk 'NF { line = $0 } END { print line }' "$plan_dir/list.err")); asking again" >&2
  return 1
}

# Reads the StartedBy tag of each race lane the first time it is listed. One that someone else
# started (a concurrent start whose apply took the state lock first) is never chosen or deleted.
check_new_lanes() {
  local name status arn owner
  while IFS=$'\t' read -r name status arn; do
    case " $race_names " in *" $name "*) ;; *) continue ;; esac
    case " $checked_lanes " in *" $name "*) continue ;; esac
    owner="$(read_started_by "$region_name" "$arn" 2>/dev/null </dev/null)" || continue
    checked_lanes="$checked_lanes $name"
    [ "$owner" != "$my_started_by" ] || continue
    foreign_lanes="$foreign_lanes $name"
    echo "start-sagemaker: endpoint $name was started by ${owner:-an identity with no StartedBy tag}, not by this run; leaving it alone" >&2
  done <<<"$lane_states"
}

# Prints lane NAME's status in the last answer: absent when unlisted or someone else's, and
# unchecked while listed but its StartedBy tag is not read yet, which keeps it live but never chosen
# or deleted.
lane_status() {
  case " $foreign_lanes " in *" $1 "*) echo absent; return 0 ;; esac
  awk -F'\t' -v name="$1" -v checked=" $checked_lanes " '$1 == name {
      found = 1; print (index(checked, " " name " ") ? $2 : "unchecked") }
    END { if (!found) print "absent" }' <<<"$lane_states"
}

# Returns 0 when DescribeEndpoint finds none of NAMES (someone else's lanes aside). Called after
# Terraform exited, because ListEndpoints can miss an endpoint created just before.
lanes_gone() {
  local lane
  for lane in "$@"; do
    case " $foreign_lanes " in *" $lane "*) continue ;; esac
    read_endpoint_owner "$region_name" "$my_started_by" "$lane" </dev/null
    [ "$endpoint_ownership" = absent ] || return 1
  done
}

# Asks SageMaker to delete lane NAME; a refusal (for example while it is Creating) is printed, and
# the next poll asks again.
delete_lane() {
  if aws sagemaker delete-endpoint --region "$region_name" --endpoint-name "$1" 2>"$plan_dir/delete.err"; then
    echo "Deleting $1."
    return 0
  fi
  echo "start-sagemaker: SageMaker refused to delete $1 ($(awk 'NF { line = $0 } END { print line }' "$plan_dir/delete.err")); asking again at the next poll" >&2
}

# After a race with no winner, before the lanes are deleted: prints each Failed lane's
# FailureReason, and the end of Terraform's log when a lane never started.
report_failed_lanes() {
  local name type status reason never_started=no
  while read -r name type; do
    status="$(lane_status "$name")"
    if [ "$status" = Failed ]; then
      reason="$(aws sagemaker describe-endpoint --region "$region_name" --endpoint-name "$name" \
        --query FailureReason --output text 2>/dev/null </dev/null)" || reason="unreadable"
      echo "start-sagemaker: $name ($type) failed: ${reason//[[:cntrl:]]/} (logs: /aws/sagemaker/Endpoints/$name)" >&2
    else
      never_started=yes
      echo "start-sagemaker: $name ($type) did not start ($status)" >&2
    fi
  done <<<"$race_lanes_list"
  [ "$never_started" = no ] || { echo "Terraform's log ends:" >&2; tail -n 20 "$plan_dir/apply.log" >&2; }
}

# Prints each lane with its hourly list price, and the worst case of all of them billing at once.
show_race_cost() {
  local index=0 name type price total=0 known=yes hours=$((race_cap_seconds / 3600))
  echo "Lanes, in order of preference; the first InService is kept and the others are deleted at once:"
  while read -r name type; do
    index=$((index + 1))
    if price="$(hourly_price "$type")"; then
      total="$(awk -v a="$total" -v b="$price" 'BEGIN { printf "%.2f", a + b }')"
      price="about \$$price/h"
    else
      price="price unknown" known=no
    fi
    printf '  %d) %s  %s  %s\n' "$index" "$name" "$type" "$price"
  done <<<"$race_lanes_list"
  if [ "$known" = yes ]; then
    echo "Worst case: all $index bill together, about \$$total/h, until one is InService; about \$$(awk -v t="$total" -v c="$race_cap_seconds" 'BEGIN { printf "%.0f", t * c / 3600 }') if none is by the $hours-hour cap."
  else
    echo "Worst case: all $index bill together until one is InService, up to the $hours-hour cap; the total is unknown because a lane's price is unknown."
  fi
  echo "The kept endpoint bills from creation until: $teardown"
}

# Plans one endpoint per RACE_TYPES entry into a private folder, refuses a plan that would do more
# to an endpoint than create it, and shows each lane's price. Once the user types yes (or passed
# --yes), applies that saved plan in the background, in its own process group, logging to a file,
# and races.
race_endpoints() {
  local name="$1" model_args="$2" rerun="$3" race_types="$4" typed="" race_lanes_list race_names
  local type types_hcl=""
  [[ "$race_poll_seconds" =~ ^[1-9][0-9]{0,4}$ ]] && [[ "$race_cap_seconds" =~ ^[0-9]{1,6}$ ]] &&
    [[ "$race_apply_wait_seconds" =~ ^[0-9]{1,6}$ ]] ||
    fail "DRIP_AI_RACE_POLL_SECONDS, DRIP_AI_RACE_CAP_SECONDS, and DRIP_AI_RACE_APPLY_WAIT_SECONDS must be whole seconds"
  plan_dir="$(mktemp -d "${TMPDIR:-/tmp}/start-sagemaker.XXXXXX")" || fail "could not create a private temporary folder"
  trap 'rm -rf "$plan_dir"' EXIT
  for type in $race_types; do types_hcl="${types_hcl:+$types_hcl, }\"$type\""; done
  tf sagemaker plan -input=false -out="$plan_dir/plan" -var "model_source=$name" -var model_from_s3=true \
    -var "started_by=$my_started_by" -var "vllm_model_args=$model_args" -var "instance_types=[$types_hcl]" ||
    fail "terraform plan failed; nothing was created"
  tf sagemaker show -json "$plan_dir/plan" >"$plan_dir/plan.json" ||
    fail "could not read the saved plan; nothing was created"
  race_lanes_list="$(start_py plan-lanes "$plan_dir/plan.json" "$lane_name_pattern" $race_types)" || exit 1
  race_names="$(cut -f1 <<<"$race_lanes_list" | tr '\n' ' ')"
  show_race_cost
  if [ "$assume_yes" = yes ]; then
    echo "Approved with --yes; creating these endpoints."
  else
    open_tty || fail "no terminal to type yes on; nothing was created"
    printf 'Type yes to create these endpoints: '
    read -r -u 5 typed || true
    echo
    close_tty
    [ "$typed" = yes ] || fail "not approved; nothing was created"
  fi
  trap 'race_signal 130' INT
  trap 'race_signal 143' TERM
  # A hung-up terminal fails every later print, which would end the race with the lanes billing.
  trap 'race_signal 129; exec >/dev/null 2>&1' HUP
  # Monitor mode gives Terraform its own process group: Ctrl-C reaches only this shell, which then
  # decides what Terraform gets.
  set -m
  tf sagemaker apply -input=false -no-color "$plan_dir/plan" >"$plan_dir/apply.log" 2>&1 </dev/null &
  apply_pid=$!
  set +m
  echo "Creating the endpoints (Terraform log: $plan_dir/apply.log); polling every ${race_poll_seconds}s. Ctrl-C deletes them all."
  race_lanes "$name" "$rerun"
}

# The race, one state machine whose phases share each poll (one ListEndpoints call):
# - race: the first lane in instance_types order to be InService wins (phase keep). With no winner
#   by the cap, or once every lane is Failed or absent after Terraform exits, or on INT, TERM, or
#   HUP, Terraform gets INT and every lane is deleted (phase drain).
# - keep and drain: every target lane still listed gets a delete each poll until it is gone, a
#   refused delete being asked again; the loop ends once none is listed and Terraform has exited.
#   In keep, Terraform gets INT after race_apply_wait_seconds or on a signal; the winner stays, and
#   a signal skips the dashboard.
# A second signal exits at once, naming every lane that still exists.
race_lanes() {
  local name="$1" rerun="$2" phase=race targets="" winner="" winner_type="" started=$SECONDS
  local winner_at=0 result=0 polled status lane lane_type live unlisted remaining left handled step summary
  local last_summary="" apply_status="" lane_states="" checked_lanes="" foreign_lanes=""
  while :; do
    # Before the poll, so an answer read after Terraform exited sees whatever it created.
    if [ -n "$apply_pid" ] && ! kill -0 "$apply_pid" 2>/dev/null; then
      if wait "$apply_pid"; then apply_status=0; else apply_status=$?; fi
      apply_pid=""
      if [ "$phase" = race ]; then
        echo "Terraform exited ($apply_status); still polling the lanes."
      else
        echo "Terraform exited ($apply_status)."
      fi
    fi
    polled=no
    if poll_lanes; then
      polled=yes
      check_new_lanes
    fi
    if [ "$phase" = race ] && [ "$race_signals" -gt 0 ]; then
      echo "Interrupted before any lane was InService; deleting every lane."
      stop_apply
      targets="$race_names" phase=drain result="$race_exit"
    elif [ "$phase" = race ] && [ "$polled" = yes ]; then
      summary="" live=no unlisted=""
      while read -r lane lane_type; do
        status="$(lane_status "$lane")"
        summary="$summary $lane $status"
        [ -n "$winner" ] || [ "$status" != InService ] || winner="$lane" winner_type="$lane_type"
        case "$status" in absent) unlisted="$unlisted $lane" ;; Failed | Deleting) ;; *) live=yes ;; esac
      done <<<"$race_lanes_list"
      [ "$summary" = "$last_summary" ] || echo "After $(((SECONDS - started) / 60)) min:$summary"
      last_summary="$summary"
      if [ -n "$winner" ]; then
        echo "$winner ($winner_type) is InService and wins; deleting the other lanes."
        for lane in $race_names; do [ "$lane" = "$winner" ] || targets="$targets $lane"; done
        phase=keep winner_at=$SECONDS
      elif [ $((SECONDS - started)) -ge "$race_cap_seconds" ]; then
        echo "start-sagemaker: no lane was InService after $((race_cap_seconds / 60)) minutes; deleting every lane" >&2
        stop_apply
        targets="$race_names" phase=drain result=1
      elif [ "$live" = no ] && [ -z "$apply_pid" ] && lanes_gone $unlisted; then
        report_failed_lanes
        targets="$race_names" phase=drain result=1
      fi
    fi
    if [ "$phase" != race ]; then
      if [ "$phase" = keep ] && { [ "$race_signals" -gt 0 ] ||
        [ $((SECONDS - winner_at)) -ge "$race_apply_wait_seconds" ]; }; then
        stop_apply
      fi
      if [ "$polled" = yes ]; then
        remaining=""
        for lane in $targets; do
          status="$(lane_status "$lane")"
          [ "$status" != absent ] || continue
          remaining="$remaining $lane"
          case "$status" in Deleting | unchecked) ;; *) delete_lane "$lane" ;; esac
        done
        [ -n "$remaining" ] || [ -n "$apply_pid" ] || ! lanes_gone $targets || break
      fi
      if [ "$race_signals" -ge 2 ] && [ -n "$apply_pid" ]; then
        stop_apply
        kill -INT -- "-$apply_pid" 2>/dev/null || kill -INT "$apply_pid" 2>/dev/null || true
        echo "start-sagemaker: stopped while Terraform (pid $apply_pid) was still running; it may still create endpoints. Each of these may bill until $teardown: ${race_names% }" >&2
        exit "$race_exit"
      fi
      if [ "$race_signals" -ge 2 ]; then
        left=""
        for lane in $race_names; do
          if [ "$polled" = no ]; then
            left="$left $lane (unknown)"
          else
            status="$(lane_status "$lane")"
            [ "$status" = absent ] || left="$left $lane ($status)"
          fi
        done
        if [ -n "$left" ]; then
          echo "start-sagemaker: stopped; these endpoints still exist and each bills until $teardown:$left" >&2
        else
          echo "start-sagemaker: stopped; no lane endpoint was listed. Check: bin/drip-ai sagemaker status" >&2
        fi
        exit "$race_exit"
      fi
    fi
    handled="$race_signals" step=0
    while [ "$step" -lt "$race_poll_seconds" ] && [ "$race_signals" -eq "$handled" ]; do
      sleep 1 || true
      step=$((step + 1))
    done
  done
  if [ "$phase" = drain ]; then
    echo "Every lane is deleted; nothing bills for them."
    [ "$result" -ne 1 ] || echo "Start again: $rerun"
    exit "$result"
  fi
  tf sagemaker state list -id="$winner" 2>/dev/null | grep -q . ||
    echo "start-sagemaker: warning: $winner is not in Terraform state (Terraform stops waiting after 60 minutes); $teardown still deletes it" >&2
  rm -rf "$plan_dir"
  trap - EXIT INT TERM HUP
  cat <<EOF_NEXT
Endpoint $winner ($winner_type) serves models/$name. Next:
  bin/drip-ai sagemaker tunnel    # in one terminal; the clients link once the model answers
  bin/drip-ai smoke               # in another
Tear down (the endpoint bills until deleted): $teardown
EOF_NEXT
  [ "$race_signals" -eq 0 ] && [ "$open_dashboard" = yes ] || exit 0
  dashboard
}

# Needs the owner gate passed. Verifies the S3 copy, the lanes it fits, and the model profile before
# Terraform sees the model; a confirmed take-over runs the stop path just before the race.
start_endpoint() {
  local name="$1" mode="$2" model_check="$3" model_args rerun="bin/drip-ai sagemaker start $1"
  local type lanes="" verified race_types
  check_model_name "$name"
  resolve_model_store
  for type in $instance_types; do lanes="$lanes $type:$(gpu_memory_gb "$type" || true)"; done
  # shellcheck disable=SC2086 # lanes is a space-separated list of TYPE:GB
  verified="$(start_py verify "$mode" "$name" "$region_name" "$bucket" $lanes)" || exit 1
  printf '%s\n' "${verified%$'\n'*}"
  race_types="${verified##*$'\n'}"
  model_args="$(model_profile_args "$name" "$model_check")" || exit 1
  if [ "$take_over_confirmed" = yes ]; then
    stop_lanes || fail "the take-over did not delete every drip-ai endpoint; nothing was started"
  fi
  [ "$mode" = manifest ] || rerun="$rerun --no-manifest"
  [ "$model_check" = yes ] || rerun="$rerun --unchecked-model"
  race_endpoints "$name" "$model_args" "$rerun" "$race_types"
}

start_named() {
  local name="$1" mode="$2" take_over="$3" model_check="$4"
  check_model_name "$name"
  load_start_context
  gate_endpoint_owner "$take_over"
  start_endpoint "$name" "$mode" "$model_check"
}

# Runs a read-only aws call and prints its output. On failure it leaves the reason in ERR_FILE and
# returns 1; on success ERR_FILE is empty.
read_aws() {
  local err_file="$1" out
  shift
  if out="$(aws "$@" 2>"$err_file")"; then
    : >"$err_file"
    printf '%s\n' "$out"
    return 0
  fi
  [ -s "$err_file" ] || echo "aws exited non-zero" >"$err_file"
  return 1
}

# Sets the data for one frame. Every AWS read is tolerated: a failure empties its output and leaves
# the reason for that panel.
collect_frame() {
  local config_name config arn lane_model lane_window
  endpoint_json="$(read_aws "$work_dir/endpoint.err" sagemaker describe-endpoint --region "$region_name" \
    --endpoint-name "$endpoint" --output json)" || endpoint_json=""
  config_name="$(DASH_JSON="$endpoint_json" start_py field EndpointConfigName)"
  if [ -n "$config_name" ] && [ "$config_name" != "$cached_config_name" ] &&
    config="$(read_aws "$work_dir/config.err" sagemaker describe-endpoint-config --region "$region_name" \
      --endpoint-config-name "$config_name" \
      --query 'ProductionVariants[0].[InstanceType,InitialInstanceCount]' --output text)"; then
    cached_config_name="$config_name"
    IFS=$'\t' read -r dashboard_instance dashboard_count <<<"$config" || true
    [[ "$dashboard_instance" =~ ^ml\.[a-z0-9.-]+$ ]] || dashboard_instance=""
    [[ "$dashboard_count" =~ ^[0-9]+$ ]] || dashboard_count=""
    dashboard_price="$(hourly_price "$dashboard_instance")" || dashboard_price=""
    read_lane_model "$region_name" "$config_name" 2>/dev/null || true
    served_model="$lane_model"
  fi
  # Read every frame, because a take-over changes the tag on the same endpoint.
  owner_text=""
  arn="$(DASH_JSON="$endpoint_json" start_py field EndpointArn)"
  if [ -n "$arn" ]; then
    owner_text="$(read_started_by "$region_name" "$arn" 2>/dev/null)" || owner_text="unknown (StartedBy unreadable)"
    [ -n "$owner_text" ] || owner_text="no StartedBy tag"
  fi
  metrics_json="$(read_aws "$work_dir/metrics.err" cloudwatch get-metric-data --region "$region_name" \
    --start-time $((now - 3600)) --end-time "$now" --scan-by TimestampDescending \
    --metric-data-queries "$metric_queries" --output json)" || metrics_json=""
  logs_text="$(read_aws "$work_dir/logs.err" logs tail "$log_group" --region "$region_name" --since 5m \
    --format short)" || logs_text=""
  proxy_code=""
  # printf is a builtin, so the key reaches curl on stdin and never appears in any argv.
  [ -z "$api_key" ] || proxy_code="$(printf 'header = "Authorization: Bearer %s"\n' "$api_key" |
    curl -sS -o /dev/null -w '%{http_code}' --max-time 3 --config - "http://127.0.0.1:$local_port/v1/models" \
      2>/dev/null)" || true
}

render_frame() {
  DASH_NOW="$now" DASH_INTERVAL="$interval" DASH_ENDPOINT="$endpoint" DASH_PORT="$local_port" \
    DASH_OWNER="$owner_text" \
    DASH_ENDPOINT_JSON="$endpoint_json" DASH_ENDPOINT_ERROR="$(cat "$work_dir/endpoint.err")" \
    DASH_INSTANCE_TYPE="$dashboard_instance" DASH_INSTANCE_COUNT="$dashboard_count" \
    DASH_PRICE="$dashboard_price" \
    DASH_METRICS_JSON="$metrics_json" DASH_METRICS_ERROR="$(cat "$work_dir/metrics.err")" \
    DASH_PROXY="$proxy_code" DASH_KEY_ERROR="$key_error" DASH_MODEL="$served_model" \
    DASH_LOGS="$logs_text" DASH_LOGS_ERROR="$(cat "$work_dir/logs.err")" DASH_COLOR="$on_tty" \
    start_py render
}

# Read-only: list, describe, tag, metric, log, and parameter reads plus a local GET, and no Terraform
# state. Follows the lane pick_lane chooses; the key is the shared stack's /drip-ai/api-key. Ctrl-C
# ends the view only.
dashboard() {
  local iterations="${DRIP_AI_WATCH_ITERATIONS:-}" interval="${DRIP_AI_WATCH_SECONDS:-15}" frame count=0
  local endpoint log_group served_model="" key_parameter=/drip-ai/api-key metric_queries lane
  local api_key="" key_error="" on_tty=""
  local now endpoint_json metrics_json logs_text proxy_code owner_text=""
  local cached_config_name="" dashboard_instance="" dashboard_count="" dashboard_price=""
  [[ "$interval" =~ ^[0-9]{1,6}$ ]] || fail "DRIP_AI_WATCH_SECONDS must be a whole number of seconds"
  [ "$interval" -ge 5 ] || interval=5
  [ -z "$iterations" ] || [[ "$iterations" =~ ^[1-9][0-9]{0,5}$ ]] ||
    fail "DRIP_AI_WATCH_ITERATIONS must be a positive whole number"
  [ -z "${DRIP_AI_NOW_EPOCH:-}" ] || [[ "$DRIP_AI_NOW_EPOCH" =~ ^[0-9]{1,12}$ ]] ||
    fail "DRIP_AI_NOW_EPOCH must be whole seconds since 1970"
  [[ "$local_port" =~ ^[0-9]{1,5}$ ]] && [ "$local_port" -ge 1 ] && [ "$local_port" -le 65535 ] ||
    fail "DRIP_AI_PORT must be a TCP port number"
  [ -n "${region_name:-}" ] || region_name="$(drip_region)" || exit 1
  pick_lane "$region_name" || exit 1
  endpoint="$lane" log_group="/aws/sagemaker/Endpoints/$lane"
  metric_queries="$(start_py metric-queries "$endpoint")"
  work_dir="$(mktemp -d "${TMPDIR:-/tmp}/start-sagemaker.XXXXXX")"
  trap 'rm -rf "$work_dir"' EXIT
  trap 'echo; echo "Closed the view. The endpoint keeps billing until: $teardown"; exit 0' INT
  # Read once into a shell variable; the key never reaches an argv or the environment.
  if ! api_key="$(aws ssm get-parameter --region "$region_name" --name "$key_parameter" \
    --with-decryption --query Parameter.Value --output text 2>"$work_dir/key.err")"; then
    api_key="" key_error="$(cat "$work_dir/key.err")"
    [ -n "$key_error" ] || key_error="aws exited non-zero"
  elif ! [[ "$api_key" =~ $api_key_pattern ]]; then
    api_key="" key_error="the stored key is not sk- followed by letters and digits"
  fi
  [ ! -t 1 ] || on_tty=1
  while :; do
    now="${DRIP_AI_NOW_EPOCH:-$(date +%s)}"
    collect_frame
    frame="$(render_frame)"
    if [ -n "$on_tty" ]; then
      printf '\033[H\033[2J%s\n' "$frame"
    else
      printf '%s\n\n' "$frame"
    fi
    count=$((count + 1))
    [ -z "$iterations" ] || [ "$count" -lt "$iterations" ] || break
    sleep "$interval"
  done
}

# INTERACTIVE yes lists and lets the user pick, behind the owner gate; no only lists, read-only.
list_or_pick() {
  local interactive="$1" take_over="$2" model_check="$3" model_names model_modes model_blockers picked_name="" picked_mode=""
  load_start_context
  if [ "$interactive" = yes ]; then
    gate_endpoint_owner "$take_over"
  else
    read_lanes "$region_name" "$my_started_by" || true
  fi
  resolve_model_store
  if ! list_models; then
    [ "$interactive" = no ] && exit 0
    exit 1
  fi
  [ "$interactive" = yes ] || exit 0
  pick_model
  start_endpoint "$picked_name" "$picked_mode" "$model_check"
}

case "${1:-}" in
  --list) [ $# -eq 1 ] || { usage >&2; exit 1; }; list_or_pick no no yes; exit 0 ;;
  --watch) [ $# -eq 1 ] || { usage >&2; exit 1; }; dashboard; exit 0 ;;
  -h | --help) usage; exit 0 ;;
esac
model_arg="" mode=manifest take_over=no model_check=yes
for argument in "$@"; do
  case "$argument" in
    --no-manifest) mode=no-manifest ;;
    --take-over) take_over=yes ;;
    --unchecked-model) model_check=no ;;
    --yes) assume_yes=yes ;;
    --no-watch) open_dashboard=no ;;
    -*) usage >&2; exit 1 ;;
    *) [ -z "$model_arg" ] || { usage >&2; exit 1; }; model_arg="$argument" ;;
  esac
done
if [ -n "$model_arg" ]; then
  start_named "$model_arg" "$mode" "$take_over" "$model_check"
elif [ "$mode" = manifest ]; then
  list_or_pick yes "$take_over" "$model_check"
else
  usage >&2
  exit 1
fi
