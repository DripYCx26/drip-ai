"""EC2 capacity blocks: search what can be bought, plan a gapless chain, and buy one by its id.

    capacity.py search [--regions R,R | --regions all] [--class C | --types T,T] [--duration H]
                       [--window D] [--json]
    capacity.py held [--regions ...] [--json]
    capacity.py chain [filters as search] --hours N [--json]
    capacity.py buy OFFERING_ID --region R --yes

`search`, `held`, and `chain` only read, under awscli.read_only(). Each offering shows its zone's
opt-in status, whether the default VPC has a subnet there ("needs subnet" when not), its upfront
fee, and whether the account's capacity-block vCPU quota covers it. UltraServer (u-*) offerings stay
hidden unless capacity.include_ultraservers is true. `chain` lays the blocks already held and the
offerings on offer end to end from now and names every gap. `buy` is the only call here that
spends: it needs --yes and exactly one offering id, and nothing here buys on its own.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.append(str(Path(__file__).resolve().parents[1] / "shared"))
from drip_ai import awscli, config, conventions, sizing  # noqa: E402
from drip_ai.awscli import AwsError  # noqa: E402

OFFERING_ID = re.compile(r"cb-[0-9a-f]{8,32}")
QUOTA_NAME = re.compile(r"(?i)capacity block")


@dataclass(frozen=True)
class Offering:
    offering_id: str
    region: str
    zone: str
    instance_type: str
    start: str
    end: str
    hours: int
    fee: Optional[str]
    currency: Optional[str]
    opt_in: str
    needs_subnet: bool
    quota_note: str

    def start_at(self) -> datetime:
        return parse(self.start)

    def end_at(self) -> datetime:
        return parse(self.end)


@dataclass(frozen=True)
class Held:
    reservation_id: str
    region: str
    zone: str
    instance_type: str
    state: str
    start: str
    end: str
    available: Optional[int]

    def start_at(self) -> datetime:
        return parse(self.start)

    def end_at(self) -> datetime:
        return parse(self.end)


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> datetime:
    fixed = os.environ.get("DRIP_AI_NOW_EPOCH", "")
    return datetime.fromtimestamp(int(fixed), timezone.utc) if fixed.isdigit() else datetime.now(timezone.utc)


def regions_for(text: Optional[str], cfg: config.Resolved, home: str) -> list:
    if text == "all":
        answer = awscli.json_call("ec2", "describe-regions", region=home)
        return sorted(r["RegionName"] for r in answer.get("Regions") or [] if r.get("OptInStatus") != "not-opted-in")
    if text:
        return [conventions.check_region(r.strip()) for r in text.split(",") if r.strip()]
    return cfg["regions"] or [home]


def types_for(args: argparse.Namespace) -> list:
    if args.types:
        return [t.strip() for t in args.types.split(",") if t.strip()]
    classes = sizing.gpu_classes()
    chosen = [args.cls] if args.cls else list(classes)
    if args.cls and args.cls not in classes:
        raise SystemExit(f"capacity: no GPU class {args.cls!r}; classes: {', '.join(classes)}")
    return [hw.on("ec2") for cls in chosen for hw in classes[cls] if hw.gpus >= 8]


def zone_facts(region: str) -> tuple:
    """({zone: opt-in status}, {zones with a default-VPC subnet})."""
    zones = awscli.json_call("ec2", "describe-availability-zones", "--all-availability-zones", region=region)
    status = {z["ZoneName"]: z.get("OptInStatus", "unknown") for z in zones.get("AvailabilityZones") or []}
    vpcs = awscli.json_call("ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true", region=region)
    subnets: set = set()
    for vpc in vpcs.get("Vpcs") or []:
        found = awscli.json_call("ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc['VpcId']}",
                                 region=region)
        subnets |= {s.get("AvailabilityZone") for s in found.get("Subnets") or []}
    return status, subnets


def vcpu_quota(region: str) -> Optional[float]:
    """The capacity-block vCPU quota, or None when the quota list cannot be read or names none."""
    result = awscli.call("service-quotas", "list-service-quotas", "--service-code", "ec2", region=region)
    if not result.ok:
        return None
    for quota in json.loads(result.stdout or "{}").get("Quotas") or []:
        if QUOTA_NAME.search(str(quota.get("QuotaName", ""))) and "vcpu" in str(quota.get("QuotaName", "")).lower():
            return float(quota.get("Value", 0))
    return None


def search_region(region: str, types: list, hours: int, window_days: int, ultraservers: bool) -> tuple:
    """(offerings, notes) for one region, earliest start first."""
    start = now()
    end = start + timedelta(days=window_days, hours=hours)
    zones, subnets = zone_facts(region)
    quota = vcpu_quota(region)
    found, notes = [], []
    for instance_type in types:
        result = awscli.call("ec2", "describe-capacity-block-offerings", "--instance-type", instance_type,
                             "--instance-count", "1", "--capacity-duration-hours", str(hours), "--start-date-range",
                             iso(start), "--end-date-range", iso(end), region=region)
        if not result.ok:
            notes.append(f"{region} {instance_type}: {awscli.last_line(result.stderr)}")
            continue
        for item in json.loads(result.stdout or "{}").get("CapacityBlockOfferings") or []:
            if item.get("InstanceType", "").startswith("u-") and not ultraservers:
                continue
            zone = item.get("AvailabilityZone", "")
            vcpu = instance_vcpu(instance_type)
            if quota is None or vcpu is None:
                quota_note = "quota unknown"
            else:
                quota_note = f"quota {quota:g} vCPU {'covers' if quota >= vcpu else 'does not cover'} {vcpu}"
            found.append(Offering(item["CapacityBlockOfferingId"], region, zone, item["InstanceType"],
                                  item["StartDate"], item["EndDate"], int(item.get("CapacityBlockDurationHours", hours)),
                                  item.get("UpfrontFee"), item.get("CurrencyCode"), zones.get(zone, "unknown"),
                                  zone not in subnets, quota_note))
    return sorted(found, key=lambda o: o.start_at()), notes


def instance_vcpu(instance_type: str) -> Optional[int]:
    try:
        return sizing.hardware(instance_type).vcpu
    except sizing.PlanRefusal:
        return None  # not in the catalog: the quota check is unknown, not passed


def held_blocks(region: str) -> list:
    answer = awscli.json_call("ec2", "describe-capacity-reservations", "--filters",
                              "Name=reservation-type,Values=capacity-block",
                              "Name=state,Values=active,scheduled,payment-pending", region=region)
    blocks = []
    for item in answer.get("CapacityReservations") or []:
        blocks.append(Held(item["CapacityReservationId"], region, item.get("AvailabilityZone", ""),
                           item.get("InstanceType", ""), item.get("State", ""), item.get("StartDate", ""),
                           item.get("EndDate", ""), item.get("AvailableInstanceCount")))
    return sorted(blocks, key=lambda b: b.start_at())


def chain(held: list, offerings: list, start: datetime, until: datetime) -> list:
    """Segments ("held" | "offer" | "gap", item or None, from, to) covering start..until."""
    segments, cursor = [], start
    while cursor < until:
        covering = [b for b in held if b.start_at() <= cursor < b.end_at()]
        if covering:
            block = max(covering, key=lambda b: b.end_at())
            segments.append(("held", block, cursor, block.end_at()))
            cursor = block.end_at()
            continue
        ready = [o for o in offerings if o.start_at() <= cursor < o.end_at()]
        if ready:
            offer = max(ready, key=lambda o: o.end_at())
            segments.append(("offer", offer, cursor, offer.end_at()))
            cursor = offer.end_at()
            continue
        upcoming = [x.start_at() for x in [*held, *offerings] if x.start_at() > cursor]
        gap_end = min(upcoming) if upcoming else until
        segments.append(("gap", None, cursor, min(gap_end, until)))
        cursor = gap_end
    return segments


def offering_line(o: Offering) -> str:
    fee = f"{o.fee} {o.currency}" if o.fee else "fee unknown"
    flags = [f"zone {o.opt_in}"] + (["needs subnet"] if o.needs_subnet else []) + [o.quota_note]
    return (f"{o.offering_id}  {o.region} {o.zone}  {o.instance_type}  {o.start} to {o.end} ({o.hours} h)  {fee}"
            f"  [{'; '.join(flags)}]")


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="capacity.py", description="EC2 capacity blocks.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("search", "held", "chain"):
        command = commands.add_parser(name)
        command.add_argument("--regions")
        command.add_argument("--class", dest="cls")
        command.add_argument("--types")
        command.add_argument("--duration", type=int)
        command.add_argument("--window", type=int)
        command.add_argument("--json", action="store_true")
        command.add_argument("--home-region", default="us-east-1")
        if name == "chain":
            command.add_argument("--hours", type=int, required=True)
    buy = commands.add_parser("buy")
    buy.add_argument("offering_id")
    buy.add_argument("--region", required=True)
    buy.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    environ = dict(os.environ)
    try:
        if args.command == "buy":
            return buy_command(args)
        with awscli.read_only():
            return read_command(args, environ)
    except (config.ConfigError, ValueError) as error:
        print(f"capacity: {error}", file=sys.stderr)
        return 1
    except AwsError as error:
        print(f"capacity: aws {error.operation} failed ({error.message})", file=sys.stderr)
        return 1


def read_command(args: argparse.Namespace, environ: dict) -> int:
    cfg = config.resolve(environ)
    regions = regions_for(args.regions, cfg, args.home_region)
    hours = args.duration or cfg["capacity.duration_hours"]
    window = args.window or cfg["capacity.window_days"]
    if args.command == "held":
        blocks = [b for region in regions for b in held_blocks(region)]
        if args.json:
            print(json.dumps([asdict(b) for b in blocks], indent=2))
        for b in blocks if not args.json else []:
            print(f"{b.reservation_id}  {b.region} {b.zone}  {b.instance_type}  {b.state}  {b.start} to {b.end}"
                  f"  available {b.available if b.available is not None else 'unknown'}")
        if not blocks and not args.json:
            print("No capacity block is active or scheduled in " + ", ".join(regions) + ".")
        return 0
    types = types_for(args)
    offerings, notes = [], []
    for region in regions:
        found, region_notes = search_region(region, types, hours, window, cfg["capacity.include_ultraservers"])
        offerings += found
        notes += region_notes
    offerings.sort(key=lambda o: o.start_at())
    if args.command == "search":
        if args.json:
            print(json.dumps({"offerings": [asdict(o) for o in offerings], "notes": notes}, indent=2))
            return 0
        for note in notes:
            print(f"capacity: warning: {note}", file=sys.stderr)
        if not offerings:
            print(f"No capacity-block offering of {', '.join(types)} for {hours} h in the next {window} days in"
                  f" {', '.join(regions)}.")
        for o in offerings:
            print(offering_line(o))
        print("Nothing was bought. To buy one: bin/drip-ai capacity buy <offering id> --region <region> --yes")
        return 0
    held = [b for region in regions for b in held_blocks(region)]
    start = now()
    segments = chain(held, offerings, start, start + timedelta(hours=args.hours))
    if args.json:
        print(json.dumps([{"kind": kind, "item": asdict(item) if item else None, "from": iso(a), "to": iso(b)}
                          for kind, item, a, b in segments], indent=2))
        return 0
    for kind, item, a, b in segments:
        if kind == "gap":
            print(f"{iso(a)} to {iso(b)}  GAP: no held block or offering covers this")
        elif kind == "held":
            print(f"{iso(a)} to {iso(b)}  held {item.reservation_id} {item.instance_type} in {item.zone}")
        else:
            print(f"{iso(a)} to {iso(b)}  offer {offering_line(item)}")
    print("Nothing was bought; each offer in the chain is a separate capacity buy.")
    return 0


def buy_command(args: argparse.Namespace) -> int:
    if not OFFERING_ID.fullmatch(args.offering_id):
        print("capacity: buy takes one capacity-block offering id (cb-...)", file=sys.stderr)
        return 1
    conventions.check_region(args.region)
    if not args.yes:
        print(f"capacity: buying {args.offering_id} in {args.region} pays its upfront fee at once and cannot be"
              " undone; rerun with --yes once you approve that fee", file=sys.stderr)
        return 1
    result = awscli.call("ec2", "purchase-capacity-block", "--capacity-block-offering-id", args.offering_id,
                         "--instance-platform", "Linux/UNIX", region=args.region)
    if not result.ok:
        print(f"capacity: AWS refused the purchase ({awscli.last_line(result.stderr)}); nothing was bought",
              file=sys.stderr)
        return 1
    reservation = (json.loads(result.stdout or "{}").get("CapacityReservation") or {}).get("CapacityReservationId")
    print(f"Bought {args.offering_id}: capacity reservation {reservation or 'id not shown'} in {args.region}."
          " Launch into it with capacity_reservation_id and market_type = \"capacity-block\" in"
          " ec2/terraform/terraform.tfvars, then bin/drip-ai ec2 apply")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
