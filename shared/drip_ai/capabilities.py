"""Whether a container image accepts a vLLM flag or engine feature: supported, rejected, or unknown.

Evidence, strongest first: a learned rejection on that image digest ("unrecognized arguments"),
the image's entry in shared/catalog/runtimes.json, a learned acceptance on that digest, then the
flag's first release (vllm_flags.json) against the image's known vLLM version. Anything else is
unknown. Policy by substrate lives in `gate`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

from drip_ai import load_catalog

SUPPORTED, REJECTED, UNKNOWN = "supported", "rejected", "unknown"
VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")


def version_tuple(text: Optional[str]) -> Optional[tuple]:
    match = VERSION.match(text or "")
    return tuple(int(part) for part in match.groups()) if match else None


@dataclass(frozen=True)
class Runtime:
    digest: str
    name: str
    vllm_version: Optional[str]
    version_class: str
    flags: Mapping[str, dict] = field(default_factory=dict)
    features: Mapping[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class Evidence:
    """What learning recorded about one image: flags it accepted or rejected, by digest."""

    accepted: frozenset = frozenset()
    rejected: frozenset = frozenset()
    vllm_version: Optional[str] = None


def runtime(digest: str) -> Runtime:
    """The catalog entry for an image digest; an image not listed has no evidence at all."""
    entry = load_catalog("runtimes")["runtimes"].get(digest)
    if entry is None:
        return Runtime(digest, f"image {digest[:19]}", None, "unknown")
    return Runtime(digest, entry["name"], entry.get("vllm_version"), entry.get("vllm_version_class", "unknown"),
                   entry.get("flags", {}), entry.get("features", {}))


def flag_status(rt: Runtime, flag: str, evidence: Evidence = Evidence()) -> tuple:
    """(status, why) for one flag on one image."""
    if flag in evidence.rejected:
        return REJECTED, f"learned: {rt.name} rejected {flag} (unrecognized arguments)"
    entry = rt.flags.get(flag)
    if entry is not None:
        return entry["status"], f"{entry['class']}: {entry.get('evidence', 'recorded in runtimes.json')}"
    if flag in evidence.accepted:
        return SUPPORTED, f"learned: {rt.name} started with {flag}"
    version = version_tuple(evidence.vllm_version or rt.vllm_version)
    since = version_tuple(load_catalog("vllm_flags")["flags"].get(flag, {}).get("since"))
    if version and since and version >= since:
        return SUPPORTED, f"vLLM {'.'.join(map(str, version))} is at or after {flag}'s first release"
    if version and since:
        return UNKNOWN, (f"vLLM {'.'.join(map(str, version))} predates {flag}'s recorded first release,"
                         " which is unverified")
    return UNKNOWN, f"no evidence that {rt.name} accepts {flag}"


def feature_status(rt: Runtime, feature: str, arch: Optional[str] = None) -> tuple:
    """(status, why) for an engine path; `feature@arch` evidence is checked before `feature`."""
    entry = rt.features.get(f"{feature}@{arch}") if arch else None
    entry = entry or rt.features.get(feature)
    if entry is None:
        where = f" on {arch}" if arch else ""
        return UNKNOWN, f"no evidence that {rt.name} supports {feature}{where}"
    return entry["status"], f"{entry['class']}: {entry.get('evidence', 'recorded in runtimes.json')}"


def usable(status: str, substrate: str) -> bool:
    """Whether the engine may derive a setting that needs this status: supported anywhere; unknown
    only on EC2, where a startup failure is reconfigured in place. SageMaker drops unknown, because
    a restart costs a second scarce node."""
    return status == SUPPORTED or (status == UNKNOWN and substrate == "ec2")


class CapabilityRefusal(ValueError):
    """An explicitly requested flag is known to be rejected by the image."""


def gate(pairs: list, explicit: set, rt: Runtime, substrate: str, evidence: Evidence = Evidence()) -> tuple:
    """Applies the substrate policy to (flag, value) pairs: returns (kept pairs, notes). A rejected
    flag is dropped, or refused when explicit; an unknown derived flag is dropped on SageMaker; an
    unknown explicit flag is kept, because explicit configuration beats evidence, with a note."""
    kept, notes = [], []
    for flag, value in pairs:
        status, why = flag_status(rt, flag, evidence)
        if status == REJECTED:
            if flag in explicit:
                raise CapabilityRefusal(f"{flag} is configured, and {why}; remove it before starting")
            notes.append(f"dropped {flag}: {why}")
            continue
        if status == UNKNOWN and flag not in explicit and substrate == "sagemaker":
            notes.append(f"dropped {flag}: {why}; SageMaker drops unknown flags")
            continue
        if status == UNKNOWN:
            notes.append(f"kept {flag}: {'configured explicitly; ' if flag in explicit else ''}{why}")
        kept.append((flag, value))
    return kept, notes
