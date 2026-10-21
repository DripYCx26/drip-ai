"""bin/drip-ai's Python commands: plan, config, learn, doctor, capacity, runtime, and logs.

    cli.py config get KEY | show [--explain] [--json] | set KEY=VALUE... | import-tfvars [PATH]
    cli.py plan MODEL --home-region R [--explain] [--set KEY=VALUE...]
    cli.py learn show | explain | forget [--kind K] [--before ISO] | export [--redact]
                 | import --log-group G --region R --model-key K --instance-type T --image-digest D
                          --dcp N --kv-dtype fp8|auto [--gpu-memory-utilization U]
    cli.py doctor --home-region R [--json] [--fix R3,R5 --yes]
    cli.py runtime probe (--help-file FILE | --docker) [--digest D] [--region R]
    cli.py logs ENDPOINT --region R [--follow]
    cli.py vllm-args --model-arg M --served-model-name N --tensor-parallel-size T --max-model-len L -- FLAG...

`plan`, `config get|show`, `learn show|explain|export`, `doctor` without --fix, and `runtime probe
--help-file` only read; they run inside awscli.read_only(). Each command prints its result, or one
`drip-ai:` line naming what failed, and exits 1.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
from drip_ai import awscli, config, conventions, learn, logsignals  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def fail(message: str) -> int:
    print(f"drip-ai: {message}", file=sys.stderr)
    return 1


def shell_words(value: object) -> str:
    """A resolved value as the shell reads it: list items space-separated, null as nothing."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --- config ------------------------------------------------------------------------------------------


def config_command(args: argparse.Namespace, environ: dict) -> int:
    if args.action == "get":
        key = config.lookup(args.key, "config get")
        print(shell_words(config.resolve(environ)[key.name]))
        return 0
    if args.action == "show":
        resolved = config.resolve(environ)
        if args.json:
            print(json.dumps({name: {"value": resolved[name], "source": resolved.source(name)}
                              for name in sorted(resolved.values)}, indent=2))
            return 0
        lines = config.explain_lines(resolved)
        if not args.explain:
            lines = [lines[0]] + [line.split("  (")[0] for line in lines[1:]]
        print(f"User file: {config.user_path(environ)}")
        print("\n".join(lines))
        return 0
    if args.action == "set":
        path = config.set_user_values(environ, args.pairs)
        print(f"Wrote {path}")
        return 0
    tfvars = Path(args.path) if args.path else REPO / "sagemaker" / "terraform" / "terraform.tfvars"
    if not tfvars.exists():
        return fail(f"{tfvars} does not exist; nothing to import")
    moved = config.import_tfvars(environ, tfvars)
    if not moved:
        print(f"{tfvars.name} holds no tuning to move; nothing changed")
        return 0
    for old, new, value in moved:
        print(f"{old} -> {new} = {json.dumps(value)}")
    print(f"Moved {len(moved)} setting(s) to {config.user_path(environ)}; the old file is"
          f" {tfvars.name}.pre-import beside it")
    return 0


# --- plan ----------------------------------------------------------------------------------------------


def lanes_module():
    sys.path.append(str(REPO / "sagemaker"))
    import lanes  # noqa: E402  (sagemaker/lanes.py: the race planner)

    return lanes


def plan_command(args: argparse.Namespace, environ: dict) -> int:
    """The serving plan per GPU class with every key's value and provenance; reads only."""
    lanes = lanes_module()
    account, _ = awscli.caller_identity(args.home_region)
    bucket = conventions.models_bucket(account, args.home_region)
    listing = awscli.json_call("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", f"models/{args.model}/",
                               region=args.home_region)
    sizes = {item["Key"].rpartition("/")[2]: item["Size"] for item in listing.get("Contents") or []}
    from drip_ai import modelinfo  # noqa: E402

    weights = modelinfo.weight_bytes(sizes)
    if weights is None:
        return fail(f"s3://{bucket}/models/{args.model}/ holds no weight files")
    namespace = argparse.Namespace(bucket=bucket, model=args.model, home_region=args.home_region,
                                   weight_bytes=weights, mode="manifest" if "drip-ai-fetch.json" in sizes
                                   else "no-manifest")
    where = f"s3://{bucket}/models/{args.model}/config.json"
    try:
        facts, status = lanes.model_facts(namespace)
        lanes.check_architecture(facts, where)
        profile_args, layer, line = lanes.profile_for(facts, status, where)
    except lanes.Refusal as refusal:
        return fail(str(refusal))
    print(f"Model: {facts.model_type} ({facts.architecture}, {facts.family}), {weights / 1e9:.1f} GB of weights")
    print("Plan, in the order drip-ai prefers: a held capacity block, one on offer, then a SageMaker race.")
    print(f"Profile: {line}")
    resolved = config.resolve(environ, profile=layer, cli=config.parse_sets(args.set or []))
    summary = learn.summary(environ) if resolved["learn.enabled"] else learn.Summary()
    plans, skipped = lanes.serve_plans(facts, resolved, profile_args, summary)
    for text in skipped:
        print(text)
    for cls, (serve_plan, pool) in plans.items():
        print(f"Serving plan for {cls} ({', '.join(pool)}):")
        for decision in serve_plan.decisions:
            print(f"  {decision.line()}")
        for note in serve_plan.notes:
            print(f"  {note}")
        print(f"  vLLM flags: {' '.join(serve_plan.argv) or 'none'}")
    if "ec2-capacity-block" in resolved["substrates"]:
        print_capacity_options(facts, resolved, profile_args, args.home_region, plans)
    print(f"SageMaker race: bin/drip-ai sagemaker start {args.model}" + ("" if plans else " (no class fits)"))
    if args.explain:
        if plans:
            values, reasons = next(iter(plans.values()))[0].derived()
            resolved = resolved.with_derived(values, reasons)
            print(f"Every key ({next(iter(plans))} derives the null ones):")
        print("\n".join(config.explain_lines(resolved)))
        regions = resolved["regions"] or [args.home_region]
        for region in regions:
            for cls, (_, pool) in plans.items():
                for ml_type in pool:
                    placed, failed = summary.counts(region, ml_type)
                    source = "learned" if placed or failed else "builtin"
                    print(f"race.order {region} {ml_type} chance {summary.chance(region, ml_type):.2f}  {source}"
                          f"  (Beta(1,1), {placed:.1f} placed and {failed:.1f} failed, 7-day half-life)")
    return 0 if plans else 1


def print_capacity_options(facts, resolved, profile_args, home: str, plans: dict) -> None:
    """The EC2 options the plan prefers before a SageMaker race: a held block, then one on offer,
    each with the EC2 serving plan for its type. Reads only."""
    from drip_ai import capabilities, conventions as names, sizing

    sys.path.append(str(REPO / "ec2"))
    import capacity  # noqa: E402  (ec2/capacity.py)

    regions = resolved["regions"] or [home]
    rt = capabilities.runtime(names.image_pins().vllm_digest)
    fitting = [hw.on("ec2") for cls, members in sizing.gpu_classes().items() if cls in plans for hw in members]
    held = [b for region in regions for b in capacity.held_blocks(region) if b.state == "active"]
    print(f"1) Held capacity blocks: {', '.join(f'{b.reservation_id} {b.instance_type} ({b.region})' for b in held) or 'none active'}")
    offers = []
    for region in regions:
        found, _ = capacity.search_region(region, fitting, resolved["capacity.duration_hours"],
                                          resolved["capacity.window_days"], resolved["capacity.include_ultraservers"])
        offers += found
    offers.sort(key=lambda o: o.start_at())
    print("2) Capacity blocks on offer (buying is separate: bin/drip-ai capacity buy <id> --region <region> --yes):")
    for offer in offers[:5]:
        print(f"   {capacity.offering_line(offer)}")
    if not offers:
        print("   none in the window")
    for instance_type in sorted({b.instance_type for b in held} | {o.instance_type for o in offers[:5]}):
        try:
            ec2_plan = sizing.plan(facts, sizing.hardware(instance_type), rt, resolved, "ec2", profile_args)
        except (sizing.PlanRefusal, sizing.ArgsError) as refusal:
            print(f"   EC2 on {instance_type}: {refusal}")
            continue
        print(f"   EC2 on {instance_type}: max_model_len {ec2_plan.max_model_len}, DCP {ec2_plan.dcp};"
              f" vllm_extra_args: {' '.join(ec2_plan.argv)}")


# --- learn ---------------------------------------------------------------------------------------------


def learn_command(args: argparse.Namespace, environ: dict) -> int:
    if args.action == "show":
        store = learn.read(environ)
        counts: dict = {}
        for record in store.records:
            counts[record["kind"]] = counts.get(record["kind"], 0) + 1
        state = "on" if learn.enabled(environ) else "off (DRIP_AI_LEARN=off)"
        print(f"Learning is {state}; store: {learn.state_dir(environ) / learn.STORE_NAME}")
        for kind in learn.KINDS:
            print(f"  {kind}: {counts.get(kind, 0)}")
        print(f"  skipped lines (malformed): {store.bad_lines}")
        return 0
    if args.action == "explain":
        summary = learn.summary(environ)
        for key, (placed, failed) in sorted(summary.placement.items()):
            region, instance_type = key.split("|")
            print(f"placement {region} {instance_type}: chance {summary.chance(region, instance_type):.2f}"
                  f" ({placed:.1f} placed, {failed:.1f} failed, decayed)")
        for digest, flags in sorted(summary.flags_rejected.items()):
            print(f"rejected on {digest[:19]}: {', '.join(flags)}")
        for digest, flags in sorted(summary.flags_accepted.items()):
            print(f"accepted on {digest[:19]}: {', '.join(flags)}")
        for key, entry in sorted(summary.kv.items()):
            print(f"kv {key}: {json.dumps(entry)}")
        p50 = summary.p50_placement_seconds()
        print(f"median wait to placement: {f'{p50 / 60:.0f} min' if p50 is not None else 'unknown'}")
        return 0
    if args.action == "forget":
        before = None
        if args.before:
            before = datetime.fromisoformat(args.before.replace("Z", "+00:00")).timestamp()
        print(f"Forgot {learn.forget(environ, args.kind, before)} observation(s).")
        return 0
    if args.action == "export":
        for record in learn.export(environ, args.redact):
            print(json.dumps(record, sort_keys=True))
        return 0
    with awscli.read_only():
        signals = logsignals.parse(logsignals.harvest(args.region, args.log_group))
    if signals.kv_pool_tokens is None and signals.available_kv_gib is None:
        return fail(f"{args.log_group} shows no KV pool or available KV memory line; nothing recorded")
    learn.append(environ, "kv_capacity", "measured", {
        "model_key": args.model_key, "instance_type": args.instance_type, "image_digest": args.image_digest,
        "dcp": args.dcp, "kv_dtype": args.kv_dtype, "gpu_memory_utilization": args.gpu_memory_utilization,
        "pool_tokens": signals.kv_pool_tokens, "available_kv_gib": signals.available_kv_gib})
    print(f"Recorded kv_capacity: pool {signals.kv_pool_tokens} tokens, available {signals.available_kv_gib} GiB"
          f" (vLLM {signals.vllm_version or 'version unknown'})")
    return 0


# --- logs, doctor, runtime -------------------------------------------------------------------------------


def logs_command(args: argparse.Namespace, environ: dict) -> int:
    group = conventions.log_group(args.endpoint)
    argv = ["aws", "logs", "tail", group, "--region", args.region, "--since", "30m"]
    if args.follow:
        argv.append("--follow")
    return subprocess.call(argv)


MODEL_ARG = re.compile(r"(/models/)?[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?")
SERVED_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]*")


def vllm_args_command(args: argparse.Namespace, environ: dict) -> int:
    """The EC2 container argv for ec2 reconfigure, base64 of its JSON, after the flag rules."""
    from drip_ai import sizing

    if not MODEL_ARG.fullmatch(args.model_arg) or not SERVED_NAME.fullmatch(args.served_model_name):
        return fail("the model or served model name from the ec2 Terraform root has an unexpected form")
    flags = args.flags[1:] if args.flags[:1] == ["--"] else args.flags
    pairs = sizing.parse_args(flags, "ec2 reconfigure")
    argv = [args.model_arg, "--served-model-name", args.served_model_name, "--tensor-parallel-size",
            str(args.tensor_parallel_size), "--max-model-len", str(args.max_model_len), *sizing.argv_of(pairs)]
    print(base64.b64encode(json.dumps(argv).encode()).decode())
    return 0


def doctor_command(args: argparse.Namespace, environ: dict) -> int:
    from drip_ai import doctor

    return doctor.main(args, environ)


def runtime_command(args: argparse.Namespace, environ: dict) -> int:
    from drip_ai import probe

    return probe.main(args, environ)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drip-ai", description="drip-ai's control-core commands.")
    groups = parser.add_subparsers(dest="group", required=True)
    cfg = groups.add_parser("config").add_subparsers(dest="action", required=True)
    get = cfg.add_parser("get")
    get.add_argument("key")
    show = cfg.add_parser("show")
    show.add_argument("--explain", action="store_true")
    show.add_argument("--json", action="store_true")
    setter = cfg.add_parser("set")
    setter.add_argument("pairs", nargs="+")
    importer = cfg.add_parser("import-tfvars")
    importer.add_argument("path", nargs="?")
    plan = groups.add_parser("plan")
    plan.add_argument("model")
    plan.add_argument("--home-region", required=True)
    plan.add_argument("--explain", action="store_true")
    plan.add_argument("--set", action="append")
    lrn = groups.add_parser("learn").add_subparsers(dest="action", required=True)
    lrn.add_parser("show")
    lrn.add_parser("explain")
    forget = lrn.add_parser("forget")
    forget.add_argument("--kind")
    forget.add_argument("--before")
    export = lrn.add_parser("export")
    export.add_argument("--redact", action="store_true")
    imp = lrn.add_parser("import")
    for flag in ("--log-group", "--region", "--model-key", "--instance-type", "--image-digest"):
        imp.add_argument(flag, required=True)
    imp.add_argument("--dcp", type=int, required=True)
    imp.add_argument("--kv-dtype", choices=("fp8", "auto"), required=True)
    imp.add_argument("--gpu-memory-utilization", type=float, default=0.9)
    doc = groups.add_parser("doctor")
    doc.add_argument("--home-region", required=True)
    doc.add_argument("--json", action="store_true")
    doc.add_argument("--fix")
    doc.add_argument("--yes", action="store_true")
    runtime = groups.add_parser("runtime").add_subparsers(dest="action", required=True)
    probe = runtime.add_parser("probe")
    source = probe.add_mutually_exclusive_group(required=True)
    source.add_argument("--help-file")
    source.add_argument("--docker", action="store_true")
    probe.add_argument("--digest")
    probe.add_argument("--region")
    vargs = groups.add_parser("vllm-args")
    vargs.add_argument("--model-arg", required=True)
    vargs.add_argument("--served-model-name", required=True)
    vargs.add_argument("--tensor-parallel-size", type=int, required=True)
    vargs.add_argument("--max-model-len", type=int, required=True)
    vargs.add_argument("flags", nargs=argparse.REMAINDER)
    logs = groups.add_parser("logs")
    logs.add_argument("endpoint")
    logs.add_argument("--region", required=True)
    logs.add_argument("--follow", action="store_true")
    return parser


READS_ONLY = {("config", "get"), ("config", "show"), ("plan", None), ("learn", "show"), ("learn", "explain"),
              ("learn", "export")}


def main(argv: list) -> int:
    args = build_parser().parse_args(argv)
    environ = dict(os.environ)
    handlers = {"config": config_command, "plan": plan_command, "learn": learn_command, "doctor": doctor_command,
                "runtime": runtime_command, "logs": logs_command, "vllm-args": vllm_args_command}
    try:
        if (args.group, getattr(args, "action", None)) in READS_ONLY:
            with awscli.read_only():
                return handlers[args.group](args, environ)
        return handlers[args.group](args, environ)
    except (config.ConfigError, learn.LearnError, ValueError) as error:
        return fail(str(error))
    except awscli.AwsError as error:
        return fail(f"aws {error.operation} failed ({error.message})")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
