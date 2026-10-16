# Lane runner

This stack keeps the build-lane container running on one CPU EC2 instance. The
container owns ranked queue claiming, isolated worktrees, adapter builds, and
the source repository's verification gates. The stack supplies the runtime
environment and the least access needed to read the queue and model key.

The lane image is produced by the source repository's build-lane work. Set
`runner_image` to that image before planning. This stack does not duplicate the
queue consumer or copy either Victor pipeline's database.

## Build the runner image

The repository now includes a reproducible image recipe. Both source
repositories are private, so BuildKit named contexts supply clean archives of
local approved refs; the Dockerfile never clones anonymously and never accepts
a credential build argument. It imports the exact reviewed refs, creates a
local Git repository for the lane worktrees, installs the pinned OpenCode CLI,
and runs the existing `seeds/build_lanes.py` consumer.

From this repository root, after checking out the approved source refs:

```bash
KINDNESS_CHECKOUT=/path/to/drip-kindness
DRIP_SAFE_CHECKOUT=/path/to/drip-safe
KINDNESS_REF=$(git -C "$KINDNESS_CHECKOUT" rev-parse HEAD)
DRIP_SAFE_REF=$(git -C "$DRIP_SAFE_CHECKOUT" rev-parse HEAD)
SOURCE_CONTEXT=$(mktemp -d)
trap 'rm -rf "$SOURCE_CONTEXT"' EXIT
mkdir -p "$SOURCE_CONTEXT/kindness" "$SOURCE_CONTEXT/drip-safe"
git -C "$KINDNESS_CHECKOUT" archive "$KINDNESS_REF" | tar -xf - -C "$SOURCE_CONTEXT/kindness"
git -C "$DRIP_SAFE_CHECKOUT" archive "$DRIP_SAFE_REF" | tar -xf - -C "$SOURCE_CONTEXT/drip-safe"
docker build \
  --build-context kindness="$SOURCE_CONTEXT/kindness" \
  --build-context drip-safe="$SOURCE_CONTEXT/drip-safe" \
  --build-arg KINDNESS_REF="$KINDNESS_REF" \
  --build-arg DRIP_SAFE_REF="$DRIP_SAFE_REF" \
  -f lane-runner/Dockerfile \
  -t <approved-registry>/drip-ai-lane-runner:$KINDNESS_REF .
```

Pass that immutable image reference as `runner_image`. The archive contains
only committed files, so ignored local credentials and untracked files cannot
enter the image. The build has no credentials and does not push an image. The
named kindness archive must contain the source runner and its supported
`BUILD_LANE_COUNT` interface.
The image creates the checkout's initial commit so the source runner can add
its lane worktrees; `KINDNESS_REMOTE` keeps its HTTPS origin without embedding
a token. The runtime `GIT_PUSH_TOKEN` is read from the external SSM secret and
used only by Git's askpass helper.

The generated OpenCode config is written to `/opt/opencode.json`, and
`OPENCODE_CONFIG` points to it before the consumer starts. This is required
because the consumer's worktrees are siblings under `/opt`, not children of
`/opt/drip-kindness`. The `providers`/`package`/`settings.baseURL` shape and
`@opencode/ai/providers/openai-compatible` package follow the
[OpenCode v2 provider documentation](https://opencode.ai/v2/docs/providers/);
the config path follows the [OpenCode config documentation](https://dev.opencode.ai/docs/config/).

## Configuration

Copy `terraform/terraform.tfvars.example` to a private `terraform.tfvars` and
set these values:

| Variable | Meaning |
| --- | --- |
| `runner_image` | Image containing the existing build-lane runner. |
| `ranked_queue_uri` | S3 object holding the ranked build queue, such as `seeds/library/build_rank.json`. |
| `lane_count` | `BUILD_LANE_COUNT` in the container. It defaults to 30 and is the only lane concurrency setting. |
| `model_endpoint_url` | OpenAI-compatible base URL. A self-hosted vLLM URL and a hosted GLM URL use the same variable. |
| `model_name` | Model identifier sent to the endpoint. |
| `runner_instance_profile_arn` | Existing instance profile ARN with queue, SSM, and Session Manager permissions. |
| `model_endpoint_secret_parameter_arn` | Optional SSM ARN. The secret value is read at boot, never stored in Terraform variables. |
| `git_push_secret_parameter_arn` | Optional SSM SecureString ARN exposed as `GIT_PUSH_TOKEN` to the image's Git remote helper. |

`BUILD_LANES_ENABLED` is written as `1` by default. The image may read it to
stop the consumer when an operator explicitly sets `lane_runner_enabled =
false`. The source runner reads `BUILD_LANE_COUNT` and `BUILD_MODEL`; the
bootstrap fetches `BUILD_RANKED_QUEUE_URI` into the persistent checkout with
an atomic rename before the service starts. The container entrypoint repeats
that synchronization only when the local marker is missing or its URI
changes. Existing source budgets and send limits remain the runtime authority.

The image must contain the current kindness checkout with
`seeds/build_lanes.py`, `seeds/library/activation_queue.json`, and its
`make verify` dependencies, plus the `opencode-cli` binary. Its entrypoint
must synchronize `BUILD_RANKED_QUEUE_URI` into the checkout before running the
consumer. The kindness runner persists claims and results by updating its
checkout and pushes `lane/*` and `lanes/shadow`; the configured image must use
`GIT_PUSH_TOKEN` to authenticate that remote. The instance root volume keeps
the checkout between reboots. The current source runner updates claim and
result JSON locally and pushes lane branches plus `lanes/shadow`; it does not
commit the queue JSON after every claim. A replacement instance therefore
needs a source-side queue checkpoint or recovery procedure before it can
resume safely, in addition to the same image, queue object, and remote
credential.

The runner has no inbound security-group rule. Use SSM for host access. This
root does not create or modify IAM roles, instance profiles, S3 policies, or
SSM policies. The existing profile must allow `s3:GetObject` for the exact
`ranked_queue_uri` object, `ssm:GetParameter` for the configured parameter ARN,
and the standard Session Manager actions. If either secret ARN is configured,
the profile also needs `ssm:GetParameter` on those exact ARNs. The Docker
container uses the host network so the model endpoint and instance role are
reachable without a second credential path.

## Apply path

Review the plan and costs, then use the repository wrapper after Michael's
approval for this exact run:

```bash
bin/drip-ai tf lane-runner plan
bin/drip-ai tf lane-runner apply
```

This change was checked without applying Terraform, starting an instance,
starting a model endpoint, or buying capacity. The lane image, queue object,
endpoint, model access, and any source-repository write credentials still need
their owners' approval before a live run.

## Endpoint choices and vLLM settings

`model_endpoint_url` is deliberately a variable because the B300 capacity
block used during development ended on 2026-10-15 at 11:30 UTC. The runner
does not reserve or discover capacity. A self-hosted vLLM endpoint can use the
existing EC2 backend; an OpenAI-compatible GLM provider can use its HTTPS API.

The existing EC2 vLLM launch exposes `max_num_seqs` as an optional variable.
When it is unset, vLLM chooses its default. The launch never emits
`--no-enable-prefix-caching`, so vLLM V1 prefix caching remains enabled by its
default. Set `max_num_seqs` only after checking the selected model's memory.

## Cost references

These are current planning references for `us-east-1` or the provider page as
marked. AWS prices, availability, transfer, storage, and taxes can change.

| Option | Current planning rate |
| --- | ---: |
| Runner `m7i.2xlarge`, us-east-1 | `$0.4032` per instance-hour (`$294.34` per 730 hours) |
| AWS P6-B300 Capacity Block | `$129.168` per instance-hour (`$16.146` per GPU-hour) |
| Nebius L40S with Intel CPU | From `$1.55` per GPU-hour |
| Hosted GLM-5.3-Flash | `$0.15` per 1M input; `$0.50` per 1M output tokens |

The runner rate uses the current `BoxUsage:m7i.2xlarge` row published
2026-10-15. Source: [AWS EC2 price list (us-east-1)][aws-ec2-prices].
The B300 rate is today's public reference, not the ended development block's
historical price, which is **unknown**:
[AWS Capacity Blocks pricing](https://aws.amazon.com/ec2/capacityblocks/pricing/).
Nebius L40S rates depend on shape and region:
[Nebius prices](https://nebius.com/prices).
Hosted GLM has no fixed hourly rate, so use actual token consumption:
[Z.AI model pricing](https://docs.z.ai/guides/overview/pricing).

[aws-ec2-prices]: <https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.json>

The B300 row is a current public reference only. AWS says Capacity Block
prices depend on supply and demand and are charged up front; the development
reservation is not recreated by this stack.
