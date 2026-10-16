#!/usr/bin/env bash
# The repo gate: Terraform format, init without a backend, validate, and test on every root; bash -n
# on every shell script; every Python unittest suite. Stops at the first failing step and names it.
# Needs no AWS credentials and touches no state. `--dry-run` prints the steps without running them.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
dry_run=no
case "$#:${1:-}" in
  0:) ;;
  1:--dry-run) dry_run=yes ;;
  *) echo "Usage: shared/check.sh [--dry-run]" >&2; exit 2 ;;
esac

# Terraform roots in gate order.
terraform_roots="bootstrap shared/terraform shared/fetch-model/terraform sagemaker/terraform ec2/terraform lane-runner/terraform"
format_only_dirs="shared/modules/conventions"
test_dirs="shared/tests shared/fetch-model/tests sagemaker/tests ec2/tests"
script_dirs="bin shared sagemaker ec2 lane-runner"

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
