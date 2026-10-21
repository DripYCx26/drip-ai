# ec2

The EC2 backend: the script behind `bin/drip-ai ec2` and the Terraform root for one GPU instance running the pinned vLLM container on `127.0.0.1:8000`, with no ingress. How to use it is in [docs/ec2.md](../docs/ec2.md).

| Path | What it is |
| --- | --- |
| `server.sh` | `apply`, `destroy`, `start`, `stop`, `status`, `logs`, `reconfigure` (rewrites `/etc/drip-ai/vllm.args` over SSM and restarts vLLM), and `tunnel` (an SSM port-forward through `tunnel_with_link`) |
| `capacity.py` | Capacity blocks: `search`, `held`, and `chain` read offerings and held blocks across regions; `buy <offering> --region R --yes` is the only purchase path |
| `terraform/` | Terraform root `ec2` (state key `ec2.tfstate`), including the first-boot script `user-data.sh.tftpl` |
| `tests/` | Unit tests for `server.sh` and `capacity.py` |

## Terraform root

| Item | Value |
| --- | --- |
| Model | `model_source` in `terraform/terraform.tfvars` (or `TF_VAR_model_source`); `apply` refuses without it. `model_from_s3 = true` when it names a folder in the models bucket |
| Variables | `project`, `region`, `instance_type` (default `g6e.2xlarge`), `root_volume_gb`, `model_source`, `model_from_s3`, `served_model_name`, `tensor_parallel_size`, `max_model_len`, `max_num_seqs`, `vllm_extra_args`, `hf_token_parameter`, `capacity_reservation_id`, `market_type` (`""` or `capacity-block`), `availability_zone`, `zone_subnets` |
| Outputs | `instance_id`, `served_model_name`, `max_model_len`, `model_arg`, `tensor_parallel_size`, `region` |
| Needs | The shared stack: the models bucket and the API key |
| Access | Egress-only security group; the instance role reads the models bucket and the API key (plus `hf_token_parameter` when set), and an explicit deny blocks every other SSM parameter; IMDSv2 with hop limit 1 |

The boot script runs once per instance and reads the API key then. It writes the vLLM argv to `/etc/drip-ai/vllm.args` and starts the container through `/usr/local/bin/drip-ai-vllm`, which `reconfigure` reruns. Terraform ignores boot-script changes, so a changed model or flag never replaces the instance on `apply`; change flags with `reconfigure`, and replace the instance with the `-replace` command in [docs/ec2.md](../docs/ec2.md#start-the-server) for a new model or key.

`max_num_seqs` optionally sets vLLM's maximum active sequences; `null` keeps the pinned vLLM default. `reconfigure` rebuilds the argv from the model, the window, and the flags you pass, so pass `--max-num-seqs N` to it to keep the setting. The launch rejects `--no-enable-prefix-caching`, so vLLM V1 prefix caching stays on.

## Tests

The repo gate runs everything: `bin/drip-ai check`. These unit tests alone:

```bash
python3 -I -m unittest discover -s ec2/tests
```
