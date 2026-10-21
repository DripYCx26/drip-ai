# sagemaker

The SageMaker backend: the scripts behind `bin/drip-ai sagemaker` and `bin/drip-ai race`, the local proxy, and the Terraform root. How to use it is in [docs/sagemaker.md](../docs/sagemaker.md).

| Path | What it is |
| --- | --- |
| `start.sh` | `start`, `watch`, and `list`: picks a model from S3, checks the copy, has `lanes.py` plan the race, takes the race lease, asks for approval, then becomes `race.py`; `--watch [--endpoint NAME]` shows the dashboard |
| `lanes.py` | Plans a race, reading only: profile and architecture checks, a serving plan per GPU class, per-region foundation, weights, and quota, lane allocation, and each lane's model, endpoint configuration, and log group; `fit-limit` for the picker |
| `race.py` | Runs an approved race across regions: the lane state machine, error-class policy, credentials pause, run journal, and `status`, `resume`, `cancel` |
| `endpoint.sh` | `status`, `logs`, `tunnel` (`--region`, `--endpoint`, `--smoke`, `--detach`), `stop`, and `deploy`; also the lane, `StartedBy` ownership, and stop functions `start.sh` reuses; `stop` signals a race on this machine and refuses while one runs on another |
| `proxy.py` | Stdlib-only proxy on `127.0.0.1` that checks the API key and signs each call to the endpoint |
| `terraform/` | Terraform root `sagemaker` (state key `sagemaker.tfstate`): the per-region foundation |
| `tests/` | Unit tests for `start.sh`, `race.py` (`test_race.py`), `endpoint.sh`, `proxy.py`, and the foundation boundary; `fake_sagemaker.py` fakes SageMaker, CloudWatch Logs, and STS for them, and `start_fixtures.py` scripts a start |

## Race

`start.sh` gates, checks, and approves; `lanes.py` plans; `race.py` races. [docs/sagemaker.md](../docs/sagemaker.md#read-the-race) has what the user sees.

1. `start` reads the owner of every `drip-ai` endpoint in the home region, then checks the S3 copy.
2. `lanes.py plan` reads `config.json`, refuses an architecture the pinned vLLM lacks or a model with no profile, and sizes each GPU class with `shared/drip_ai/sizing.py`, skipping a class with no room for the window. For each configured region it checks the execution role (`iam get-role`), the weights, and the endpoint quota, then allocates up to `race.max_lanes` lanes, one per eligible region and class first and the rest by learned placement chance, within `race.max_spend_per_hour_usd`. It simulates the role's log permission on each lane's exact log group and refuses on a denial. It writes the plan to a private file and prints it.
3. `start` takes the `sagemaker-race` lease (15 minutes, refreshed every 3) in the home region's state bucket, so a second `start` refuses, asks for `yes` (or takes `--yes`), takes the lease again, and execs `race.py run`, which keeps the pid the lease names.
4. Each poll lists each region's endpoints once. A `PLANNED` lane ensures its model, configuration, and log group (each created only when SageMaker answers absent), journals its intent, describes its endpoint name, and creates it only on absent, with INT and HUP blocked. It follows the lane by ARN and creation time through `PROVISIONING` (`Creating`), `BOOTING` (a log stream created at or after the endpoint's `CreationTime` exists; checked every 5 minutes), and `IN_SERVICE`.
5. A `Failed` lane with a capacity reason and no log stream is deleted and planned again. Any other failure is read (`shared/drip_ai/logsignals.py`), recorded, deleted, and never relaunched. Quota parks a lane; throttling and timeouts back off and mark it unsure, and an unsure lane is adopted only when its `RunId` tag, configuration, and creation time are this run's. A lane whose name now holds someone else's endpoint leaves the race untouched. A `Failed` endpoint an earlier run of yours left under a lane's name (tagged `ManagedBy=drip-ai`) is deleted and the lane created again when its configuration has changed since, or it failed for capacity before any container started; with the configuration that failed, the lane leaves the race. Only `Could not find endpoint` counts as absent.
6. Expired credentials pause the race (no mutating call; `sts get-caller-identity` every minute; a reminder every 15 minutes) until the same base principal works again.
7. The first lane `IN_SERVICE` wins; every other lane is deleted once SageMaker allows, for up to `DRIP_AI_STOP_WAIT_SECONDS` (2 hours). The cap (`--hours`, wall-clock), a signal, a lost lease, or every lane out ends the race with no winner and no limit on cleanup.
8. The journal `~/.local/state/drip-ai/runs/<id>.json` records every lane; `race resume <id>` re-adopts and races on, `race cancel <id>` deletes.

`DRIP_AI_RACE_POLL_SECONDS`, `DRIP_AI_RACE_CAP_SECONDS`, `DRIP_AI_RACE_LEASE_SECONDS`, `DRIP_AI_STOP_WAIT_SECONDS`, and `DRIP_AI_RACE_VIRTUAL_CLOCK=1` (sleeps advance a virtual clock) are test hooks.

## Terraform root

| Item | Value |
| --- | --- |
| Variables | `project`, `region` |
| Outputs | `region`, `execution_role_arn` |
| Resources | The execution role `drip-ai-sagemaker-execution` (`-<region>` outside us-east-1) and its policy: logs on `/aws/sagemaker/Endpoints/drip-ai-*`, read on `models/*` of the region's bucket, pull on the pinned image repository; a precondition refuses the apply when the image tag moved off the pinned digest |
| Not here | Models, endpoint configurations, log groups, and endpoints, which `race.py` creates through the API |
| Needs | The shared stack in the same region: the models bucket and the API key |

`bin/drip-ai setup` applies it. Race tuning lives in the drip-ai config, not in `terraform.tfvars`; `bin/drip-ai config import-tfvars` moves an older file's tuning.

## Proxy

`proxy.py` binds `127.0.0.1` only. It reads the API key from line 1 of stdin, signs each call with credentials from `aws configure export-credentials` (AWS CLI 2.9 or newer), and sends every call through `InvokeEndpointWithResponseStream`. It serves `GET /v1/models` itself and forwards `POST` to `/v1/chat/completions`, `/v1/completions`, `/v1/responses`, `/v1/messages`, and `/v1/messages/count_tokens`. Its limits are in [docs/connect-clients.md](../docs/connect-clients.md).

## Tests

The repo gate runs everything: `bin/drip-ai check`. These unit tests alone:

```bash
python3 -I -m unittest discover -s sagemaker/tests
```
