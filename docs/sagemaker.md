# SageMaker backend

The SageMaker backend runs the model on a managed SageMaker real-time endpoint with AWS's vLLM image. A local proxy signs your calls to it with your AWS credentials, so the endpoint needs no open port.

Choose it when your account has SageMaker endpoint quota for 8-GPU instances. An endpoint has no stop: it bills from creation until you delete it. Cost estimates are in [costs](costs.md).

## Before you start

- Foundation: `bin/drip-ai setup` applies the SageMaker foundation, the execution role, in its region. Run it once per region you race in, with `DRIP_AI_REGION` set to that region.
- Quota: in Service Quotas, SageMaker, "<type> for endpoint usage" must be at least 1 for a type to race. `start` reads it live with `aws service-quotas list-service-quotas --service-code sagemaker` (you need `servicequotas:ListServiceQuotas`).
- Profile: the public registry is empty. [Add a local profile](models.md#add-a-profile) for your exact model release before starting GPUs.
- Weights: the model is in each raced region's models bucket ([put the weights in S3](running.md#put-the-weights-in-s3)), or its profile sets `"weights": "hf"` to download the fetched revision from Hugging Face on the endpoint (public models only, because the endpoint gets no token).
- Optional tuning: regions, GPU classes, lane counts, and the context window live in your drip-ai config ([configure drip-ai](configuration.md)).

## Plan before you spend

`bin/drip-ai plan <name> --explain` reads the model's `config.json` from S3 and prints, for every GPU class the race could use, the context window, decode context parallelism (DCP), KV-cache precision, MTP, and the vLLM flags, each with its reason and the layer it came from. It only reads. [Model configuration](models.md#how-the-plan-sizes-a-model) explains the rules.

## Start an endpoint

```bash
bin/drip-ai sagemaker start          # pick from a numbered list of models in S3
bin/drip-ai sagemaker start <name>   # or name one
bin/drip-ai up <name>                # or start, then tunnel and smoke, in one terminal
```

Before it creates anything, `start` checks:

1. Who owns any running `drip-ai` endpoint in the home region. Your own opens the dashboard; someone else's is refused (see [share an account](setup.md#share-an-account)).
2. That the S3 copy is complete.
3. That the model's architecture is one the pinned vLLM serves and its `model_type` has a [model profile](models.md#model-profiles). A refusal prints the line to add.
4. For each GPU class, whether it holds the model with room for the context window, skipping the others with a line such as `Skipping h100 (ml.p5.48xlarge): 800.0 GB of weights leave no KV-cache memory on ml.p5.48xlarge (8 x 85.5 GB)`.
5. For each configured region, the foundation, the weights, and the quota, skipping a region that lacks one: `us-east-2: skipped; the execution role drip-ai-sagemaker-execution-us-east-2 does not exist; ...`.
6. That the execution role may write each lane's log group, by simulating `logs:CreateLogStream` and `logs:PutLogEvents`.

Then it prints each region and class with its lanes and quota, the serving plan, and the approval block. This synthetic example races one class in one region:

```text
us-east-1 h200: 2 lanes (quota ml.p5en.48xlarge 2, ml.p5e.48xlarge 0)
Lanes, in order of preference; the first InService is kept and the others are deleted:
  1) drip-ai-h200-us-east-1-1  us-east-1  ml.p5en.48xlarge about $X/h
  2) drip-ai-h200-us-east-1-2  us-east-1  ml.p5en.48xlarge about $X/h
Retries: a lane that fails for lack of capacity is deleted and created again at once; any other failure ends that lane.
Cap: 8 hours without an InService lane, then every lane is deleted.
Worst case: up to 2 endpoints can bill at once if several get machines, about $X/h (each lane at its pool's highest list price).
Type yes to start this race:
```

`--yes` answers that prompt after the block prints. That one `yes` covers the whole race: creating lanes, recreating capacity failures, and deleting the losers. `--detach` races in the background and returns at once; `bin/drip-ai race status` follows it.

## Read the race

A lane is one endpoint for one GPU class in one region. Its InstancePools hold every type of that class with quota, in the order learning ranks them for the region (`h200` is `ml.p5en.48xlarge` and `ml.p5e.48xlarge`), so SageMaker places it on whichever type has a machine. The race runs at most `race.max_lanes` lanes (default 4) whose summed list price stays under `race.max_spend_per_hour_usd` when you set one. Every eligible region and class gets one lane first; extra lanes go to the highest placement chance.

`start` creates each lane's model, endpoint configuration, log group (kept 14 days), and endpoint through the AWS API, tagged `Project=drip-ai`, `ManagedBy=drip-ai`, `Stack=sagemaker`, `StartedBy=<identity>`, and `RunId=<run>`. Models and configurations are named by a hash of their content (`drip-ai-m-<hash>`, `drip-ai-c-<hash>-<n>`), so a later start with the same settings reuses them. Lanes are named `drip-ai-<class>-<region>-<n>`, such as `drip-ai-h200-us-east-2-1`. It prints a line whenever a status changes, with synthetic lines and timing shown below (not a startup estimate):

```text
Racing 3 lanes in us-east-2, us-east-1; polling every 15s. Ctrl-C deletes every lane of this run. Run id: 20261009T170203Z-a1b2c3
After 0 min: drip-ai-h200-us-east-2-1 Creating drip-ai-h200-us-east-1-1 Creating drip-ai-h200-us-east-2-2 Creating
After 31 min: drip-ai-h200-us-east-1-1 retry 1 (capacity)
After 52 min: drip-ai-h200-us-east-2-1 InService ...
drip-ai-h200-us-east-2-1 (h200 in us-east-2) is InService and wins; deleting the other lanes.
```

`start` identifies its lanes by ARN and creation time, and creates one only after SageMaker answers that the name is absent, so it never touches a same-named endpoint this run did not create. Each lane shows one of these statuses:

| Status | Meaning | What happens next |
| --- | --- | --- |
| `absent` | SageMaker does not list the endpoint yet, it is already deleted, or someone else started it | `start` is about to create it, or the lane is out of the race |
| `Creating` | SageMaker is finding capacity in the pool, booting the instance, and loading the weights | Treat it as billing; the worst-case estimate counts it |
| `InService` | The model is serving | The first lane here wins and bills until `sagemaker stop`; the others get deleted |
| `Failed` | The endpoint could not start | Bills nothing; see the retry rules below |
| `parked` | SageMaker refused it for quota | Stays out of the race |
| `Deleting` | SageMaker is removing it | Gone at a later poll |

A lane that fails for lack of capacity before its container wrote a log is deleted and created again at once, and `start` prints `After <n> min: <lane> retry <k> (capacity)`. Only a log stream created at or after the endpoint's `CreationTime` counts, because a reused endpoint name keeps the streams of earlier endpoints. Any other failure, or one after the container logged, is never relaunched: `start` reads the end of the log, records what it found, and prints `<lane> (<class> in <region>) failed: <reason> [<kind>] (logs: /aws/sagemaker/Endpoints/<lane>); not retrying it`, where `<kind>` is `unknown_flag`, `oom`, `kv_too_small`, `download`, or `health` when the log says so. A `Failed` endpoint of yours from an earlier run no longer blocks `start`: when the new plan's configuration differs, the race deletes it and creates the lane with the new one, printing `<lane> failed in an earlier run (<reason>) with configuration <old>, which has changed since; deleting it and creating the lane with <new>`; with the same configuration it prints `<lane> failed in an earlier run with this same configuration (<reason>); not relaunching it` and races the other lanes. If every lane ends, `start` deletes them, prints `Attempts per lane:` with `  <lane> (<class> in <region>): N created; last failure: <reason|none>` for each, and exits 1.

The first lane `InService` wins and `start` deletes the others. SageMaker deletes a lane that is still `Creating` only after that ends, so `start` prints `<lane> is Creating, and SageMaker deletes it only after that ends; asking again every 15s`. Deleting the losers is capped at 2 hours after the win; then `start` exits 1 and names what still bills.

`--hours N` caps the race in wall-clock time, because endpoints bill while a laptop sleeps: a whole number from 1 to 24, default `race.hours` (8). It is on `start` only, not on `up`. At the cap, `start` deletes every lane and exits 1 with the attempts per lane. Cleanup after Ctrl-C, the cap, or every lane failing has no time limit and ends with `Every lane of this run is deleted; nothing bills for them.`

### Expired credentials

If your AWS session expires mid-race, `start` pauses: it makes no change to AWS, prints `AWS credentials stopped working (...); the race is paused ... Run: aws login (or aws sso login)` once and again every 15 minutes, and runs `notify.argv` if you set one. It checks every minute and resumes once the same identity signs in. If another identity signs in, it stops without deleting anything and prints the lanes that may still bill and the resume command.

### Signals, kills, and resume

Closing the terminal keeps the race going unattended. Ctrl-C before a winner deletes every lane and waits out any still `Creating`. A second Ctrl-C prints `stopped; these endpoints of this run may still exist and bill until bin/drip-ai sagemaker stop: ...` and exits at once.

Every race writes a journal, `~/.local/state/drip-ai/runs/<run id>.json`, before each create and after it. If `start` is killed, `bin/drip-ai race resume <run id>` re-adopts the run's lanes by journal and `RunId` tag, creates nothing it can find, and races on; `bin/drip-ai race cancel <run id>` deletes them instead. Both ask first.

## How long it takes

Startup depends on capacity, download size, and model initialization. These are control-flow limits configured by the scripts, not measured deployment times:

| Phase | Time |
| --- | --- |
| Poll interval | 15 seconds; one `ListEndpoints` per region per poll |
| A lane looking for capacity | Up to `sagemaker.provision_timeout_seconds` (3600) in its pool |
| A capacity failure | The lane is deleted and created again at once |
| Deleting a losing lane | Retried until it leaves `Creating`, up to 2 hours after the win |
| Race limit | `--hours`, whole hours 1 to 24, default 8 |

Once a lane wins, `start` opens the dashboard; `--no-watch` returns instead, as `up` does. Open the [tunnel](running.md#open-the-tunnel) in another terminal with the command `start` prints.

## Watch the endpoint

These commands only read:

- `bin/drip-ai sagemaker watch [--endpoint NAME]` shows a live dashboard: state, estimated cost, an idle warning, usage, GPU, CPU, memory, proxy health, and recent logs. Ctrl-C closes only the view. For a pooled lane it shows the instance type and price as unavailable, because the endpoint configuration names the pool, not the type SageMaker placed.
- `bin/drip-ai sagemaker status` lists every `drip-ai` endpoint in the home and configured regions, its state, and who started it.
- `bin/drip-ai sagemaker logs [--endpoint NAME]` and `bin/drip-ai logs <endpoint> --region R [--follow]` print the endpoint's log.
- `bin/drip-ai race status` shows this machine's races and their lanes.
- `bin/drip-ai sagemaker list` lists the models in S3 and which one is serving.

Without `--endpoint`, `watch`, `logs`, and `tunnel` follow the one `InService` lane in the region and refuse with "race in progress" while several exist.

## Stop the endpoint

```bash
bin/drip-ai sagemaker stop
```

If a race runs on this machine, `stop` prints `Ending the race (pid N); it deletes its own lanes before it exits.`, waits up to 5 minutes, then deletes the lanes itself. If a race runs on another machine, `stop` refuses and names that machine. Then it unlinks your coding tools and deletes every endpoint of yours in the home and configured regions, plus the legacy names `drip-ai` and `drip-ai-<type>`, printing `Deleting endpoint <name> (<status>).` for each and waiting until each is gone. Someone else's endpoint, or an untagged one, is left alone with `Leaving endpoint <name> alone: ...`; `--force` and its typed name delete it too. If an endpoint is still in a transitional state such as `Creating`, `stop` retries every 15 seconds for up to 2 hours. An interrupted or unsuccessful stop names what may still bill.

`stop` deletes endpoints only. The free models, endpoint configurations, and log groups a race created stay, so the next start reuses them; `bin/drip-ai doctor` lists the ones nothing uses and `doctor --fix R4,R5 --yes` deletes them. The execution role stays for the next start. The weights in S3 and the API key stay.

## Command reference

These are all the SageMaker commands:

| Command | What it does |
| --- | --- |
| `bin/drip-ai up [name] [--yes]` | Runs `sagemaker start --no-watch`, then `sagemaker tunnel --smoke` on the winner in the foreground; stops if the start fails |
| `bin/drip-ai sagemaker start [name] [--hours N] [--no-manifest] [--take-over] [--unchecked-model] [--yes] [--no-watch \| --detach]` | Starts the race. `--hours N` caps it. `--no-manifest` accepts a folder from `models upload`. `--unchecked-model` skips the profile and architecture checks with a warning. `--take-over` deletes someone else's endpoints first, after you type each name. `--yes` answers the race's `yes`. `--no-watch` returns instead of opening the dashboard; `--detach` races in the background |
| `bin/drip-ai sagemaker deploy <folder> [--take-over] [--yes]` | Uploads `models/<folder>/` and starts it with `--no-manifest`; refuses before uploading while any endpoint runs |
| `bin/drip-ai sagemaker tunnel [--smoke] [--region R] [--endpoint NAME] [--detach]` | Serves the endpoint on `127.0.0.1` through the local proxy and links the clients in `clients.link`; [connect clients](connect-clients.md) lists its other flags |
| `bin/drip-ai serve [--region R] [--endpoint NAME]` | The same tunnel, in the background; `serve list` and `serve stop --port P` manage them |
| `bin/drip-ai sagemaker watch [--endpoint NAME]` | Live dashboard |
| `bin/drip-ai sagemaker status` | Every `drip-ai` endpoint and its owner |
| `bin/drip-ai sagemaker logs [--endpoint NAME]` | The last 30 minutes of the endpoint log |
| `bin/drip-ai sagemaker list` | Models in S3 and the one serving |
| `bin/drip-ai race status\|resume\|cancel` | Follow, resume, or end a race this machine started |
| `bin/drip-ai sagemaker stop [--force] [--yes]` | Ends a race on this machine and deletes your endpoints in every configured region; `--force` and each typed name for someone else's |

Through the proxy, a request body can be at most 6 MiB, and SageMaker's time limit can cut a very long response; [connect clients](connect-clients.md) has the details.
