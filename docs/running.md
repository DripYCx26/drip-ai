# Run a model

Every backend runs a model the same way: put the weights in S3, start the server, open the tunnel, check it, and stop it when you finish. Only the start and stop commands differ by backend.

## Choose a backend

Pick the backend your account has GPU quota and capacity for.

| Topic | SageMaker | EC2 |
| --- | --- | --- |
| What runs | A managed SageMaker endpoint with AWS's vLLM image | One GPU instance you manage, running the pinned vLLM container |
| GPUs | 8x H200 or 8x H100 (`ml.p5en`, `ml.p5e`, `ml.p5`), or B200 and B300 (`ml.p6-b200`, `ml.p6-b300`) once quota is approved | L40S by default (`g6e`), any type your quota allows |
| Capacity | `start` races endpoints across your regions and GPU classes and keeps the first that comes up; capacity is not guaranteed | Needs EC2 GPU vCPU quota, or a [capacity block](ec2.md#use-a-capacity-block) |
| Pause | No stop: delete it, and start again later | `ec2 stop` and `ec2 start`; the disk bills while stopped |
| Commands | `bin/drip-ai sagemaker ...` | `bin/drip-ai ec2 ...` |
| Details | [SageMaker backend](sagemaker.md) | [EC2 backend](ec2.md) |

[Choose a model](models.md) says which models fit which instances.

## Put the weights in S3

Both backends load a model from `models/<name>/` in the models bucket. Get it there one of two ways.

From Hugging Face, public or gated:

```bash
bin/drip-ai fetch <Org/Name>
```

`fetch` previews the repo and its cost, asks for your Hugging Face token with hidden input (press Enter for a public model), and waits for `yes`. For a public model, `--yes` skips both questions. A Fargate task inside AWS then copies the model into S3 while you watch. Ctrl-C detaches and the copy keeps running; `bin/drip-ai fetch --watch <name>` reattaches. Type the token only at that prompt. Flags are in the [fetch-model README](../shared/fetch-model/README.md).

From a folder on your machine, in Hugging Face format (`config.json` and `*.safetensors` at the top):

1. Put it at `models/<folder>/`.
2. Run `bin/drip-ai models upload <folder>`. Rerun it to resume.

`bin/drip-ai models list` shows what the bucket holds. EC2 can also download straight from Hugging Face; see the [EC2 backend](ec2.md#choose-the-model).

## Start the server

Configure the selected model's serving flags before starting GPUs. SageMaker requires a [local model profile](models.md#add-a-profile) and derives the window and memory settings; read them first with `bin/drip-ai plan <name> --explain`. EC2 reads your `vllm_extra_args` directly. The public snapshot has no selected model or parser preset.

Follow your backend's page:

- SageMaker: `bin/drip-ai sagemaker start`, then read [the race](sagemaker.md#read-the-race).
- EC2: `bin/drip-ai ec2 apply`; see [start the server](ec2.md#start-the-server).

The server bills from here until you stop it.

## Open the tunnel

Run the tunnel in its own terminal and leave it open, with the command `start` printed for the winning endpoint:

```bash
bin/drip-ai sagemaker tunnel --region <region> --endpoint <name>   # or: bin/drip-ai ec2 tunnel
```

The tunnel serves the model at `http://localhost:<port>` (`ports.base`, default 8000) and accepts only requests that carry that region's API key. It prints `Context window: <n> tokens` and, once the model answers, links the coding tools in `clients.link` (opencode only by default; `bin/drip-ai config set clients.link=codex,claude,opencode` adds Codex and Claude Code). Ctrl-C closes the tunnel and restores their config. `bin/drip-ai serve` runs it in the background instead. [Connect clients](connect-clients.md) covers each tool.

## Check it works

In another terminal, with the tunnel open:

```bash
bin/drip-ai smoke
```

It calls every API the clients use and fails on an empty answer, raw reasoning or tool-call markup, or a tool call that does not come back as a proper `tool_calls` entry. A passing smoke test proves these API checks for the running model; it does not measure model quality or cover every client feature.

## Do it in one command

On SageMaker, `up` runs the start, the tunnel, and the check in one terminal:

```bash
bin/drip-ai up [name] [--yes]
```

It runs `sagemaker start`, stops there if the start fails or you decline it, then holds the tunnel open and runs `smoke` once your coding tools link. If your endpoint already runs, it connects to that one. Ctrl-C closes the tunnel, restores your tools' config, and reminds you that the endpoint bills until `bin/drip-ai sagemaker stop`.

## Approve a run with --yes

`setup`, `fetch`, `up`, `sagemaker start|stop|deploy`, and `ec2 apply|destroy` ask for confirmation before their infrastructure or copy changes. Review the Terraform plans and any cost estimates shown. `ec2 start` resumes an existing instance immediately without a confirmation prompt, so approve its cost before invoking it. Add `--yes` to answer that question for one run you already approved, so a script or an app can run it without that confirmation prompt. `ec2 start` also accepts `--yes`, but already starts without prompting. One `yes` on `sagemaker start` covers the whole race, including recreating capacity failures and deleting the losers. `--hours N` (1 to 24, default 8) caps the race on `start` only, not on `up`. `--yes` never answers these:

- The endpoint name that `--take-over` and `--force` ask you to type before touching someone else's endpoint.
- The Hugging Face token. `fetch --yes` refuses a private or gated model; run it without `--yes` and type the token at the prompt.

## Stop paying

Stop the server when you finish:

- SageMaker: `bin/drip-ai sagemaker stop` ends any race on this machine and deletes your endpoints in every configured region. The free models, configurations, and log groups stay for the next start; `bin/drip-ai doctor` lists the unused ones. Starting again is a full start.
- EC2: `bin/drip-ai ec2 stop` pauses the instance, and `bin/drip-ai ec2 destroy` deletes it and its disk.

These keep billing after a stop:

- The weights in S3, until you delete the `models/<name>/` folder by hand.
- An EC2 instance's disk, until `ec2 destroy`.

The fetch stack has no idle Fargate compute cost; retained logs and storage may still bill. [Troubleshooting](troubleshooting.md#check-what-you-spend) shows how to check what is still running.
