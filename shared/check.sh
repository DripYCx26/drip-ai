#!/usr/bin/env bash
# The repo gate: Terraform format, init without a backend, validate, and test on every root; bash -n
# on every shell script; every Python unittest suite. Stops at the first failing step and names it.
# Needs no AWS credentials and touches no state. `--dry-run` prints the steps without running them.
# `--public` first checks the tree is fit to publish: profiles.json is {}, and no catalog or default
# file names a model_type or a Hugging Face repo id.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
dry_run=no public=no
for argument in "$@"; do
  case "$argument" in
    --dry-run) dry_run=yes ;;
    --public) public=yes ;;
    *) echo "Usage: shared/check.sh [--dry-run] [--public]" >&2; exit 2 ;;
  esac
done

# Exits 1, naming the file and key, when the tree carries a model choice the public release keeps out.
public_tree() {
  python3 -I - "$repo_root" <<'PY'
import json, re, sys
from pathlib import Path

root = Path(sys.argv[1])
REPO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*(@[0-9a-f]{7,40})?")
problems = []
if json.loads((root / "shared/model-profiles/profiles.json").read_text()) != {}:
    problems.append("shared/model-profiles/profiles.json is not {}")

def walk(node, path, name):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "model_type" or REPO_ID.fullmatch(key):
                problems.append(f"{name} has key {'.'.join(path + [key])}")
            walk(value, path + [key], name)
    elif isinstance(node, list):
        for item in node:
            walk(item, path, name)

for path in sorted([*root.glob("shared/catalog/*.json"), root / "shared/config/defaults.json"]):
    walk(json.loads(path.read_text()), [], str(path.relative_to(root)))
for problem in problems:
    print(f"check --public: {problem}", file=sys.stderr)
sys.exit(1 if problems else 0)
PY
}

# Terraform roots in gate order.
terraform_roots="bootstrap shared/terraform shared/fetch-model/terraform sagemaker/terraform ec2/terraform"
format_only_dirs="shared/modules/conventions"
test_dirs="shared/tests shared/fetch-model/tests sagemaker/tests ec2/tests"
script_dirs="bin shared sagemaker ec2"

# step NAME COMMAND [ARGS...]
step() {
  local name="$1"
  shift
  if [ "$dry_run" = yes ]; then
    echo "$name: $*"
    return 0
  fi
  echo "== $name"
  "$@" || { echo "check: FAILED at $name: $*" >&2; exit 1; }
}

cd "$repo_root"
[ "$public" = no ] || step "public tree" public_tree
# Each root gets a throwaway data dir, so a checkout initialised against the real S3 backend never
# makes the gate read that backend (or need credentials). Providers come from a shared plugin cache.
gate_data=""
if [ "$dry_run" = no ]; then
  gate_data="$(mktemp -d)"
  trap 'rm -rf "$gate_data"' EXIT
  export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-$HOME/.terraform.d/plugin-cache}"
  mkdir -p "$TF_PLUGIN_CACHE_DIR"
fi
for dir in $format_only_dirs $terraform_roots; do
  [ -d "$dir" ] || continue
  step "fmt $dir" terraform -chdir="$dir" fmt -check -recursive
done
for dir in $terraform_roots; do
  [ -d "$dir" ] || continue
  export TF_DATA_DIR="$gate_data/${dir//\//_}"
  step "init $dir" terraform -chdir="$dir" init -backend=false -lockfile=readonly -input=false
  step "validate $dir" terraform -chdir="$dir" validate
  step "test $dir" terraform -chdir="$dir" test
done
unset TF_DATA_DIR
existing_script_dirs=""
for dir in $script_dirs; do
  [ ! -d "$dir" ] || existing_script_dirs="$existing_script_dirs $dir"
done
# .terraform/ holds downloaded modules and providers, not repo scripts.
scripts="$(find $existing_script_dirs -name .terraform -prune -o -type f -name '*.sh' -print | LC_ALL=C sort)"
step "bash -n bin/drip-ai" bash -n bin/drip-ai
while IFS= read -r script; do
  [ -n "$script" ] || continue
  step "bash -n $script" bash -n "$script"
done <<<"$scripts"
for dir in $test_dirs; do
  [ -d "$dir" ] || continue
  set -- "$dir"/test_*.py
  [ -e "$1" ] || continue
  step "unittest $dir" python3 -I -m unittest discover -s "$dir"
done
[ "$dry_run" = yes ] || echo "check: all steps passed"
