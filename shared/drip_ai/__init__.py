"""drip-ai's control core: configuration, sizing, learning, AWS calls, and lifecycle checks.

Stdlib only, Python 3.9 or newer, run with `python3 -I`. Entry scripts append this package's
parent folder to sys.path, as shared/lib/json_link.py does, so nothing here shadows the stdlib.
This file holds the package-wide paths: the repository's JSON catalogs and the private state folder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = REPO_ROOT / "shared" / "catalog"
STATE_DIR_MODE = 0o700


def load_catalog(name: str) -> dict:
    """One committed catalog, such as "instances"; a missing or malformed file raises."""
    with open(CATALOG_DIR / f"{name}.json") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"shared/catalog/{name}.json is not a JSON object")
    return document


def state_dir(environ: Mapping[str, str], *parts: str) -> Path:
    """${XDG_STATE_HOME:-~/.local/state}/drip-ai[/parts], created 0700 on first use."""
    base = environ.get("XDG_STATE_HOME", "")
    if not base or not os.path.isabs(base):
        base = os.path.join(environ.get("HOME") or str(Path.home()), ".local", "state")
    path = Path(base, "drip-ai", *parts)
    path.mkdir(mode=STATE_DIR_MODE, parents=True, exist_ok=True)
    for folder in [Path(base, "drip-ai"), *[Path(base, "drip-ai", *parts[: i + 1]) for i in range(len(parts))]]:
        if folder.is_symlink() or not folder.is_dir():
            raise OSError(f"{folder} is not a private folder")
        os.chmod(folder, STATE_DIR_MODE)
    return path
