"""bin/drip-ai runtime probe: which vLLM serve flags an image accepts, learned before a scarce launch.

    runtime probe --help-file FILE [--digest D]   parse a captured `vllm serve --help`
    runtime probe --docker [--digest D]           run the image's `vllm serve --help` with docker,
                                                  after checking disk.min_free_gb of free space

The parser takes the vLLM version from the help text when it shows one, and every `--flag` it lists.
Each flag drip-ai sets or checks (shared/catalog/vllm_flags.json) becomes supported or rejected for
that image digest in shared/catalog/runtimes.json, class measured, dated; that file is the one
place a probe writes. Help text proves a flag parses, not that the engine path behind it works on a
given GPU, so engine features (DCP on MLA, fp8 KV layouts) keep their evidence. The digest
defaults to the SageMaker image pinned in shared/modules/conventions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
from drip_ai import CATALOG_DIR, config, conventions, load_catalog  # noqa: E402

HELP_FLAG = re.compile(r"(?m)^\s{0,8}(--[a-z0-9][a-z0-9-]*)")
VERSION = re.compile(r"(?i)\bvllm\b[^\n0-9]{0,40}?\b(?:version\s*)?v?([0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]*)")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
DOCKER_TIMEOUT_SECONDS = 1800
# Where a pulled image lands: Docker Desktop keeps its disk image under the home folder; Linux
# Docker keeps layers here.
DOCKER_LINUX_ROOT = "/var/lib/docker"
HELP_MAX_BYTES = 4 * 1024 * 1024


def parse_help(text: str) -> tuple:
    """(vLLM version or None, every --flag the help lists)."""
    flags = set(HELP_FLAG.findall(text))
    flags |= set(re.findall(r"(?:^|[\s,\[{])(--[a-z0-9][a-z0-9-]*)(?=[\s,=\]}])", text))
    match = VERSION.search(text)
    return (match.group(1) if match else None), flags


def record(digest: str, name: str, version: str | None, listed: set) -> dict:
    """Writes the probe's evidence for DIGEST into runtimes.json; returns {flag: status}."""
    catalog = load_catalog("runtimes")
    entry = catalog["runtimes"].setdefault(digest, {"name": name, "vllm_version": None,
                                                    "vllm_version_class": "unknown", "flags": {}, "features": {}})
    if version:
        entry["vllm_version"], entry["vllm_version_class"] = version, "measured"
    today = date.today().isoformat()
    statuses = {}
    for flag in load_catalog("vllm_flags")["flags"]:
        status = "supported" if flag in listed else "rejected"
        evidence = f"{'listed' if flag in listed else 'not listed'} in vllm serve --help, probed {today}"
        entry["flags"][flag] = {"status": status, "class": "measured", "evidence": evidence}
        statuses[flag] = status
    path = CATALOG_DIR / "runtimes.json"
    fd, scratch = tempfile.mkstemp(prefix=".runtimes.", suffix=".json", dir=CATALOG_DIR)
    with os.fdopen(fd, "w") as handle:
        json.dump(catalog, handle, indent=2)
        handle.write("\n")
    os.chmod(scratch, 0o644)
    os.replace(scratch, path)
    return statuses


def disk_refusal(environ: dict) -> str | None:
    """Why a docker pull must not start on this machine, or None: a disk an image would land on has
    less free space than disk.min_free_gb. A pulled image is tens of GB, and one filled a laptop."""
    floor = config.resolve(environ)["disk.min_free_gb"]
    if floor == 0:
        return None
    home = environ.get("HOME") or str(Path.home())
    for path in (home, DOCKER_LINUX_ROOT):
        if not os.path.isdir(path):
            continue
        free = shutil.disk_usage(path).free / 1e9
        if free < floor:
            return (f"{path} has {free:.1f} GB free, under disk.min_free_gb {floor}, so --docker would not pull the"
                    " image here. Capture `vllm serve --help` on a GPU host and pass it with --help-file, or free"
                    " space and try again")
    return None


def docker_help(image: str) -> str:
    """`vllm serve --help` from the image; the user signs docker in to ECR first when it is private."""
    proc = subprocess.run(["docker", "run", "--rm", "--entrypoint", "vllm", image, "serve", "--help"],
                          capture_output=True, text=True, timeout=DOCKER_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["docker exited non-zero"])[-1][:200]
        raise RuntimeError(f"docker could not run {image} ({last}); for AWS's image, sign docker in first:"
                           " aws ecr get-login-password --region <region> | docker login --username AWS"
                           " --password-stdin 763104351884.dkr.ecr.<region>.amazonaws.com")
    return proc.stdout


def main(args: argparse.Namespace, environ: dict) -> int:
    pins = conventions.image_pins()
    digest = args.digest or pins.sagemaker_digest
    if not DIGEST.fullmatch(digest):
        print("drip-ai: --digest takes sha256:<64 hex digits>", file=sys.stderr)
        return 1
    try:
        if args.help_file:
            text = Path(args.help_file).read_text(errors="replace")[:HELP_MAX_BYTES]
            source = args.help_file
        elif args.docker:
            image = (pins.vllm_image if digest == pins.vllm_digest
                     else pins.sagemaker_image(args.region or "us-east-1") if digest == pins.sagemaker_digest
                     else None)
            if image is None:
                print("drip-ai: --docker probes a pinned image only; pass --help-file for another digest",
                      file=sys.stderr)
                return 1
            refusal = disk_refusal(environ)
            if refusal:
                print(f"drip-ai: {refusal}", file=sys.stderr)
                return 1
            text, source = docker_help(image), image
        else:
            print("drip-ai: runtime probe takes --help-file or --docker", file=sys.stderr)
            return 1
    except (OSError, RuntimeError, subprocess.TimeoutExpired, config.ConfigError) as error:
        print(f"drip-ai: {error}", file=sys.stderr)
        return 1
    version, listed = parse_help(text)
    if not listed:
        print(f"drip-ai: {source} lists no --flag; is it vllm serve --help output?", file=sys.stderr)
        return 1
    name = load_catalog("runtimes")["runtimes"].get(digest, {}).get("name") or f"image {digest[:19]}"
    statuses = record(digest, name, version, listed)
    print(f"Probed {name}: vLLM {version or 'version not shown'}, {len(listed)} flags listed.")
    for flag, status in sorted(statuses.items()):
        print(f"  {flag}: {status}")
    print("Recorded in shared/catalog/runtimes.json; commit it so every plan uses it.")
    return 0
