"""Regenerate vllm-architectures.json from vLLM's model registry at one git ref.

Run it whenever the SageMaker image pin in shared/modules/conventions changes, with the vLLM tag
that image is built from:

    python3 -I shared/model-profiles/regenerate_architectures.py v0.30.0

It reads vllm/model_executor/models/registry.py as text and never imports vLLM. Only the
generative tables count, because the endpoint serves chat: an embedding or reward model would boot
and then fail every request. Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import urllib.request
from pathlib import Path

REGISTRY_URL = (
    "https://raw.githubusercontent.com/vllm-project/vllm/{ref}/vllm/model_executor/models/registry.py"
)
GENERATIVE_TABLES = (
    "_TEXT_GENERATION_MODELS",
    "_MULTIMODAL_MODELS",
    "_TRANSFORMERS_SUPPORTED_MODELS",
)
FETCH_TIMEOUT_SECONDS = 60
REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
OUTPUT = Path(__file__).resolve().parent / "vllm-architectures.json"


class RegistryError(Exception):
    """The registry text does not have the shape this script reads; the message is one line."""


def architectures(registry_text: str) -> list:
    """Sorted architecture names from the generative tables; refuses a missing or empty table."""
    try:
        module = ast.parse(registry_text)
    except SyntaxError as error:
        raise RegistryError(f"registry.py does not parse: {error.msg}") from error
    found = {}
    for node in module.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in GENERATIVE_TABLES:
                found[target.id] = [
                    key.value
                    for key in node.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                ]
    for table in GENERATIVE_TABLES:
        if not found.get(table):
            raise RegistryError(f"registry.py has no non-empty {table}; update GENERATIVE_TABLES")
    return sorted({name for table in GENERATIVE_TABLES for name in found[table]})


def document(ref: str, names: list) -> dict:
    return {
        "vllm_ref": ref,
        "source": REGISTRY_URL.format(ref=ref),
        "tables": list(GENERATIVE_TABLES),
        "architectures": names,
    }


def fetch(ref: str) -> str:
    with urllib.request.urlopen(REGISTRY_URL.format(ref=ref), timeout=FETCH_TIMEOUT_SECONDS) as reply:
        return reply.read().decode("utf-8")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ref", help="vLLM git tag or commit, such as v0.30.0")
    parser.add_argument("--registry", type=Path, help="read this registry.py instead of GitHub")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    if REF_PATTERN.fullmatch(args.ref) is None:
        print("regenerate: ref must be a tag or commit name", file=sys.stderr)
        return 1
    try:
        text = args.registry.read_text() if args.registry else fetch(args.ref)
        names = architectures(text)
    except (OSError, RegistryError) as error:
        print(f"regenerate: {error}", file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(document(args.ref, names), indent=2) + "\n")
    print(f"wrote {len(names)} architectures for vLLM {args.ref} to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
