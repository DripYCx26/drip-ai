---
name: model-teardown
description: Stops or destroys the drip-ai GPU model server on AWS, on a SageMaker endpoint or EC2, and reports what still bills. Use when asked to stop, pause, shut down, save money, tear down, or destroy the model server. Not for deploying; use model-deploy.
---

# Model teardown

Ends or pauses GPU spend for drip-ai. It does not deploy or change the model.

## When to Activate

- "Stop the server", "pause it overnight", "how much is running", "tear it all down".
- Do not trigger to deploy or swap a model; use `model-deploy`.

## Core Concepts

- A SageMaker endpoint has no stop. It bills from creation until deletion, about $72.80 an hour on `ml.p5en.48xlarge`, so the only off switch is `bin/drip-ai sagemaker stop`, which destroys the sagemaker root after the user types `yes`. It deletes the endpoint, config, model, role, and log group, and first restores the linked coding clients' settings. The S3 weights and the API key remain. Bringing it back is a full deploy with `model-deploy`.
- `bin/drip-ai ec2 stop` is reversible: GPU billing ends, but the EBS volume still bills, and `bin/drip-ai ec2 start` resumes it. `bin/drip-ai ec2 destroy` deletes the instance and its volume.
- The fetch stack costs nothing while idle; destroying it is optional. Model folders under `s3://<models bucket>/models/` keep billing storage (about $0.023 per GB-month) until the prefix is deleted by hand, and the user names that deletion.
- Destroying the shared stack deletes the API key (every client config then needs a new key) and fails while the models bucket holds weights. Destroying `bootstrap/` is blocked by `prevent_destroy` on the state bucket. Do either only on the user's explicit request.
- One user at a time: `sagemaker stop` refuses an endpoint another identity started unless `--force` and the typed endpoint name. Someone else's endpoint is theirs to stop.
- Run Terraform only through `bin/drip-ai` or the `tf` helper in `shared/lib/common.sh`. The `drip-ai` resource group lists every tagged resource; use it to confirm nothing is left running.

## Procedure

1. Run `bin/drip-ai sagemaker status` and `bin/drip-ai ec2 status`, and report the state of each. Both are read-only.
2. To pause EC2: run `bin/drip-ai ec2 stop`.
3. To end SageMaker spend: confirm with the user, then run `bin/drip-ai sagemaker stop --yes` (or have them run it without `--yes` and type `yes`). If `status` names another identity, stop and ask the user to reach that person; pass `--force` only when the user says that person agreed. If the user declines the destroy, Codex is already unlinked; tell them `bin/drip-ai sagemaker tunnel` links it again. If SageMaker refuses to delete an endpoint that is still `Creating`, `stop` rechecks ownership and retries every 15 seconds for up to 2 hours. Keep it running; if it times out or is interrupted, check status and rerun it to finish cleanup. If Terraform can't reconcile a failed endpoint, `aws sagemaker delete-endpoint --endpoint-name <name>` on a `drip-ai-*` endpoint from `status` is the only sanctioned manual call.
4. To destroy EC2: confirm with the user, then run `bin/drip-ai ec2 destroy --yes`.
5. Optional: destroy the fetch stack with `bin/drip-ai tf fetch-model destroy`. Delete S3 weights only if the user names that as intended.
6. Run `aws resource-groups list-group-resources --group drip-ai --query 'ResourceIdentifiers[].ResourceArn'` and report what remains.

## Code Examples

Pass (SageMaker): "Stop it for the night" explains there is no pause, gets approval, has the user run `bin/drip-ai sagemaker stop`, then reports "Endpoint deleted; S3 weights still bill."

Pass (EC2): "Stop it for the night" runs `bin/drip-ai ec2 stop`, then reports "Instance stopped; the EBS volume and S3 weights still bill."

Fail: "Stop it for the night" destroys the shared stack. That deletes the API key, so every client config needs a new key.

## Anti-Patterns

- Emptying the S3 bucket to make a destroy pass without asking the user. Re-uploading 70B weights takes hours.
- Reporting "nothing is running" without checking the resource group.
- Reporting the S3 cost as zero after the endpoint is deleted. Fetched and uploaded models stay in S3 and bill storage.
- Telling the user a SageMaker endpoint is stopped or paused. It bills until it is deleted.
- Passing `--force` to stop an endpoint another identity started, without the user naming who agreed.
- Running `terraform destroy` directly. It skips the per-account backend and the endpoint-owner check; use `bin/drip-ai` or the `tf` helper.

## Related Skills

- `model-deploy`: bring the server back or change the model.
