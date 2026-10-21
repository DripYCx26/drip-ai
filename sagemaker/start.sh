#!/usr/bin/env bash
# Starts a SageMaker endpoint for a model already in the S3 models bucket by racing endpoint lanes
# across the configured regions and GPU classes, then shows a live, read-only dashboard of the
# winner. Design: docs/DECISIONS.md, 2026-10-05 "Folders per backend, Terraform roots per stack",
# 2026-10-07 "Create SageMaker endpoints through the AWS API", and 2026-10-09 "Plan, race, and
# learn in a Python control core". Bash 3.2 and 5.
#
# Before anything changes it reads the StartedBy tag of every drip-ai endpoint in the home region:
# the caller's own opens the dashboard, and anyone else's is refused unless --take-over, each typed
# endpoint name, and the stop path. It checks the S3 copy, then sagemaker/lanes.py plans the race
# (read-only: profile, sizing, regions, quotas, prices) and prints it. With the race lease taken and
# the user's typed yes (or --yes), this process becomes sagemaker/race.py, which creates, retries,
# and deletes the lanes through the API and keeps the first in service. --no-watch returns instead
# of opening the dashboard, so `bin/drip-ai up` can open the tunnel next; --detach races in the
# background and returns at once.
#
# Test hooks: DRIP_AI_NOW_EPOCH fixes "now", DRIP_AI_WATCH_ITERATIONS stops the dashboard after
# that many refreshes, DRIP_AI_TTY replaces /dev/tty as the picker's, take-over's, and approval's
# input; sagemaker/race.py lists the race's own.
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
# shared/terraform/main.tf stores "sk-" plus a random_password with special = false; a bare
# alphanumeric key still passes. Anything else is refused rather than quoted into a curl config line.
api_key_pattern='^(sk-)?[A-Za-z0-9]+$'
teardown='bin/drip-ai sagemaker stop'
# Tuning that moved from sagemaker/terraform/terraform.tfvars to the drip-ai config file.
legacy_tfvars_keys="instance_type instance_types lanes_per_type lane_counts served_model_name max_model_len vllm_extra_args vllm_model_args tensor_parallel_size inference_ami_version volume_size_gb startup_timeout_seconds download_timeout_seconds model_source model_from_s3"
race_hours=8 race_hours_set=no
race_lease_seconds="${DRIP_AI_RACE_LEASE_SECONDS:-900}"
# Race state the EXIT trap shares until race.py takes over this process.
race_lease_held=no plan_dir="" take_over_confirmed=no
# --no-watch sets this to no; --detach to detach. --yes sets endpoint.sh's assume_yes, which
# answers the race's own question and the take-over's deletes.
open_dashboard=yes detach=no

usage() {
  cat <<'EOF_USAGE'
Usage: sagemaker/start.sh [--take-over] [--unchecked-model] [--yes] [--no-watch | --detach] [--hours N]
                                                               pick a model from the S3 bucket, then start it
       sagemaker/start.sh <name> [--no-manifest] [--take-over] [--unchecked-model] [--yes]
                              [--no-watch | --detach] [--hours N]  start models/<name>/ from the S3 bucket
       sagemaker/start.sh --list                               list the models in the S3 bucket
       sagemaker/start.sh --watch [--endpoint NAME]            show the live endpoint dashboard

<name> is a folder under s3://<models bucket>/models/ (name or Org/Name). Without --no-manifest
it must be a complete copy made by shared/fetch-model/fetch-model.sh. --no-manifest accepts a folder uploaded
with shared/models.sh upload (config.json and *.safetensors at the top).
The model's config.json must name an architecture the pinned vLLM registers and a model_type
with a profile in shared/model-profiles/profiles.json; --unchecked-model skips both checks.
Starting plans one lane per GPU class that holds the model, in every configured region with the
execution role, the weights, and endpoint quota, up to race.max_lanes (default 4); each lane's
InstancePools hold every type of its class. It shows each lane's price and the serving plan, and
starts the race once you type yes (--yes answers that question; it still prints the plan first).
A lane that fails for lack of capacity is deleted and created again at once, and any other
failure ends that lane. The first InService is kept and the others are deleted; Ctrl-C or TERM
before that deletes them all, as does --hours N (default race.hours, 8, at most 24) without a
winner. One race runs per account at a time. Your own running endpoint opens the dashboard
instead; someone else's is refused unless --take-over and you type each endpoint name, which
deletes theirs first; --yes never types a name. --no-watch returns instead of opening the
dashboard and exits 0 only when your endpoint exists; --detach races in the background (follow it
with bin/drip-ai race status). Tuning lives in the drip-ai config (bin/drip-ai config show). The
dashboard is read-only; DRIP_AI_WATCH_SECONDS sets its refresh (default 15, min 5).
EOF_USAGE
}

fail() { echo "start-sagemaker: $*" >&2; exit 1; }

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


def verify(mode, name, region, bucket):
    """Exits 1 with a refusal unless models/<name>/ in S3 is one complete model. Prints a summary
    line, then the total bytes and the weight bytes, tab-separated, as the last line."""
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
    weights = sum(size for path, size in sizes.items() if path.endswith((".safetensors", ".bin", ".pt", ".gguf")))
    print(f"{where}: {len(sizes)} files, {gigabytes(total)}, checked")
    if total / DOWNLOAD_CAP_SECONDS > DOWNLOAD_WARN_BYTES_PER_SECOND:
        print(f"start-sagemaker: warning: loading {gigabytes(total)} within SageMaker's"
              f" {DOWNLOAD_CAP_SECONDS} s download limit needs about"
              f" {total / DOWNLOAD_CAP_SECONDS / 10**6:.0f} MB/s from S3", file=sys.stderr)
    print(f"{total}\t{weights or total}")


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
    if blocker is None and size is not None and limit is not None and size > limit:
        blocker = f"{gigabytes(size)} is over 90% of {instance} GPU memory ({limit / GB:.0f} GB)"
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


def price(instance_type):
    """The hourly list price of a SageMaker type from shared/catalog/prices.json, or nothing."""
    path = os.path.join(os.environ["DRIP_AI_REPO"], "shared", "catalog", "prices.json")
    with open(path) as handle:
        entry = json.load(handle)["sagemaker"].get(instance_type)
    if entry:
        print(f"{entry['usd_per_hour']:.2f}")


def main(argv):
    command, args = argv[0], argv[1:]
    try:
        if command == "verify":
            verify(*args)
        elif command == "discover":
            discover(*args)
        elif command == "render":
            render(os.environ)
        elif command == "metric-queries":
            metric_queries(*args)
        elif command == "price":
            price(*args)
        elif command == "epoch":
            epoch = parse_time(args[0])
            if epoch is None:
                return 1
            print(int(epoch))
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

# Sets fit_instance and fit_limit: the raced type with the most GPU memory and the weight bytes it
# allows (gpu_memory_utilization of that memory), from shared/catalog/instances.json and the config.
set_fit() {
  local line
  line="$(python3 -I "$repo_root/sagemaker/lanes.py" fit-limit)" || exit 1
  IFS=$'\t' read -r fit_instance fit_limit <<<"$line" || true
}

# Sets model_names, model_modes, and model_blockers, and prints the numbered table. Returns 1 when
# the bucket holds no model.
list_models() {
  local table name mode blocker display current fit_rule
  current="$(current_model)"
  set_fit
  table="$(start_py discover "$region_name" "$bucket" "$fit_instance" "$fit_limit" "$current")" || exit 1
  if [ -z "$table" ]; then
    echo "No models in s3://$bucket/models/. Copy one with shared/fetch-model/fetch-model.sh," \
      "or upload one with shared/models.sh upload."
    return 1
  fi
  fit_rule="weights must fit in the GPU memory of the largest raced type, $fit_instance; the plan sizes each GPU class"
  [ -n "$fit_limit" ] || fit_rule="no raced type has GPU memory recorded, so fit is not checked"
  echo "Models in s3://$bucket/models/ ($fit_rule):"
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
# reads reuse it.
load_start_context() {
  region_name="$(drip_region)" || exit 1
  load_caller_identity || exit 1
  my_started_by="$(sanitize_tag_value "$drip_caller_arn_value")"
}

# Sets bucket. Refuses sagemaker/terraform/terraform.tfvars tuning, which now lives in the drip-ai
# config, so it is never silently ignored. Needs region_name and the caller identity loaded.
resolve_model_store() {
  local tfvars="$repo_root/sagemaker/terraform/terraform.tfvars" key
  if [ -f "$tfvars" ]; then
    for key in $legacy_tfvars_keys; do
      ! grep -Eqs "^[[:space:]]*$key[[:space:]]*=" "$tfvars" ||
        fail "sagemaker/terraform/terraform.tfvars sets $key, which now lives in your drip-ai config; move it there with: bin/drip-ai config import-tfvars"
    done
  fi
  bucket="$(tf_output shared models_bucket)" ||
    fail "no models_bucket output in the shared Terraform root; apply shared/terraform first"
  [[ "$bucket" =~ $bucket_pattern ]] || fail "the models_bucket output is not a bucket name"
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

# Runs before anything changes, so a race starts only when no live drip-ai endpoint exists in the
# home region. None, or only the caller's own Failed ones (they bill nothing; the race deletes one
# whose configuration has changed since and never relaunches the one that failed): returns. Any
# other of the caller's own: opens the dashboard and exits. Anyone else's, or one whose owner cannot
# be read: refuses unless TAKE_OVER is yes and the user types each of their names; the stop path
# then runs right before the race.
gate_endpoint_owner() {
  local take_over="$1" name status failed=""
  read_lanes "$region_name" "$my_started_by" ||
    fail "could not list the SageMaker endpoints in $region_name; nothing changed"
  if [ -z "$lanes_other" ] && [ -n "$lanes_mine" ]; then
    for name in $lanes_mine; do
      status="$(awk -F'\t' -v name="$name" '$1 == name { print $2; exit }' <<<"$lanes_listed")"
      [ "$status" = Failed ] || { failed=""; break; }
      failed="$failed $name"
    done
    if [ -n "$failed" ]; then
      echo "Endpoint$failed (yours) failed and bills nothing. The race deletes one whose configuration has changed since and creates it again; it never relaunches the configuration that failed."
      return 0
    fi
  fi
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
  echo "Taking over deletes every drip-ai endpoint once you type each name, then races new ones tagged as yours."
  confirm_other_lanes || fail "the typed name did not match $endpoint_name; nothing changed"
  take_over_confirmed=yes
}

# The EXIT trap from the moment start takes the race lease until race.py takes over this process:
# releases the lease and removes the private folder. Safe to run twice.
race_finish() {
  [ -z "$plan_dir" ] || rm -rf "$plan_dir"
  plan_dir=""
  [ "$race_lease_held" = yes ] || return 0
  race_lease_held=no
  lease_release "$race_lease" ||
    echo "start-sagemaker: could not release the $race_lease lease; it expires on its own within $((race_lease_seconds / 60)) minutes" >&2
}

# Takes the race lease for this process, or refreshes it; lease_acquire names a live holder.
take_race_lease() {
  lease_acquire "$race_lease" "$race_lease_seconds" || return 1
  race_lease_held=yes
}

# Needs the owner gate passed. Verifies the S3 copy, then plans the race read-only with
# sagemaker/lanes.py, which prints the profile, the serving plan, the lanes, and their prices. Takes
# the race lease, which a second start, and a stop on another machine, then refuse; a confirmed
# take-over runs the stop path; once the user types yes (or passed --yes), this process becomes
# sagemaker/race.py, keeping its pid, so the lease stays named for the race.
start_endpoint() {
  local name="$1" mode="$2" model_check="$3" rerun="bin/drip-ai sagemaker start $1" verified total weights
  local typed="" plan_args=() race_args=()
  check_model_name "$name"
  resolve_model_store
  verified="$(start_py verify "$mode" "$name" "$region_name" "$bucket")" || exit 1
  printf '%s\n' "${verified%$'\n'*}"
  IFS=$'\t' read -r total weights <<<"${verified##*$'\n'}"
  [ "$mode" = manifest ] || rerun="$rerun --no-manifest"
  if [ "$model_check" = no ]; then
    rerun="$rerun --unchecked-model"
    plan_args+=(--unchecked-model)
  fi
  [ "$race_hours_set" = no ] || plan_args+=(--hours "$race_hours")
  trap race_finish EXIT
  plan_dir="$(mktemp -d "${TMPDIR:-/tmp}/start-sagemaker.XXXXXX")" || fail "could not create a private temporary folder"
  python3 -I "$repo_root/sagemaker/lanes.py" plan --model "$name" --mode "$mode" --home-region "$region_name" \
    --bucket "$bucket" --weight-bytes "$weights" --out "$plan_dir/plan.json" ${plan_args[@]+"${plan_args[@]}"} ||
    exit 1
  take_race_lease || fail "could not take the $race_lease lease; nothing was created"
  if [ "$take_over_confirmed" = yes ]; then
    stop_lanes || fail "the take-over did not delete every drip-ai endpoint; nothing was started"
  fi
  if [ "$assume_yes" = yes ]; then
    echo "Approved with --yes; starting the race."
  else
    open_tty || fail "no terminal to type yes on; nothing was created"
    printf 'Type yes to start this race: '
    read -r -u 5 typed || true
    echo
    close_tty
    [ "$typed" = yes ] || fail "not approved; nothing was created"
  fi
  # The answer can take longer than the lease lasts, so it is taken again before anything changes.
  take_race_lease || fail "could not keep the $race_lease lease; nothing was created"
  [ "$open_dashboard" = no ] || race_args+=(--dashboard)
  [ "$detach" = no ] || race_args+=(--detach)
  # From here race.py owns the plan folder's file and the lease; the folder itself is emptied by it.
  trap - EXIT
  exec python3 -I "$repo_root/sagemaker/race.py" run --plan "$plan_dir/plan.json" --rerun "$rerun" \
    ${race_args[@]+"${race_args[@]}"}
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
    dashboard_price=""
    [ -z "$dashboard_instance" ] || dashboard_price="$(DRIP_AI_REPO="$repo_root" start_py price "$dashboard_instance")" ||
      dashboard_price=""
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

# dashboard [ENDPOINT]
# Read-only: list, describe, tag, metric, log, and parameter reads plus a local GET, and no Terraform
# state. Follows ENDPOINT, else the lane pick_lane chooses; the key is the shared stack's
# /drip-ai/api-key. Ctrl-C ends the view only.
dashboard() {
  local iterations="${DRIP_AI_WATCH_ITERATIONS:-}" interval="${DRIP_AI_WATCH_SECONDS:-15}" frame count=0
  local endpoint log_group served_model="" key_parameter=/drip-ai/api-key metric_queries lane="${1:-}"
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
  if [ -n "$lane" ]; then
    [[ "$lane" =~ $lane_name_pattern ]] || fail "--endpoint must name a drip-ai endpoint"
  else
    pick_lane "$region_name" || exit 1
  fi
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
  local fit_instance="" fit_limit=""
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
  --watch)
    case "$#:${2:-}" in
      1:) dashboard ;;
      3:--endpoint) dashboard "$3" ;;
      *) usage >&2; exit 1 ;;
    esac
    exit 0
    ;;
  -h | --help) usage; exit 0 ;;
esac
model_arg="" mode=manifest take_over=no model_check=yes
while [ $# -gt 0 ]; do
  case "$1" in
    --no-manifest) mode=no-manifest ;;
    --take-over) take_over=yes ;;
    --unchecked-model) model_check=no ;;
    --yes) assume_yes=yes ;;
    --no-watch) open_dashboard=no ;;
    --detach) detach=yes open_dashboard=no ;;
    --hours) [ $# -ge 2 ] || { usage >&2; exit 1; }; race_hours="$2" race_hours_set=yes; shift ;;
    -*) usage >&2; exit 1 ;;
    *) [ -z "$model_arg" ] || { usage >&2; exit 1; }; model_arg="$1" ;;
  esac
  shift
done
[[ "$race_hours" =~ ^[1-9][0-9]?$ ]] && [ "$race_hours" -le 24 ] || fail "--hours takes a whole number of hours from 1 to 24"
[[ "$race_lease_seconds" =~ ^[1-9][0-9]{0,4}$ ]] || fail "DRIP_AI_RACE_LEASE_SECONDS must be whole seconds"
if [ -n "$model_arg" ]; then
  start_named "$model_arg" "$mode" "$take_over" "$model_check"
elif [ "$mode" = manifest ]; then
  list_or_pick yes "$take_over" "$model_check"
else
  usage >&2
  exit 1
fi
