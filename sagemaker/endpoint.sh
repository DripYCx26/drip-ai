#!/usr/bin/env bash
# Operator commands for the SageMaker endpoints: status, logs, tunnel, stop, and deploy. Design:
# docs/DECISIONS.md, 2026-10-05 "Folders per backend, Terraform roots per stack", and 2026-10-06
# "Race instance types". Bash 3.2 and 5.
#
# Every endpoint named drip-ai or drip-ai-<lane> is a lane: start races one per instance_types
# entry and keeps the first InService. sagemaker/start.sh sources this file for the lane and owner
# functions above the CLI guard, so start and stop judge ownership one way. Sourced, it defines
# functions and returns.
#
# Test hooks: DRIP_AI_TTY replaces /dev/tty for the typed endpoint name; DRIP_AI_STOP_POLL_SECONDS
# and DRIP_AI_STOP_WAIT_SECONDS shorten stop's wait on a Creating endpoint; common.sh lists the rest.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../shared/lib/common.sh
source "$repo_root/shared/lib/common.sh"

# Lane endpoint names: drip-ai-<instance type> from sagemaker/terraform (var.project drip-ai), and
# the single endpoint drip-ai that earlier versions created.
lane_name_pattern='^drip-ai(-[a-z0-9-]+)?$'
# Lets the destroy plan pass model_source's validations; destroy reads only state.
destroy_model_source=placeholder/for-destroy
endpoint_arn_pattern='^arn:aws[a-z-]*:sagemaker:[a-z0-9-]+:[0-9]{12}:endpoint/[A-Za-z0-9-]+$'
tty_open=no
# SageMaker refuses DeleteEndpoint while an endpoint is in one of these states ("Cannot update
# in-progress endpoint", seen live 2026-10-06 while Creating); stop asks again every poll.
delete_wait_statuses=" Creating Updating SystemUpdating RollingBack "
stop_poll_seconds="${DRIP_AI_STOP_POLL_SECONDS:-15}"
stop_wait_seconds="${DRIP_AI_STOP_WAIT_SECONDS:-7200}"
# The lane endpoints stop_lanes has not seen gone yet; its signal trap names them.
stop_pending=""
# yes when the user passed --yes: stop_lanes then answers Terraform's destroy question.
assume_yes=no

# Prints VALUE in the AWS tag-value charset that sagemaker/terraform checks for started_by: ASCII
# letters, digits, spaces, and _ . : / = + - @, at most 256 characters. Any other byte becomes '-'.
sanitize_tag_value() {
  local value
  value="$(printf '%s' "$1" | LC_ALL=C tr -c 'A-Za-z0-9 _.:/=+@-' '-')"
  printf '%s\n' "${value:0:256}"
}

# Prints the StartedBy tag of ARN, or nothing when the tag is absent; returns 1 when unreadable.
read_started_by() {
  local region="$1" arn="$2" value
  value="$(aws sagemaker list-tags --region "$region" --resource-arn "$arn" \
    --query "Tags[?Key=='StartedBy'].Value | [0]" --output text)" || return 1
  [ "$value" != None ] || value=""
  # Shown on the terminal, so control characters go.
  printf '%s\n' "${value//[[:cntrl:]]/}"
}

# list_lanes REGION
# Prints NAME, STATUS, and ARN, tab-separated, of every lane endpoint, from one ListEndpoints call
# (the CLI follows every page). Returns 1 when the call fails; aws prints why on stderr.
list_lanes() {
  local listed name status arn
  listed="$(aws sagemaker list-endpoints --region "$1" --name-contains drip-ai \
    --query 'Endpoints[].[EndpointName,EndpointStatus,EndpointArn]' --output text)" || return 1
  while IFS=$'\t' read -r name status arn; do
    [[ "$name" =~ $lane_name_pattern ]] || continue
    printf '%s\t%s\t%s\n' "$name" "$status" "$arn"
  done <<<"$listed"
}

# read_endpoint_owner REGION MY_STARTED_BY NAME
# Sets endpoint_name to NAME and endpoint_ownership to absent, mine, other, or unknown (a read
# failed; endpoint_read_error says why), plus endpoint_status, endpoint_arn, endpoint_created,
# endpoint_config, and endpoint_owner ("" when the StartedBy tag is missing, which counts as
# someone else's).
read_endpoint_owner() {
  local region="$1" mine="$2" err_file fields failure
  endpoint_name="$3" endpoint_ownership=unknown endpoint_status="" endpoint_arn="" endpoint_created=""
  endpoint_config="" endpoint_owner="" endpoint_read_error=""
  err_file="$(mktemp)" || { endpoint_read_error="could not create a temporary file"; return 0; }
  if ! fields="$(aws sagemaker describe-endpoint --region "$region" --endpoint-name "$endpoint_name" \
    --query '[EndpointArn,EndpointStatus,CreationTime,EndpointConfigName]' --output text 2>"$err_file")"; then
    if grep -q 'Could not find endpoint' "$err_file"; then
      endpoint_ownership=absent
    else
      endpoint_read_error="$(awk 'NF { line = $0 } END { print line }' "$err_file")"
      [ -n "$endpoint_read_error" ] || endpoint_read_error="aws sagemaker describe-endpoint failed"
    fi
    rm -f "$err_file"
    return 0
  fi
  IFS=$'\t' read -r endpoint_arn endpoint_status endpoint_created endpoint_config <<<"$fields" || true
  if ! [[ "$endpoint_arn" =~ $endpoint_arn_pattern ]] || ! [[ "$endpoint_status" =~ ^[A-Za-z]+$ ]]; then
    endpoint_read_error="aws sagemaker describe-endpoint returned an unexpected ARN or status"
    rm -f "$err_file"
    return 0
  fi
  if ! endpoint_owner="$(read_started_by "$region" "$endpoint_arn" 2>"$err_file")"; then
    failure="$(awk 'NF { line = $0 } END { print line }' "$err_file")"
    endpoint_read_error="could not read the endpoint's StartedBy tag (${failure:-aws sagemaker list-tags failed})"
    endpoint_owner=""
  elif [ -n "$endpoint_owner" ] && [ "$endpoint_owner" = "$mine" ]; then
    endpoint_ownership=mine
  else
    endpoint_ownership=other
  fi
  rm -f "$err_file"
}

# One line naming who holds the endpoint, for refusals and warnings.
endpoint_holder_text() {
  case "$endpoint_ownership" in
    other)
      if [ -n "$endpoint_owner" ]; then
        echo "started by $endpoint_owner at ${endpoint_created:-an unknown time} (status $endpoint_status)"
      else
        echo "started at ${endpoint_created:-an unknown time} with no StartedBy tag (status $endpoint_status)"
      fi
      ;;
    *) echo "of unknown owner ($endpoint_read_error)" ;;
  esac
}

# Opens the terminal (or DRIP_AI_TTY) on fd 5 once, so the take-over prompt and the model picker
# read one stream.
open_tty() {
  local tty_path="${DRIP_AI_TTY:-/dev/tty}"
  [ "$tty_open" = no ] || return 0
  { exec 5<"$tty_path"; } 2>/dev/null || return 1
  tty_open=yes
}

close_tty() {
  [ "$tty_open" = yes ] || return 0
  exec 5<&-
  tty_open=no
}

# Returns 0 only when the user types the endpoint name exactly on the terminal.
confirm_endpoint_name() {
  local typed=""
  open_tty || { echo "no terminal to type the endpoint name on" >&2; return 1; }
  printf 'Type the endpoint name (%s) to continue: ' "$endpoint_name"
  read -r -u 5 typed || true
  echo
  [ "$typed" = "$endpoint_name" ]
}

# read_lane_model REGION ENDPOINT_CONFIG
# Sets lane_model and lane_window to the served model name and max_model_len of the endpoint whose
# DescribeEndpoint names ENDPOINT_CONFIG, read from its model's environment, because a race leaves
# the Terraform outputs missing or stale. Returns 1 when a read fails or a value has another form.
read_lane_model() {
  local model values
  lane_model="" lane_window=""
  [ -n "$2" ] || return 1
  model="$(aws sagemaker describe-endpoint-config --region "$1" --endpoint-config-name "$2" \
    --query 'ProductionVariants[0].ModelName' --output text)" || return 1
  values="$(aws sagemaker describe-model --region "$1" --model-name "$model" \
    --query 'PrimaryContainer.Environment.[SM_VLLM_SERVED_MODEL_NAME,SM_VLLM_MAX_MODEL_LEN]' --output text)" ||
    return 1
  IFS=$'\t' read -r lane_model lane_window <<<"$values" || true
  [[ "$lane_model" =~ ^[A-Za-z0-9][A-Za-z0-9._/:-]*$ ]] && [[ "$lane_window" =~ ^[1-9][0-9]{0,8}$ ]] ||
    { lane_model="" lane_window=""; return 1; }
}

# read_lanes REGION MY_STARTED_BY
# Reads the owner of every lane endpoint. Sets lanes_mine and lanes_other (space-separated names;
# untagged and unreadable ones count as other), lanes_other_text (the first other lane's holder
# line), and lanes_listed (list_lanes' NAME, STATUS, ARN lines). Returns 1 when the endpoints cannot
# be listed.
read_lanes() {
  local listed name
  lanes_mine="" lanes_other="" lanes_other_text="" lanes_listed=""
  listed="$(list_lanes "$1")" || return 1
  lanes_listed="$listed"
  for name in $(cut -f1 <<<"$listed"); do
    read_endpoint_owner "$1" "$2" "$name"
    case "$endpoint_ownership" in
      absent) ;;
      mine) lanes_mine="$lanes_mine $name" ;;
      *)
        lanes_other="$lanes_other $name"
        [ -n "$lanes_other_text" ] || lanes_other_text="endpoint $name was $(endpoint_holder_text)"
        ;;
    esac
  done
}

# Has the user type each name in lanes_other. Sets lanes_confirmed to one "NAME ARN CREATED" line
# per typed name, so stop_lanes deletes the endpoint confirmed and not a same-name one made later.
# Returns 1 at the first name that does not match. Needs region_name and my_started_by.
confirm_other_lanes() {
  local name
  lanes_confirmed=""
  for name in $lanes_other; do
    read_endpoint_owner "$region_name" "$my_started_by" "$name"
    [ "$endpoint_ownership" != absent ] || continue
    echo "Endpoint $name was $(endpoint_holder_text)."
    confirm_endpoint_name || return 1
    lanes_confirmed="$lanes_confirmed$name $endpoint_arn $endpoint_created"$'\n'
  done
}

# pick_lane REGION
# Sets lane to the endpoint that watch, logs, and tunnel follow: the one InService lane, else the
# only lane. Returns 1, saying why, when none exists, the list fails, or several exist without
# exactly one InService (a race in progress or interrupted).
pick_lane() {
  local listed
  lane=""
  listed="$(list_lanes "$1")" ||
    { echo "sagemaker-endpoint: could not list the SageMaker endpoints in $1" >&2; return 1; }
  if [ -z "$listed" ]; then
    echo "sagemaker-endpoint: no drip-ai endpoint in $1; start one with: bin/drip-ai sagemaker start" >&2
    return 1
  fi
  lane="$(awk -F'\t' '{ count++; only = $1 } $2 == "InService" { up++; serving = $1 }
    END { if (up == 1) print serving; else if (count == 1) print only }' <<<"$listed")"
  [ -z "$lane" ] || return 0
  echo "sagemaker-endpoint: race in progress or interrupted ($(awk -F'\t' '{ printf "%s%s %s", sep, $1, $2; sep = ", " }' <<<"$listed")); follow it with bin/drip-ai sagemaker status, or delete every lane with bin/drip-ai sagemaker stop" >&2
  return 1
}

# delete_orphan_endpoint NAME [CONFIRMED_ARN CONFIRMED_CREATED]
# Runs after the destroy. The provider records an endpoint in state only once it is InService, so a
# timed-out, Failed, or raced one outlives the destroy. Deletes NAME when it is the caller's, or when
# its ARN and CreationTime match the confirmed ones, so a same-name replacement is refused; each
# retry checks that again. While SageMaker refuses because the endpoint is Creating (or another
# in-progress state), asks again every stop_poll_seconds for up to stop_wait_seconds. Waits until it
# is gone; returns 1 while it still exists. Needs region_name and my_started_by.
delete_orphan_endpoint() {
  local confirmed_arn="${2:-}" confirmed_created="${3:-}" err_file reason waited_on="" started=$SECONDS
  [[ "$stop_poll_seconds" =~ ^[1-9][0-9]{0,4}$ ]] && [[ "$stop_wait_seconds" =~ ^[0-9]{1,6}$ ]] || {
    echo "sagemaker-endpoint: DRIP_AI_STOP_POLL_SECONDS and DRIP_AI_STOP_WAIT_SECONDS must be whole seconds" >&2
    return 1
  }
  err_file="$(mktemp)" || return 1
  while :; do
    read_endpoint_owner "$region_name" "$my_started_by" "$1"
    case "$endpoint_ownership" in
      absent) rm -f "$err_file"; return 0 ;;
      unknown)
        echo "sagemaker-endpoint: could not confirm endpoint $endpoint_name is gone ($endpoint_read_error); check: bin/drip-ai sagemaker status" >&2
        rm -f "$err_file"
        return 1
        ;;
      other)
        if [ -z "$confirmed_arn" ] || [ "$endpoint_arn" != "$confirmed_arn" ] ||
          [ "$endpoint_created" != "$confirmed_created" ]; then
          echo "sagemaker-endpoint: endpoint $endpoint_name is still there and was $(endpoint_holder_text); not deleting it. It bills until deleted" >&2
          rm -f "$err_file"
          return 1
        fi
        ;;
    esac
    [ -n "$waited_on" ] || echo "Endpoint $endpoint_name is $endpoint_status and not in Terraform state; deleting it."
    aws sagemaker delete-endpoint --region "$region_name" --endpoint-name "$endpoint_name" 2>"$err_file" && break
    reason="$(awk 'NF { line = $0 } END { print line }' "$err_file")"
    case "$delete_wait_statuses" in
      *" $endpoint_status "*) ;;
      *)
        echo "sagemaker-endpoint: SageMaker refused to delete endpoint $endpoint_name ($reason); it bills until deleted. Check: bin/drip-ai sagemaker status" >&2
        rm -f "$err_file"
        return 1
        ;;
    esac
    if [ $((SECONDS - started)) -ge "$stop_wait_seconds" ]; then
      echo "sagemaker-endpoint: endpoint $endpoint_name is still $endpoint_status after $((stop_wait_seconds / 60)) minutes and SageMaker refuses to delete it ($reason); it bills until deleted. Rerun bin/drip-ai sagemaker stop" >&2
      rm -f "$err_file"
      return 1
    fi
    [ "$waited_on" = "$endpoint_status" ] ||
      echo "Endpoint $endpoint_name is $endpoint_status, and SageMaker deletes it only after that ends; asking again every ${stop_poll_seconds}s for up to $((stop_wait_seconds / 60)) minutes. Ctrl-C stops waiting."
    waited_on="$endpoint_status"
    sleep "$stop_poll_seconds"
  done
  rm -f "$err_file"
  echo "Waiting for SageMaker to delete it..."
  aws sagemaker wait endpoint-deleted --region "$region_name" --endpoint-name "$endpoint_name" || true
  read_endpoint_owner "$region_name" "$my_started_by" "$endpoint_name"
  if [ "$endpoint_ownership" != absent ]; then
    echo "sagemaker-endpoint: endpoint $endpoint_name still exists (${endpoint_status:-unreadable}); it bills until deleted. Check: bin/drip-ai sagemaker status" >&2
    return 1
  fi
  echo "Endpoint $endpoint_name deleted."
}

# The trap stop_lanes sets while it deletes: names what may still bill and how to finish.
stop_interrupted() {
  trap - INT TERM HUP
  echo >&2
  echo "sagemaker-endpoint: stopped waiting; these endpoints may still exist and bill until deleted:${stop_pending% }. Finish with: bin/drip-ai sagemaker stop${lanes_confirmed:+ --force}" >&2
  exit "$1"
}

# The stop path, also run by `start --take-over`: unlinks every client, destroys the sagemaker root
# (Terraform asks first unless assume_yes), then deletes every lane endpoint left outside state that
# is the caller's or confirmed in lanes_confirmed. Returns 1 when the destroy fails or is declined,
# or while a lane endpoint still exists. Needs region_name and my_started_by.
stop_lanes() {
  local listed name arn created status=0
  # "not linked" is a success exit; only a real failure warns, and the destroy still runs.
  unlink_all_clients
  if ! tf_approved "$assume_yes" sagemaker destroy -var "model_source=$destroy_model_source" \
    -var "started_by=$my_started_by"; then
    echo "sagemaker-endpoint: destroy failed or was declined; check with: bin/drip-ai sagemaker status" >&2
    return 1
  fi
  if ! listed="$(list_lanes "$region_name")"; then
    echo "sagemaker-endpoint: could not list the endpoints left after the destroy; check with: bin/drip-ai sagemaker status" >&2
    return 1
  fi
  stop_pending=" $(cut -f1 <<<"$listed" | tr '\n' ' ')"
  trap 'stop_interrupted 130' INT
  trap 'stop_interrupted 143' TERM
  trap 'stop_interrupted 129' HUP
  for name in $(cut -f1 <<<"$listed"); do
    arn="" created=""
    read -r arn created < <(awk -v name="$name" '$1 == name { print $2, $3; exit }' <<<"${lanes_confirmed:-}") || true
    if delete_orphan_endpoint "$name" "$arn" "$created"; then
      stop_pending="${stop_pending/ $name / }"
    else
      status=1
    fi
  done
  trap - INT TERM HUP
  return "$status"
}

# Library part ends here when sourced.
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

usage() {
  cat <<'EOF_USAGE'
Usage: sagemaker/endpoint.sh status            each drip-ai endpoint's state and who started it
       sagemaker/endpoint.sh logs              last 30 minutes of the serving endpoint's logs
       sagemaker/endpoint.sh tunnel [--no-link | --smoke]
                                               serve 127.0.0.1:$DRIP_AI_PORT (default 8000) through
                                               sagemaker/proxy.py; links Codex, Claude Code, and
                                               opencode once InService, and unlinks them on exit;
                                               --smoke runs the smoke test once they link
       sagemaker/endpoint.sh stop [--force] [--yes]
                                               unlink the clients, destroy the sagemaker root (asks first),
                                               then delete every drip-ai endpoint left outside Terraform
                                               state, waiting out one that is still Creating;
                                               --force plus each typed name for someone else's
       sagemaker/endpoint.sh deploy <folder> [--take-over] [--yes]
                                               upload models/<folder>/ and start it (asks before spending);
                                               refuses before uploading when an endpoint exists, unless --take-over
logs and tunnel follow the one InService endpoint, or the only one; they refuse while a race runs.
--yes answers Terraform's and start's own question; it never types an endpoint name.
EOF_USAGE
}

endpoint_fail() { echo "sagemaker-endpoint: $*" >&2; exit 1; }

# Sets region_name and my_started_by in this shell, so later $(...) reads reuse the identity.
load_context() {
  region_name="$(drip_region)" || exit 1
  load_caller_identity || exit 1
  my_started_by="$(sanitize_tag_value "$drip_caller_arn_value")"
}

status_command() {
  local listed name
  load_context
  listed="$(list_lanes "$region_name")" || endpoint_fail "could not list the SageMaker endpoints in $region_name"
  [ -n "$listed" ] || { echo "No drip-ai endpoint in $region_name (nothing bills for one)."; return 0; }
  for name in $(cut -f1 <<<"$listed"); do
    read_endpoint_owner "$region_name" "$my_started_by" "$name"
    case "$endpoint_ownership" in
      absent) continue ;;
      unknown) endpoint_fail "could not read endpoint $name: $endpoint_read_error" ;;
    esac
    echo "Endpoint $name in $region_name: $endpoint_status, created ${endpoint_created:-unknown}"
    if [ "$endpoint_ownership" = mine ]; then
      echo "  started by $endpoint_owner (you)"
    elif [ -n "$endpoint_owner" ]; then
      echo "  started by $endpoint_owner (someone else)"
    else
      echo "  started by unknown (no StartedBy tag)"
    fi
    [ "$endpoint_status" != Failed ] || aws sagemaker describe-endpoint --region "$region_name" \
      --endpoint-name "$name" --query FailureReason --output text
  done
}

logs_command() {
  local lane
  region_name="$(drip_region)" || exit 1
  pick_lane "$region_name" || exit 1
  aws logs tail "/aws/sagemaker/Endpoints/$lane" --region "$region_name" --since 30m
}

# tunnel_with_link's readiness check: InService only; Failed, Deleting, missing, or an unreadable
# endpoint never links a client.
endpoint_in_service() {
  local status
  status="$(aws sagemaker describe-endpoint --region "$region_name" --endpoint-name "$endpoint_name" \
    --query EndpointStatus --output text 2>/dev/null)" || return 1
  [ "$status" = InService ]
}

# The tunnel's background child. printf is a builtin, so the key reaches the proxy on stdin and
# never any argv or the environment.
run_proxy() {
  local key
  key="$(aws ssm get-parameter --region "$region_name" --name "$key_parameter" --with-decryption \
    --query Parameter.Value --output text)" || {
    echo "sagemaker-endpoint: could not read the API key from $key_parameter" >&2
    return 1
  }
  printf '%s\n' "$key" | python3 -I "$repo_root/sagemaker/proxy.py" --region "$region_name" \
    --endpoint "$endpoint_name" --model "$lane_model" --port "${DRIP_AI_PORT:-8000}"
}

tunnel_command() {
  local key_parameter lane lane_model lane_window status=0
  case "$#:${1:-}" in
    0:) ;;
    1:--no-link) DRIP_AI_LINK_CLIENTS=none ;;
    1:--no-codex-link) DRIP_AI_CODEX_LINK=0 ;;
    1:--smoke) tunnel_smoke=yes ;;
    *) usage >&2; exit 1 ;;
  esac
  load_context
  pick_lane "$region_name" || exit 1
  read_endpoint_owner "$region_name" "$my_started_by" "$lane"
  case "$endpoint_ownership" in
    absent) endpoint_fail "no endpoint $endpoint_name in $region_name; start one with: bin/drip-ai sagemaker start" ;;
    unknown) echo "sagemaker-endpoint: warning: endpoint $endpoint_name is $(endpoint_holder_text)" >&2 ;;
    other) echo "sagemaker-endpoint: warning: endpoint $endpoint_name was $(endpoint_holder_text); it is theirs, and it keeps billing them" >&2 ;;
  esac
  read_lane_model "$region_name" "$endpoint_config" ||
    endpoint_fail "could not read the served model name and context window of endpoint $endpoint_name"
  key_parameter="$(tf_output shared api_key_parameter)" ||
    endpoint_fail "could not read api_key_parameter from the shared Terraform root"
  [[ "$key_parameter" =~ ^/[A-Za-z0-9_./-]+$ ]] ||
    endpoint_fail "the api_key_parameter output is not a parameter name"
  tunnel_with_link "$lane_model" "$lane_window" endpoint_in_service run_proxy || status=$?
  exit "$status"
}

# Destroys only the sagemaker root, then deletes every lane endpoint the destroy left behind.
# Someone else's endpoint (or one whose owner cannot be read) needs --force and its typed name;
# Terraform then shows the plan and asks for yes.
stop_command() {
  local force=no argument
  for argument in "$@"; do
    case "$argument" in
      --force) force=yes ;;
      --yes) assume_yes=yes ;;
      *) usage >&2; exit 1 ;;
    esac
  done
  load_context
  read_lanes "$region_name" "$my_started_by" ||
    endpoint_fail "could not list the SageMaker endpoints in $region_name; nothing was destroyed"
  if [ -n "$lanes_other" ]; then
    [ "$force" = yes ] ||
      endpoint_fail "$lanes_other_text; it is not yours to stop. To destroy it anyway, rerun with --force and type the endpoint name"
    confirm_other_lanes || endpoint_fail "the typed name did not match $endpoint_name; nothing was destroyed"
    close_tty
  fi
  [ -n "$lanes_mine$lanes_other" ] ||
    echo "No drip-ai endpoint in $region_name; destroying what is left of the sagemaker root."
  stop_lanes || exit 1
  echo "The sagemaker root is destroyed and no drip-ai endpoint exists; the weights in the S3 models bucket stay and bill for storage."
}

# Runs before the upload, which can push hundreds of GB. No endpoint: returns. Any endpoint, yours
# included, refuses unless TAKE_OVER is yes; sagemaker/start.sh still decides the take-over.
gate_deploy_owner() {
  [ "$1" = no ] || return 0
  read_lanes "$region_name" "$my_started_by" ||
    endpoint_fail "could not list the SageMaker endpoints in $region_name; nothing was uploaded"
  [ -z "$lanes_other" ] ||
    endpoint_fail "$lanes_other_text; nothing was uploaded. To replace it, rerun with --take-over and type the endpoint name"
  [ -z "$lanes_mine" ] ||
    endpoint_fail "endpoint$lanes_mine is already running (yours); nothing was uploaded. Stop it first or start a different model; --take-over uploads anyway"
}

deploy_command() {
  local folder="" take_over=no argument start_args=()
  for argument in "$@"; do
    case "$argument" in
      --take-over) take_over=yes ;;
      --yes) start_args+=(--yes) ;;
      *) [ -z "$folder" ] || { usage >&2; exit 1; }; folder="$argument" ;;
    esac
  done
  [ -n "$folder" ] || { usage >&2; exit 1; }
  # One folder directly under models/, as shared/models.sh upload takes it.
  [[ "$folder" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
    endpoint_fail "folder must be one name under models/: letters, digits, '.', '_', '-', starting with a letter or digit"
  [ -f "$repo_root/shared/models.sh" ] || endpoint_fail "shared/models.sh not found; nothing was uploaded"
  load_context
  gate_deploy_owner "$take_over"
  bash "$repo_root/shared/models.sh" upload "$folder" ||
    endpoint_fail "the upload of models/$folder/ failed or was refused; nothing was started"
  [ "$take_over" = no ] || start_args+=(--take-over)
  exec bash "$repo_root/sagemaker/start.sh" "$folder" --no-manifest ${start_args[@]+"${start_args[@]}"}
}

command="${1:-}"
shift || true
case "$command" in
  status) [ $# -eq 0 ] || { usage >&2; exit 1; }; status_command ;;
  logs) [ $# -eq 0 ] || { usage >&2; exit 1; }; logs_command ;;
  tunnel) tunnel_command "$@" ;;
  stop) stop_command "$@" ;;
  deploy) deploy_command "$@" ;;
  -h | --help | help) usage ;;
  *) usage >&2; exit 1 ;;
esac
