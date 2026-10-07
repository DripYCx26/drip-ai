---
name: model-deploy
description: Deploys or swaps the drip-ai vLLM model server on AWS, on a SageMaker endpoint or EC2, including fetching Hugging Face weights, uploading local weights, and smoke-testing the API. Use when asked to deploy, launch, serve, swap, fetch, or upload a model, or to change instance size. Not for stopping or destroying; use model-teardown.
---

# Model deploy

Brings the drip-ai GPU server up with a chosen model and proves it answers on all three client APIs. It does not stop or destroy infra.

## When to Activate

- "Deploy Org/Model", "serve my weights in models/foo", "switch to a 70B model", "fetch this model", "upload this model".
- "Deploy on SageMaker", "use the p5en endpoint", "we have no EC2 GPU quota".
- Do not trigger to stop, pause, or tear down; use `model-teardown`.

## Core Concepts

- The model is chosen per run. On SageMaker, `bin/drip-ai sagemaker start` takes a model from S3 and passes it to Terraform as `-var`; `sagemaker/terraform/terraform.tfvars` holds tuning only. On EC2, `model_source` lives in `ec2/terraform/terraform.tfvars`, and a changed model replaces the instance.
- Every apply and every fetch spends money. The user approves that specific run; then you may run it with `--yes`, which prints the plan and costs and answers only that run's `yes`. The Hugging Face token prompt and the endpoint name `--take-over` asks for stay in the user's own terminal. You run only read-only commands unprompted: `status`, `status --json`, `list`, `models list`, `logs`, `watch`, `check`.
- Never handle the Hugging Face token. The user types it into the hidden prompt of `bin/drip-ai fetch`; never ask for it, read it, or pass it on a command line.
- One user at a time: the endpoint's `StartedBy` tag names who holds it. Someone else's endpoint is theirs to stop.
- Run Terraform only through `bin/drip-ai` or the `tf` helper in `shared/lib/common.sh`, because the backend bucket is generated per AWS account.
- A SageMaker endpoint bills from creation until deletion, about $72.80 an hour on `ml.p5en.48xlarge` (the `ml.p5e.48xlarge` price is unknown), and has no stop. `start` races one endpoint per `instance_types` entry, so up to 3 bill at once until the first is `InService`.
- Sizing and parsers live in `docs/models.md`; read it instead of guessing VRAM. Setup and permissions live in `docs/setup.md`, the race and its statuses in `docs/sagemaker.md`, and failures in `docs/troubleshooting.md`.

## Procedure

### SageMaker endpoint

1. Check the endpoint quota for each type in `instance_types` (default `ml.p5en.48xlarge` and `ml.p5e.48xlarge`): `aws service-quotas list-service-quotas --service-code sagemaker --region us-east-1 --query "Quotas[?QuotaName=='ml.p5en.48xlarge for endpoint usage'].Value"`. Each must be at least 1. If none is, stop and tell the user.
2. Run `bin/drip-ai sagemaker status`. No endpoint: continue. Yours: point the user at `bin/drip-ai sagemaker watch` and `tunnel`. Someone else's: stop and tell the user who holds it.
3. Optional tuning in `sagemaker/terraform/terraform.tfvars` (copy the `.example`): `instance_types` (1 to 3 types on one line, raced in order), `tensor_parallel_size` (the GPU count, 8 on p5, p5e, and p5en), `served_model_name`, and `max_model_len` (default 131072). The public profile map starts empty. Configure tool-call and reasoning parsers in the model's local profile in `shared/model-profiles/profiles.json`, keyed by its `config.json` `model_type`; `start` refuses a model with no profile or an architecture the pinned vLLM lacks, and prints the line to add. `vllm_extra_args` is only for overrides. Never put `model_source` there.
4. Get the model into S3 and check it with `bin/drip-ai models list`. For Hugging Face, public or gated, tell the user to run `bin/drip-ai fetch <Org/Name>` in their own terminal (Fargate about $0.40/h, at most about $2.45 at the 6 hour cap). For weights in `models/<folder>/`, tell the user to run `bin/drip-ai models upload <folder>`.
5. After the user approves, have them run `bin/drip-ai sagemaker start` (a picker) or `bin/drip-ai sagemaker start <name> [--no-manifest]`, with the hourly cost stated. `start` prints each lane's price and waits for their `yes`, then races the lanes for up to 3 hours, keeps the first `InService`, and deletes the others. `bin/drip-ai sagemaker deploy <folder>` does the upload and the start for a local folder.
6. Follow with `bin/drip-ai sagemaker status` and `watch`. If no H200 lane comes up, every lane fails, or `start` was killed mid-race, use `docs/troubleshooting.md`. Terraform's 60-minute timeout during a race is expected; `start` keeps polling.
7. Ask the user to run `bin/drip-ai sagemaker tunnel` (it holds a terminal and links their coding tools; they start a fresh client session afterwards), then run `bin/drip-ai smoke`.

### EC2 server

1. Check the quota: `aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA --region <region> --query Quota.Value`. It must cover the instance's vCPUs. If not, stop and tell the user.
2. Copy `ec2/terraform/terraform.tfvars.example` to `ec2/terraform/terraform.tfvars` and set `model_source`, `model_from_s3`, `instance_type`, `tensor_parallel_size`, `max_model_len`, and `vllm_extra_args` for the model's tool-call parser.
3. For local weights, run `bin/drip-ai models upload <name>` first and set `model_from_s3 = true`.
4. After the user approves, have them run `bin/drip-ai ec2 apply` and type `yes`. The instance starts billing.
5. Poll `bin/drip-ai ec2 logs` until the vLLM log shows the server is listening, or fails. If it logs `no config.json`, the weights never uploaded; upload them, then run `tf ec2 apply -replace=aws_instance.server` through the helper rather than leaving the instance billing.
6. Ask the user to run `bin/drip-ai ec2 tunnel`, then run `bin/drip-ai smoke`.

## Code Examples

Pass (a 70B BF16 model on 4 GPUs, `ec2/terraform/terraform.tfvars`):

```hcl
model_source         = "Org/Some-70B-Instruct"
instance_type        = "g6e.12xlarge"
tensor_parallel_size = 4
```

Fail (the GPU count doesn't match; vLLM exits at startup):

```hcl
instance_type        = "g6e.12xlarge"
tensor_parallel_size = 1
```

Pass (`sagemaker/terraform/terraform.tfvars`, tuning only):

```hcl
instance_types       = ["ml.p5en.48xlarge", "ml.p5e.48xlarge"]
tensor_parallel_size = 8
max_model_len        = 131072
vllm_extra_args      = [] # parsers come from shared/model-profiles/profiles.json
```

Pass (a gated model):

```text
"Deploy Org/Gated-Model" -> tell the user to run `bin/drip-ai fetch Org/Gated-Model` and type the token at the hidden prompt, then `bin/drip-ai sagemaker start`.
```

Fail (the token travels through chat and a command line):

```text
"Deploy Org/Gated-Model" -> ask the user to paste the token, then pass it to the fetch.
```

## Anti-Patterns

- Running `aws ec2 run-instances` by hand. The instance is untagged and Terraform can't see it, so it bills unnoticed.
- Calling the deploy done after `apply` succeeds. The apply only proves the instance or endpoint launched; only `smoke` proves the model serves.
- Running `bin/drip-ai setup`, `fetch`, `up`, `sagemaker start`, `sagemaker deploy`, or `ec2 apply`, with or without `--yes`, before the user approves that run, or handling the Hugging Face token. They bill, and only the user types the token.
- Passing `--take-over` because `start` refused. Another identity holds the endpoint; pass it only when the user names the person who agreed.
- Running `terraform -chdir=... apply` directly. It skips the per-account backend and the account guard; use `bin/drip-ai` or the `tf` helper.
- Creating or deleting SageMaker resources with the AWS CLI. The one exception is `aws sagemaker delete-endpoint --endpoint-name <drip-ai endpoint>` when `bin/drip-ai sagemaker stop` can't delete a failed one.
- Printing the API key into chat or logs. Use `bin/drip-ai key` only inside `export DRIP_AI_API_KEY="$(bin/drip-ai key)"`.

## Related Skills

- `model-teardown`: stop or destroy the server.
