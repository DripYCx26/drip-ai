# SageMaker backend

The SageMaker backend runs the model on a managed SageMaker real-time endpoint with AWS's vLLM image. A local proxy signs your calls to it with your AWS credentials, so the endpoint needs no open port.

Choose it when your account has SageMaker endpoint quota for 8-GPU H100 or H200 instances. An endpoint has no stop: it bills from creation until you delete it. Cost estimates are in the [README](../README.md#costs).

## Before you start

- Quota: in Service Quotas, SageMaker, "<type> for endpoint usage" must be at least 1 for each type you race, because `start` creates one endpoint per type at once.
- Profile: the public registry is empty. [Add a local profile](models.md#add-a-profile) with the settings for your exact model release before starting GPUs.
- Weights: the model is in S3 ([put the weights in S3](running.md#put-the-weights-in-s3)) and fits at least one configured instance type; `start` skips types too small for the weights ([choose a model](models.md)).
- Optional tuning: copy `sagemaker/terraform/terraform.tfvars.example` to `terraform.tfvars` beside it (gitignored) to change `instance_types`, `served_model_name`, `max_model_len`, or `vllm_extra_args`. The model is never set there; `start` chooses it per run.

`instance_types` lists 1 to 3 types on one line, raced in order. The default is `["ml.p5en.48xlarge", "ml.p5e.48xlarge"]`, both 8x H200. Adding `"ml.p5.48xlarge"` (8x H100) gives the race another capacity option. `start` skips any type too small for the weights. Each additional lane can add to startup spend.

## Start an endpoint

```bash
bin/drip-ai sagemaker start          # pick from a numbered list of models in S3
bin/drip-ai sagemaker start <name>   # or name one
bin/drip-ai up <name>                # or start, then tunnel and smoke, in one terminal
```

Before it creates anything, `start` checks:

1. Who owns any running `drip-ai` endpoint. Your own opens the dashboard; someone else's is refused (see [share an account](setup.md#share-an-account)).
2. That the S3 copy is complete, and which types hold it in 90% of their GPU memory. It skips the others with a line such as `Skipping ml.p5.48xlarge: 800.0 GB is over 90% of its 640 GB GPU memory`, and refuses when none can. This is a synthetic sizing example, not a deployment measurement.
3. That the model's architecture is one the pinned vLLM serves and its `model_type` has a [model profile](models.md#model-profiles). A refusal prints the line to add.

Then it prints one lane per instance type with its hourly price and the worst case of all lanes billing at once, and waits for `yes`; `--yes` answers it after those lines print. After that, the race starts.

## Read the race

`start` creates one endpoint per lane, named `drip-ai-<type>` such as `drip-ai-p5-48xlarge`, and polls them every 15 seconds. It prints a line whenever a status changes, with synthetic timing shown below (not a startup estimate):

```text
After 0 min: drip-ai-p5en-48xlarge absent drip-ai-p5e-48xlarge absent drip-ai-p5-48xlarge absent
After 1 min: drip-ai-p5en-48xlarge Creating drip-ai-p5e-48xlarge Creating drip-ai-p5-48xlarge Creating
After 23 min: drip-ai-p5en-48xlarge Creating drip-ai-p5e-48xlarge Creating drip-ai-p5-48xlarge InService
drip-ai-p5-48xlarge (ml.p5.48xlarge) is InService and wins; deleting the other lanes.
```

Each lane shows one of these statuses:

| Status | Meaning | What happens next |
| --- | --- | --- |
| `absent` | SageMaker does not list the endpoint yet, it is already deleted, or someone else started it | Terraform is still creating the model and configuration, or the lane is out of the race |
| `unchecked` | Listed, but its owner tag is not read yet | Read again at the next poll; never chosen or deleted until then |
| `Creating` | SageMaker is finding capacity, booting the instance, and loading the weights | Treat it as billing; the worst-case estimate counts it |
| `InService` | The model is serving | The first lane here wins and bills until `sagemaker stop`; the others get deleted |
| `Failed` | The endpoint could not start | Bills nothing; `start` prints its `FailureReason` and log group when no lane wins |
| `Deleting` | SageMaker is removing it | Gone at a later poll |

The first lane `InService` wins, with ties going to the earlier type in `instance_types`. If every lane fails, or none is `InService` after 3 hours, `start` deletes every lane and exits with the command to rerun. Ctrl-C before a winner deletes every lane; a second Ctrl-C exits at once and names what still bills.

SageMaker refuses to delete an endpoint while it is `Creating`. A losing lane therefore lingers until it leaves `Creating`, and `start` asks again every poll. Keep `start` running until it reports the other lanes gone.

## How long it takes

Startup depends on capacity, download size, and model initialization. These are control-flow limits configured by the scripts, not measured deployment times:

| Phase | Time |
| --- | --- |
| Poll interval | 15 seconds |
| A lane reaching `InService` | No guaranteed startup time |
| Terraform's wait | Stops after 60 minutes and usually reports a timeout during a race; this is expected, and the race keeps going |
| After a winner | `start` gives Terraform 5 minutes, then stops it and deletes the other lanes |
| Race limit | 3 hours with no winner |

When Terraform timed out, `start` warns that the winner is not in Terraform state. `sagemaker stop` still deletes it.

Once a lane wins, `start` opens the dashboard; `--no-watch` returns instead, as `up` does. Open the [tunnel](running.md#open-the-tunnel) in another terminal.

## Watch the endpoint

These commands only read:

- `bin/drip-ai sagemaker watch` shows a live dashboard: state, estimated cost, an idle warning, usage, GPU, CPU, memory, proxy health, and recent logs. Ctrl-C closes only the view.
- `bin/drip-ai sagemaker status` lists every `drip-ai` endpoint, its state, and who started it.
- `bin/drip-ai sagemaker logs` prints the last 30 minutes of the serving endpoint's log.
- `bin/drip-ai sagemaker list` lists the models in S3 and which one is serving.

`watch`, `logs`, and `tunnel` refuse with "race in progress" until exactly one lane is `InService`.

## Stop the endpoint

```bash
bin/drip-ai sagemaker stop
```

`stop` unlinks your coding tools, destroys the endpoint after you type `yes` (or pass `--yes`), deletes any of your `drip-ai` endpoints that Terraform lost track of, and waits until each is gone. The weights in S3 and the API key stay. If an endpoint is still in a transitional state such as `Creating`, `stop` retries every 15 seconds for up to 2 hours. An interrupted or unsuccessful stop names what may still bill; check status and rerun cleanup.

## Command reference

These are all the SageMaker commands:

| Command | What it does |
| --- | --- |
| `bin/drip-ai up [name] [--yes]` | Runs `sagemaker start --no-watch`, then `sagemaker tunnel --smoke` in the foreground; stops if the start fails |
| `bin/drip-ai sagemaker start [name] [--no-manifest] [--take-over] [--unchecked-model] [--yes] [--no-watch]` | Starts the race. `--no-manifest` accepts a folder from `models upload` instead of a complete `fetch` copy. `--unchecked-model` skips the profile and architecture checks with a warning. `--take-over` deletes someone else's endpoints first, after you type each name. `--yes` answers the race's `yes`. `--no-watch` returns instead of opening the dashboard, and exits 0 only when your endpoint exists |
| `bin/drip-ai sagemaker deploy <folder> [--take-over] [--yes]` | Uploads `models/<folder>/` and starts it with `--no-manifest`; refuses before uploading while any endpoint runs |
| `bin/drip-ai sagemaker tunnel [--smoke]` | Serves the endpoint at `http://localhost:8000` through the local proxy and links your coding tools; `--smoke` runs the smoke test once they link; [connect clients](connect-clients.md) lists its other flags |
| `bin/drip-ai sagemaker watch` | Live dashboard |
| `bin/drip-ai sagemaker status` | Every `drip-ai` endpoint and its owner |
| `bin/drip-ai sagemaker logs` | The last 30 minutes of the endpoint log |
| `bin/drip-ai sagemaker list` | Models in S3 and the one serving |
| `bin/drip-ai sagemaker stop [--force] [--yes]` | Deletes your endpoints; `--force` and each typed name for someone else's; `--yes` answers the destroy's `yes` |

Through the proxy, a request body can be at most 6 MiB, and SageMaker's time limit can cut a very long response; [connect clients](connect-clients.md) has the details.
