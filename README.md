# drip-ai

Run an open-weight language model in your own AWS account and use it from Codex, Claude Code or opencode.

`bin/drip-ai` copies the weights into S3, starts vLLM on a SageMaker endpoint or an EC2 GPU instance, and opens a tunnel so the model answers on `http://127.0.0.1:8000`. Nothing is exposed to the internet, and there are no weights or model presets in this repository. You bring the model.

## Why

Hosted APIs are easy until you want a particular open-weight model, your data kept in your own account, or a bill you control. Running vLLM yourself is mostly the same chores each time: weights in S3, the right serving flags, a way in that is not a public port, and remembering to switch the GPUs off. This wraps those chores in one command line.

## Prerequisites

- Linux, macOS, or [WSL](docs/setup.md#use-linux-or-windows)
- Git, Bash, `curl` and Python 3.9 or newer
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) 2.22 or newer and [Terraform](https://developer.hashicorp.com/terraform/install) 1.11 or newer
- An AWS profile with the [permissions](docs/setup.md#permissions) in the docs, and GPU quota in your Region
- For EC2, the [Session Manager plugin](docs/ec2.md#before-you-start)

## Get started

This path uses SageMaker. For one smaller instance you can pause, see the [EC2 guide](docs/ec2.md).

```bash
git clone https://github.com/DripYCx26/drip-ai.git
cd drip-ai
export AWS_PROFILE=your-profile DRIP_AI_REGION=us-east-1

bin/drip-ai setup                      # state bucket, models bucket, API key; starts no GPUs
bin/drip-ai fetch Org/Name             # copy a Hugging Face model into S3
bin/drip-ai plan Org/Name --explain    # what it will run, and why
bin/drip-ai sagemaker start Org/Name   # race endpoints, keep the first that serves
bin/drip-ai sagemaker tunnel           # in its own terminal; leave it open
bin/drip-ai smoke                      # in another terminal
bin/drip-ai sagemaker stop             # when you are done
```

Every command that spends money shows its plan and waits for `yes`. Before the first start, add a [profile](docs/models.md#add-a-profile) with your model's parser flags to `shared/model-profiles/profiles.json`; SageMaker refuses a model without one. `bin/drip-ai up Org/Name` runs start, tunnel and smoke together, and on macOS `drip-ai.command` opens a small menu over the same commands.

**Closing the tunnel does not stop billing.** A SageMaker endpoint costs money until you delete it with `sagemaker stop`. See [costs](docs/costs.md).

## How it works

```text
coding tool -> localhost:8000 -> tunnel -> vLLM
                                  SageMaker: local proxy signs the request with your AWS credentials
                                  EC2:       SSM port-forward to the instance
```

Terraform builds the shared pieces (state, models bucket, API key in SSM) and the EC2 server. SageMaker endpoints are created through the API, tagged `Project=drip-ai`, and raced: `start` tries your configured Regions and GPU classes at once and deletes the losers. A small Python core works out the context window, parallelism and vLLM flags from the model's `config.json` and the GPUs, and `plan --explain` shows its reasoning. The [architecture](docs/architecture.md) page has the rest.

## Models and hardware

Any architecture the pinned vLLM release serves can work; the list in `shared/model-profiles/vllm-architectures.json` is compatibility data, not a promise that a given model boots. You are responsible for the weights' license. See [choose a model](docs/models.md) for sizing.

- **SageMaker:** 8-GPU instances in four classes: H200 (`ml.p5en`, `ml.p5e`), H100 (`ml.p5`), B200 and B300 (`ml.p6`, once quota is approved).
- **EC2:** `g6e` (L40S) by default, or any GPU type your quota allows, including [capacity blocks](docs/ec2.md#use-a-capacity-block).

## Coding clients

The tunnel links the clients named in `clients.link` while it runs and restores their settings when it closes. opencode is linked by default; add the others with `bin/drip-ai config set clients.link=codex,claude,opencode`. The Claude desktop app is not supported. Details are in [connect clients](docs/connect-clients.md).

## Configuration

Settings come in layers, from built-in defaults to your own file at `~/.config/drip-ai/config.json`. `bin/drip-ai config show --explain` prints every key with its value and where it came from, and `config set` changes your file. [Configure drip-ai](docs/configuration.md) lists the keys.

## Troubleshooting

`bin/drip-ai doctor` lists what drip-ai left running in AWS and on your machine. [Troubleshooting](docs/troubleshooting.md) covers capacity failures, stuck endpoints, expired credentials and checking spend.

## Docs

The [docs index](docs/README.md) lists every guide. To contribute, read [CONTRIBUTING.md](CONTRIBUTING.md) and run `bin/drip-ai check --public`. Report vulnerabilities as described in [SECURITY.md](SECURITY.md). If you use a coding agent to install this, point it at [AGENTS.md](AGENTS.md).

## License

Project code is under the [MIT License](LICENSE). Model weights, container images and the other software drip-ai uses keep their own licenses; check them before you download, serve or share anything. See [NOTICE](NOTICE).
