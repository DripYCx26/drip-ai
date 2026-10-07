# drip-ai

Self-hosted open-weight LLMs on AWS, served by vLLM behind an API key, on a SageMaker endpoint or an EC2 GPU server, for use from opencode, Codex, and Claude Code. This page is the guide for agents; `README.md` and `docs/` are the guide for people.

## Layout

Each folder's `README.md` indexes that folder only.

| Path | What it is |
| --- | --- |
| `bin/` | `drip-ai`, the one entry point; it routes every command to the script that owns it |
| `drip-ai.command` | macOS double-click launcher: a Terminal menu over the everyday `bin/drip-ai` commands |
| `bootstrap/` | Terraform root for the per-account state bucket; keeps local state |
| `shared/` | Code every backend uses: `terraform/` (models bucket, API key), `fetch-model/` (Hugging Face to S3 job), `model-profiles/` (vLLM flags per model type), `modules/conventions/` (names, image pins), `lib/` (shell library, Codex link), `testing/`, `tests/`, `check.sh`, `models.sh`, `clients.sh` |
| `sagemaker/` | SageMaker endpoint: `start.sh`, `endpoint.sh`, `proxy.py`, `terraform/`, `tests/` |
| `ec2/` | EC2 GPU server: `server.sh`, `terraform/` with the boot script, `tests/` |
| `models/` | Local weight folders to upload; contents are gitignored |
| `docs/` | The user guide (setup, running, one page per backend, models, clients, troubleshooting), `architecture.md`, and `DECISIONS.md`; `README.md` links every page |
| `.claude/skills/` | Agent procedures; `.agents/skills` links here for Codex |

## Rules

- Tag every AWS resource `Project=drip-ai`. Each root's provider `default_tags` also sets `ManagedBy=terraform` and `Stack=<stack>`; never create resources outside Terraform, because untagged resources hide spend. The one exception is the ECS task `shared/fetch-model/fetch-model.sh` launches from the Terraform-owned task definition, which carries `Project=drip-ai` from creation.
- Never run `terraform apply` or `destroy` in any root, `bin/drip-ai setup`, `up`, `sagemaker start|stop|deploy`, `fetch`, or `ec2 apply|destroy|start` without the user approving that run, because a GPU instance, SageMaker endpoint, or Fargate task bills by the hour. An endpoint has no stop; it bills until deleted.
- Pass `--yes` only to a run the user approved, naming that command and its cost. It answers that run's own `yes` after the plan prints; it never types an endpoint name for `--take-over` or `--force`, nor the Hugging Face token.
- Run Terraform only through `bin/drip-ai` or the `tf` helper in `shared/lib/common.sh`, because the backend bucket is generated per AWS account at init and the helper refuses a checkout initialised for another account.
- Never take over or destroy an endpoint `StartedBy` another identity without that person's approval. `sagemaker start --take-over` and `sagemaker stop --force` exist for that; pass them only when the user names the person who agreed.
- Never add an ingress rule or a public port. Clients connect through `sagemaker tunnel` or `ec2 tunnel`, because vLLM's `--api-key` guards only `/v1` routes. The SageMaker proxy binds `127.0.0.1` only.
- Never print, commit, or log the API key or a Hugging Face token. Read them from SSM at use time. The user types the Hugging Face token only into the `bin/drip-ai fetch` prompt; never pass it on a command line. Codex and the Claude Code CLI get the key from a command that reads SSM, never from a file. opencode has no such hook, so its key sits in a 0600 file only while a tunnel is linked (see `docs/DECISIONS.md`), and `bin/drip-ai key` goes only into `export DRIP_AI_API_KEY="$(bin/drip-ai key)"`.
- Pin container images to an exact tag or digest at least one day old. The pins live in `shared/modules/conventions` (`vllm_image`, `sagemaker_image_tag`, `sagemaker_image_digest`); change them only with a dated `docs/DECISIONS.md` entry.
- Record any change to serving, networking, tagging, or state in `docs/DECISIONS.md`, newest first.

## Operate it

The full procedures are the `model-deploy` and `model-teardown` skills; this is the order. Commands marked approval bill money. Tell the user the command and its cost; once they approve that run, run it yourself with `--yes`, which prints the plan and costs and answers only its own `yes`. Two things stay in the user's own terminal: a private or gated model's Hugging Face token (`fetch --yes` refuses one), and the endpoint name `--take-over` and `--force` ask them to type.

1. Run `bin/drip-ai status --json` (or `sagemaker status`, `ec2 status`, and `models list`) first. It only reads. An endpoint `StartedBy` someone else is theirs; stop and tell the user.
2. Approval: `bin/drip-ai fetch <Org/Name> --yes` copies a public model to S3; for a gated one, ask the user to run it without `--yes`. Skip it when the model is already in S3 with `fetch` `complete`.
3. Approval: `bin/drip-ai sagemaker start <name> --yes --no-watch` races one endpoint per `instance_types` entry that holds the model, and returns once one serves.
4. Ask the user to run `bin/drip-ai sagemaker tunnel` in its own terminal; it holds that terminal. `bin/drip-ai up <name>` in that terminal does steps 3 to 5 at once.
5. Run `bin/drip-ai smoke`. Only a pass proves the model serves.
6. Approval: `bin/drip-ai sagemaker stop --yes` deletes the endpoints.

The race prints `After <n> min: <endpoint> <status> ...` whenever a status changes:

- `absent`: not listed yet, already deleted, or another identity's; nothing bills for it.
- `Creating`: SageMaker is getting capacity and loading the model; treat it as billing. It cannot be deleted until it leaves `Creating`, so keep `start` running until it reports the losers gone.
- `Failed`: the lane did not start and bills nothing; with no winner, `start` prints each `FailureReason` and exits 1.
- `InService`: serving; the first one wins and bills until `stop`.

A Terraform timeout after 60 minutes during a race is expected. To report what still bills, run both `status` commands and `aws resource-groups list-group-resources --group drip-ai --query 'ResourceIdentifiers[].ResourceArn'`, and name the S3 weights, which bill until deleted. Failures are in `docs/troubleshooting.md`.

## Gate

Run from the repo root before claiming a change works. It needs no AWS credentials and touches no state:

```bash
bin/drip-ai check
```

A live change is proven only by `bin/drip-ai smoke` with a tunnel open.
