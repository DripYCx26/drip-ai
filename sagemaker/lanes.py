"""Plans a SageMaker race, reading only: which (region, GPU class) lanes run, in what order, and
the model, endpoint configuration, and log group each one needs.

    lanes.py plan --model NAME --mode manifest|no-manifest --home-region R --bucket B
                  --weight-bytes N --out FILE [--unchecked-model] [--hours N] [--set key=value ...]
    lanes.py fit-limit       the largest raced type and 90% of its GPU memory, for the model picker

A lane is one endpoint whose InstancePools hold every type of one GPU class with quota, in the
learned order for its region. Every eligible (region, class) gets one lane while race.max_lanes and
race.max_spend_per_hour_usd allow; extra lanes go to the highest learned placement chance. A region
is skipped only for a missing foundation (the execution role), missing weights, or zero quota;
learning orders regions and never excludes one. Model and configuration names are content hashes,
so a plan that changes nothing reuses them. Everything runs under awscli.read_only(), and the plan
also simulates the execution role's log permission on each lane's exact log group.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.append(str(Path(__file__).resolve().parents[1] / "shared"))
from drip_ai import awscli, capabilities, config, conventions, learn, load_catalog, modelinfo, sizing  # noqa: E402
from drip_ai.awscli import AwsError  # noqa: E402

PLAN_VERSION = 1
FILE_MODE = 0o600
QUOTA_SUFFIX = " for endpoint usage"
LOG_RETENTION_DAYS = 14
VARIANT = "main"
ARCHITECTURES_PATH = config.REPO_ROOT / "shared" / "model-profiles" / "vllm-architectures.json"
STATUS_OBJECT = "drip-ai-fetch.json"
PROFILE_TEMPLATE = ('"{model_type}": {{"vllm_args": ["--enable-auto-tool-choice", "--tool-call-parser",'
                    ' "<parser>", "--reasoning-parser", "<parser>"], "source": "<model card URL>"}},')
TEARDOWN = "bin/drip-ai sagemaker stop"
FIXED_ENV = {"VLLM_NO_USAGE_STATS": "1", "DO_NOT_TRACK": "1", "STANDARD_AUTO_INSTALL_REQ": "false"}


class Refusal(Exception):
    """The race cannot be planned; one user-facing line, and nothing was created."""


def say(text: str = "", err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    stream.write(text + "\n")
    stream.flush()


def tag_value(value: str) -> str:
    """AWS tag-value charset (letters, digits, space, and _ . : / = + @ -), at most 256: the same
    rule as sanitize_tag_value in sagemaker/endpoint.sh, which the owner checks compare against."""
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.:/=+@-")
    return "".join(c if c in allowed else "-" for c in value)[:256]


def short_hash(document: object) -> str:
    return hashlib.sha1(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:8]


# --- Model and profile checks ----------------------------------------------------------------------


def read_s3_text(bucket: str, key: str, region: str) -> str:
    result = awscli.call("s3", "cp", f"s3://{bucket}/{key}", "-", region=region, output=None)
    return result.raise_for_error().stdout


def model_facts(args: argparse.Namespace) -> tuple:
    """(ModelFacts, fetch status object or None) from the home region's copy."""
    where = f"s3://{args.bucket}/models/{args.model}/config.json"
    try:
        data = read_s3_text(args.bucket, f"models/{args.model}/config.json", args.home_region).encode()
    except AwsError as error:
        raise Refusal(f"could not read {where} ({error.message})") from None
    try:
        facts = modelinfo.parse_config(data, args.weight_bytes)
    except modelinfo.ModelInfoError as error:
        raise Refusal(f"{where}: {error}") from None
    status = None
    if args.mode == "manifest":
        try:
            status = json.loads(read_s3_text(args.bucket, f"models/{args.model}/{STATUS_OBJECT}", args.home_region))
        except (AwsError, ValueError):
            status = None
    return facts, status if isinstance(status, dict) else None


def check_architecture(facts: modelinfo.ModelFacts, where: str) -> None:
    try:
        document = json.loads(ARCHITECTURES_PATH.read_text())
        ref, names = document["vllm_ref"], set(document["architectures"])
    except (OSError, ValueError, KeyError, TypeError):
        raise Refusal("the vLLM architecture list is unreadable; rerun"
                      " shared/model-profiles/regenerate_architectures.py") from None
    if facts.architecture not in names:
        raise Refusal(f"{where} names architecture {facts.architecture!r}, which vLLM {ref} in the pinned"
                      " image does not register for text generation, so the endpoint would not boot;"
                      " pick another model, or pass --unchecked-model to try anyway")


def profile_for(facts: modelinfo.ModelFacts, status: Optional[dict], where: str) -> tuple:
    """(vllm_args, profile layer, display line)."""
    try:
        profiles = config.load_profiles()
    except config.ConfigError as error:
        raise Refusal(str(error)) from None
    repo_revision = None
    if status and isinstance(status.get("repo"), str) and isinstance(status.get("revision"), str):
        repo_revision = f"{status['repo']}@{status['revision']}"
    found = config.find_profile(profiles, facts.model_type, repo_revision)
    if found is None:
        raise Refusal(f"no vLLM profile for model_type {facts.model_type!r} ({facts.architecture}). Add one line to"
                      f" {config.PROFILES_NAME} with the tool-call and reasoning parsers from the model card"
                      " and the vLLM docs, then start again:\n  "
                      + PROFILE_TEMPLATE.format(model_type=facts.model_type))
    key, entry = found
    args = list(entry["vllm_args"])
    line = (f"{facts.model_type} ({facts.architecture}): {' '.join(args) or 'no flags'}"
            f"  (profile {key}; source: {entry['source'][:160]})")
    return args, config.profile_layer(entry, key), line


# --- Regions ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RegionFacts:
    region: str
    skip: Optional[str]
    role_arn: Optional[str]
    bucket: Optional[str]
    quotas: dict
    quota_note: Optional[str]


def read_quotas(region: str) -> tuple:
    """({ml type: whole quota}, note); an unreadable list leaves every quota unknown."""
    result = awscli.call("service-quotas", "list-service-quotas", "--service-code", "sagemaker", "--query",
                         f"Quotas[?ends_with(QuotaName, '{QUOTA_SUFFIX}')].[QuotaName,Value]", region=region)
    if not result.ok:
        return {}, f"could not read the SageMaker endpoint quotas ({awscli.last_line(result.stderr)})"
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError:
        return {}, "the SageMaker endpoint quotas answer was not JSON"
    quotas = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, list) and len(row) == 2 and isinstance(row[0], str) and isinstance(row[1], (int, float)):
            quotas[row[0][: -len(QUOTA_SUFFIX)]] = int(row[1])
    return quotas, None


def weights_present(region: str, bucket: str, model: str, mode: str) -> Optional[str]:
    """None when this region's bucket holds a complete copy, else why not."""
    key = f"models/{model}/{STATUS_OBJECT}" if mode == "manifest" else f"models/{model}/config.json"
    if mode == "no-manifest":
        result = awscli.call("s3api", "head-object", "--bucket", bucket, "--key", key, region=region)
        return None if result.ok else f"no s3://{bucket}/{key} ({awscli.last_line(result.stderr)})"
    try:
        status = json.loads(read_s3_text(bucket, key, region))
    except (AwsError, ValueError) as error:
        return f"no complete copy in s3://{bucket}/models/{model}/ ({getattr(error, 'message', 'not JSON')})"
    if not isinstance(status, dict) or status.get("status") != "complete":
        return f"the copy in s3://{bucket}/models/{model}/ is not complete"
    return None


def region_facts(region: str, account: str, args: argparse.Namespace, weights: str, home_bucket: str) -> RegionFacts:
    role = conventions.execution_role_name(region)
    result = awscli.call("iam", "get-role", "--role-name", role, "--query", "Role.Arn", region=region)
    if not result.ok:
        why = ("does not exist" if result.error_class == "absent"
               else f"could not be read ({awscli.last_line(result.stderr)})")
        return RegionFacts(region, f"the execution role {role} {why}; apply the foundation there with:"
                                   f" DRIP_AI_REGION={region} bin/drip-ai setup", None, None, {}, None)
    role_arn = json.loads(result.stdout or '""')
    bucket = home_bucket if region == args.home_region else conventions.models_bucket(account, region)
    if weights == "s3":
        missing = weights_present(region, bucket, args.model, args.mode)
        if missing:
            return RegionFacts(region, f"{missing}; copy it there with DRIP_AI_REGION={region} bin/drip-ai fetch,"
                                       " or set weights hf in the model profile", role_arn, bucket, {}, None)
    quotas, note = read_quotas(region)
    return RegionFacts(region, None, role_arn, bucket, quotas, note)


# --- Allocation ------------------------------------------------------------------------------------


@dataclass
class Slot:
    region: str
    cls: str
    pool: list
    capacity: int
    chance: float
    price: Optional[float]
    order: tuple
    quota_text: str
    lanes: int = 0


def hourly_price(ml_type: str) -> Optional[float]:
    entry = load_catalog("prices")["sagemaker"].get(ml_type)
    return float(entry["usd_per_hour"]) if entry else None


def price_text(pool: list) -> str:
    parts = []
    for ml_type in pool:
        price = hourly_price(ml_type)
        parts.append(f"{ml_type} {'about $%.2f/h' % price if price is not None else 'price unknown'}")
    return ", ".join(parts)


def allocate(slots: list, max_lanes: int, spend_cap: Optional[float]) -> tuple:
    """(lanes as (slot, number) in race order, notes)."""
    lanes, notes, spent = [], [], 0.0

    def affordable(slot: Slot) -> bool:
        if spend_cap is None:
            return True
        if slot.price is None:
            return False
        return spent + slot.price <= spend_cap + 1e-9

    for slot in sorted(slots, key=lambda s: (-s.chance, s.order)):
        if len(lanes) >= max_lanes:
            break
        if not affordable(slot):
            notes.append(f"{slot.region} {slot.cls}: no lane; race.max_spend_per_hour_usd {spend_cap:g} leaves no room"
                         + ("" if slot.price is not None else " for a lane of unknown price"))
            continue
        slot.lanes = 1
        spent += slot.price or 0.0
        lanes.append((slot, 1))
    while len(lanes) < max_lanes:
        open_slots = [s for s in slots if 0 < s.lanes < s.capacity and affordable(s)]
        if not open_slots:
            break
        slot = sorted(open_slots, key=lambda s: (-s.chance, s.lanes, s.order))[0]
        slot.lanes += 1
        spent += slot.price or 0.0
        lanes.append((slot, slot.lanes))
    return lanes, notes


# --- Resources -------------------------------------------------------------------------------------


def model_spec(region: str, facts_region: RegionFacts, plan: sizing.ServePlan, cfg: config.Resolved,
               model: str, weights: str, status: Optional[dict]) -> dict:
    owned = {
        "SM_VLLM_SERVED_MODEL_NAME": cfg["serve.served_model_name"],
        "SM_VLLM_TENSOR_PARALLEL_SIZE": str(plan.tensor_parallel_size),
        "SM_VLLM_MAX_MODEL_LEN": str(plan.max_model_len),
        **FIXED_ENV,
    }
    container = {"Image": conventions.image_pins().sagemaker_image(region), "Mode": "SingleModel"}
    if weights == "hf":
        if not status or not isinstance(status.get("repo"), str) or not isinstance(status.get("revision"), str):
            raise Refusal("serve.weights hf needs the fetch status object's repo and revision; fetch the model first")
        owned.update({"SM_VLLM_MODEL": status["repo"], "SM_VLLM_REVISION": status["revision"],
                      "HF_XET_HIGH_PERFORMANCE": "1"})
    else:
        owned["SM_VLLM_MODEL"] = "/opt/ml/model"
        container["ModelDataSource"] = {"S3DataSource": {
            "S3Uri": f"s3://{facts_region.bucket}/models/{model}/", "S3DataType": "S3Prefix",
            "CompressionType": "None"}}
    try:
        container["Environment"] = sizing.sm_env(list(plan.pairs), owned)
    except sizing.ArgsError as error:
        raise Refusal(str(error)) from None
    body = {"container": container, "role": facts_region.role_arn}
    return {"name": conventions.check_name(f"drip-ai-m-{short_hash(body)}"), "container": container,
            "role_arn": facts_region.role_arn}


def config_spec(model_name: str, pool: list, cfg: config.Resolved, number: int) -> dict:
    variant = {
        "VariantName": VARIANT,
        "ModelName": model_name,
        "InitialInstanceCount": 1,
        "InstancePools": [{"InstanceType": t, "Priority": i + 1} for i, t in enumerate(pool)],
        "VariantInstanceProvisionTimeoutInSeconds": cfg["sagemaker.provision_timeout_seconds"],
        "ModelDataDownloadTimeoutInSeconds": cfg["sagemaker.download_timeout_seconds"],
        "ContainerStartupHealthCheckTimeoutInSeconds": cfg["sagemaker.startup_timeout_seconds"],
        "InitialVariantWeight": 1.0,
    }
    if cfg["sagemaker.inference_ami_version"]:
        variant["InferenceAmiVersion"] = cfg["sagemaker.inference_ami_version"]
    if cfg["sagemaker.volume_size_gb"]:
        variant["VolumeSizeInGB"] = cfg["sagemaker.volume_size_gb"]
    name = conventions.check_name(f"drip-ai-c-{short_hash([variant])}-{number}")
    return {"name": name, "variants": [variant]}


def simulate_logs(region: str, role_arn: str, arns: list) -> Optional[str]:
    """Why the execution role may not write these log groups, or None. A simulation the caller may
    not run is unknown, which is a warning, not a refusal."""
    result = awscli.call("iam", "simulate-principal-policy", "--policy-source-arn", role_arn, "--action-names",
                         "logs:CreateLogStream", "logs:PutLogEvents", "--resource-arns",
                         *[f"{arn}:*" for arn in arns], "--query", "EvaluationResults[].[EvalActionName,EvalDecision]",
                         region=region)
    if not result.ok:
        say(f"start-sagemaker: warning: could not check the execution role's log permission in {region}"
            f" ({awscli.last_line(result.stderr)})", err=True)
        return None
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    denied = sorted({row[0] for row in rows if isinstance(row, list) and len(row) == 2 and row[1] != "allowed"})
    if denied:
        return (f"the execution role in {region} may not {' or '.join(denied)} on its lanes' log groups, so a"
                " failed lane would leave no log; apply the foundation there again")
    return None


# --- The plan --------------------------------------------------------------------------------------


def serve_plans(facts: modelinfo.ModelFacts, cfg: config.Resolved, profile_args: list,
                summary: learn.Summary) -> tuple:
    """({class: (ServePlan, pool of ml types)}, skipped lines) for every class the model fits."""
    digest = conventions.image_pins().sagemaker_digest
    rt = capabilities.runtime(digest)
    evidence = capabilities.Evidence(frozenset(summary.flags_accepted.get(digest, ())),
                                     frozenset(summary.flags_rejected.get(digest, ())),
                                     summary.vllm_versions.get(digest))
    allowed_classes = cfg["race.classes"]
    allowed_types = cfg["race.instance_types"]
    plans, skipped = {}, []
    for cls, members in sizing.gpu_classes().items():
        if allowed_classes is not None and cls not in allowed_classes:
            continue
        pool = [hw.on("sagemaker") for hw in members if allowed_types is None or hw.on("sagemaker") in allowed_types]
        if not pool:
            continue
        hw = sizing.hardware(pool[0])
        measured = sizing.Measured()
        for dtype in ("fp8", "auto"):
            entry = summary.kv.get(learn.kv_key(facts.key, pool[0], digest, dtype, cfg["serve.gpu_memory_utilization"]))
            if entry:
                gib = entry.get("available_kv_gib")
                measured = sizing.Measured(int(gib * (1 << 30)) if gib else None, entry.get("pool_tokens"),
                                           entry.get("dcp"))
                break
        try:
            plans[cls] = (sizing.plan(facts, hw, rt, cfg, "sagemaker", profile_args, evidence, measured), pool)
        except (sizing.PlanRefusal, sizing.ArgsError) as refusal:
            skipped.append(f"Skipping {cls} ({', '.join(pool)}): {refusal}")
    return plans, skipped


def build(args: argparse.Namespace, environ: dict) -> tuple:
    """(plan document, lines to print). Read-only."""
    account, caller = awscli.caller_identity(args.home_region)
    facts, status = model_facts(args)
    where = f"s3://{args.bucket}/models/{args.model}/config.json"
    profile_args, layer, lines = [], {}, []
    if args.unchecked_model:
        say("start-sagemaker: warning: --unchecked-model: the model is not checked against vLLM's architectures or"
            f" {config.PROFILES_NAME}; only serve.extra_args apply, so the boot can fail or tool calls can go"
            " unparsed", err=True)
    else:
        check_architecture(facts, where)
        profile_args, layer, line = profile_for(facts, status, where)
        say(f"Model profile: {line}", err=True)
    cli = config.parse_sets(args.set or [])
    if args.hours is not None:
        cli["race.hours"] = args.hours
    cfg = config.resolve(environ, profile=layer, cli=cli)
    summary = learn.summary(environ) if cfg["learn.enabled"] else learn.Summary()
    warn_short_session(summary)
    plans, skipped = serve_plans(facts, cfg, profile_args, summary)
    lines += skipped
    if not plans:
        raise Refusal(f"s3://{args.bucket}/models/{args.model}/ fits no GPU class drip-ai races"
                      f" ({'; '.join(s.split(': ', 1)[1] for s in skipped) or 'none configured'}); nothing was created")
    weights = cfg["serve.weights"]
    regions = cfg["regions"] or [args.home_region]
    region_index = {r: i for i, r in enumerate(regions)}
    class_index = {c: i for i, c in enumerate(plans)}
    slots, infos = [], {}
    for region in regions:
        info = region_facts(region, account, args, weights, args.bucket)
        infos[region] = info
        if info.skip:
            lines.append(f"{region}: skipped; {info.skip}")
            continue
        if info.quota_note:
            say(f"start-sagemaker: warning: {region}: {info.quota_note}; one lane per class", err=True)
        for cls, (plan, pool) in plans.items():
            known = {t: info.quotas[t] for t in pool if t in info.quotas}
            usable_pool = [t for t in pool if known.get(t, 1) > 0]
            quota_text = ", ".join(f"{t} {known[t] if t in known else 'unknown'}" for t in pool)
            if not usable_pool:
                lines.append(f"{region} {cls}: 0 lanes (quota {quota_text}); skipping it")
                continue
            ordered = sorted(usable_pool, key=lambda t: (-summary.chance(region, t), pool.index(t)))
            capacity = sum(known.get(t, 1) for t in ordered) if known else 1
            prices = [hourly_price(t) for t in ordered]
            price = None if any(p is None for p in prices) else max(prices)
            chance = max(summary.chance(region, t) for t in ordered)
            slots.append(Slot(region, cls, ordered, capacity, chance, price, (region_index[region], class_index[cls]),
                              quota_text))
    if not slots:
        raise Refusal("no region has a GPU class with endpoint quota, a foundation, and the weights; request quota"
                      ' in Service Quotas (SageMaker, "<type> for endpoint usage"); nothing was created')
    allocated, notes = allocate(slots, cfg["race.max_lanes"], cfg["race.max_spend_per_hour_usd"])
    lines += notes
    for slot in sorted(slots, key=lambda s: s.order):
        count = slot.lanes
        lines.append(f"{slot.region} {slot.cls}: {count} lane{'s' if count != 1 else ''} (quota {slot.quota_text})")
    if not allocated:
        raise Refusal("race.max_spend_per_hour_usd leaves no lane; raise it or clear it; nothing was created")
    run_lanes = []
    log_arns: dict = {}
    for slot, number in allocated:
        plan = plans[slot.cls][0]
        name = conventions.check_name(f"drip-ai-{slot.cls}-{slot.region}-{number}")
        model = model_spec(slot.region, infos[slot.region], plan, cfg, args.model, weights, status)
        endpoint_config = config_spec(model["name"], slot.pool, cfg, number)
        log_arns.setdefault(slot.region, []).append(conventions.log_group_arn(account, slot.region, name))
        run_lanes.append({"name": name, "region": slot.region, "class": slot.cls, "pool": slot.pool,
                          "price": slot.price, "model": model, "config": endpoint_config,
                          "log_group": conventions.log_group(name), "chance": round(slot.chance, 3)})
    for region, arns in log_arns.items():
        problem = simulate_logs(region, infos[region].role_arn, arns)
        if problem:
            raise Refusal(problem + "; nothing was created")
    for cls, (plan, pool) in plans.items():
        lines.append(f"Serving plan for {cls} ({', '.join(pool)}):")
        lines += [f"  {decision.line()}" for decision in plan.decisions]
        lines += [f"  {note}" for note in plan.notes]
        lines.append(f"  vLLM flags: {' '.join(plan.argv) or 'none'}")
    hours = cfg["race.hours"]
    cap = int(environ.get("DRIP_AI_RACE_CAP_SECONDS") or hours * 3600)
    document = {
        "version": PLAN_VERSION, "model": args.model, "mode": args.mode, "unchecked": bool(args.unchecked_model),
        "home_region": args.home_region, "account": account, "caller_arn": caller,
        "started_by": tag_value(caller), "cap_seconds": cap, "hours": hours, "lanes": run_lanes,
        "image_digest": conventions.image_pins().sagemaker_digest, "model_key": facts.key,
        "notify": cfg["notify.argv"], "learn": cfg["learn.enabled"], "protect": cfg["lifecycle.protect"],
        "gpu_memory_utilization": cfg["serve.gpu_memory_utilization"],
        "classes": {cls: {"dcp": plan.dcp, "kv_dtype": plan.kv_cache_dtype, "max_model_len": plan.max_model_len,
                          "flags": plan.argv} for cls, (plan, _) in plans.items()},
    }
    return document, lines


def warn_short_session(summary: learn.Summary) -> None:
    """Warns when the credentials end sooner than a lane has usually taken to be placed."""
    usual = summary.p50_placement_seconds()
    if usual is None:
        return
    result = awscli.call("configure", "export-credentials", "--format", "process", output=None)
    if not result.ok:
        return
    try:
        expiry = json.loads(result.stdout).get("Expiration")  # only this field is read; the keys are dropped
    except ValueError:
        return
    from datetime import datetime

    try:
        left = datetime.fromisoformat(str(expiry).replace("Z", "+00:00")).timestamp() - time.time()
    except ValueError:
        return
    if left < usual:
        say(f"start-sagemaker: warning: your AWS session ends in {max(0, int(left // 60))} min, sooner than the"
            f" {int(usual // 60)} min a lane has usually waited for capacity; run aws login first, or the race"
            " pauses until you do", err=True)


def race_block(document: dict) -> list:
    lines = ["Lanes, in order of preference; the first InService is kept and the others are deleted:"]
    total, known = 0.0, True
    for index, lane in enumerate(document["lanes"], 1):
        lines.append(f"  {index}) {lane['name']}  {lane['region']}  {price_text(lane['pool'])}")
        if lane["price"] is None:
            known = False
        else:
            total += lane["price"]
    count = len(document["lanes"])
    lines.append("Retries: a lane that fails for lack of capacity is deleted and created again at once;"
                 " any other failure ends that lane.")
    lines.append(f"Cap: {duration_text(document['cap_seconds'])} without an InService lane, then every lane is deleted.")
    if known:
        lines.append(f"Worst case: up to {count} endpoints can bill at once if several get machines, about ${total:,.2f}/h"
                     " (each lane at its pool's highest list price).")
    else:
        lines.append(f"Worst case: up to {count} endpoints can bill at once if several get machines; the hourly total"
                     " is unknown because a pool's price is unknown.")
    lines.append(f"The kept endpoint bills from creation until: {TEARDOWN}")
    return lines


def duration_text(seconds: int) -> str:
    count, unit = seconds, "second"
    if seconds > 0 and seconds % 3600 == 0:
        count, unit = seconds // 3600, "hour"
    elif seconds > 0 and seconds % 60 == 0:
        count, unit = seconds // 60, "minute"
    return f"1 {unit}" if count == 1 else f"{count} {unit}s"


def write_private(path: Path, document: object) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        json.dump(document, handle, indent=2)


def fit_limit(environ: dict) -> tuple:
    """(SageMaker type with the most GPU memory among the raced classes, 90% of that memory)."""
    cfg = config.resolve(environ)
    best = None
    for cls, members in sizing.gpu_classes().items():
        if cfg["race.classes"] is not None and cls not in cfg["race.classes"]:
            continue
        for hw in members:
            if cfg["race.instance_types"] is not None and hw.on("sagemaker") not in cfg["race.instance_types"]:
                continue
            if hw.hbm_bytes_per_gpu and (best is None or hw.gpus * hw.hbm_bytes_per_gpu > best[1]):
                best = (hw.on("sagemaker"), hw.gpus * hw.hbm_bytes_per_gpu)
    if best is None:
        return None, None
    return best[0], int(best[1] * cfg["serve.gpu_memory_utilization"])


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="lanes.py", description="Plans a SageMaker race; reads only.")
    commands = parser.add_subparsers(dest="command", required=True)
    plan_cmd = commands.add_parser("plan")
    plan_cmd.add_argument("--model", required=True)
    plan_cmd.add_argument("--mode", choices=("manifest", "no-manifest"), required=True)
    plan_cmd.add_argument("--home-region", required=True)
    plan_cmd.add_argument("--bucket", required=True)
    plan_cmd.add_argument("--weight-bytes", type=int, required=True)
    plan_cmd.add_argument("--out", required=True)
    plan_cmd.add_argument("--unchecked-model", action="store_true")
    plan_cmd.add_argument("--hours", type=int)
    plan_cmd.add_argument("--set", action="append")
    commands.add_parser("fit-limit")
    args = parser.parse_args(argv)
    environ = dict(os.environ)
    try:
        if args.command == "fit-limit":
            instance, limit = fit_limit(environ)
            print(f"{instance or ''}\t{limit or ''}")
            return 0
        with awscli.read_only():
            document, lines = build(args, environ)
    except (Refusal, config.ConfigError, sizing.PlanRefusal, sizing.ArgsError, conventions.BadName) as error:
        say(f"start-sagemaker: {error}", err=True)
        return 1
    except AwsError as error:
        say(f"start-sagemaker: aws {error.operation} failed ({error.message}); nothing was created", err=True)
        return 1
    for line in lines + race_block(document):
        say(line)
    write_private(Path(args.out), document)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
