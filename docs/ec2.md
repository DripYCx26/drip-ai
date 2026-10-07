# EC2 backend

The EC2 backend runs the model on one GPU instance that you manage, in the pinned vLLM container. The instance has no open port; the tunnel reaches it through an SSM port-forward.

Choose it when your account has EC2 GPU quota, or when you want to stop the server overnight and start it again without a full redeploy. The account has one EC2 server, kept in shared Terraform state, so agree with your team before you apply.

## Before you start

- Quota: in Service Quotas, EC2, "Running On-Demand G and VT instances" needs 8 vCPUs for `g6e.2xlarge` or 48 for `g6e.12xlarge`.
- The Session Manager plugin, which the tunnel and logs use: install it for [macOS](https://docs.aws.amazon.com/systems-manager/latest/userguide/install-plugin-macos-overview.html), [Linux](https://docs.aws.amazon.com/systems-manager/latest/userguide/install-plugin-linux-overview.html), or [Windows](https://docs.aws.amazon.com/systems-manager/latest/userguide/install-plugin-windows.html), then check it with `session-manager-plugin --version`.

## Choose the model

The model and instance live in `ec2/terraform/terraform.tfvars` (gitignored):

1. Copy `ec2/terraform/terraform.tfvars.example` to `ec2/terraform/terraform.tfvars`.
2. Set `model_source`. Use a Hugging Face repo (`Org/Name`), or a folder in the models bucket together with `model_from_s3 = true`.
3. Size `instance_type`, `tensor_parallel_size`, and `max_model_len` with [model sizing](models.md#ec2-sizing).
4. Set `vllm_extra_args` to the tool-call and reasoning flags required by your exact model release. The public default is empty, and EC2 does not read SageMaker profiles.

For a gated Hugging Face repo, store a token under a name such as `/drip-ai/ec2-hf-token` and set `hf_token_parameter`. `/drip-ai/hf-token` belongs to `fetch`.

## Start the server

1. Run `bin/drip-ai ec2 apply` and type `yes`. The instance starts billing.
2. Run `bin/drip-ai ec2 logs` until vLLM reports it is serving. The first boot downloads the weights; duration depends on their size, throughput, and initialization.
3. [Open the tunnel](running.md#open-the-tunnel) with `bin/drip-ai ec2 tunnel`.

A changed model or vLLM flag replaces the instance on the next `apply`. A new API key does not, so replace it yourself after a key rotation:

```bash
bin/drip-ai tf ec2 apply -replace=aws_instance.server
```

## Pause or delete the server

- `bin/drip-ai ec2 stop` stops the instance. GPU billing ends; its disk still bills.
- `bin/drip-ai ec2 start` starts it again and waits until it runs.
- `bin/drip-ai ec2 destroy` deletes the instance and its disk after you type `yes`.

## Command reference

These are all the EC2 commands:

| Command | What it does |
| --- | --- |
| `bin/drip-ai ec2 apply` | Creates or updates the server from `terraform.tfvars`; waits for `yes` |
| `bin/drip-ai ec2 destroy` | Deletes the instance and its disk; waits for `yes` |
| `bin/drip-ai ec2 start` | Starts the stopped instance and waits until it runs |
| `bin/drip-ai ec2 stop` | Stops the instance |
| `bin/drip-ai ec2 status` | Instance id, type, and state |
| `bin/drip-ai ec2 logs` | The last 100 lines of the boot log and the vLLM log |
| `bin/drip-ai ec2 tunnel` | Serves `http://localhost:8000` through SSM and links your coding tools |
