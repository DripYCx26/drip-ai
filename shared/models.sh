#!/usr/bin/env bash
# Model weights in the shared S3 models bucket: upload a local folder, or list what the bucket holds.
# Design: docs/DECISIONS.md, 2026-10-05 "Folders per backend, Terraform roots per stack". Bash 3.2
# compatible. The bucket comes from the shared root's output; the region from drip_region.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$repo_root/shared/lib/common.sh"

# One folder directly under models/: the first segment of model_name_pattern in common.sh.
folder_pattern='^[A-Za-z0-9][A-Za-z0-9._-]*$'
bucket_pattern='^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'

usage() {
  cat <<'EOF_USAGE'
Usage: shared/models.sh upload <folder>   sync models/<folder>/ to s3://<models bucket>/models/<folder>/
       shared/models.sh list              list the model folders in the models bucket

<folder> is one folder name under models/ holding config.json and at least one *.safetensors file.
EOF_USAGE
}

upload_fail() { echo "upload: $1" >&2; exit 1; }
list_fail() { echo "list: $1" >&2; exit 1; }

# Sets the caller's region_name and bucket; $1 names the caller's failure function.
resolve_models_store() {
  local fail="$1"
  region_name="$(drip_region)" || "$fail" "no valid AWS region; nothing was changed"
  bucket="$(tf_output shared models_bucket)" ||
    "$fail" "could not read the models bucket from the shared Terraform root; run bin/drip-ai setup"
  [[ "$bucket" =~ $bucket_pattern ]] || "$fail" "no models bucket in the shared Terraform state"
}

# SageMaker's S3Prefix loads every object under models/<folder>/, so another model below it (an
# Org/Name model at models/<folder>/<Name>/) would load with this one. The rule is
# sagemaker/start.sh's nested_model for an uploaded folder, so start never refuses what upload
# accepted: a sub-folder directly below models/<folder>/ holds another model when it has
# drip-ai-fetch.json, or config.json beside a *.safetensors file. A lone sub-folder config.json
# (original/config.json) is this model's own. Reads `sub/file` paths relative to the folder on
# stdin and prints `MARKER<tab>SUB` for the first such sub-folder, or nothing.
first_nested_model() {
  LC_ALL=C awk -F/ '
    NF == 2 && $2 != "" {
      if ($2 == "drip-ai-fetch.json") fetch[$1] = 1
      else if ($2 == "config.json") config[$1] = 1
      else if ($2 ~ /\.safetensors$/) weights[$1] = 1
    }
    function consider(sub_folder, marker) {
      if (found == "" || sub_folder < first) { found = marker; first = sub_folder }
    }
    END {
      for (s in config) if (s in weights) consider(s, "config.json beside *.safetensors")
      for (s in fetch) consider(s, "drip-ai-fetch.json")
      if (found != "") print found "\t" first
    }'
}

# Refuses before any CLI call when the local folder holds another model; the sync would upload it.
# .cache/ and .git/ are not synced. find -L follows symlinks, as the sync does.
refuse_local_nested_models() {
  local folder="$1" fail="$2" files nested marker sub
  files="$(cd "$repo_root/models/$folder" &&
    find -L . -mindepth 2 -maxdepth 2 -type f ! -path './.cache/*' ! -path './.git/*')" ||
    "$fail" "could not read models/$folder/; nothing was uploaded"
  nested="$(printf '%s\n' "$files" | sed 's|^\./||' | first_nested_model)"
  [ -n "$nested" ] || return 0
  IFS=$'\t' read -r marker sub <<<"$nested"
  "$fail" "models/$folder/$sub/ holds another model ($marker), and SageMaker would load it with this one; move it out of models/$folder/, or upload it as its own folder. Nothing was uploaded"
}

# The same rule on what S3 already holds under models/<folder>/: one paginated list of the folder's
# keys, filtered to the three file names the rule reads; a list error refuses. Needs region_name and
# bucket.
refuse_s3_nested_models() {
  local folder="$1" fail="$2" prefix="models/$1/" listed nested marker sub
  listed="$(aws s3api list-objects-v2 --region "$region_name" --bucket "$bucket" --prefix "$prefix" \
    --query "Contents[?ends_with(Key, '/config.json') || ends_with(Key, '/drip-ai-fetch.json') || ends_with(Key, '.safetensors')].Key" \
    --output text)" ||
    "$fail" "could not list s3://$bucket/$prefix; nothing was uploaded"
  # Text output joins a page's keys with tabs and prints None for a page without matches.
  nested="$(printf '%s\n' "$listed" | tr '\t' '\n' |
    awk -v prefix_length="${#prefix}" 'length($0) > prefix_length { print substr($0, prefix_length + 1) }' |
    first_nested_model)"
  [ -n "$nested" ] || return 0
  IFS=$'\t' read -r marker sub <<<"$nested"
  "$fail" "s3://$bucket/$prefix$sub/ holds another model ($marker), and SageMaker would load it with this one; move it out of models/$folder/ in S3, or use another folder name. Nothing was uploaded"
}

# Every local check runs before any terraform or aws call, so a bad folder spends nothing.
upload() {
  local folder="$1" region_name bucket
  [[ "$folder" =~ $folder_pattern ]] ||
    upload_fail "folder name must be one folder under models/: letters, digits, '.', '_', '-', starting with a letter or digit"
  [ -f "$repo_root/models/$folder/config.json" ] || upload_fail "models/$folder/config.json not found"
  set -- "$repo_root/models/$folder"/*.safetensors
  [ -e "$1" ] || upload_fail "models/$folder/ has no *.safetensors files"
  refuse_local_nested_models "$folder" upload_fail
  resolve_models_store upload_fail
  refuse_s3_nested_models "$folder" upload_fail
  aws s3 sync --region "$region_name" \
    --exclude '.cache/*' --exclude '.git/*' --exclude '.DS_Store' --exclude '*/.DS_Store' \
    "$repo_root/models/$folder/" "s3://$bucket/models/$folder/" ||
    upload_fail "aws s3 sync failed; rerun to resume"
}

# Prints the keys and sub-prefixes directly below PREFIX, one per line; prefixes end in '/'. Text
# output joins a page's values with tabs and prints None for a page without any.
list_level() {
  aws s3api list-objects-v2 --region "$region_name" --bucket "$bucket" --prefix "$1" --delimiter / \
    --query '[Contents[].Key, CommonPrefixes[].Prefix][]' --output text |
    tr '\t' '\n' | awk -v prefix="$1" '$0 != "" && $0 != "None" && $0 != prefix'
}

# Prints the origin of the model at PREFIX (fetch-model or upload) from ENTRIES, or nothing when
# PREFIX holds no model files directly.
model_origin() {
  local prefix="$1" entries="$2"
  if printf '%s\n' "$entries" | grep -Fxq "${prefix}drip-ai-fetch.json"; then
    echo fetch-model
  elif printf '%s\n' "$entries" | grep -Fxq "${prefix}config.json"; then
    echo upload
  fi
}

# Folders under models/ that hold config.json or drip-ai-fetch.json, one or two segments deep.
list_models() {
  local region_name bucket first second entries sub_entries origin name count=0
  resolve_models_store list_fail
  first="$(list_level models/)" || list_fail "could not list s3://$bucket/models/"
  printf 'MODEL\tSOURCE\n'
  while IFS= read -r prefix; do
    case "$prefix" in */) ;; *) continue ;; esac
    entries="$(list_level "$prefix")" || list_fail "could not list s3://$bucket/$prefix"
    origin="$(model_origin "$prefix" "$entries")"
    if [ -n "$origin" ]; then
      name="${prefix#models/}"
      printf '%s\t%s\n' "${name%/}" "$origin"
      count=$((count + 1))
      continue
    fi
    second="$(printf '%s\n' "$entries" | grep '/$' || true)"
    while IFS= read -r sub; do
      [ -n "$sub" ] || continue
      sub_entries="$(list_level "$sub")" || list_fail "could not list s3://$bucket/$sub"
      origin="$(model_origin "$sub" "$sub_entries")"
      [ -n "$origin" ] || continue
      name="${sub#models/}"
      printf '%s\t%s\n' "${name%/}" "$origin"
      count=$((count + 1))
    done <<<"$second"
  done <<<"$first"
  if [ "$count" -eq 0 ]; then
    echo "No models in s3://$bucket/models/."
  else
    echo "fetch-model copies may still be in progress; bin/drip-ai sagemaker list checks completeness."
  fi
}

case "${1:-}" in
  upload)
    [ $# -eq 2 ] || { usage >&2; exit 2; }
    upload "$2"
    ;;
  list)
    [ $# -eq 1 ] || { usage >&2; exit 2; }
    list_models
    ;;
  help | -h | --help) usage ;;
  *) usage >&2; exit 2 ;;
esac
