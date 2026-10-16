# ec2

The EC2 backend: the script behind `bin/drip-ai ec2` and the Terraform root for one GPU instance running the pinned vLLM container on `127.0.0.1:8000`, with no ingress. How to use it is in [docs/ec2.md](../docs/ec2.md).

| Path | What it is |
| --- | --- |
| `server.sh` | `apply`, `destroy`, `start`, `stop`, `status`, `logs`, and `tunnel` (an SSM port-forward through `tunnel_with_link`) |
| `terraform/` | Terraform root `ec2` (state key `ec2.tfstate`), including the first-boot script `user-data.sh.tftpl` |
| `tests/` | Unit tests for `server.sh` |

## Terraform root

| Item | Value |
| --- | --- |
| Model | `model_source` in `terraform/terraform.tfvars` (or `TF_VAR_model_source`); `apply` refuses without it. `model_from_s3 = true` when it names a folder in the models bucket |
| Variables | Core settings plus model, vLLM, and token settings listed below. |
| Outputs | `instance_id`, `served_model_name`, `max_model_len`, `region` |
| Needs | The shared stack: the models bucket and the API key |
| Access | Egress-only security group; the instance role reads the models bucket and the API key (plus `hf_token_parameter` when set), and an explicit deny blocks every other SSM parameter; IMDSv2 with hop limit 1 |

The variables are `project`, `region`, `instance_type`, `root_volume_gb`,
`model_source`, `model_from_s3`, `served_model_name`, `tensor_parallel_size`,
`max_model_len`, `max_num_seqs`, `vllm_extra_args`, and
`hf_token_parameter`.

The boot script runs once per instance and reads the API key then. A changed model or vLLM flag changes the rendered boot script, so the next `apply` replaces the instance; a rotated key does not, which is why [docs/ec2.md](../docs/ec2.md#start-the-server) shows the `-replace` command.

`max_num_seqs` optionally sets vLLM's maximum active sequences. Leave it
`null` to use the pinned vLLM default. The launch rejects
`--no-enable-prefix-caching`, so vLLM V1 prefix caching remains enabled.

## Tests

The repo gate runs everything: `bin/drip-ai check`. These unit tests alone:

```bash
python3 -I -m unittest discover -s ec2/tests
```
