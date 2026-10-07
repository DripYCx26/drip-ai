#!/usr/bin/env bash
# Double-click in Finder to open a Terminal menu for the everyday drip-ai commands (macOS).
# Every command it runs still asks before spending; this only saves typing.
cd "$(dirname "$0")" || exit 1
repo="$(pwd)"

# The tunnel holds its terminal, so it gets a window of its own. Arguments: the bin/drip-ai command.
open_tunnel_window() {
  osascript -e "tell application \"Terminal\" to do script \"cd '$repo' && bin/drip-ai $*\"" \
    -e 'tell application "Terminal" to activate' >/dev/null
  echo "Opened the tunnel in a new window. Keep it open while you use Codex, Claude Code, or opencode."
}

while true; do
  cat <<'EOF_MENU'

drip-ai
  g) Go                start SageMaker, then open the tunnel and smoke test in a new window
  1) Status            what is running and billing
  2) Fetch a model     copy a Hugging Face model into S3
  3) Start SageMaker   race the instance types and keep the winner
  4) Open tunnel       serve the model on localhost:8000 and link your coding tools
  5) Smoke test        check the model answers (needs the tunnel)
  6) Stop SageMaker    delete the endpoints and end the billing
  7) EC2 status
  q) Quit
EOF_MENU
  printf 'Choose: '
  IFS= read -r choice || exit 0
  case "$choice" in
    g | G) bin/drip-ai sagemaker start --no-watch && open_tunnel_window sagemaker tunnel --smoke ;;
    1) bin/drip-ai sagemaker status; bin/drip-ai models list ;;
    2)
      printf 'Hugging Face model (Org/Name): '
      IFS= read -r model || continue
      [ -n "$model" ] && bin/drip-ai fetch "$model"
      ;;
    3) bin/drip-ai sagemaker start ;;
    4) open_tunnel_window sagemaker tunnel ;;
    5) bin/drip-ai smoke ;;
    6) bin/drip-ai sagemaker stop ;;
    7) bin/drip-ai ec2 status ;;
    q | Q) exit 0 ;;
    *) echo "Pick g, 1-7, or q." ;;
  esac
done
