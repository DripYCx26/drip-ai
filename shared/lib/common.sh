# Shared functions for the drip-ai operator scripts. Source it; do not run it.
#
# What it holds: the Terraform wrappers for the per-stack roots (state in the caller's S3 bucket),
# the one-at-a-time lease, the tool-version check, and the tunnel that links the clients. No function
# reads or writes a tfvars file or other session state on disk; the model goes per run with `-var`.
# Design: docs/DECISIONS.md, 2026-10-05 "Folders per backend, Terraform roots per stack".
#
# Contract for callers:
# - Set `set -euo pipefail` before sourcing. This file changes no shell options and runs nothing
#   but variable assignments.
# - Set `repo_root` to the repo checkout before sourcing; when unset, it is derived from this
#   file's location (shared/lib/../..).
# - Bash 3.2 compatible (macOS /bin/bash) and bash 5; no GNU- or BSD-only flags. Date math and JSON
#   go through `python3 -I`.
# - Functions print `drip-ai: message` to stderr and return non-zero on failure themselves, because
#   callers run them inside $(...), `||`, or `if`, where errexit is off. The caller decides what a
#   failure means: `tf_init sagemaker || exit 1`. One exception: tunnel_with_link replaces the
#   caller's EXIT, INT, TERM, and HUP traps, and a signal exits the caller's shell after cleanup.
# - The region comes from DRIP_AI_REGION, then AWS_REGION, AWS_DEFAULT_REGION, and us-east-1.
# - DRIP_AI_STATE_BUCKET_SUFFIX (1 to 12 of a-z, 0-9, inner hyphens) is appended to the state bucket
#   name, to escape a name another account took. `tf bootstrap` passes it to bootstrap/ as
#   `-var state_bucket_suffix=...`, so the bucket it creates is the one state_bucket names.
# - A command that spends takes --yes, which answers only that run's own questions (Terraform's
#   apply or destroy, a launch confirmation); tf_approved passes it to Terraform. It never answers
#   a typed endpoint name or the Hugging Face token prompt.
# - Test hooks: DRIP_AI_NOW_EPOCH fixes "now"; DRIP_AI_TUNNEL_POLL_SECONDS and
#   DRIP_AI_TUNNEL_WAIT_SECONDS shorten the tunnel's readiness wait.

repo_root="${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
# A model folder under models/ in S3: one or two segments, each starting with a letter or digit.
# The same rule lives in shared/fetch-model/copier.py and the model_source validation in
# sagemaker/terraform/variables.tf and ec2/terraform/variables.tf.
model_name_pattern='^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$'

# endpoint_hourly_price INSTANCE_TYPE
# List prices from the costs table in README.md, checked 2026-10-05.
endpoint_hourly_price() {
  case "$1" in
    ml.p5en.48xlarge) echo 'about $72.80/h' ;;
    ml.p5.48xlarge) echo 'about $63.30/h' ;;
    *) echo 'its hourly list price (not recorded in README.md)' ;;
  esac
}

# ---------------------------------------------------------------------------------------------
# Per-stack Terraform roots, shared S3 state, the one-at-a-time lease, and the client-linking tunnel.

region_pattern='^[a-z]{2}(-[a-z]+)+-[0-9]+$'
state_bucket_suffix_pattern='^[a-z0-9]([a-z0-9-]{0,10}[a-z0-9])?$'
lease_name_pattern='^[a-z0-9][a-z0-9-]{0,62}$'
# The longest lease a caller may take; a crashed holder blocks others at most this long.
lease_max_ttl_seconds=86400
codex_provider=drip-ai
# The clients link and unlink know, in the order they run.
link_client_names="codex claude opencode"
# Filled by load_caller_identity; a $(...) subshell inherits them, so load once in the main shell.
drip_account_id=""
drip_caller_arn_value=""
tunnel_child_pid=""
# Set by link_clients: the clients it linked, and the ones it skipped because they are not installed.
linked_clients=""
skipped_clients=""
# Set to yes before tunnel_with_link to run the smoke test once, right after the clients link.
tunnel_smoke=no

# One stdlib program for the JSON and date work the shell cannot do portably.
drip_py() {
  python3 -I -c '
import json, os, sys
from datetime import datetime, timezone

ISO = "%Y-%m-%dT%H:%M:%SZ"

def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(ISO)

def backend_bucket(path):
    with open(path) as f:
        bucket = ((json.load(f).get("backend") or {}).get("config") or {}).get("bucket")
    if not isinstance(bucket, str) or not bucket:
        return 1
    print(bucket)
    return 0

def lease_body():
    now, ttl = int(os.environ["LEASE_NOW"]), int(os.environ["LEASE_TTL"])
    print(json.dumps({
        "owner_arn": os.environ["LEASE_OWNER_ARN"],
        "host": os.environ["LEASE_HOST"],
        "pid": int(os.environ["LEASE_PID"]),
        "acquired_at": iso(now),
        "expires_at": iso(now + ttl),
    }))
    return 0

def lease_read(path, now):
    """Prints owner_arn, host, pid, expires_at, and expired (yes/no), tab-separated."""
    try:
        with open(path) as f:
            lease = json.load(f)
        fields = [lease["owner_arn"], lease["host"], lease["pid"], lease["expires_at"]]
        expires = datetime.strptime(lease["expires_at"], ISO).replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 1
    if type(lease["pid"]) is not int or not 0 < lease["pid"] < 2 ** 31:
        return 1
    text = [str(value) for value in fields]
    if any(not value or not value.isprintable() or "\t" in value for value in text):
        return 1
    text.append("yes" if expires.timestamp() <= int(now) else "no")
    print("\t".join(text))
    return 0

def port_accepts(port):
    """Exits 0 when something accepts a TCP connection on 127.0.0.1:PORT."""
    import socket
    try:
        socket.create_connection(("127.0.0.1", int(port)), timeout=2).close()
    except OSError:
        return 1
    return 0

def lock_info(path):
    """Prints the Key: value pairs of the Lock Info block in Terraform error output."""
    pairs, inside = [], False
    with open(path, errors="replace") as f:
        for raw in f:
            line = raw.strip().lstrip("│").strip()
            if line.startswith("Lock Info:"):
                inside = True
            elif inside and ":" in line:
                key, _, value = line.partition(":")
                if key in ("ID", "Path", "Operation", "Who", "Created") and value.strip():
                    pairs.append(key + "=" + value.strip())
            elif inside and not line:
                break
    print(", ".join(pairs) or "not shown")
    return 0

command, args = sys.argv[1], sys.argv[2:]
handlers = {"backend-bucket": backend_bucket, "lease-body": lease_body, "lease-read": lease_read,
            "lock-info": lock_info, "port-accepts": port_accepts}
sys.exit(handlers[command](*args))
' "$@"
}

drip_now_epoch() {
  local now="${DRIP_AI_NOW_EPOCH:-}"
  [ -n "$now" ] || now="$(date +%s)"
  [[ "$now" =~ ^[0-9]{1,12}$ ]] || { echo "drip-ai: the clock gave '$now', not a Unix time" >&2; return 1; }
  echo "$now"
}

# Prints DRIP_AI_REGION, else AWS_REGION, else AWS_DEFAULT_REGION, else us-east-1.
drip_region() {
  local region="${DRIP_AI_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}}"
  [[ "$region" =~ $region_pattern ]] || {
    echo "drip-ai: '$region' is not an AWS region name; set DRIP_AI_REGION, AWS_REGION, or AWS_DEFAULT_REGION" >&2
    return 1
  }
  echo "$region"
}

# Sets drip_account_id and drip_caller_arn_value from one STS call; later calls reuse them.
load_caller_identity() {
  local region identity account arn
  [ -z "$drip_account_id" ] || return 0
  region="$(drip_region)" || return 1
  identity="$(aws sts get-caller-identity --region "$region" --query '[Account,Arn]' --output text)" || {
    echo "drip-ai: could not read your AWS identity (aws sts get-caller-identity); check your AWS credentials" >&2
    return 1
  }
  account="${identity%%$'\t'*}"
  arn="${identity#*$'\t'}"
  arn="${arn%$'\n'}"
  if ! [[ "$account" =~ ^[0-9]{12}$ ]] || ! [[ "$arn" =~ ^arn:aws[a-z-]*:(iam|sts)::[0-9]{12}:[!-~]+$ ]]; then
    echo "drip-ai: aws sts get-caller-identity returned an unexpected account or ARN" >&2
    return 1
  fi
  drip_account_id="$account"
  drip_caller_arn_value="$arn"
}

drip_account() { load_caller_identity && echo "$drip_account_id"; }
drip_caller_arn() { load_caller_identity && echo "$drip_caller_arn_value"; }

# Prints DRIP_AI_STATE_BUCKET_SUFFIX, empty when unset; returns 1 when it is malformed.
state_bucket_suffix() {
  local suffix="${DRIP_AI_STATE_BUCKET_SUFFIX:-}"
  [ -z "$suffix" ] || [[ "$suffix" =~ $state_bucket_suffix_pattern ]] || {
    echo "drip-ai: DRIP_AI_STATE_BUCKET_SUFFIX must be 1 to 12 lowercase letters, digits, or inner hyphens" >&2
    return 1
  }
  echo "$suffix"
}

# The Terraform state bucket for the caller's account and region, plus DRIP_AI_STATE_BUCKET_SUFFIX
# when set. bootstrap/ builds the same name from its state_bucket_suffix variable.
state_bucket() {
  local account region suffix
  suffix="$(state_bucket_suffix)" || return 1
  account="$(drip_account)" && region="$(drip_region)" || return 1
  echo "drip-ai-tfstate-$account-$region${suffix:+-$suffix}"
}

# Prints present or missing; any other head-bucket failure prints the reason and returns 1. The
# bucket name is predictable and S3 names are global, so the bucket must belong to the caller's
# account: S3 answers 403 for a bucket another account owns.
state_bucket_state() {
  local bucket="$1" region="$2" err_file
  load_caller_identity || return 1
  err_file="$(mktemp)" || return 1
  if aws s3api head-bucket --bucket "$bucket" --expected-bucket-owner "$drip_account_id" --region "$region" \
    >/dev/null 2>"$err_file"; then
    rm -f "$err_file"
    echo present
    return 0
  fi
  if grep -Eq '\(404\)|Not Found|NoSuchBucket' "$err_file"; then
    rm -f "$err_file"
    echo missing
    return 0
  fi
  if grep -Eq '\(403\)|Forbidden|AccessDenied' "$err_file"; then
    echo "drip-ai: the state bucket name $bucket is taken by another account, or your credentials lack s3:ListBucket on it; set DRIP_AI_STATE_BUCKET_SUFFIX or contact your admin:" >&2
    cat "$err_file" >&2
    rm -f "$err_file"
    return 1
  fi
  echo "drip-ai: could not check the state bucket $bucket:" >&2
  cat "$err_file" >&2
  rm -f "$err_file"
  return 1
}

# Prints the absolute directory of a Terraform root.
tf_root_dir() {
  case "${1:-}" in
    shared) echo "$repo_root/shared/terraform" ;;
    fetch-model) echo "$repo_root/shared/fetch-model/terraform" ;;
    sagemaker) echo "$repo_root/sagemaker/terraform" ;;
    ec2) echo "$repo_root/ec2/terraform" ;;
    bootstrap) echo "$repo_root/bootstrap" ;;
    *)
      echo "drip-ai: unknown Terraform root '${1:-}'; use shared, fetch-model, sagemaker, ec2, or bootstrap" >&2
      return 1
      ;;
  esac
}

# Refuses when DIR was initialised against a state bucket other than BUCKET. Never migrates state:
# switching accounts or regions is an explicit `init --reconfigure`.
check_backend_bucket() {
  local key="$1" dir="$2" bucket="$3" backend_file="$2/.terraform/terraform.tfstate" recorded
  [ -f "$backend_file" ] || return 0
  if ! recorded="$(drip_py backend-bucket "$backend_file" 2>/dev/null)"; then
    echo "drip-ai: $key: no S3 backend recorded in ${backend_file#"$repo_root"/}; run bin/drip-ai init --reconfigure" >&2
    return 1
  fi
  [ "$recorded" = "$bucket" ] && return 0
  echo "drip-ai: $key: this checkout was initialised for another AWS account/region or state bucket suffix (state in $recorded, yours is $bucket); run bin/drip-ai init --reconfigure" >&2
  return 1
}

# tf_init KEY [--reconfigure]
# Initialises a root against the caller's state bucket, which must already exist. bootstrap/ keeps
# local state and gets a plain init. Every init keeps the committed .terraform.lock.hcl read-only,
# so state that names a provider the lock file does not pin stops the init instead of installing it.
tf_init() {
  local key="${1:-}" option="${2:-}" reconfigure="" dir region bucket state
  case "$option" in
    "") ;;
    --reconfigure) reconfigure=-reconfigure ;;
    *) echo "drip-ai: tf_init takes only --reconfigure, not '$option'" >&2; return 1 ;;
  esac
  dir="$(tf_root_dir "$key")" || return 1
  [ -d "$dir" ] || { echo "drip-ai: $key: ${dir#"$repo_root"/}/ not found" >&2; return 1; }
  region="$(drip_region)" || return 1
  if [ "$key" = bootstrap ]; then
    TF_VAR_region="$region" terraform -chdir="$dir" init -input=false -lockfile=readonly ${reconfigure:+"$reconfigure"}
    return
  fi
  load_caller_identity || return 1
  bucket="$(state_bucket)" || return 1
  state="$(state_bucket_state "$bucket" "$region")" || return 1
  if [ "$state" = missing ]; then
    echo "drip-ai: $key: the state bucket $bucket does not exist in this account and region; run: bin/drip-ai setup" >&2
    return 1
  fi
  [ -n "$reconfigure" ] || check_backend_bucket "$key" "$dir" "$bucket" || return 1
  TF_VAR_region="$region" terraform -chdir="$dir" init -input=false -lockfile=readonly \
    -backend-config="bucket=$bucket" -backend-config="region=$region" ${reconfigure:+"$reconfigure"}
}

# tf KEY ARGS...
# Runs terraform in a root: initialises it when its backend is not recorded yet, refuses a checkout
# initialised for another account or region, and exports TF_VAR_region. For bootstrap, apply, plan,
# destroy, and import also get -var state_bucket_suffix=... while DRIP_AI_STATE_BUCKET_SUFFIX is set.
# Terraform's stdin and stdout pass through untouched; stderr passes through live while a copy is
# kept, so a state-lock error also names the holder. Returns Terraform's exit code.
tf() {
  local key="${1:-}" dir region bucket suffix err_file status lock_info
  shift || true
  dir="$(tf_root_dir "$key")" || return 1
  [ -d "$dir" ] || { echo "drip-ai: $key: ${dir#"$repo_root"/}/ not found" >&2; return 1; }
  region="$(drip_region)" || return 1
  if [ "$key" = bootstrap ]; then
    suffix="$(state_bucket_suffix)" || return 1
    [ -d "$dir/.terraform" ] || tf_init bootstrap >&2 || return 1
    case "${1:-}" in
      apply | plan | destroy | import) [ -z "$suffix" ] || set -- "$1" -var "state_bucket_suffix=$suffix" "${@:2}" ;;
    esac
  else
    load_caller_identity || return 1
    bucket="$(state_bucket)" || return 1
    # Init output goes to stderr so `$(tf_output ...)` captures only the value.
    [ -f "$dir/.terraform/terraform.tfstate" ] || tf_init "$key" >&2 || return 1
    check_backend_bucket "$key" "$dir" "$bucket" || return 1
  fi
  err_file="$(mktemp)" || return 1
  # fd 4 is the caller's stdout; fd 3 carries Terraform's exit code out of the pipeline. tee ignores
  # Ctrl-C so Terraform can still report a graceful interrupt instead of dying of SIGPIPE.
  {
    status="$(
      {
        {
          if TF_VAR_region="$region" terraform -chdir="$dir" "$@" 2>&1 1>&4 4>&- 3>&-; then
            echo 0 >&3
          else
            echo "$?" >&3
          fi
        } | (trap '' INT TERM HUP; exec tee "$err_file" >&2 3>&- 4>&-)
      } 3>&1
    )"
  } 4>&1
  [[ "$status" =~ ^[0-9]+$ ]] || status=1
  if [ "$status" -ne 0 ] && grep -q 'Error acquiring the state lock' "$err_file"; then
    lock_info="$(drip_py lock-info "$err_file")" || lock_info="not shown"
    echo "drip-ai: someone else is applying $key right now (lock info: $lock_info)" >&2
  fi
  rm -f "$err_file"
  return "$status"
}

tf_output() { tf "$1" output -raw "$2"; }

# tf_approved YES KEY COMMAND ARGS...
# Runs `tf KEY COMMAND ARGS...`. When YES is yes (the user passed --yes to this run), it adds
# -auto-approve and -input=false, so Terraform still prints the plan but asks nothing.
tf_approved() {
  local yes="$1"
  shift
  if [ "$yes" = yes ]; then
    tf "$1" "$2" -auto-approve -input=false "${@:3}"
  else
    tf "$@"
  fi
}

# Prints precondition, missing, unsupported, or other for the aws stderr in FILE.
aws_failure_kind() {
  if grep -q 'Unknown options' "$1"; then
    echo unsupported
  elif grep -Eq 'PreconditionFailed|\(412\)|ConditionalRequestConflict|\(409\)' "$1"; then
    echo precondition
  elif grep -Eq 'NoSuchKey|\(404\)|Not Found' "$1"; then
    echo missing
  else
    echo other
  fi
}

# lease_put DIR CONDITION_FLAG CONDITION_VALUE
# Writes DIR/body.json to the lease key under one S3 condition. Prints ok or a failure kind.
lease_put() {
  local dir="$1" flag="$2" value="$3"
  if aws s3api put-object --region "$lease_region" --bucket "$lease_bucket" --key "$lease_key" \
    --expected-bucket-owner "$drip_account_id" --body "$dir/body.json" --content-type application/json \
    "$flag" "$value" --query ETag --output text >/dev/null 2>"$dir/put.err"; then
    echo ok
  else
    aws_failure_kind "$dir/put.err"
  fi
}

# lease_get DIR
# Reads the lease into DIR/lease.json and its ETag into DIR/etag. Prints ok or a failure kind.
lease_get() {
  local dir="$1"
  if aws s3api get-object --region "$lease_region" --bucket "$lease_bucket" --key "$lease_key" \
    --expected-bucket-owner "$drip_account_id" --query ETag --output text "$dir/lease.json" \
    >"$dir/etag" 2>"$dir/get.err"; then
    echo ok
  else
    aws_failure_kind "$dir/get.err"
  fi
}

# Sets lease_bucket, lease_key, lease_region, lease_host for NAME. Callers declare them local. Every
# lease call passes --expected-bucket-owner, so a lease never lands in a bucket another account owns.
lease_target() {
  [[ "$1" =~ $lease_name_pattern ]] || { echo "drip-ai: invalid lease name '$1'" >&2; return 1; }
  lease_region="$(drip_region)" || return 1
  load_caller_identity || return 1
  lease_bucket="$(state_bucket)" || return 1
  lease_key="leases/$1.json"
  lease_host="$(uname -n)"
}

lease_report_aws_error() {
  echo "drip-ai: lease $1: $2 s3://$lease_bucket/$lease_key failed:" >&2
  cat "$3" >&2
}

# True when PID names a running process on this machine. kill -0 is a shell builtin, so this works
# where ps is missing (minimal Linux images); "not permitted" means the process exists but belongs to
# another local user, which still counts as alive.
pid_alive() {
  local err
  kill -0 "$1" 2>/dev/null && return 0
  err="$(LC_ALL=C kill -0 "$1" 2>&1)" || true
  case "$err" in *ermitted*) return 0 ;; esac
  return 1
}

# lease_acquire NAME TTL_SECONDS [PID]
# Takes s3://<state bucket>/leases/NAME.json for PID (default: this shell, $$) with a create-only
# write. An expired lease is taken over, and a live one refreshed when it names the caller's ARN,
# this host, and PID, or another PID that no longer runs here; each write goes only if the object
# is unchanged since it was read (If-Match), so two racing callers cannot both win. A live lease
# held by another identity, host, or running process here is refused, naming its holder.
lease_acquire() {
  local name="${1:-}" ttl="${2:-}" holder_pid="${3:-$$}" lease_bucket lease_key lease_region lease_host
  local dir now result attempt owner host pid expires expired
  [[ "$ttl" =~ ^[1-9][0-9]{0,5}$ ]] && [ "$ttl" -le "$lease_max_ttl_seconds" ] || {
    echo "drip-ai: lease TTL must be 1 to $lease_max_ttl_seconds seconds, not '$ttl'" >&2
    return 1
  }
  [[ "$holder_pid" =~ ^[1-9][0-9]{0,9}$ ]] || {
    echo "drip-ai: lease pid must be a process id, not '$holder_pid'" >&2
    return 1
  }
  lease_target "$name" || return 1
  now="$(drip_now_epoch)" || return 1
  dir="$(mktemp -d)" || return 1
  if ! LEASE_OWNER_ARN="$drip_caller_arn_value" LEASE_HOST="$lease_host" LEASE_PID="$holder_pid" LEASE_NOW="$now" \
    LEASE_TTL="$ttl" drip_py lease-body >"$dir/body.json"; then
    rm -rf "$dir"
    return 1
  fi
  for attempt in 1 2 3; do
    result="$(lease_put "$dir" --if-none-match '*')"
    case "$result" in
      ok) rm -rf "$dir"; return 0 ;;
      precondition) ;;
      unsupported)
        echo "drip-ai: this AWS CLI has no S3 conditional writes (put-object --if-none-match); install AWS CLI v2.22 or newer" >&2
        rm -rf "$dir"
        return 1
        ;;
      *) lease_report_aws_error "$name" "creating" "$dir/put.err"; rm -rf "$dir"; return 1 ;;
    esac
    result="$(lease_get "$dir")"
    case "$result" in
      ok) ;;
      missing) continue ;;
      *) lease_report_aws_error "$name" "reading" "$dir/get.err"; rm -rf "$dir"; return 1 ;;
    esac
    if ! IFS=$'\t' read -r owner host pid expires expired < <(drip_py lease-read "$dir/lease.json" "$now"); then
      echo "drip-ai: lease $name: s3://$lease_bucket/$lease_key is unreadable; if no one is running $name, delete it with: aws s3 rm s3://$lease_bucket/$lease_key" >&2
      rm -rf "$dir"
      return 1
    fi
    if [ "$expired" = no ] && { [ "$owner" != "$drip_caller_arn_value" ] || [ "$host" != "$lease_host" ]; }; then
      echo "drip-ai: $name is in use by $owner on $host (pid $pid) until $expires; wait for it to finish" >&2
      rm -rf "$dir"
      return 1
    fi
    if [ "$expired" = no ] && [ "$pid" != "$holder_pid" ] && pid_alive "$pid"; then
      echo "drip-ai: $name is held by another drip-ai process on this machine (pid $pid) until $expires; wait or stop it" >&2
      rm -rf "$dir"
      return 1
    fi
    result="$(lease_put "$dir" --if-match "$(cat "$dir/etag")")"
    case "$result" in
      ok) rm -rf "$dir"; return 0 ;;
      precondition | missing) ;;
      unsupported)
        echo "drip-ai: this AWS CLI has no S3 conditional writes (put-object --if-match); install AWS CLI v2.22 or newer" >&2
        rm -rf "$dir"
        return 1
        ;;
      *) lease_report_aws_error "$name" "updating" "$dir/put.err"; rm -rf "$dir"; return 1 ;;
    esac
  done
  echo "drip-ai: lease $name changed hands three times while acquiring it; try again" >&2
  rm -rf "$dir"
  return 1
}

# lease_release NAME
# Deletes the lease only while it still names the caller's ARN and host, whatever its pid, so a
# later process (fetch-model.sh --watch) can release a lease a finished run left held. Deletes with
# If-Match when the CLI supports it, so a takeover after the read survives; an older CLI deletes
# after the compare.
lease_release() {
  local name="${1:-}" lease_bucket lease_key lease_region lease_host dir result owner host pid expires expired etag
  lease_target "$name" || return 1
  dir="$(mktemp -d)" || return 1
  result="$(lease_get "$dir")"
  case "$result" in
    ok) ;;
    missing) rm -rf "$dir"; return 0 ;;
    *) lease_report_aws_error "$name" "reading" "$dir/get.err"; rm -rf "$dir"; return 1 ;;
  esac
  if ! IFS=$'\t' read -r owner host pid expires expired < <(drip_py lease-read "$dir/lease.json" 0) ||
    [ "$owner" != "$drip_caller_arn_value" ] || [ "$host" != "$lease_host" ]; then
    echo "drip-ai: lease $name is no longer yours; left it in place" >&2
    rm -rf "$dir"
    return 0
  fi
  etag="$(cat "$dir/etag")"
  if ! aws s3api delete-object --region "$lease_region" --bucket "$lease_bucket" --key "$lease_key" \
    --expected-bucket-owner "$drip_account_id" --if-match "$etag" >/dev/null 2>"$dir/delete.err"; then
    case "$(aws_failure_kind "$dir/delete.err")" in
      unsupported)
        aws s3api delete-object --region "$lease_region" --bucket "$lease_bucket" --key "$lease_key" \
          --expected-bucket-owner "$drip_account_id" >/dev/null 2>"$dir/delete.err" || { lease_report_aws_error "$name" "deleting" "$dir/delete.err"; rm -rf "$dir"; return 1; }
        ;;
      precondition | missing) echo "drip-ai: lease $name changed after it was read; left it in place" >&2 ;;
      *) lease_report_aws_error "$name" "deleting" "$dir/delete.err"; rm -rf "$dir"; return 1 ;;
    esac
  fi
  rm -rf "$dir"
}

# version_at_least HAVE NEED: compares dotted versions numerically, up to three parts.
version_at_least() {
  local have_major have_minor have_patch need_major need_minor need_patch
  IFS=. read -r have_major have_minor have_patch <<<"$1"
  IFS=. read -r need_major need_minor need_patch <<<"$2"
  have_patch="${have_patch%%[!0-9]*}"
  [ "${have_major:-0}" -ne "${need_major:-0}" ] && { [ "${have_major:-0}" -gt "${need_major:-0}" ]; return; }
  [ "${have_minor:-0}" -ne "${need_minor:-0}" ] && { [ "${have_minor:-0}" -gt "${need_minor:-0}" ]; return; }
  [ "${have_patch:-0}" -ge "${need_patch:-0}" ]
}

# Checks python3 >= 3.9, AWS CLI v2 >= 2.9, and Terraform >= 1.11 (S3 lockfile locking), naming
# every missing or old tool before returning 1. Warns when the AWS CLI predates the S3 conditional
# writes the lease needs.
require_tools() {
  local missing=0 version output
  # ASSUMPTION: 2.22.0 is the first AWS CLI v2 with put-object --if-match (S3, November 2024);
  # unverified. lease_acquire still refuses clearly on a CLI without it.
  local conditional_write_cli=2.22.0
  if version="$(python3 -I -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"; then
    version_at_least "$version" 3.9 || { echo "drip-ai: python3 is $version; install Python 3.9 or newer" >&2; missing=1; }
  else
    echo "drip-ai: python3 not found; install Python 3.9 or newer" >&2
    missing=1
  fi
  if output="$(aws --version 2>&1)" && [[ "$output" =~ aws-cli/([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    version="${BASH_REMATCH[1]}"
    if [ "${version%%.*}" != 2 ] || ! version_at_least "$version" 2.9; then
      echo "drip-ai: AWS CLI is $version; install AWS CLI v2.9 or newer" >&2
      missing=1
    elif ! version_at_least "$version" "$conditional_write_cli"; then
      echo "drip-ai: warning: AWS CLI $version may lack S3 conditional writes; leases need $conditional_write_cli or newer" >&2
    fi
  else
    echo "drip-ai: AWS CLI v2 not found; install it (https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)" >&2
    missing=1
  fi
  if output="$(CHECKPOINT_DISABLE=1 terraform version 2>/dev/null)" && [[ "$output" =~ Terraform\ v([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    version="${BASH_REMATCH[1]}"
    version_at_least "$version" 1.11 || { echo "drip-ai: Terraform is $version; install Terraform 1.11 or newer" >&2; missing=1; }
  else
    echo "drip-ai: terraform not found; install Terraform 1.11 or newer" >&2
    missing=1
  fi
  return "$missing"
}

# Stops the tunnel child's whole process group (it runs in its own; see tunnel_with_link): TERM,
# up to five seconds, then KILL.
stop_tunnel_child() {
  local pid="$tunnel_child_pid" tries=0
  [ -n "$pid" ] || return 0
  tunnel_child_pid=""
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || return 0
  while kill -0 "$pid" 2>/dev/null && [ "$tries" -lt 50 ]; do
    sleep 0.1
    tries=$((tries + 1))
  done
  kill -KILL -- "-$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

client_label() {
  case "$1" in
    codex) echo Codex ;;
    claude) echo "Claude Code" ;;
    *) echo "$1" ;;
  esac
}

# What makes CLIENT read the new config.
client_restart() {
  case "$1" in
    codex) echo "restart Codex" ;;
    claude) echo "new session" ;;
    *) echo "restart $1" ;;
  esac
}

# Prints the clients a tunnel links, space-separated: DRIP_AI_LINK_CLIENTS (a comma list of codex,
# claude, and opencode, or all, or none; default all), less codex when DRIP_AI_CODEX_LINK=0.
# Returns 1, naming it, on an unknown client.
link_client_selection() {
  local requested="${DRIP_AI_LINK_CLIENTS:-all}" name selected=""
  case "$requested" in
    all) requested="${link_client_names// /,}" ;;
    none) requested="" ;;
  esac
  for name in ${requested//,/ }; do
    case " $link_client_names " in
      *" $name "*) ;;
      *) echo "drip-ai: DRIP_AI_LINK_CLIENTS names '$name'; use codex, claude, opencode, all, or none" >&2; return 1 ;;
    esac
    [ "$name" != codex ] || [ "${DRIP_AI_CODEX_LINK:-1}" != 0 ] || continue
    case " $selected " in *" $name "*) continue ;; esac
    selected="${selected:+$selected }$name"
  done
  echo "$selected"
}

# unlink_client CLIENT [--force]: restores what the link changed; "not linked" is a success.
unlink_client() {
  local client="$1"
  shift
  case "$client" in
    codex) python3 -I "$repo_root/shared/lib/codex_link.py" unlink --provider "$codex_provider" "$@" ;;
    *) python3 -I "$repo_root/shared/lib/json_link.py" unlink --client "$client" "$@" ;;
  esac
}

# For the stop paths: unlinks every client, because the links would point at nothing. A failure
# warns, the other clients still unlink, and the caller carries on.
unlink_all_clients() {
  local client
  for client in $link_client_names; do
    unlink_client "$client" ||
      echo "drip-ai: could not restore the $(client_label "$client") config; run bin/drip-ai unlink $client" >&2
  done
}

# The EXIT trap of tunnel_with_link: restore each linked client first, then stop the child.
tunnel_cleanup() {
  local client clients="$linked_clients"
  trap - EXIT INT TERM HUP
  linked_clients=""
  for client in $clients; do
    unlink_client "$client" ||
      echo "drip-ai: could not restore the $(client_label "$client") config; run bin/drip-ai unlink $client" >&2
  done
  stop_tunnel_child
}

# Exits 0 when GET /v1/models on PORT answers 200 with KEY. printf is a builtin, so the key reaches
# curl's config on stdin and never any argv.
tunnel_models_ready() {
  local key="$1" port="$2" code
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$key" |
    curl -sS -o /dev/null -w '%{http_code}' --max-time 5 --config - "http://127.0.0.1:$port/v1/models" \
      2>/dev/null)" || true
  [ "$code" = 200 ]
}

# prepare_api_key_command KEY_PARAMETER REGION
# Sets api_key_command (the absolute aws path) and api_key_args to the SSM read that Codex's auth
# command and Claude Code's apiKeyHelper run, then runs it once as a desktop app would: HOME and a
# system PATH only. Returns 1, saying why, when it prints nothing; its output is discarded unseen.
prepare_api_key_command() {
  local aws_path aws_dir output
  aws_path="$(command -v aws)" || { echo "drip-ai: aws not found on PATH" >&2; return 1; }
  aws_dir="$(cd "$(dirname "$aws_path")" && pwd -P)" || return 1
  api_key_command="$aws_dir/$(basename "$aws_path")"
  api_key_args=(ssm get-parameter --region "$2" --name "$1" --with-decryption --query Parameter.Value
    --output text)
  [ -z "${AWS_PROFILE:-}" ] || api_key_args+=(--profile "$AWS_PROFILE")
  if output="$(env -i HOME="$HOME" PATH=/usr/bin:/bin "$api_key_command" "${api_key_args[@]}" 2>/dev/null)" &&
    [ -n "$output" ]; then
    return 0
  fi
  echo "drip-ai: the API key command failed with only HOME and PATH=/usr/bin:/bin set, so the Codex desktop app and Claude Code may not see your credentials; use a named profile from ~/.aws/config (export AWS_PROFILE=<name>) and reopen the tunnel" >&2
  return 1
}

# run_link_program CLIENT PORT MODEL CONTEXT_WINDOW KEY OWNER_PID
# Codex and Claude Code get the command prepare_api_key_command set. opencode gets KEY on stdin:
# printf is a builtin, so it never reaches any argv.
run_link_program() {
  local client="$1" port="$2" model="$3" window="$4" key="$5" owner="$6" argument args
  case "$client" in
    codex)
      args=(link --provider "$codex_provider" --model "$model" --base-url "http://127.0.0.1:$port/v1"
        --context-window "$window" --auth-command "$api_key_command")
      # The = form keeps argparse from reading an argument such as --region as an option.
      for argument in "${api_key_args[@]}"; do args+=("--auth-arg=$argument"); done
      python3 -I "$repo_root/shared/lib/codex_link.py" "${args[@]}" --owner-pid "$owner"
      ;;
    claude)
      args=(link --client claude --model "$model" --port "$port" --context-window "$window"
        --auth-command "$api_key_command")
      for argument in "${api_key_args[@]}"; do args+=("--auth-arg=$argument"); done
      python3 -I "$repo_root/shared/lib/json_link.py" "${args[@]}" --owner-pid "$owner"
      ;;
    *)
      printf '%s\n' "$key" | python3 -I "$repo_root/shared/lib/json_link.py" link --client "$client" \
        --model "$model" --port "$port" --context-window "$window" --owner-pid "$owner"
      ;;
  esac
}

# link_clients PORT MODEL CONTEXT_WINDOW KEY_PARAMETER REGION KEY OWNER_PID CLIENT...
# Links each CLIENT on its own, so one failure leaves the others alone. Prints one line per client
# linked or failed, and under a linked one each note the link program adds after its summary (a
# stale link taken over, a change of yours kept); a client that is not installed is skipped
# without a word. Adds each to
# linked_clients before its link starts, so a signal mid-link still unlinks it, and takes it out
# again on a failure or a skip. Returns 1 when any link failed.
link_clients() {
  local port="$1" model="$2" window="$3" parameter="$4" region="$5" key="$6" owner="$7"
  local client output notes helper=unchecked status=0
  shift 7
  for client in "$@"; do
    if [ "$client" != opencode ]; then
      if [ "$helper" = unchecked ]; then
        if prepare_api_key_command "$parameter" "$region"; then helper=ready; else helper=failed; fi
      fi
      if [ "$helper" = failed ]; then
        echo "$(client_label "$client"): not linked"
        status=1
        continue
      fi
    fi
    linked_clients="$linked_clients $client"
    if ! output="$(run_link_program "$client" "$port" "$model" "$window" "$key" "$owner")"; then
      linked_clients="${linked_clients% "$client"}"
      echo "$(client_label "$client"): not linked"
      status=1
      continue
    fi
    case "$output" in
      *"config not found; skipping"*)
        linked_clients="${linked_clients% "$client"}"
        skipped_clients="$skipped_clients $client"
        ;;
      *)
        echo "$(client_label "$client"): linked ($(client_restart "$client"))"
        # The programs print "linked ... (owner pid N)", then "; "-separated notes.
        case "$output" in
          *"; "*)
            notes="${output#*; }"
            printf '  %s\n' "${notes//; /$'\n'  }"
            ;;
        esac
        ;;
    esac
  done
  return "$status"
}

# tunnel_with_link MODEL CONTEXT_WINDOW READY_FUNCTION PROXY_COMMAND [ARGS...]
# Runs PROXY_COMMAND (a command or shell function serving 127.0.0.1:$DRIP_AI_PORT, default 8000) as
# a background child in its own process group. Every 15 s for up to 60 min it checks that /v1/models
# answers 200 with the API key and READY_FUNCTION returns 0, then links each installed client that
# link_client_selection names to MODEL with CONTEXT_WINDOW (an empty MODEL or CONTEXT_WINDOW links
# none). On exit, Ctrl-C, TERM, or HUP it unlinks exactly the clients it linked, then stops the
# child. With tunnel_smoke=yes it runs shared/clients.sh smoke once after linking; a failed smoke
# is reported and the tunnel stays open. Returns the child's exit code. Reads api_key_parameter from
# the shared root's outputs. Replaces any caller trap on EXIT, INT, TERM, HUP.
tunnel_with_link() {
  local model="${1:-}" context_window="${2:-}" ready_function="${3:-}" port="${DRIP_AI_PORT:-8000}" link=yes
  local poll="${DRIP_AI_TUNNEL_POLL_SECONDS:-15}" wait_seconds="${DRIP_AI_TUNNEL_WAIT_SECONDS:-3600}"
  local region parameter="" key="" deadline status had_monitor=no clients
  shift 3 || { echo "drip-ai: tunnel_with_link needs MODEL CONTEXT_WINDOW READY_FUNCTION PROXY_COMMAND" >&2; return 1; }
  [ $# -gt 0 ] || { echo "drip-ai: tunnel_with_link needs a proxy command" >&2; return 1; }
  type "$ready_function" >/dev/null 2>&1 || { echo "drip-ai: no readiness check '$ready_function'" >&2; return 1; }
  [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$port" -le 65535 ] || { echo "drip-ai: DRIP_AI_PORT must be a TCP port" >&2; return 1; }
  [[ "$poll" =~ ^[1-9][0-9]{0,4}$ ]] && [[ "$wait_seconds" =~ ^[0-9]{1,6}$ ]] ||
    { echo "drip-ai: DRIP_AI_TUNNEL_POLL_SECONDS and DRIP_AI_TUNNEL_WAIT_SECONDS must be whole seconds" >&2; return 1; }
  # The readiness check sends the API key to whatever answers on the port, so it must be free.
  if drip_py port-accepts "$port"; then
    echo "drip-ai: port $port is already in use; set DRIP_AI_PORT" >&2
    return 1
  fi
  region="$(drip_region)" || return 1
  [ "${DRIP_AI_CODEX_LINK:-1}" != 0 ] || echo "Codex link off (DRIP_AI_CODEX_LINK=0)."
  clients="$(link_client_selection)" || clients=""
  if [ -z "$clients" ]; then
    link=no
    echo "No client links (DRIP_AI_LINK_CLIENTS=${DRIP_AI_LINK_CLIENTS:-all})."
  elif [ -z "$model" ] || [ -z "$context_window" ] || ! load_caller_identity ||
    ! parameter="$(tf_output shared api_key_parameter)" ||
    ! key="$(aws ssm get-parameter --region "$region" --name "$parameter" --with-decryption \
      --query Parameter.Value --output text)"; then
    link=no
    echo "drip-ai: could not read the model, context window, or API key; no client linked" >&2
  elif ! [[ "$model" =~ ^[A-Za-z0-9][A-Za-z0-9._/:-]*$ ]] || ! [[ "$context_window" =~ ^[1-9][0-9]{0,8}$ ]] ||
    ! [[ "$parameter" =~ ^/[A-Za-z0-9_./-]+$ ]] || ! [[ "$key" =~ ^[!-~]+$ ]] || [[ "$key" == *[\"\\]* ]]; then
    link=no
    echo "drip-ai: the model, context window, key parameter, or API key has an unexpected form; no client linked" >&2
  fi
  [ "$link" = yes ] || [ "$tunnel_smoke" = no ] ||
    echo "No smoke test: it runs once the clients link, and none will; run bin/drip-ai smoke once the model answers."
  trap tunnel_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  # Monitor mode gives the child its own process group, so cleanup stops its children too, and
  # Ctrl-C reaches only this shell, which unlinks the clients before stopping the child.
  case "$-" in *m*) had_monitor=yes ;; esac
  set -m
  "$@" </dev/null &
  tunnel_child_pid=$!
  [ "$had_monitor" = yes ] || set +m
  if [ "$link" = yes ]; then
    deadline=$(($(date +%s) + wait_seconds))
    echo "Context window: $context_window tokens (max_model_len); each linked client gets the same window."
    echo "Waiting for the model to answer on 127.0.0.1:$port; the clients link once it is ready."
    while kill -0 "$tunnel_child_pid" 2>/dev/null; do
      if tunnel_models_ready "$key" "$port" && "$ready_function"; then
        # shellcheck disable=SC2086 # clients is a space-separated list of names
        link_clients "$port" "$model" "$context_window" "$parameter" "$region" "$key" "$$" $clients || true
        if [ "$tunnel_smoke" = yes ]; then
          bash "$repo_root/shared/clients.sh" smoke ||
            echo "drip-ai: the smoke test failed; the tunnel stays open" >&2
        fi
        break
      fi
      if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "drip-ai: the model was not ready after $((wait_seconds / 60)) minutes; no client linked, the tunnel stays open" >&2
        break
      fi
      sleep "$poll"
    done
  fi
  key=""
  if wait "$tunnel_child_pid" 2>/dev/null; then status=0; else status=$?; fi
  tunnel_child_pid=""
  tunnel_cleanup
  return "$status"
}
