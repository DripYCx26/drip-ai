#!/usr/bin/env bash
# Client-side commands: print the API key for a client, smoke-test every API on the open tunnel, and
# link, unlink, or report Codex, Claude Code, and opencode. Design: docs/DECISIONS.md, 2026-10-05
# "Folders per backend, Terraform roots per stack", and 2026-10-06 "Tunnels link Claude Code and
# opencode". Needs no Terraform: the key is read from SSM by its conventional name. Bash 3.2
# compatible.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$repo_root/shared/lib/common.sh"

api_key_parameter="$api_key_parameter_name"

usage() {
  cat <<'EOF_USAGE'
Usage: shared/clients.sh key                       print the API key (only into a pipe or $(...))
       shared/clients.sh smoke                     check every API on localhost:$DRIP_AI_PORT (default 8000)
       shared/clients.sh link [CLIENT] --model NAME --context-window TOKENS
                                                   point CLIENT (default: every installed one) at
                                                   localhost:$DRIP_AI_PORT; the tunnel does this itself
       shared/clients.sh unlink [CLIENT] [--force] restore CLIENT's config (default: all)
       shared/clients.sh status [CLIENT]           whether CLIENT is linked (default: all)
       shared/clients.sh codex link|unlink|status  pass through to shared/lib/codex_link.py

CLIENT is codex, claude, or opencode.

smoke expects the one model the tunnel serves; DRIP_AI_MODEL names it when several are served.
EOF_USAGE
}

fail() { echo "$1" >&2; exit 1; }

# The local port: DRIP_AI_PORT, else ports.base from the drip-ai config (default 8000).
smoke_port() {
  local port="${DRIP_AI_PORT:-}"
  [ -n "$port" ] || port="$(drip_config ports.base)" || return 1
  [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] || { echo "drip-ai: the local port '$port' is not a TCP port" >&2; return 1; }
  echo "$port"
}

# The region whose key opens the tunnel on PORT: DRIP_AI_REGION when set, else the region of the
# registered drip-ai tunnel on that port, else the home region. Each region's shared stack holds
# its own key.
key_region() {
  local region
  if [ -z "${DRIP_AI_REGION:-}" ] && [ -n "${1:-}" ] &&
    region="$(python3 -I "$repo_root/shared/drip_ai/serve.py" region --port "$1" 2>/dev/null)" &&
    [[ "$region" =~ $region_pattern ]]; then
    echo "$region"
    return 0
  fi
  drip_region
}

# Prints the key. Refused characters would break the curl config line the key travels in.
api_key() {
  local region key
  region="$(key_region "${local_port:-}")" || return 1
  key="$(aws ssm get-parameter --region "$region" --name "$api_key_parameter" --with-decryption \
    --query Parameter.Value --output text)" || {
    echo "drip-ai: could not read $api_key_parameter from SSM in $region; check your AWS credentials and region" >&2
    return 1
  }
  if ! [[ "$key" =~ ^[!-~]+$ ]] || [[ "$key" == *[\"\\]* ]]; then
    echo "drip-ai: $api_key_parameter has an unexpected form" >&2
    return 1
  fi
  printf '%s\n' "$key"
}

# A terminal would show the key on screen and in scrollback.
key_command() {
  if [ -t 1 ]; then
    echo "key: refusing to print the API key to a terminal; use: export DRIP_AI_API_KEY=\"\$(bin/drip-ai key)\"" >&2
    exit 1
  fi
  api_key || exit 1
}

# curl ARGS... with the key as a header read from a config on stdin; printf is a builtin, so the key
# never reaches any argv. Returns curl's status so a caller's `|| smoke_fail` sees a failed request.
curl_with_key() {
  printf 'header = "Authorization: Bearer %s"\n' "$key" | curl --config - "$@"
}

post() {
  local path="$1" body="$2"
  curl_with_key -fsS --max-time 120 "http://localhost:$local_port$path" \
    -H 'Content-Type: application/json' -d "$body"
}

# expect_shape CHECK EXPECTED: exits non-zero unless the JSON on stdin passes CHECK. "answer" needs
# a non-empty reply with no raw reasoning or tool-call markup, which a missing or wrong parser leaves
# in the content; "messages-answer" and "responses-answer" need such a text block in a Messages or
# Responses reply, so a reply that is only thinking fails; "tool-call" needs a first tool call to
# the function EXPECTED with JSON arguments, and content that is null or free of that markup.
expect_shape() {
  python3 -I -c '
import json, sys
check, expected = sys.argv[1], sys.argv[2]
RAW_MARKUP = ("<think>", "</think>", "<tool_call>", "</tool_call>", "<|", "<\uff5c")
try:
    doc = json.load(sys.stdin)
except ValueError:
    doc = None

def message(d):
    return d["choices"][0]["message"]

def has_markup(content):
    return any(m in content for m in RAW_MARKUP)

def is_clean_text(text):
    return isinstance(text, str) and bool(text.strip()) and not has_markup(text)

def is_clean_answer(d):
    return is_clean_text(message(d).get("content"))

def is_parsed_tool_call(d):
    function = message(d)["tool_calls"][0]["function"]
    content = message(d).get("content")
    json.loads(function["arguments"])
    return function["name"] == expected and (content is None or (isinstance(content, str) and not has_markup(content)))

checks = {
    "/v1/models": lambda d: any(m.get("id") == expected for m in d.get("data", [])),
    "/v1/chat/completions": lambda d: "choices" in d,
    "answer": is_clean_answer,
    "tool-call": is_parsed_tool_call,
    "/v1/messages": lambda d: d.get("type") == "message",
    "/v1/responses": lambda d: d.get("object") == "response",
    "messages-answer": lambda d: any(
        b.get("type") == "text" and is_clean_text(b.get("text")) for b in d["content"]),
    "responses-answer": lambda d: any(
        p.get("type") == "output_text" and is_clean_text(p.get("text"))
        for item in d["output"] if item.get("type") == "message" for p in item["content"]),
}
try:
    ok = isinstance(doc, dict) and checks[check](doc)
except (AttributeError, TypeError, KeyError, IndexError, ValueError):
    ok = False
sys.exit(0 if ok else 1)
' "$1" "$2"
}

# Prints the only model id in the /v1/models JSON on stdin; fails on none or several.
only_model() {
  python3 -I -c '
import json, re, sys
try:
    ids = [m.get("id") for m in json.load(sys.stdin).get("data", [])]
except (ValueError, AttributeError, TypeError):
    sys.exit(1)
if len(ids) != 1 or not isinstance(ids[0], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]*", ids[0]):
    sys.exit(1)
print(ids[0])
'
}

smoke_fail() { echo "smoke: $1" >&2; exit 1; }

# ASSUMPTION: a reasoning model answers "Say ok" within this many tokens, thinking included.
answer_max_tokens=1024
smoke_tool=get_weather

smoke() {
  local key model="${DRIP_AI_MODEL:-}" body stream answer_body tool_body
  key="$(api_key)" || exit 1
  echo "== /v1/models"
  body="$(curl_with_key -fsS --max-time 10 "http://localhost:$local_port/v1/models")" ||
    smoke_fail "/v1/models request failed"
  echo "$body"
  if [ -z "$model" ]; then
    model="$(only_model <<<"$body")" || smoke_fail "/v1/models does not list exactly one model; set DRIP_AI_MODEL"
  fi
  [[ "$model" =~ ^[A-Za-z0-9][A-Za-z0-9._/:-]*$ ]] || smoke_fail "DRIP_AI_MODEL is not a model name"
  expect_shape /v1/models "$model" <<<"$body" || smoke_fail "/v1/models does not list $model"
  answer_body="{\"model\":\"$model\",\"max_tokens\":$answer_max_tokens,\"messages\":[{\"role\":\"user\",\"content\":\"Say ok\"}]}"
  tool_body="{\"model\":\"$model\",\"max_tokens\":$answer_max_tokens,\"messages\":[{\"role\":\"user\",\"content\":\"Call the $smoke_tool tool for Paris. Do not answer in text.\"}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"$smoke_tool\",\"description\":\"Current weather for a city\",\"parameters\":{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},\"required\":[\"city\"]}}}],\"tool_choice\":\"auto\"}"
  echo "== /v1/chat/completions (opencode)"
  body="$(post /v1/chat/completions "$answer_body")" || smoke_fail "/v1/chat/completions request failed"
  echo "$body"
  expect_shape /v1/chat/completions "$model" <<<"$body" || smoke_fail "/v1/chat/completions has no \"choices\""
  expect_shape answer "$model" <<<"$body" ||
    smoke_fail "/v1/chat/completions answered with empty content or raw <think> or tool-call markup; check the model profile's parsers"
  echo "== /v1/chat/completions tool call"
  body="$(post /v1/chat/completions "$tool_body")" || smoke_fail "/v1/chat/completions tool call request failed"
  echo "$body"
  expect_shape tool-call "$smoke_tool" <<<"$body" ||
    smoke_fail "/v1/chat/completions did not return a parsed $smoke_tool tool call; check the model profile's tool-call parser"
  echo "== /v1/chat/completions stream"
  stream="$(curl_with_key -fsSN --max-time 120 "http://localhost:$local_port/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$model\",\"max_tokens\":16,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Say ok\"}]}")" ||
    smoke_fail "streaming /v1/chat/completions request failed"
  grep -q '^data: *{' <<<"$stream" || smoke_fail "streaming /v1/chat/completions sent no data: event"
  grep -q '^data: \[DONE\]' <<<"$stream" || smoke_fail "streaming /v1/chat/completions did not end with data: [DONE]"
  echo "stream ok"
  echo "== /v1/messages (Claude Code)"
  body="$(post /v1/messages "$answer_body")" || smoke_fail "/v1/messages request failed"
  echo "$body"
  expect_shape /v1/messages "$model" <<<"$body" || smoke_fail "/v1/messages has no \"type\":\"message\""
  expect_shape messages-answer "$model" <<<"$body" ||
    smoke_fail "/v1/messages answered with no text block (only thinking, or raw markup); check the model profile's parsers"
  echo "== /v1/responses (Codex)"
  body="$(post /v1/responses "{\"model\":\"$model\",\"max_output_tokens\":$answer_max_tokens,\"input\":\"Say ok\"}")" ||
    smoke_fail "/v1/responses request failed"
  echo "$body"
  expect_shape /v1/responses "$model" <<<"$body" || smoke_fail "/v1/responses has no \"object\":\"response\""
  expect_shape responses-answer "$model" <<<"$body" ||
    smoke_fail "/v1/responses answered with no output_text (only reasoning, or raw markup); check the model profile's parsers"
}

# Sets clients to the one CLIENT named, or to every client when none is.
pick_clients() {
  clients="$link_client_names"
  [ -n "${1:-}" ] || return 0
  case " $link_client_names " in
    *" $1 "*) clients="$1" ;;
    *) usage >&2; exit 2 ;;
  esac
}

# The tunnel links each client itself; this is for a manual link, which points at
# localhost:$DRIP_AI_PORT whether or not a tunnel serves it.
link_command() {
  local model="" window="" client="" key region
  while [ $# -gt 0 ]; do
    case "$1" in
      --model) [ $# -ge 2 ] || { usage >&2; exit 2; }; model="$2"; shift 2 ;;
      --context-window) [ $# -ge 2 ] || { usage >&2; exit 2; }; window="$2"; shift 2 ;;
      -*) usage >&2; exit 2 ;;
      *) [ -z "$client" ] || { usage >&2; exit 2; }; client="$1"; shift ;;
    esac
  done
  [ -n "$model" ] && [ -n "$window" ] || { usage >&2; exit 2; }
  pick_clients "$client"
  region="$(drip_region)" || exit 1
  key="$(api_key)" || exit 1
  # shellcheck disable=SC2086 # clients is a space-separated list of names
  link_clients "$local_port" "$model" "$window" "$api_key_parameter" "$region" "$key" "$$" $clients ||
    exit 1
  for client in $skipped_clients; do echo "$(client_label "$client"): not installed; skipped"; done
}

unlink_command() {
  local client="" force=() status=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --force) force=(--force) ;;
      -*) usage >&2; exit 2 ;;
      *) [ -z "$client" ] || { usage >&2; exit 2; }; client="$1" ;;
    esac
    shift
  done
  pick_clients "$client"
  for client in $clients; do
    unlink_client "$client" ${force[@]+"${force[@]}"} || status=1
  done
  exit "$status"
}

status_command() {
  local client output status=0
  [ $# -le 1 ] || { usage >&2; exit 2; }
  pick_clients "${1:-}"
  for client in $clients; do
    if [ "$client" = codex ]; then
      output="$(python3 -I "$repo_root/shared/lib/codex_link.py" status)" || status=1
    else
      output="$(python3 -I "$repo_root/shared/lib/json_link.py" status --client "$client")" || status=1
    fi
    echo "$(client_label "$client"): $output"
  done
  exit "$status"
}

# Codex's own link program, for flags the commands above do not take, such as unlink --model.
codex_command() {
  case "${1:-}" in
    link | unlink | status)
      [ -f "$repo_root/shared/lib/codex_link.py" ] || fail "drip-ai: shared/lib/codex_link.py not found"
      exec python3 -I "$repo_root/shared/lib/codex_link.py" "$@"
      ;;
    *) usage >&2; exit 2 ;;
  esac
}

command="${1:-}"
shift || true
case "$command" in
  key | smoke | link) local_port="$(smoke_port)" || exit 1 ;;
esac
case "$command" in
  key) [ $# -eq 0 ] || { usage >&2; exit 2; }; key_command ;;
  smoke) [ $# -eq 0 ] || { usage >&2; exit 2; }; smoke ;;
  link) link_command "$@" ;;
  unlink) unlink_command "$@" ;;
  status) status_command "$@" ;;
  codex) codex_command "$@" ;;
  help | -h | --help) usage ;;
  *) usage >&2; exit 2 ;;
esac
