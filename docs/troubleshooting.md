# Troubleshooting

Find the symptom, then follow its fix. `bin/drip-ai sagemaker status` and `bin/drip-ai ec2 status` are always safe to run first.

## No GPU capacity

SageMaker capacity is not guaranteed even when your account has quota. [Startup limits](sagemaker.md#how-long-it-takes) explains polling and timeouts.

- Race more types: add `"ml.p5.48xlarge"` to `instance_types` in `sagemaker/terraform/terraform.tfvars` when the model fits 576 GB ([SageMaker sizing](models.md#sagemaker-sizing)).
- Check the quota: each raced type needs SageMaker endpoint quota of at least 1.
- Try EC2 if the account has EC2 GPU quota ([choose a backend](running.md#choose-a-backend)).

## Terraform times out during a race

Terraform stops waiting for an endpoint after 60 minutes and reports a timeout. During a race this is expected: `start` keeps polling for up to 3 hours. If the winner came up after Terraform gave up, `start` warns that it is not in Terraform state; `bin/drip-ai sagemaker stop` still deletes it.

## A losing lane will not go away

SageMaker refuses to delete an endpoint while it is `Creating`, so a losing lane lingers until it leaves `Creating`. `start` asks again every 15 seconds; keep it running until it reports the other lanes gone. If you already closed it, run `bin/drip-ai sagemaker stop` once `status` shows the lane `InService` or `Failed`. Until then it may bill.

## Every lane failed

`start` prints each lane's `FailureReason` and log group, deletes the lanes, and exits 1. The usual causes are a missing or wrong [model profile](models.md#model-profiles), a bad `vllm_extra_args` entry, a `max_model_len` too large for the free memory, or a model that does not fit. Fix it and run `start` again.

## start was killed mid-race

A closed laptop, a lost connection, or a killed process leaves the lanes billing. Run `bin/drip-ai sagemaker status`, then `bin/drip-ai sagemaker stop`, which deletes every `drip-ai` endpoint of yours and says "destroyed" only once each is gone.

## A coding tool does not use the model

- Start a fresh session after the tunnel says it linked, because a session that was open before keeps its old settings. For the Codex app, quit the whole app first; a new window is not enough.
- Check that the tunnel is still open; closing it restores your old config.
- [Connect clients](connect-clients.md) has per-tool checks and the commands for a link left behind by a crashed tunnel.

If you declined the destroy during `sagemaker stop`, your tools are already unlinked; rerun `bin/drip-ai sagemaker tunnel` to link them again.

## Credentials expire

An SSO login can expire during a long start. Run `aws sso login` before `setup` and `sagemaker start`. If Terraform reports a credential error while `aws` commands work, run `eval "$(aws configure export-credentials --format env)"` in that shell and retry.

## Someone else holds a lock or lease

- `someone else is applying <root> right now`: another member is applying that stack. Wait for them. If a crash left the lock behind and nobody is applying, run Terraform's `force-unlock <ID>` through `bin/drip-ai tf <root>` with the lock ID from the message.
- A fetch says the lease is in use: wait for the named holder. If the lease is unreadable and no fetch runs, delete it with `aws s3 rm s3://<state bucket>/leases/fetch-model.json`.
- The CLI warns that leases need AWS CLI 2.22 or newer: upgrade it.
- `fetch` fails with `ParameterAlreadyExists`: someone created `/drip-ai/hf-token` by hand. Save that token under another name, then run `aws ssm delete-parameter --name /drip-ai/hf-token`.

## Check what you spend

Every resource carries the tags `Project=drip-ai`, `ManagedBy`, and `Stack`.

1. See what exists: in the AWS console, open Resource Groups, then `drip-ai`. From a terminal, run `aws resource-groups list-group-resources --group drip-ai --query 'ResourceIdentifiers[].ResourceArn'`.
2. Turn on the cost tags once per account: `aws ce update-cost-allocation-tags-status --cost-allocation-tags-status TagKey=Project,Status=Active TagKey=Stack,Status=Active`. Billing sees a new tag up to 24 hours after a resource uses it.
3. In Cost Explorer, group by the `Project` or `Stack` tag to see drip-ai spend per stack.

`sagemaker watch` shows an estimated cost for the running endpoint. [Stop paying](running.md#stop-paying) lists what bills after a stop.
