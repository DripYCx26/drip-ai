#!/usr/bin/env bash
# Operator commands for the SageMaker endpoints: status, logs, tunnel, stop, and deploy. Design:
# docs/DECISIONS.md, 2026-10-05 "Folders per backend, Terraform roots per stack", 2026-10-07
# "Create SageMaker endpoints through the AWS API", and 2026-10-09 "Plan, race, and learn in a
# Python control core". Bash 3.2 and 5.
#
# Every endpoint named drip-ai or drip-ai-<suffix> is a lane: start races drip-ai-<GPU class>-
# <region>-<n> endpoints through the API and keeps the first InService; drip-ai-<type>-<n>,
# drip-ai-<type>, and drip-ai are lanes earlier versions created. sagemaker/start.sh sources this
# file for the lane and owner functions above the CLI guard, so start and stop judge ownership one
# way. Sourced, it defines functions and returns. stop deletes endpoints only; the free models,
# endpoint configurations, and log groups a race created stay for the next start, which reuses
# them by content hash, until bin/drip-ai doctor cleans them up.
#
# Test hooks: DRIP_AI_TTY replaces /dev/tty for the typed endpoint name; DRIP_AI_STOP_POLL_SECONDS
# and DRIP_AI_STOP_WAIT_SECONDS shorten stop's wait on a Creating endpoint; common.sh lists the rest.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../shared/lib/common.sh
source "$repo_root/shared/lib/common.sh"

# Lane endpoint names: drip-ai-<instance type>-<n> from sagemaker/terraform's lanes output
# (var.project drip-ai), and drip-ai-<instance type> and drip-ai that earlier versions created.
lane_name_pattern='^drip-ai(-[a-z0-9-]+)?$'
# The S3 lease a race holds from before its plan until it exits, so one race runs per account.
race_lease=sagemaker-race
endpoint_arn_pattern='^arn:aws[a-z-]*:sagemaker:[a-z0-9-]+:[0-9]{12}:endpoint/[A-Za-z0-9-]+$'
tty_open=no
# SageMaker refuses DeleteEndpoint while an endpoint is in one of these states ("Cannot update
# in-progress endpoint", seen live 2026-10-06 while Creating); stop asks again every poll.
delete_wait_statuses=" Creating Updating SystemUpdating RollingBack "
stop_poll_seconds="${DRIP_AI_STOP_POLL_SECONDS:-15}"
# Also how long a race that has a winner keeps asking to delete a loser that is still Creating.
stop_wait_seconds="${DRIP_AI_STOP_WAIT_SECONDS:-7200}"
# The lane endpoints stop_lanes has not seen gone yet; its signal trap names them.
stop_pending=""
# yes when the user passed --yes: it answers start's race question; stop asks nothing.
assume_yes=no
# How long stop waits for a race on this machine to exit after TERM, in one-second sleeps.
stop_race_wait_seconds=300

# Prints VALUE in the AWS tag-value charset, for the StartedBy tag start puts on each endpoint:
# ASCII letters, digits, spaces, and _ . : / = + - @, at most 256 characters. Any other byte becomes
# '-'. sagemaker/start.sh relies on it holding no quote or backslash when it builds the tags' JSON.
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
# line), lanes_other_lines (NAME, tab, holder text per other lane), and lanes_listed (list_lanes'
# NAME, STATUS, ARN lines). Returns 1 when the endpoints cannot be listed.
read_lanes() {
  local listed name
  lanes_mine="" lanes_other="" lanes_other_text="" lanes_other_lines="" lanes_listed=""
  listed="$(list_lanes "$1")" || return 1
  lanes_listed="$listed"
  for name in $(cut -f1 <<<"$listed"); do
    read_endpoint_owner "$1" "$2" "$name"
    case "$endpoint_ownership" in
      absent) ;;
      mine) lanes_mine="$lanes_mine $name" ;;
      *)
        lanes_other="$lanes_other $name"
        lanes_other_lines="$lanes_other_lines$name"$'\t'"$(endpoint_holder_text)"$'\n'
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

# delete_lane_endpoint NAME [CONFIRMED_ARN CONFIRMED_CREATED]
# Deletes NAME when it is the caller's, or when its ARN and CreationTime match the confirmed ones,
# so a same-name replacement is refused; each retry checks that again. While SageMaker refuses
# because the endpoint is Creating (or another in-progress state), asks again every
# stop_poll_seconds for up to stop_wait_seconds. Waits until it is gone; returns 1 while it still
# exists. Needs region_name and my_started_by.
delete_lane_endpoint() {
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
    [ -n "$waited_on" ] || echo "Deleting endpoint $endpoint_name ($endpoint_status)."
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

# The stop path, also run by `start --take-over`: unlinks every client, then deletes every lane
# endpoint in region_name that is the caller's or confirmed in lanes_confirmed, waiting until each
# is gone. Someone else's endpoint that was not confirmed is left alone, named. Returns 1 while a
# lane endpoint it should delete still exists. Needs region_name and my_started_by.
stop_lanes() {
  local listed name arn created holder status=0
  # "not linked" is a success exit; only a real failure warns, and the deletes still run.
  unlink_all_clients
  if ! listed="$(list_lanes "$region_name")"; then
    echo "sagemaker-endpoint: could not list the endpoints to delete in $region_name; nothing was deleted there. Check with: bin/drip-ai sagemaker status" >&2
    return 1
  fi
  stop_pending=" $(cut -f1 <<<"$listed" | tr '\n' ' ')"
  trap 'stop_interrupted 130' INT
  trap 'stop_interrupted 143' TERM
  trap 'stop_interrupted 129' HUP
  for name in $(cut -f1 <<<"$listed"); do
    arn="" created=""
    read -r arn created < <(awk -v name="$name" '$1 == name { print $2, $3; exit }' <<<"${lanes_confirmed:-}") || true
    if [ -z "$arn" ] && [ "${stop_leave_others:-no}" = yes ]; then
      holder="$(awk -F'\t' -v name="$name" '$1 == name { print $2; exit }' <<<"${lanes_other_lines:-}")"
      if [ -n "$holder" ]; then
        echo "Leaving endpoint $name alone: it was $holder."
        stop_pending="${stop_pending/ $name / }"
        continue
      fi
    fi
    if delete_lane_endpoint "$name" "$arn" "$created"; then
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
Usage: sagemaker/endpoint.sh status            each drip-ai endpoint's state and who started it, in
                                               the home region and every configured region
       sagemaker/endpoint.sh logs [--endpoint NAME]
                                               last 30 minutes of the serving endpoint's logs
       sagemaker/endpoint.sh tunnel [--no-link | --no-codex-link | --smoke] [--region R]
                                    [--endpoint NAME] [--detach]
                                               serve 127.0.0.1:<port> (ports.base, default 8000)
                                               through sagemaker/proxy.py; links the clients in
                                               clients.link once InService, and unlinks them on exit;
                                               --smoke runs the smoke test once they link; --detach
                                               keeps it running in the background
       sagemaker/endpoint.sh stop [--force] [--yes]
                                               end a race running on this machine, unlink the clients,
                                               and delete every drip-ai endpoint of yours in each
                                               configured region (waiting out one still Creating);
                                               anyone else's is left alone unless --force and you type
                                               its name
       sagemaker/endpoint.sh deploy <folder> [--take-over] [--yes]
                                               upload models/<folder>/ and start it (asks before spending);
                                               refuses before uploading when an endpoint exists, unless --take-over
logs and tunnel follow --endpoint, else the one InService endpoint, or the only one; they refuse
while a race runs. --yes answers start's own question; it never types an endpoint name.
EOF_USAGE
}

endpoint_fail() { echo "sagemaker-endpoint: $*" >&2; exit 1; }

# Sets region_name and my_started_by in this shell, so later $(...) reads reuse the identity.
load_context() {
  region_name="$(drip_region)" || exit 1
  load_caller_identity || exit 1
  my_started_by="$(sanitize_tag_value "$drip_caller_arn_value")"
}

# Every drip-ai endpoint in each region lane_regions names, its state, and who started it.
status_command() {
  local listed name regions region
  load_context
  regions="$(lane_regions)" || endpoint_fail "could not read the configured regions"
  for region in $regions; do
    region_name="$region"
    listed="$(list_lanes "$region_name")" || endpoint_fail "could not list the SageMaker endpoints in $region_name"
    [ -n "$listed" ] || { echo "No drip-ai endpoint in $region_name (nothing bills for one)."; continue; }
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
  done
}

logs_command() {
  local lane=""
  case "$#:${1:-}" in
    0:) ;;
    2:--endpoint) lane="$2" ;;
    *) usage >&2; exit 1 ;;
  esac
  region_name="$(drip_region)" || exit 1
  if [ -n "$lane" ]; then
    [[ "$lane" =~ $lane_name_pattern ]] || endpoint_fail "--endpoint must name a drip-ai endpoint"
  else
    pick_lane "$region_name" || exit 1
  fi
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

# tunnel [--no-link | --no-codex-link | --smoke] [--region R] [--endpoint NAME] [--detach]
# Serves one endpoint on the port shared/drip_ai/serve.py picks, registered as this shell, and
# links the clients once it is InService. --region and --endpoint pick an endpoint outside the home
# region or beside others; the API key is that region's. --detach reruns it in the background.
tunnel_command() {
  local key_parameter=/drip-ai/api-key lane="" lane_model lane_window status=0 detach=no passthrough=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-link) export DRIP_AI_LINK_CLIENTS=none; passthrough+=("$1") ;;
      --no-codex-link) DRIP_AI_CODEX_LINK=0; passthrough+=("$1") ;;
      --smoke) tunnel_smoke=yes; passthrough+=("$1") ;;
      --region) [ $# -ge 2 ] || { usage >&2; exit 1; }; export DRIP_AI_REGION="$2"; passthrough+=("$1" "$2"); shift ;;
      --endpoint) [ $# -ge 2 ] || { usage >&2; exit 1; }; lane="$2"; passthrough+=("$1" "$2"); shift ;;
      --detach) detach=yes ;;
      *) usage >&2; exit 1 ;;
    esac
    shift
  done
  if [ "$detach" = yes ]; then
    exec python3 -I "$repo_root/shared/drip_ai/serve.py" detach --log "tunnel-$(date +%s)" -- \
      bash "$repo_root/sagemaker/endpoint.sh" tunnel ${passthrough[@]+"${passthrough[@]}"}
  fi
  load_context
  if [ -n "$lane" ]; then
    [[ "$lane" =~ $lane_name_pattern ]] || endpoint_fail "--endpoint must name a drip-ai endpoint"
  else
    pick_lane "$region_name" || exit 1
  fi
  read_endpoint_owner "$region_name" "$my_started_by" "$lane"
  case "$endpoint_ownership" in
    absent) endpoint_fail "no endpoint $endpoint_name in $region_name; start one with: bin/drip-ai sagemaker start" ;;
    unknown) echo "sagemaker-endpoint: warning: endpoint $endpoint_name is $(endpoint_holder_text)" >&2 ;;
    other) echo "sagemaker-endpoint: warning: endpoint $endpoint_name was $(endpoint_holder_text); it is theirs, and it keeps billing them" >&2 ;;
  esac
  read_lane_model "$region_name" "$endpoint_config" ||
    endpoint_fail "could not read the served model name and context window of endpoint $endpoint_name"
  serve_prepare sagemaker "$region_name" "$endpoint_name" || { status=$?; [ "$status" -eq 2 ] && exit 0; exit 1; }
  tunnel_with_link "$lane_model" "$lane_window" endpoint_in_service run_proxy || status=$?
  exit "$status"
}

# Ends a live race before stop lists anything. A race on this machine gets TERM, which makes it
# delete its own lanes and exit; stop waits up to stop_race_wait_seconds for that, then deletes
# whatever is left itself. A race on another machine is refused, naming it. A lease that expired,
# or whose process is gone, is no race.
end_live_race() {
  local waited=0
  lease_holder "$race_lease" || endpoint_fail "could not read the $race_lease lease; nothing was deleted"
  [ -n "$lease_holder_pid" ] && [ "$lease_holder_expired" = no ] || return 0
  [ "$lease_holder_host" = "$(uname -n)" ] ||
    endpoint_fail "a race started by $lease_holder_arn is running on $lease_holder_host (pid $lease_holder_pid, lease until $lease_holder_expires); stop it there or wait for it to end; nothing was deleted"
  pid_alive "$lease_holder_pid" || return 0
  kill -TERM "$lease_holder_pid" 2>/dev/null ||
    endpoint_fail "could not signal the race on this machine (pid $lease_holder_pid, started by $lease_holder_arn); nothing was deleted"
  echo "Ending the race (pid $lease_holder_pid); it deletes its own lanes before it exits."
  while pid_alive "$lease_holder_pid" && [ "$waited" -lt "$stop_race_wait_seconds" ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if pid_alive "$lease_holder_pid"; then
    echo "The race (pid $lease_holder_pid) is still deleting its lanes after $((stop_race_wait_seconds / 60)) minutes; deleting them here too."
  fi
}

# Prints the regions stop and status cover: the home region, then every configured race region.
lane_regions() {
  local configured
  configured="$(drip_config regions)" || return 1
  printf '%s\n' "$region_name" $configured | awk 'NF && !seen[$0]++' | tr '\n' ' '
}

# Ends a live race, then deletes every lane endpoint of the caller's in each region lane_regions
# names. Someone else's endpoint (or one whose owner cannot be read) is left alone and named,
# unless --force and its typed name. Exits 1 while an endpoint of yours still exists.
stop_command() {
  local force=no argument regions region status=0
  for argument in "$@"; do
    case "$argument" in
      --force) force=yes ;;
      --yes) assume_yes=yes ;;
      *) usage >&2; exit 1 ;;
    esac
  done
  load_context
  end_live_race
  regions="$(lane_regions)" || endpoint_fail "could not read the configured regions; nothing was deleted"
  for region in $regions; do
    region_name="$region"
    if ! read_lanes "$region_name" "$my_started_by"; then
      echo "sagemaker-endpoint: could not list the SageMaker endpoints in $region_name; nothing was deleted there" >&2
      status=1
      continue
    fi
    lanes_confirmed=""
    if [ -n "$lanes_other" ] && [ "$force" = yes ]; then
      confirm_other_lanes || endpoint_fail "the typed name did not match $endpoint_name; nothing more was deleted"
    fi
    if [ -z "$lanes_mine$lanes_other" ]; then
      echo "No drip-ai endpoint in $region_name."
      continue
    fi
    stop_leave_others=yes stop_lanes || status=1
  done
  close_tty
  [ "$status" -eq 0 ] || exit 1
  echo "No drip-ai endpoint of yours exists in ${regions% }; the weights in the S3 models bucket stay and bill for storage. bin/drip-ai doctor lists the free models, configurations, and log groups races left."
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
  logs) logs_command "$@" ;;
  tunnel) tunnel_command "$@" ;;
  stop) stop_command "$@" ;;
  deploy) deploy_command "$@" ;;
  -h | --help | help) usage ;;
  *) usage >&2; exit 1 ;;
esac
