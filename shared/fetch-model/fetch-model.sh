#!/usr/bin/env bash
# Copies a Hugging Face model into the S3 models bucket with a tagged ECS Fargate task and shows the
# copy landing in S3. Design: docs/DECISIONS.md, 2026-10-05 "Fetch Hugging Face models into S3".
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=../lib/common.sh
source "$repo_root/shared/lib/common.sh"

# Prompts read from this path. Tests point it at a file; the token never travels through argv. With
# --yes the script never opens it.
tty_path="${DRIP_AI_TTY:-/dev/tty}"
watch_seconds="${DRIP_AI_WATCH_SECONDS:-10}"
copier="$repo_root/shared/fetch-model/copier.py"
# The copier's own default deadline; the script stops the task this long after it as a backstop.
job_deadline_seconds=21600
stop_grace_seconds=1200
repo_pattern='^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$'
revision_pattern='^[A-Za-z0-9._/-]{1,128}$'
token_pattern='^hf_[A-Za-z0-9]{30,64}$'
bucket_pattern='^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
cluster_pattern='^[A-Za-z0-9_-]{1,255}$'
task_definition_pattern='^arn:aws[a-z-]*:ecs:[a-z0-9-]+:[0-9]{12}:task-definition/[A-Za-z0-9_-]+:[0-9]+$'
task_arn_pattern='^arn:aws[a-z-]*:ecs:[a-z0-9-]+:[0-9]{12}:task/[A-Za-z0-9_-]+/[A-Za-z0-9]+$'
log_group_pattern='^[A-Za-z0-9_./#-]+$'
parameter_pattern='^/[A-Za-z0-9_./-]+$'
# One fetch at a time holds this lease from before the token prompt until the token parameter is
# deleted, because every fetch shares the one parameter.
fetch_lease=fetch-model
fetch_lease_seconds=1800

usage() {
  cat <<'EOF_USAGE'
Usage: shared/fetch-model/fetch-model.sh [ORG/NAME] [--revision REV] [--name NAME] [--all-files] [--allow-concurrent] [--yes]
       shared/fetch-model/fetch-model.sh --watch NAME

Copies a Hugging Face model to s3://<models bucket>/models/NAME/ with an ECS Fargate task (about
$0.40/h), asking for the token with hidden input and showing the copy as it lands in S3.
NAME defaults to ORG/NAME. Ctrl-C detaches; --watch NAME reattaches. Rerunning resumes a copy.
A fetch another AWS identity started blocks a new one unless --allow-concurrent.
--yes answers the apply and the launch question after their summaries print, skips the token for
a public repo, refuses a private or gated one (its token is typed only at the hidden prompt), and
needs no terminal.
EOF_USAGE
}

fail() { echo "fetch-model: $1" >&2; exit 1; }
usage_fail() { echo "fetch-model: $1" >&2; usage >&2; exit 2; }

# State and helpers for the JSON the copier and AWS return. Bash only orchestrates CLI calls; this
# program parses, keeps per-tick state in $state_file, and renders. Text that came from Hugging Face
# or the job log is printed only after control characters are replaced, because a repo owner
# chooses the file names.
read -r -d '' fetch_state_program <<'EOF_PY' || true
import datetime, json, re, sys, time

STATUS_OBJECT = "drip-ai-fetch.json"
SHA = re.compile(r"[0-9a-f]{40}")
TASK_ARN = re.compile(r"arn:aws[a-z-]*:ecs:[a-z0-9-]+:[0-9]{12}:task/[A-Za-z0-9_-]+/[A-Za-z0-9]+")
STATUS_WORD = re.compile(r"[A-Z_]{1,32}")
RATE_TICKS = 5
STARTING = ("PROVISIONING", "PENDING", "ACTIVATING")
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?")


def fail(message):
    sys.stderr.write("fetch-model: " + message + "\n")
    sys.exit(1)


def printable(text, limit=160):
    return "".join(c if c.isprintable() else "?" for c in str(text))[:limit]


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(path, state):
    with open(path, "w") as f:
        json.dump(state, f)


def is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def file_sizes(files):
    sizes = {}
    for entry in files:
        path, size = entry["path"], entry["size"]
        if not isinstance(path, str) or not path or not is_count(size):
            raise ValueError("bad file entry")
        sizes[path] = size
    return sizes


def checked_plan(doc):
    if not SHA.fullmatch(str(doc.get("revision_sha", ""))) or not is_count(doc.get("total_bytes")):
        raise ValueError("plan has no commit sha or total")
    return {
        "repo": printable(doc.get("repo", ""), 96),
        "revision": doc["revision_sha"],
        "files": file_sizes(doc["files"]),
        "total_bytes": doc["total_bytes"],
        "gated": doc.get("gated") is True,
    }


def checked_status(doc):
    files = file_sizes(doc["files"])
    return {
        "repo": printable(doc.get("repo", ""), 96),
        "revision": str(doc.get("revision", "")),
        "status": str(doc.get("status", "")),
        "files": files,
        "total_bytes": doc["total_bytes"] if is_count(doc.get("total_bytes")) else sum(files.values()),
    }


def totals(state):
    """The status object once present, else the local preflight, else the job's PLAN line. The PLAN
    line is display-only: Docker splits log lines over 16 KB, so it may never parse."""
    for source in (state.get("status"), state.get("preflight"), state.get("log", {}).get("plan")):
        if source:
            return source
    return None


def parse_time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def gb(count):
    return "%.2f" % (count / 1e9)


def duration(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return "%dh %02dm" % (seconds // 3600, seconds % 3600 // 60)
    return "%dm %02ds" % (seconds // 60, seconds % 60)


def mode_plan(state, args):
    plan = checked_plan(json.load(sys.stdin))
    state["preflight"] = plan
    print("|".join([plan["revision"], str(len(plan["files"])), str(plan["total_bytes"]),
                    "yes" if plan["gated"] else "no"]))


def mode_runtask(state, args):
    doc = json.load(sys.stdin)
    failures, tasks = doc.get("failures") or [], doc.get("tasks") or []
    if failures or not tasks:
        for failure in failures:
            sys.stderr.write("  %s %s\n" % (printable(failure.get("reason", "")), printable(failure.get("detail", ""))))
        fail("ECS did not start the task")
    arn = str(tasks[0].get("taskArn", ""))
    if not TASK_ARN.fullmatch(arn):
        fail("run-task returned no task ARN")
    print(arn)


def mode_task(state, args):
    tasks = json.load(sys.stdin).get("tasks") or []
    task = tasks[0] if tasks else {}
    status = str(task.get("lastStatus", "UNKNOWN"))
    if not STATUS_WORD.fullmatch(status):
        status = "UNKNOWN"
    exit_code = next((c.get("exitCode") for c in task.get("containers", []) if c.get("name") == "fetch"), None)
    if not is_count(exit_code):
        exit_code = None
    created = parse_time(task.get("createdAt"))
    elapsed = int(time.time() - created) if created else None
    state["task"] = {"status": status, "exit": exit_code, "elapsed": elapsed,
                     "reason": printable(task.get("stoppedReason", ""))}
    print("%s|%s|%s" % (status, "" if exit_code is None else exit_code, "" if elapsed is None else elapsed))


def mode_logs(state, args):
    sent = args[0]
    doc = json.load(sys.stdin)
    log = state.setdefault("log", {"progress": {}, "finished": {}, "lines": []})
    events = doc.get("events") or []
    for event in events:
        message = str(event.get("message", "")).rstrip("\n")
        kind, _, body = message.partition(" ")
        try:
            if kind == "PLAN":
                log["plan"] = checked_plan(json.loads(body))
            elif kind == "PROGRESS":
                progress = json.loads(body)
                if is_count(progress["done"]) and str(progress["path"]) not in log["finished"]:
                    log["progress"][str(progress["path"])] = progress["done"]
            elif kind in ("FILE_DONE", "FILE_SKIP"):
                path = str(json.loads(body)["path"])
                log["finished"][path] = True
                log["progress"].pop(path, None)
        except (ValueError, KeyError, TypeError, AttributeError):
            pass  # A malformed job line still shows below; it only cannot move the counters.
        log["lines"] = (log["lines"] + [printable(message)])[-5:]
    token = str(doc.get("nextForwardToken") or sent)
    if "|" in token or not token.isprintable():
        fail("unexpected log token")
    print("%s|%s" % (token, "more" if events and token != sent else "end"))


def mode_s3(state, args):
    prefix, objects = args[0], {}
    for line in sys.stdin:
        key, _, size = line.rstrip("\n").rpartition("\t")
        if key.startswith(prefix) and size.isdigit() and key[len(prefix):] != STATUS_OBJECT:
            objects[key[len(prefix):]] = int(size)
    state["s3"] = objects


def mode_status(state, args):
    try:
        state["status"] = checked_status(json.load(sys.stdin))
    except (ValueError, KeyError, TypeError, AttributeError):
        state["status_unreadable"] = True


def progress_lines(state):
    plan, s3 = totals(state), state.get("s3")
    log = state.get("log", {})
    if s3 is None:
        s3_line, landed_done = "S3 listing not read yet", None
    elif plan:
        done = [path for path, size in plan["files"].items() if s3.get(path) == size]
        landed_done = sum(plan["files"][path] for path in done)
        percent = int(100 * landed_done / plan["total_bytes"]) if plan["total_bytes"] else 100
        s3_line = "S3 done files %d/%d  %s/%s GB (%d%%)" % (
            len(done), len(plan["files"]), gb(landed_done), gb(plan["total_bytes"]), percent)
    else:
        landed_done = sum(s3.values())
        s3_line = "S3 done files %d/?  %s/? GB" % (len(s3), gb(landed_done))
    in_flight = {path: done for path, done in log.get("progress", {}).items()
                 if s3 is None or path not in s3}
    flight_line = "in flight %d files, %s GB" % (len(in_flight), gb(sum(in_flight.values())))
    rate_line = "rate --, ETA --"
    if landed_done is not None:
        samples = (state.get("samples", []) + [[time.time(), landed_done + sum(in_flight.values())]])
        state["samples"] = samples[-(RATE_TICKS + 1):]
        first, last = state["samples"][0], state["samples"][-1]
        if last[0] > first[0]:
            rate = max(0.0, (last[1] - first[1]) / (last[0] - first[0]))
            eta = "--"
            if plan and rate > 0:
                eta = duration(max(0, plan["total_bytes"] - last[1]) / rate)
            rate_line = "rate %.1f MB/s, ETA %s" % (rate / 1e6, eta)
    return s3_line, flight_line, rate_line


def mode_render(state, args):
    style, note = args[0], args[1]
    plan, task = totals(state), state.get("task", {})
    title = "%s @ %s" % (plan["repo"], plan["revision"][:7]) if plan else "repo @ unknown revision"
    elapsed = "elapsed " + duration(task["elapsed"]) if task.get("elapsed") is not None else "elapsed unknown"
    task_line = "task %s, %s" % (task.get("status", "UNKNOWN"), elapsed)
    s3_line, flight_line, rate_line = progress_lines(state)
    if style == "line":
        parts = [time.strftime("%H:%M:%S"), title, task_line, s3_line, flight_line, rate_line]
        print(" | ".join(parts + ([note] if note else [])))
        return
    print("\n".join([title, task_line, s3_line, flight_line, rate_line] + ([note] if note else [])))
    lines = state.get("log", {}).get("lines", [])
    if lines:
        print("last log lines:")
        print("\n".join("  " + line for line in lines))
    print("Ctrl-C detaches; the task keeps going without this script.")


def mode_verify(state, args):
    status, s3, preflight = state.get("status"), state.get("s3"), state.get("preflight")
    if not status or status["status"] != "complete":
        fail("the status object does not say complete")
    # The copier rewrites the status object before copying, so without a local preflight (a gated
    # repo or --watch) its own commit sha is the copied revision.
    if not SHA.fullmatch(status["revision"]) or (preflight and status["revision"] != preflight["revision"]):
        fail("the status object's revision does not match the copied revision")
    if s3 is None:
        fail("the S3 listing could not be read")
    wrong = [path for path, size in status["files"].items() if s3.get(path) != size]
    if wrong or len(s3) != len(status["files"]) or sum(s3.values()) != status["total_bytes"]:
        fail("S3 holds %d objects, %d bytes; the status object lists %d files, %d bytes"
             % (len(s3), sum(s3.values()), len(status["files"]), status["total_bytes"]))
    print("%d files, %s GB" % (len(status["files"]), gb(status["total_bytes"])))


def mode_starting(state, args):
    """Counts described tasks that may still be starting. ECS resolves a task's secrets before its
    container starts, so a task whose status is unreadable counts as starting."""
    tasks = json.load(sys.stdin).get("tasks") or []
    starting = [task for task in tasks
                if str(task.get("lastStatus", "")) in STARTING
                or not STATUS_WORD.fullmatch(str(task.get("lastStatus", "")))]
    print(len(starting))


def copied_name(task):
    """The --name a fetch task copies to, from its command override, or None when unreadable."""
    for override in (task.get("overrides") or {}).get("containerOverrides") or []:
        command = [str(part) for part in override.get("command") or []]
        if "--name" in command[:-1]:
            name = command[command.index("--name") + 1]
            if MODEL_NAME.fullmatch(name) and ".." not in name:
                return name
    return None


def mode_others(state, args):
    """Prints `other|TEXT` for each described task that another identity started, and
    `starting|TEXT` for each task copying another model name (startedBy differs) that may still be
    starting, whoever started it. A task without a StartedBy tag predates the tag, so it counts as
    another identity's; one whose status is unreadable counts as starting."""
    own_tag, own_started_by = args[0], args[1]
    for task in json.load(sys.stdin).get("tasks") or []:
        arn = printable(task.get("taskArn", ""), 200)
        tags = {str(tag["key"]): str(tag["value"]) for tag in task.get("tags") or []}
        started_by = tags.get("StartedBy")
        if started_by != own_tag:
            print("other|task %s started by %s" % (arn, printable(started_by, 256) if started_by
                                                   else "an identity it does not record"))
        status = str(task.get("lastStatus", ""))
        if str(task.get("startedBy", "")) != own_started_by and (
                status in STARTING or not STATUS_WORD.fullmatch(status)):
            name = copied_name(task)
            print("starting|task %s%s is still starting (%s)|%s" % (
                arn, " for models/%s/" % name if name else "",
                status if STATUS_WORD.fullmatch(status) else "UNKNOWN", name or ""))


def mode_copy_state(state, args):
    if state.get("status"):
        print(printable(state["status"]["status"], 32) or "unknown")
    else:
        print("unreadable" if state.get("status_unreadable") else "missing")


def mode_field(state, args):
    plan, task = totals(state), state.get("task", {})
    values = {"repo": plan["repo"] if plan else "", "revision": plan["revision"] if plan else "",
              "reason": task.get("reason", "")}
    print(values[args[0]])


MODES = {"plan": mode_plan, "runtask": mode_runtask, "task": mode_task, "logs": mode_logs,
         "s3": mode_s3, "status": mode_status, "render": mode_render, "verify": mode_verify,
         "starting": mode_starting, "others": mode_others, "copy_state": mode_copy_state,
         "field": mode_field}

state_path = sys.argv[2]
state = load(state_path)
try:
    MODES[sys.argv[1]](state, sys.argv[3:])
except (ValueError, KeyError, TypeError, AttributeError) as error:
    fail("unreadable %s data (%s)" % (sys.argv[1], type(error).__name__))
save(state_path, state)
EOF_PY

state_dir=""
state_file=""
fetch_state() { python3 -I -c "$fetch_state_program" "$1" "$state_file" "${@:2}"; }

tty_open=false
open_tty() {
  [ "$tty_open" = false ] || return 0
  if ! { exec 3<"$tty_path"; } 2>/dev/null; then
    fail "needs a terminal to ask for input; run it from an interactive shell"
  fi
  tty_open=true
}

repo=""
name=""
revision=""
all_files=false
allow_concurrent=false
assume_yes=no
watch_name=""
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --revision | --name | --watch)
        [ $# -ge 2 ] || usage_fail "$1 needs a value"
        case "$1" in
          --revision) revision="$2" ;;
          --name) name="$2" ;;
          --watch) watch_name="$2" ;;
        esac
        shift 2
        ;;
      --all-files) all_files=true; shift ;;
      --allow-concurrent) allow_concurrent=true; shift ;;
      --yes) assume_yes=yes; shift ;;
      -h | --help) usage; exit 0 ;;
      -*) usage_fail "unknown option $1" ;;
      *)
        [ -z "$repo" ] || usage_fail "one repo id only"
        repo="$1"
        shift
        ;;
    esac
  done
  if [ -n "$watch_name" ] && { [ -n "$repo" ] || [ -n "$name" ] || [ -n "$revision" ] || [ "$all_files" = true ] ||
    [ "$allow_concurrent" = true ] || [ "$assume_yes" = yes ]; }; then
    usage_fail "--watch takes only a model name"
  fi
}

check_name() {
  [[ "$1" =~ $model_name_pattern ]] && [[ "$1" != *..* ]] ||
    fail "model name must be NAME or ORG/NAME of letters, digits, '.', '_', '-', each part starting with a letter or digit, without '..'"
}

# Runs before any terraform or aws call, so a bad input spends nothing.
resolve_inputs() {
  if [ -z "$repo" ]; then
    [ "$assume_yes" = no ] || usage_fail "--yes needs the repo on the command line"
    open_tty
    printf 'Hugging Face repo (ORG/NAME): ' >&2
    IFS= read -r -u 3 repo || true
  fi
  [[ "$repo" =~ $repo_pattern ]] && [[ "$repo" != *..* ]] ||
    fail "repo must be ORG/NAME of letters, digits, '.', '_', '-', without '..'"
  [ -n "$name" ] || name="$repo"
  check_name "$name"
  if [ -n "$revision" ]; then
    [[ "$revision" =~ $revision_pattern ]] && [[ "$revision" != *..* ]] ||
      fail "revision must be 1 to 128 letters, digits, '.', '_', '/', '-', without '..'"
  fi
}

# ECS startedBy allows 36 characters of [A-Za-z0-9_-]; the checksum keeps names that clean or
# truncate to the same text apart.
started_by_for() {
  local cleaned checksum
  cleaned="$(printf '%s' "$1" | tr -c 'A-Za-z0-9_-' '_')"
  checksum="$(printf '%s' "$1" | cksum | awk '{ print $1 }')"
  printf '%s-%s' "${cleaned:0:25}" "$checksum"
}

# The StartedBy tag value for an identity: its ARN with any character an ECS tag value or the AWS
# CLI's tag shorthand cannot carry replaced by '_', cut to the 256-character tag value limit.
started_by_tag_for() {
  local cleaned
  cleaned="$(printf '%s' "$1" | tr -c 'A-Za-z0-9_.:/=+@-' '_')"
  printf '%s' "${cleaned:0:256}"
}

plan_revision=""
token_required=false
launch_summary=""
launch_cost=""
preflight() {
  local plan_json summary file_count total_bytes gated rc=0 args=(plan --repo "$repo")
  [ -z "$revision" ] || args+=(--revision "$revision")
  [ "$all_files" = false ] || args+=(--all-files)
  [ -f "$copier" ] || fail "shared/fetch-model/copier.py not found"
  echo "Checking $repo on Hugging Face without a token..."
  plan_json="$(python3 -I "$copier" "${args[@]}")" || rc=$?
  case "$rc" in
    0) ;;
    5)
      token_required=true
      launch_summary="$repo @ ${revision:-main} (private or gated; size shows once the task starts)"
      launch_cost="Cost: Fargate task about \$0.40/h, at most about \$2.45 at the 6 h deadline; public IPv4 \$0.005/h while it runs; S3 storage about \$0.023/GB-month."
      echo "$repo is private or gated: the copy needs a token with access. Size and revision show once the task starts."
      echo "$launch_cost"
      return 0
      ;;
    2) fail "Hugging Face has no such repo or revision, or the copier refused the input" ;;
    *) fail "preflight failed (copier exit $rc); nothing was started" ;;
  esac
  summary="$(printf '%s' "$plan_json" | fetch_state plan)" || fail "the preflight returned no usable plan"
  IFS='|' read -r plan_revision file_count total_bytes gated <<<"$summary"
  [ "$gated" = no ] || token_required=true
  launch_summary="$repo @ ${plan_revision:0:7}: $file_count files, $(awk -v b="$total_bytes" 'BEGIN { printf "%.1f", b / 1e9 }') GB"
  launch_cost="Cost: Fargate task about \$0.40/h, at most about \$2.45 at the 6 h deadline; public IPv4 \$0.005/h while it runs."
  echo "$launch_summary, gated: $gated"
  echo "$launch_cost"
  echo "Cost: S3 storage about \$$(awk -v b="$total_bytes" 'BEGIN { printf "%.2f", b / 1e9 * 0.023 }')/month for this copy (\$0.023/GB-month) until models/$name/ is deleted."
}

bucket=""
region_name=""
cluster=""
task_definition=""
network=""
log_group=""
token_parameter=""
fetch_applied=false

# stack_output KEY NAME prints one output of a Terraform root. Returns 1 when the root has no such
# output yet (never applied, or applied before the output existed), and 2 after showing Terraform's
# error for any other failure, so a credentials or backend problem is not mistaken for "not applied".
stack_output() {
  local error_file="$state_dir/output.err" value
  if value="$(tf_output "$1" "$2" 2>"$error_file")"; then
    [ -n "$value" ] || return 1
    printf '%s' "$value"
    return 0
  fi
  grep -q "Output \"$2\" not found" "$error_file" && return 1
  cat "$error_file" >&2
  return 2
}

# The fetch-model root looks up the models bucket the shared root creates, so it cannot apply first.
require_shared_root() {
  local models_bucket rc=0
  models_bucket="$(stack_output shared models_bucket)" || rc=$?
  [ "$rc" -ne 2 ] ||
    fail "could not read the shared stack's models_bucket output; if it was never applied, run bin/drip-ai setup first (creates the shared models bucket)"
  [ "$rc" -eq 0 ] && [ -n "$models_bucket" ] ||
    fail "the shared stack has no models_bucket output; run bin/drip-ai setup first (creates the shared models bucket)"
}

# Sets fetch_applied=false when the fetch resources are not in state yet. Called as a statement,
# never in $(...) or a condition, so `fail` and errexit still stop the script.
load_fetch_outputs() {
  local rc=0
  fetch_applied=false
  cluster="$(stack_output fetch-model fetch_model_cluster)" || rc=$?
  [ "$rc" -ne 2 ] || fail "could not read the fetch-model stack's outputs; nothing was started"
  [ "$rc" -eq 0 ] || return 0
  bucket="$(tf_output fetch-model models_bucket)"
  task_definition="$(tf_output fetch-model fetch_model_task_definition)"
  network="$(tf_output fetch-model fetch_model_network)"
  log_group="$(tf_output fetch-model fetch_model_log_group)"
  token_parameter="$(tf_output fetch-model fetch_model_token_parameter)"
  region_name="$(tf_output fetch-model region)"
  [[ "$region_name" =~ $region_pattern ]] || fail "unexpected region output from the fetch-model stack"
  [[ "$cluster" =~ $cluster_pattern ]] || fail "unexpected fetch_model_cluster output"
  [[ "$bucket" =~ $bucket_pattern ]] || fail "unexpected models_bucket output"
  [[ "$task_definition" =~ $task_definition_pattern ]] || fail "unexpected fetch_model_task_definition output"
  [[ "$network" == '{'*'}' ]] || fail "unexpected fetch_model_network output"
  [[ "$log_group" =~ $log_group_pattern ]] || fail "unexpected fetch_model_log_group output"
  [[ "$token_parameter" =~ $parameter_pattern ]] || fail "unexpected fetch_model_token_parameter output"
  fetch_applied=true
}

started_by=""
started_by_tag=""
task_arn=""
task_id=""
find_running_task() {
  local found
  found="$(aws ecs list-tasks --region "$region_name" --cluster "$cluster" --started-by "$started_by" \
    --desired-status RUNNING --query 'taskArns[0]' --output text)" ||
    fail "could not list running fetch tasks; nothing was started"
  [ "$found" != None ] || found=""
  [ -z "$found" ] || [[ "$found" =~ $task_arn_pattern ]] || fail "list-tasks returned an unexpected task ARN"
  task_arn="$found"
  task_id="${task_arn##*/}"
}

hf_token=""
prompt_token() {
  [ "$assume_yes" = no ] || return 0
  open_tty
  printf 'Hugging Face token (Enter to skip for public models): ' >&2
  IFS= read -rs -u 3 hf_token || true
  printf '\n' >&2
  if [ -z "$hf_token" ]; then
    [ "$token_required" = false ] || fail "$repo is gated or private; rerun and enter a token that has access"
    return 0
  fi
  if [[ ! "$hf_token" =~ $token_pattern ]]; then
    hf_token=""
    fail "that is not a Hugging Face token (hf_ then 30 to 64 letters or digits); nothing was changed"
  fi
}

# Prints the ARN of each fetch task in the cluster that is starting or running, one per line.
running_fetch_tasks() {
  local listed arn
  listed="$(aws ecs list-tasks --region "$region_name" --cluster "$cluster" --desired-status RUNNING \
    --query 'taskArns' --output text)" || return 1
  while IFS= read -r arn; do
    [ -n "$arn" ] && [ "$arn" != None ] || continue
    [[ "$arn" =~ $task_arn_pattern ]] || return 1
    echo "$arn"
  done < <(printf '%s\n' "$listed" | tr '\t' '\n')
}

# Refuses, unless --allow-concurrent, while a fetch task another identity started is starting or
# running, or while any fetch task for another model name, the caller's own included, may still be
# starting: it reads the one token parameter when it starts, so a new fetch would overwrite the
# token it needs. A task for this name was already reattached by main. describe-tasks takes at most
# 100 ARNs.
refuse_other_fetches() {
  local listed arn batch line kind text task_name others="" starting="" starting_name="" index=0 arns=()
  listed="$(running_fetch_tasks)" || fail "could not list the running fetch tasks; nothing was started"
  while IFS= read -r arn; do
    [ -z "$arn" ] || arns+=("$arn")
  done <<<"$listed"
  while [ "$index" -lt "${#arns[@]}" ]; do
    batch="$(aws ecs describe-tasks --region "$region_name" --cluster "$cluster" --include TAGS \
      --tasks "${arns[@]:index:100}" --output json | fetch_state others "$started_by_tag" "$started_by")" ||
      fail "could not read who started the running fetch tasks; nothing was started"
    while IFS= read -r line; do
      kind="${line%%|*}"
      text="${line#*|}"
      case "$kind" in
        other) [ -n "$others" ] || others="$text" ;;
        starting)
          task_name="${text##*|}"
          text="${text%|*}"
          [ -n "$starting" ] || { starting="$text"; starting_name="$task_name"; }
          ;;
      esac
    done <<<"$batch"
    index=$((index + 100))
  done
  if [ "$allow_concurrent" = true ]; then
    [ -z "$others" ] || echo "--allow-concurrent: copying alongside another identity's fetch: $others"
    [ -z "$starting" ] || echo "--allow-concurrent: copying while another fetch is starting: $starting"
    return 0
  fi
  [ -z "$others" ] ||
    fail "another identity's fetch is running in $cluster: $others; wait for it to finish, or rerun with --allow-concurrent to copy alongside it"
  [ -z "$starting" ] ||
    fail "a fetch for another model in $cluster: $starting, and it reads the shared token parameter when it starts; wait for it to reach RUNNING (watch it: shared/fetch-model/fetch-model.sh --watch ${starting_name:-NAME}), or rerun with --allow-concurrent"
  return 0
}

lease_held=false
take_fetch_lease() {
  lease_acquire "$fetch_lease" "$fetch_lease_seconds" ||
    fail "could not take the $fetch_lease lease; nothing was started"
  lease_held=true
}

# lease_release itself leaves a lease that another identity or host holds in place.
release_fetch_lease() {
  lease_held=false
  lease_release "$fetch_lease" ||
    echo "fetch-model: could not release the $fetch_lease lease; it expires on its own within $((fetch_lease_seconds / 60)) minutes" >&2
}

# An apply with nothing to change asks nothing (a detached run can leave the token parameter in
# place), so the launch has its own question. Anything but y or yes starts nothing.
confirm_launch() {
  local answer=""
  echo "Ready to copy $launch_summary to models/$name/"
  echo "$launch_cost"
  if [ "$assume_yes" = yes ]; then
    echo "Starting the copy (--yes)."
    return 0
  fi
  open_tty
  printf 'Start the copy? [y/N] ' >&2
  IFS= read -r -u 3 answer || true
  case "$answer" in
    y | Y | yes | YES | Yes) return 0 ;;
  esac
  echo "Not started; nothing billed."
  exit 0
}

apply_fetch_resources() {
  if [ "$assume_yes" = yes ]; then
    echo "Applying the fetch-model stack; --yes approves the plan it prints."
  else
    echo "Applying the fetch-model stack; read the plan and type yes."
  fi
  tf_approved "$assume_yes" fetch-model apply || fail "apply failed or was declined; no task was started"
}

# The token reaches the AWS CLI only through a 0600 file in the private state dir, written by printf,
# a builtin, so it is never in argv or a child's environment. The CLI loads --cli-input-json file://
# only from a regular file (not /dev/stdin), and the file goes as soon as the call returns. A skipped
# token writes "none", so an older token cannot reach a public copy.
# token_parameter_live means this run owns deleting the parameter: ECS resolves the task
# definition's secrets before the container starts, so it is deleted only once no launched task can
# still be PROVISIONING or PENDING (see watch_task and on_exit).
token_parameter_live=false
token_input_file=""
write_token_parameter() {
  local put_status=0
  token_parameter_live=true
  token_input_file="$state_dir/token-param.json"
  # set -C refuses a file already at that path, so the umask always applies.
  (
    umask 077
    set -C
    printf '{"Name":"%s","Value":"%s","Type":"SecureString","Overwrite":true}' \
      "$token_parameter" "${hf_token:-none}" >"$token_input_file"
  ) || put_status=1
  hf_token=""
  unset hf_token
  if [ "$put_status" -eq 0 ]; then
    aws ssm put-parameter --region "$region_name" --cli-input-json "file://$token_input_file" >/dev/null ||
      put_status=1
  fi
  rm -f "$token_input_file"
  [ "$put_status" -eq 0 ] || fail "could not write the token to $token_parameter; no task was started"
}

# SSM keeps readable version history, so the parameter is deleted rather than overwritten.
delete_token_parameter_command() {
  echo "aws ssm delete-parameter --region $region_name --name $token_parameter"
}

delete_token_parameter() {
  local error
  if error="$(aws ssm delete-parameter --region "$region_name" --name "$token_parameter" 2>&1 >/dev/null)" ||
    [[ "$error" == *ParameterNotFound* ]]; then
    token_parameter_live=false
    return 0
  fi
  echo "fetch-model: could not delete the token parameter; delete it now: $(delete_token_parameter_command)" >&2
  return 1
}

copy_overrides() {
  local command
  command="\"copy\",\"--repo\",\"$repo\",\"--revision\",\"${plan_revision:-${revision:-main}}\",\"--name\",\"$name\",\"--bucket\",\"$bucket\",\"--region\",\"$region_name\""
  [ "$all_files" = false ] || command="$command,\"--all-files\""
  printf '{"containerOverrides":[{"name":"fetch","command":[%s]}]}' "$command"
}

launch_task() {
  local response
  response="$(aws ecs run-task --region "$region_name" --cluster "$cluster" \
    --task-definition "$task_definition" --launch-type FARGATE \
    --network-configuration "$network" \
    --propagate-tags TASK_DEFINITION --enable-ecs-managed-tags \
    --tags key=Project,value=drip-ai key=ManagedBy,value=shared/fetch-model/fetch-model.sh \
    "key=StartedBy,value=$started_by_tag" \
    --started-by "$started_by" --overrides "$(copy_overrides)" --output json)" ||
    fail "run-task failed; no task was started"
  task_arn="$(printf '%s' "$response" | fetch_state runtask)" || exit 1
  task_id="${task_arn##*/}"
  echo "Started task $task_id."
}

s3_console_url() {
  # Names hold only unreserved URL characters and '/', so they need no further encoding.
  echo "https://$region_name.console.aws.amazon.com/s3/buckets/$bucket?region=$region_name&prefix=models/$name/"
}

print_links() {
  echo "S3 (objects appear as each file completes): $(s3_console_url)"
  echo "ECS task: https://$region_name.console.aws.amazon.com/ecs/v2/clusters/$cluster/tasks/$task_id/configuration?region=$region_name"
}

note=""
read_s3() {
  local listing
  if listing="$(aws s3api list-objects-v2 --region "$region_name" --bucket "$bucket" --prefix "models/$name/" \
    --query 'Contents[].[Key,Size]' --output text 2>/dev/null)"; then
    printf '%s\n' "$listing" | fetch_state s3 "models/$name/" || note="S3 listing unreadable"
  else
    note="S3 listing failed; showing the last good one"
  fi
}

# The status object is missing until the copier writes it; verify refuses a run without it.
read_status_object() {
  local document
  document="$(aws s3 cp --region "$region_name" "s3://$bucket/models/$name/drip-ai-fetch.json" - 2>/dev/null)" ||
    return 0
  printf '%s' "$document" | fetch_state status
}

log_token=""
read_log() {
  local page result pages=0 error_file="$state_dir/log.err" args
  while [ "$pages" -lt 10 ]; do
    pages=$((pages + 1))
    args=(--start-from-head)
    [ -z "$log_token" ] || args+=(--next-token "$log_token")
    if ! page="$(aws logs get-log-events --region "$region_name" --log-group-name "$log_group" \
      --log-stream-name "fetch/fetch/$task_id" "${args[@]}" --output json 2>"$error_file")"; then
      grep -q ResourceNotFoundException "$error_file" || note="job log read failed"
      return 0
    fi
    result="$(printf '%s' "$page" | fetch_state logs "$log_token")" || { note="job log unreadable"; return 0; }
    log_token="${result%|*}"
    [ "${result##*|}" = more ] || return 0
  done
}

render() {
  if [ -t 1 ]; then
    printf '\033[H\033[J'
    print_links
    fetch_state render frame "$note"
  else
    fetch_state render line "$note"
  fi
}

rerun_command() {
  local source_repo source_revision
  source_repo="$(fetch_state field repo)" || source_repo=""
  source_revision="$(fetch_state field revision)" || source_revision=""
  printf 'shared/fetch-model/fetch-model.sh %s --name %s' "${source_repo:-ORG/NAME}" "$name"
  [ -z "$source_revision" ] || printf ' --revision %s' "$source_revision"
  [ "$all_files" = false ] || printf ' --all-files'
  printf '\n'
}

exit_meaning() {
  case "$1" in
    2) echo "the copier refused its input or the model is over its size cap" ;;
    3) echo "s3://$bucket/models/$name/ holds another revision; choose another --name" ;;
    4) echo "a file failed its size or hash check, so it was not stored" ;;
    5) echo "Hugging Face refused access: the repo is private or gated and the token was missing or lacks access" ;;
    124) echo "the copy hit its 6 h deadline" ;;
    143) echo "the task was stopped before it finished" ;;
    *) echo "the copier exited with unexpected code $1" ;;
  esac
}

finish() {
  local exit_code="$1" summary reason
  if [ "$exit_code" = 0 ]; then
    summary="$(fetch_state verify)" ||
      fail "the task exited 0, but the copy is not verified; rerun to resume: $(rerun_command)"
    echo "Copied $summary to s3://$bucket/models/$name/"
    echo "Next: sagemaker/start.sh $name"
    exit 0
  fi
  if [ -z "$exit_code" ]; then
    reason="$(fetch_state field reason)" || reason=""
    echo "fetch-model: the task stopped before the copier finished: ${reason:-no reason given}" >&2
  else
    echo "fetch-model: copier exit $exit_code: $(exit_meaning "$exit_code")" >&2
  fi
  echo "Rerunning the same command resumes: $(rerun_command)" >&2
  exit 1
}

# count_starting_tasks [EXCLUDE_ARN] prints how many tasks in the fetch cluster, other than
# EXCLUDE_ARN, may still be starting. Every fetch task reads the one token parameter when it starts,
# whatever model it copies. describe-tasks takes at most 100 ARNs.
count_starting_tasks() {
  local exclude="${1:-}" listed arn count total=0 index=0 arns=()
  listed="$(running_fetch_tasks)" || return 1
  while IFS= read -r arn; do
    [ -z "$arn" ] || [ "$arn" = "$exclude" ] || arns+=("$arn")
  done <<<"$listed"
  while [ "$index" -lt "${#arns[@]}" ]; do
    count="$(aws ecs describe-tasks --region "$region_name" --cluster "$cluster" \
      --tasks "${arns[@]:index:100}" --output json | fetch_state starting)" || return 1
    total=$((total + count))
    index=$((index + 100))
  done
  echo "$total"
}

# --watch NAME with no task running for NAME: the copy ended, or a detached run's task stopped. The
# token parameter goes unless a fetch task may still be starting; then S3 says how the copy ended.
report_ended_copy() {
  local starting copy_state summary
  if ! starting="$(count_starting_tasks)"; then
    echo "fetch-model: could not check for fetch tasks still starting, so the token stays in SSM at $token_parameter; once none is starting, delete it: $(delete_token_parameter_command)" >&2
  elif [ "$starting" -gt 0 ]; then
    echo "fetch-model: $starting fetch task(s) in $cluster are still starting and read the token from SSM when they start, so it stays at $token_parameter; once they run, delete it: $(delete_token_parameter_command)" >&2
  elif delete_token_parameter; then
    echo "No fetch task is starting, so the token parameter $token_parameter is deleted."
    release_fetch_lease
  fi
  read_s3
  read_status_object
  copy_state="$(fetch_state copy_state)"
  case "$copy_state" in
    missing) fail "no fetch found for $name: no running task and no s3://$bucket/models/$name/drip-ai-fetch.json" ;;
    complete)
      summary="$(fetch_state verify)" ||
        fail "the status object says complete, but the copy is not verified; rerun to resume: $(rerun_command)"
      echo "No fetch task is running for $name; the copy finished: $summary at s3://$bucket/models/$name/"
      echo "Next: sagemaker/start.sh $name"
      exit 0
      ;;
    *)
      echo "fetch-model: no fetch task is running for $name, and the copy stopped before finishing (status: $copy_state)" >&2
      echo "Rerunning the same fetch command resumes: $(rerun_command)" >&2
      exit 1
      ;;
  esac
}

# Deletes the token parameter only while no other fetch task may still be starting, because each
# reads it when it starts; the watched task is past PENDING, so it does not count. Otherwise keeps
# it, records why in token_kept_reason, and returns 1.
token_kept_reason=""
delete_token_when_unused() {
  local starting
  if ! starting="$(count_starting_tasks "$task_arn")"; then
    token_kept_reason="could not check for other fetch tasks still starting, so the token stays in SSM at $token_parameter"
  elif [ "$starting" -gt 0 ]; then
    token_kept_reason="$starting other fetch task(s) in $cluster are still starting and read the token from SSM when they start, so it stays at $token_parameter"
  else
    token_kept_reason=""
    delete_token_parameter
    return
  fi
  return 1
}

task_past_pending=false
watch_task() {
  local task_line last_status exit_code elapsed kept_reported="" stop_requested=false
  [[ "$watch_seconds" =~ ^[0-9]+$ ]] && [ "$watch_seconds" -ge 5 ] || watch_seconds=5
  [ -t 1 ] || print_links
  while :; do
    note=""
    task_line="$(aws ecs describe-tasks --region "$region_name" --cluster "$cluster" --tasks "$task_arn" \
      --output json | fetch_state task)" || task_line="UNKNOWN||"
    IFS='|' read -r last_status exit_code elapsed <<<"$task_line"
    [ "$last_status" != UNKNOWN ] || note="task status unreadable"
    case "$last_status" in
      UNKNOWN | PROVISIONING | PENDING | ACTIVATING) ;;
      *)
        task_past_pending=true
        if [ "$token_parameter_live" = true ]; then
          if delete_token_when_unused; then
            release_fetch_lease
          elif [ -n "$token_kept_reason" ] && [ "$token_kept_reason" != "$kept_reported" ]; then
            kept_reported="$token_kept_reason"
            echo "fetch-model: $token_kept_reason; this watch retries every tick and deletes it once none is starting" >&2
          fi
        fi
        ;;
    esac
    read_s3
    read_status_object
    read_log
    if [ "$token_parameter_live" = true ] && [ -n "$token_kept_reason" ]; then
      note="${note:+$note; }token kept in SSM while another fetch task starts"
    fi
    render
    [ "$last_status" != STOPPED ] || finish "$exit_code"
    [ -n "$elapsed" ] || elapsed="$SECONDS"
    if [ "$stop_requested" = false ] && [ "$elapsed" -ge $((job_deadline_seconds + stop_grace_seconds)) ]; then
      echo "fetch-model: the task outlived the copier's 6 h deadline by 20 minutes; stopping it."
      aws ecs stop-task --region "$region_name" --cluster "$cluster" --task "$task_arn" \
        --reason "fetch-model.sh: past the 6 h deadline" >/dev/null && stop_requested=true
    fi
    sleep "$watch_seconds"
  done
}

on_interrupt() {
  if [ -n "$task_arn" ]; then
    echo
    if [ "$task_past_pending" = true ]; then
      echo "Detached; the copy keeps running and billing (about \$0.40/h)."
    else
      echo "Detached; the task is still starting, and then copies and bills (about \$0.40/h) without this script."
    fi
    echo "Reattach: shared/fetch-model/fetch-model.sh --watch $name"
    echo "Stop it:  aws ecs stop-task --region $region_name --cluster $cluster --task $task_arn"
  fi
  exit 130
}

# With no task launched the parameter goes now; with one past PENDING it goes unless another fetch
# task may still be starting. A launched task that may still be starting needs it, so it stays and a
# later --watch or rerun deletes it. The lease this run took goes once the parameter is gone or was
# never written, and otherwise stays with the token.
on_exit() {
  [ -z "$token_input_file" ] || rm -f "$token_input_file"
  if [ "$token_parameter_live" = true ]; then
    if [ -z "$task_arn" ]; then
      delete_token_parameter || true
    elif [ "$task_past_pending" = true ]; then
      if ! delete_token_when_unused && [ -n "$token_kept_reason" ]; then
        echo "fetch-model: $token_kept_reason; shared/fetch-model/fetch-model.sh --watch $name deletes it once none is starting, or then: $(delete_token_parameter_command)" >&2
      fi
    else
      echo "fetch-model: the token stays in SSM at $token_parameter until the task starts, because ECS reads it then; shared/fetch-model/fetch-model.sh --watch $name (or rerunning) deletes it, or once the task runs: $(delete_token_parameter_command)" >&2
    fi
  fi
  if [ "$lease_held" = true ]; then
    if [ "$token_parameter_live" = false ]; then
      release_fetch_lease
    else
      echo "fetch-model: the $fetch_lease lease stays held while the token is in SSM, so no other fetch writes it; shared/fetch-model/fetch-model.sh --watch $name releases it when it deletes the token" >&2
    fi
  fi
  [ -z "$state_dir" ] || rm -rf "$state_dir"
}

main() {
  parse_args "$@"
  state_dir="$(mktemp -d)"
  state_file="$state_dir/state.json"
  trap on_exit EXIT
  trap on_interrupt INT
  if [ -n "$watch_name" ]; then
    name="$watch_name"
    check_name "$name"
    started_by="$(started_by_for "$name")"
    # Once in this shell, so every $(tf_output ...) subshell reuses the identity.
    load_caller_identity || exit 1
    load_fetch_outputs
    [ "$fetch_applied" = true ] || fail "no fetch resources in Terraform state; run shared/fetch-model/fetch-model.sh ORG/NAME first"
    find_running_task
    [ -n "$task_arn" ] || report_ended_copy
    # An interrupted run may have left the token for this task; delete it once the task starts.
    token_parameter_live=true
    watch_task
  fi
  resolve_inputs
  preflight
  # The token is typed only at the hidden prompt, which --yes skips, so a gated copy cannot start.
  [ "$assume_yes" = no ] || [ "$token_required" = false ] ||
    fail "$repo is private or gated, and its token is typed only at the hidden prompt; rerun without --yes. Nothing was started"
  started_by="$(started_by_for "$name")"
  load_caller_identity || exit 1
  started_by_tag="$(started_by_tag_for "$drip_caller_arn_value")"
  require_shared_root
  load_fetch_outputs
  if [ "$fetch_applied" = true ]; then
    find_running_task
    if [ -n "$task_arn" ]; then
      echo "A fetch task for $name is already running; watching it."
      token_parameter_live=true
      watch_task
    fi
    refuse_other_fetches
  fi
  take_fetch_lease
  prompt_token
  apply_fetch_resources
  load_fetch_outputs
  [ "$fetch_applied" = true ] || fail "the apply finished but the fetch outputs are missing; check: terraform -chdir=shared/fetch-model/terraform output"
  confirm_launch
  # The plan may have waited past part of the lease; refresh it before the token window opens.
  take_fetch_lease
  write_token_parameter
  launch_task
  watch_task
}

main "$@"
