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

The boot script runs once. A later `apply` never replaces the instance for a changed model, flag, or key, because Terraform ignores boot-script changes. Change flags in place instead:

```bash
bin/drip-ai ec2 reconfigure -- --enable-auto-tool-choice --tool-call-parser <parser>
```

`reconfigure` rewrites `/etc/drip-ai/vllm.args` on the instance over SSM RunCommand (the model, the served name, the tensor parallel size, and the window from Terraform, then your flags, checked by the same rules as SageMaker's), then restarts vLLM there; it serves again once the model loads. With no flags it clears them. If the vLLM log shows `unrecognized arguments: <flag>`, rerun `reconfigure` without that flag. To change the model, or to take a rotated API key, replace the instance yourself:

```bash
bin/drip-ai tf ec2 apply -replace=aws_instance.server
```

## Use a capacity block

A capacity block reserves GPUs for a fixed window at an upfront fee. These commands only read:

```bash
bin/drip-ai capacity search --class h200           # offerings in your regions, earliest first
bin/drip-ai capacity held                          # blocks you hold, active or scheduled
bin/drip-ai capacity chain --class h200 --hours 72 # held blocks and offerings end to end, with each gap
```

`search` covers the config's `regions` (or `--regions all`), for `capacity.duration_hours` (24) starting within `capacity.window_days` (7). Each line names the offering id, zone, window, fee, and flags: `zone not-opted-in` (opt the zone in first), `needs subnet` (the default VPC has no subnet there), and whether the capacity-block vCPU quota covers the type. UltraServer offerings stay hidden unless `capacity.include_ultraservers` is true.

Buying spends the fee at once and cannot be undone. Only this command buys, and only with `--yes` and one offering id:

```bash
bin/drip-ai capacity buy <offering id> --region <region> --yes
```

To launch into the block, set these in `ec2/terraform/terraform.tfvars`, then `bin/drip-ai ec2 apply`:

```hcl
capacity_reservation_id = "cr-..."          # from capacity buy or capacity held
market_type             = "capacity-block"
availability_zone       = "us-east-1a"      # the block's zone
zone_subnets            = { "us-east-1a" = "subnet-..." }
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
| `bin/drip-ai ec2 reconfigure [-- FLAG...]` | Rewrites the instance's vLLM flags and restarts vLLM in place |
| `bin/drip-ai ec2 tunnel` | Serves `http://localhost:<port>` through SSM and links the clients in `clients.link` |
| `bin/drip-ai serve ec2` | The same tunnel, in the background |
| `bin/drip-ai capacity search\|held\|chain\|buy` | Capacity blocks; only `buy` spends |
