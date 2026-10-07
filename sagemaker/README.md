# sagemaker

The SageMaker backend: the scripts behind `bin/drip-ai sagemaker`, the local proxy, and the Terraform root. How to use it is in [docs/sagemaker.md](../docs/sagemaker.md).

| Path | What it is |
| --- | --- |
| `start.sh` | `start`, `watch`, and `list`: picks a model from S3, runs the preflight, plans one endpoint per instance type whose GPU memory holds the model, races them, then shows the dashboard (`--no-watch` returns instead) |
| `endpoint.sh` | `status`, `logs`, `tunnel` (`--smoke` runs the smoke test once the clients link), `stop`, and `deploy`; also the lane, `StartedBy` ownership, and stop functions `start.sh` reuses |
| `proxy.py` | Stdlib-only proxy on `127.0.0.1` that checks the API key and signs each call to the endpoint |
| `terraform/` | Terraform root `sagemaker` (state key `sagemaker.tfstate`) |
| `tests/` | Unit tests for `start.sh`, `endpoint.sh`, and `proxy.py` |

## Race

The race runs in `start.sh` as one loop whose phases share each `ListEndpoints` poll:

1. `start` skips each `instance_types` entry whose GPU memory, at 90%, cannot hold the weights, and refuses when none can. It runs `terraform plan` with the rest as `-var instance_types` into a private temporary folder and refuses the plan unless it only creates one endpoint per entry.
2. After the typed `yes`, or `--yes`, it applies that saved plan in the background, in its own process group, logging to a file.
3. The first lane `InService` wins. Every other lane gets `DeleteEndpoint` each poll until it is gone. Terraform gets 5 minutes, then INT.
4. With no winner after 3 hours, or once every lane is `Failed` or absent after Terraform exits, every lane is deleted and `start` exits 1.
5. A signal before a winner deletes every lane; after a winner it finishes deleting the others and keeps the winner. A second signal exits at once and names what still exists.
6. A lane another identity started under the same name is never chosen or deleted.

`DRIP_AI_RACE_POLL_SECONDS`, `DRIP_AI_RACE_CAP_SECONDS`, and `DRIP_AI_RACE_APPLY_WAIT_SECONDS` override the 15-second poll, the 3-hour cap, and the 5-minute Terraform wait.

## Terraform root

| Item | Value |
| --- | --- |
| Per-run variables | `model_source`, `model_from_s3`, `started_by`, `vllm_model_args` (the model profile's flags), and `instance_types` (the configured types the model fits), passed by `start.sh` with `-var` |
| Tuning variables | `region`, `instance_types` (1 to 3, no repeats, on one line), `tensor_parallel_size`, `served_model_name`, `max_model_len`, `vllm_extra_args`, `inference_ami_version`, `volume_size_gb`, `startup_timeout_seconds`, `download_timeout_seconds`, set in the optional `terraform/terraform.tfvars` |
| Outputs | `region`; `tunnel` and `watch` read the served model name and `max_model_len` from the endpoint's model, because a race usually leaves its apply failed |
| Resources | One shared model and execution role `drip-ai-sagemaker-execution`; per instance type an endpoint configuration, an endpoint `drip-ai-<type>` tagged `StartedBy`, and its log group `/aws/sagemaker/Endpoints/drip-ai-<type>` |
| Needs | The shared stack: the models bucket and the API key |

The flag rules for `vllm_extra_args` are in [docs/models.md](../docs/models.md#add-vllm-flags).

## Proxy

`proxy.py` binds `127.0.0.1` only. It reads the API key from line 1 of stdin, signs each call with credentials from `aws configure export-credentials` (AWS CLI 2.9 or newer), and sends every call through `InvokeEndpointWithResponseStream`. It serves `GET /v1/models` itself and forwards `POST` to `/v1/chat/completions`, `/v1/completions`, `/v1/responses`, `/v1/messages`, and `/v1/messages/count_tokens`. Its limits are in [docs/connect-clients.md](../docs/connect-clients.md).

## Tests

The repo gate runs everything: `bin/drip-ai check`. These unit tests alone:

```bash
python3 -I -m unittest discover -s sagemaker/tests
```
