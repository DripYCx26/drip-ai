#!/usr/bin/env bash
# The EC2 GPU server: apply or destroy its Terraform root, start or stop the instance, show its
# state and logs, and open the SSM tunnel that links Codex, Claude Code, and opencode. Design:
# docs/DECISIONS.md, 2026-10-05 "Folders per backend, Terraform roots per stack". Bash 3.2 compatible.
# apply and destroy show the plan and wait for the user's yes; --yes answers it, after the plan.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../shared/lib/common.sh
source "$repo_root/shared/lib/common.sh"

local_port="${DRIP_AI_PORT:-8000}"
tfvars_file="$repo_root/ec2/terraform/terraform.tfvars"
instance_pattern='^i-[0-9a-f]{8,17}$'
# Destroy does not read the model, but the root's model_source has no default and is validated.
destroy_model_source=unused-on-destroy

usage() {
  cat <<'EOF_USAGE'
Usage: ec2/server.sh <command>

  apply     create or update the EC2 server from ec2/terraform/terraform.tfvars (asks before spending)
  destroy   unlink the clients, then delete the EC2 server and its volume (asks first)
  start     start the stopped instance and wait until it runs
  stop      unlink the clients, then stop the instance (its EBS volume still bills)
  status    show the instance id, type, and state
  logs      last 100 lines of the bootstrap and vLLM logs
  tunnel [--no-link]
            serve localhost:$DRIP_AI_PORT (default 8000) through an SSM port-forward and link Codex,
            Claude Code, and opencode until it closes

apply, destroy, and start take --yes: it answers Terraform's question after the plan prints. start
asks nothing, and takes --yes so every command that spends does.
EOF_USAGE
}

fail() { echo "ec2: $1" >&2; exit 1; }

# True when terraform.tfvars sets model_source on a line of its own.
tfvars_sets_model_source() {
  [ -f "$tfvars_file" ] && grep -Eq '^[[:space:]]*model_source[[:space:]]*=' "$tfvars_file"
}

apply() {
  tfvars_sets_model_source || [ -n "${TF_VAR_model_source:-}" ] ||
    fail "ec2/terraform/terraform.tfvars does not set model_source; copy ec2/terraform/terraform.tfvars.example to ec2/terraform/terraform.tfvars, set model_source (and model_from_s3 = true for a folder in the models bucket), and rerun"
  tf_approved "$assume_yes" ec2 apply || fail "apply failed or was declined"
}

# The links would point at a server that no longer exists, so they go first, as sagemaker stop does.
destroy() {
  unlink_all_clients
  if tfvars_sets_model_source || [ -n "${TF_VAR_model_source:-}" ]; then
    tf_approved "$assume_yes" ec2 destroy || fail "destroy failed or was declined"
  else
    tf_approved "$assume_yes" ec2 destroy -var "model_source=$destroy_model_source" ||
      fail "destroy failed or was declined"
  fi
}

# Sets the globals region and instance; called directly so a failed read stops the script.
resolve_instance() {
  region="$(drip_region)" || exit 1
  instance="$(tf_output ec2 instance_id)" || fail "could not read instance_id from the ec2 Terraform root"
  [[ "$instance" =~ $instance_pattern ]] || fail "no EC2 instance in the ec2 Terraform state; run bin/drip-ai ec2 apply"
}

instance_state() {
  aws ec2 describe-instances --region "$region" --instance-ids "$instance" \
    --query 'Reservations[0].Instances[0].State.Name' --output text
}

# The readiness check tunnel_with_link runs before it links the clients.
instance_running() {
  [ "$(instance_state 2>/dev/null)" = running ]
}

port_forward() {
  aws ssm start-session --region "$region" --target "$instance" \
    --document-name AWS-StartPortForwardingSession \
    --parameters "portNumber=8000,localPortNumber=$local_port"
}

remote_logs() {
  local command_id
  command_id="$(aws ssm send-command --region "$region" --instance-ids "$instance" \
    --document-name AWS-RunShellScript \
    --parameters 'commands=["tail -n 100 /var/log/drip-ai-bootstrap.log","docker logs --tail 100 vllm 2>&1"]' \
    --query Command.CommandId --output text)"
  # The waiter exits non-zero when the command fails; the invocation status below reports that failure.
  aws ssm wait command-executed --region "$region" --command-id "$command_id" --instance-id "$instance" || true
  aws ssm get-command-invocation --region "$region" --command-id "$command_id" --instance-id "$instance" \
    --query '[Status,StandardOutputContent,StandardErrorContent]' --output text
}

command="${1:-}"
assume_yes=no
case "$#:$command:${2:-}" in
  [01]:* | 2:tunnel:--no-link | 2:tunnel:--no-codex-link) ;;
  2:apply:--yes | 2:destroy:--yes | 2:start:--yes) assume_yes=yes ;;
  *) usage >&2; exit 2 ;;
esac
region=""
instance=""
case "$command" in
  apply) apply ;;
  destroy) destroy ;;
  start)
    resolve_instance
    aws ec2 start-instances --region "$region" --instance-ids "$instance" --output text
    aws ec2 wait instance-running --region "$region" --instance-ids "$instance"
    ;;
  stop)
    resolve_instance
    unlink_all_clients
    aws ec2 stop-instances --region "$region" --instance-ids "$instance" --output text
    ;;
  status)
    resolve_instance
    aws ec2 describe-instances --region "$region" --instance-ids "$instance" \
      --query 'Reservations[0].Instances[0].[InstanceId,InstanceType,State.Name]' --output text
    ;;
  logs)
    resolve_instance
    remote_logs
    ;;
  tunnel)
    case "${2:-}" in
      --no-link) DRIP_AI_LINK_CLIENTS=none ;;
      --no-codex-link) DRIP_AI_CODEX_LINK=0 ;;
    esac
    resolve_instance
    served_model="$(tf_output ec2 served_model_name)" || served_model=""
    context_window="$(tf_output ec2 max_model_len)" || context_window=""
    tunnel_with_link "$served_model" "$context_window" instance_running port_forward
    ;;
  help | -h | --help) usage ;;
  *) usage >&2; exit 2 ;;
esac
