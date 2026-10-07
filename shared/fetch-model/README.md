# fetch-model

Copies a Hugging Face model into the models bucket from inside AWS, so the weights never pass through your laptop and a gated model's token never reaches an endpoint or its Terraform state. Everyday use is in [docs/running.md](../../docs/running.md#put-the-weights-in-s3); this page is the full reference.

| Path | What it is |
| --- | --- |
| `fetch-model.sh` | The operator script behind `bin/drip-ai fetch`: preview, token prompt, lease, apply, launch, live progress |
| `copier.py` | The program the Fargate task runs (`plan` previews a repo anonymously, `copy` copies it, verified); stdlib plus the image's `botocore` |
| `terraform/` | Terraform root `fetch-model` (state key `fetch-model.tfstate`) |
| `tests/` | Unit tests for the script and the copier |

## Usage

```bash
bin/drip-ai fetch [ORG/NAME] [--revision REV] [--name NAME] [--all-files] [--allow-concurrent] [--yes]
bin/drip-ai fetch --watch NAME
```

| Argument | Effect |
| --- | --- |
| `ORG/NAME` | The Hugging Face repo; prompted for when omitted |
| `--revision REV` | Branch, tag, or commit; the copy pins the commit sha |
| `--name NAME` | The folder under `models/` in S3; defaults to `ORG/NAME` |
| `--all-files` | Keeps duplicate formats that are otherwise skipped when the repo has `*.safetensors` |
| `--allow-concurrent` | Copies alongside another identity's running fetch instead of refusing |
| `--yes` | Answers the apply and `Start the copy?` after their summaries print, and never opens a terminal. Skips the token for a public repo and refuses a private or gated one, whose token is typed only at the hidden prompt. Needs `ORG/NAME` |
| `--watch NAME` | Reattaches to a detached fetch; takes no other argument |

What a run does, in order:

1. Previews the repo on Hugging Face without a token and prints the file count, size, and cost.
2. Refuses while a fetch task another AWS identity started is running, or while any fetch task for a different model (yours included) is still starting, unless `--allow-concurrent`. `--allow-concurrent` skips only these checks.
3. Takes the S3 lease `fetch-model` (30 minutes, in your state bucket) and holds it from before the token prompt until the token parameter is deleted, because every fetch shares one token parameter. Another identity's live lease, or another live drip-ai process on this machine, blocks a new fetch; your own process refreshes it.
4. Prompts for your Hugging Face token with hidden input; Enter skips it for a public model. A gated or private repo needs one.
5. Applies the fetch stack after you type `yes`, then shows the model, revision, size, and cost and asks `Start the copy? [y/N]`; anything but yes exits with nothing billed. Then it writes the token to SSM from a 0600 file in a private temporary folder, deletes that file right after the call, and launches the tagged Fargate task. The token never appears on a command line.
6. Shows live progress from S3 object totals and the job log, with the S3 console link. Ctrl-C detaches and the copy keeps running; `--watch NAME` reattaches, and rerunning the same command resumes a partial copy.
7. Deletes the token parameter once the task has started and no other fetch task is still starting (ECS reads the token when a task starts), then releases the lease. If you detach early, `--watch` or a rerun deletes it; the script prints the `aws ssm delete-parameter` command to run by hand.

The copy ends with `drip-ai-fetch.json` in the model folder, which `bin/drip-ai sagemaker start` checks before it starts an endpoint. `DRIP_AI_WATCH_SECONDS` sets the refresh (default 10, minimum 5).

## Terraform root

| Item | Value |
| --- | --- |
| Variables | `project`, `region` (must match the shared stack) |
| Outputs | `fetch_model_cluster`, `fetch_model_task_definition`, `fetch_model_network`, `fetch_model_log_group`, `fetch_model_token_parameter`, `models_bucket`, `region` |
| Resources | ECS cluster `drip-ai-fetch-model`, log group `/drip-ai/fetch-model`, an egress-only TCP 443 security group, the write-only token parameter `/drip-ai/hf-token`, two IAM roles, and a task definition (8 vCPU, 16 GiB, 100 GiB ephemeral) on the pinned vLLM image |
| Needs | The shared stack applied first: it looks up the models bucket by name |

Idle, the stack costs nothing. Destroy it only if you want it gone; the weights in S3 stay, because the shared stack owns the bucket:

```bash
bin/drip-ai tf fetch-model destroy
```

## Tests

The repo gate runs everything: `bin/drip-ai check`. These unit tests alone:

```bash
python3 -I -m unittest discover -s shared/fetch-model/tests
```
