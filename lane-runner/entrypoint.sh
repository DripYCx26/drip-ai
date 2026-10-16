#!/bin/sh
# Run the existing kindness build-lane consumer from a persistent checkout.
set -eu

kindness_root=${KINDNESS_ROOT:-/opt/drip-kindness}
rank_path="$kindness_root/seeds/library/build_rank.json"
rank_marker="$kindness_root/.ranked-queue-uri"

: "${BUILD_RANKED_QUEUE_URI:?BUILD_RANKED_QUEUE_URI is required}"
: "${BUILD_LANE_COUNT:=30}"
: "${BUILD_MODEL:?BUILD_MODEL is required}"
: "${OPENAI_BASE_URL:?OPENAI_BASE_URL is required}"

if [ ! -f "$rank_path" ] || [ ! -f "$rank_marker" ] || [ "$(cat "$rank_marker")" != "$BUILD_RANKED_QUEUE_URI" ]; then
  python3 - "$BUILD_RANKED_QUEUE_URI" "$rank_path" <<'PY'
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import boto3

source = urlparse(sys.argv[1])
if source.scheme != "s3" or not source.netloc or not source.path.lstrip("/"):
    raise SystemExit("BUILD_RANKED_QUEUE_URI must be an s3://bucket/key URI")
destination = Path(sys.argv[2])
destination.parent.mkdir(parents=True, exist_ok=True)
temporary = destination.with_suffix(destination.suffix + ".tmp")
boto3.client("s3").download_file(source.netloc, source.path.lstrip("/"), str(temporary))
os.replace(temporary, destination)
PY
  printf '%s' "$BUILD_RANKED_QUEUE_URI" > "$rank_marker"
fi

if [ -n "${GIT_PUSH_TOKEN:-}" ]; then
  askpass=/tmp/drip-ai-git-askpass
  cat > "$askpass" <<'ASKPASS'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' "x-access-token" ;;
  *Password*) printf '%s' "${GIT_PUSH_TOKEN:?}" ;;
  *) exit 1 ;;
esac
ASKPASS
  chmod 700 "$askpass"
  export GIT_ASKPASS="$askpass"
  export GIT_TERMINAL_PROMPT=0
fi

opencode_config=/opt/opencode.json
python3 - "$opencode_config" <<'PY'
import json
import os
import sys
from pathlib import Path

model = os.environ["BUILD_MODEL"]
provider, separator, model_id = model.partition("/")
if not separator:
    provider, model_id = "openai", model
config = {
    "$schema": "https://opencode.ai/config.json",
    "model": f"{provider}/{model_id}",
    "providers": {
        provider: {
            "env": ["OPENAI_API_KEY"],
            "package": "@opencode/ai/providers/openai-compatible",
            "settings": {"baseURL": os.environ["OPENAI_BASE_URL"]},
            "models": {model_id: {"modelID": model_id}},
        }
    },
}
Path(sys.argv[1]).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY

test -s "$opencode_config" || {
  echo "lane-runner: OpenCode config was not written at $opencode_config" >&2
  exit 1
}
export OPENCODE_CONFIG="$opencode_config"

cd "$kindness_root"
exec python3 seeds/build_lanes.py
