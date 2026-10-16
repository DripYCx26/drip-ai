# drip-ai

Run an open-weight language model in your own AWS account and connect it to Codex, Claude Code CLI, or opencode. `bin/drip-ai` handles model downloads, Terraform infrastructure, a local tunnel, and temporary client configuration.

Choose a managed SageMaker endpoint or an EC2 GPU instance. Both serve vLLM through `http://127.0.0.1:8000` while your tunnel is open. The infrastructure has no inbound security-group rules. You choose the model and its serving settings; this repository does not include weights or a preconfigured model.

## Prerequisites

Install these tools on the machine where you run drip-ai:

- Git, bash, `curl`, and Python 3.9 or newer as `python3`.
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), version 2.22 or newer.
- [Terraform](https://developer.hashicorp.com/terraform/install), version 1.11 or newer.
- Your own AWS credentials and permission to create the resources described in [setup](docs/setup.md#permissions).
- GPU quota in your chosen AWS Region. EC2 also needs the [Session Manager plugin](docs/ec2.md#before-you-start).

Use Linux, macOS, or [WSL on Windows](docs/setup.md#use-linux-or-windows). Client linking supports Codex, Claude Code CLI, and opencode; the Claude desktop app is not supported.

## Get started

This walkthrough uses SageMaker. For a smaller instance or a server you can pause, follow the [EC2 guide](docs/ec2.md).

### 1. Clone and sign in

Replace `your-profile` with your AWS profile:

```bash
git clone https://github.com/DripYCx26/drip-ai.git
cd drip-ai
export AWS_PROFILE=your-profile
export DRIP_AI_REGION=us-east-1
aws sts get-caller-identity
```

For SSO, sign in with `aws sso login --profile your-profile` first. [Setup](docs/setup.md) covers access, quotas, and sharing an account.

### 2. Create the shared infrastructure

```bash
bin/drip-ai setup
```

Review the Terraform plans and type `yes` to create the state bucket, models bucket, API key in SSM, and resource group. This creates billable storage, but does not start a GPU server.

### 3. Download your model into S3

Replace `Org/Name` with a model repository you have permission to use:

```bash
bin/drip-ai fetch Org/Name
bin/drip-ai models list
```

`fetch` shows the copy plan and estimated costs, asks for a Hugging Face token with hidden input, and starts a Fargate copy task after confirmation. Press Enter at the token prompt for a public model. Wait until the copy is complete before starting the server. You can also [upload local weights](docs/running.md#put-the-weights-in-s3).

### 4. Configure serving before starting GPUs

Read [model configuration](docs/models.md), then add a local entry to `shared/model-profiles/profiles.json` for your model's `model_type`. The public registry starts empty. Use the selected model's documentation and the pinned vLLM release to choose tool-call and reasoning flags. SageMaker refuses models without a profile or a supported architecture.

To tune instance types, context length, and other settings:

```bash
cp sagemaker/terraform/terraform.tfvars.example sagemaker/terraform/terraform.tfvars
```

Edit that file for your model and AWS quota. Confirm that the weights and context cache fit the GPUs. [SageMaker configuration](docs/sagemaker.md#before-you-start) explains the instance race and its costs.

### 5. Start and connect

```bash
bin/drip-ai sagemaker start Org/Name --no-watch
bin/drip-ai sagemaker tunnel
```

Review the race plan before typing `yes`. Several endpoints can bill at once while the race runs. Keep the start command running until it finishes cleaning up losing endpoints, then leave the tunnel terminal open.

The tunnel temporarily links installed coding clients. Restart Codex or opencode, or open a new Claude Code session, after it reports a successful link. [Client configuration](docs/connect-clients.md) explains credentials, restoration, and manual linking.

### 6. Test and shut down

In a second terminal:

```bash
bin/drip-ai smoke
```

A passing smoke test checks text responses across the client APIs and a parsed tool call. It does not establish model quality or compatibility with every client feature.

When you finish, close the tunnel with Ctrl-C and delete the endpoints:

```bash
bin/drip-ai sagemaker stop
bin/drip-ai status --json
```

**Closing the tunnel does not stop GPU billing.** SageMaker endpoints bill until deleted. S3 weights and other retained storage continue to bill after shutdown.

After configuration, `bin/drip-ai up Org/Name` combines start, tunnel, and smoke in one terminal. On macOS, double-click `drip-ai.command` for the same operations in a Terminal menu.

## Costs

The figures below are planning estimates recorded for us-east-1, not a current quote. Confirm AWS pricing, quotas, and the command's plan before approving a run. Taxes, data transfer, requests, logs, and other resources can add costs.

| Resource | Planning estimate | Billing lifecycle |
| --- | --- | --- |
| SageMaker `ml.p5en.48xlarge` | $72.80/hour | Treat creation as billing; continues until deletion |
| SageMaker `ml.p5.48xlarge` | $63.30/hour | Same |
| SageMaker `ml.p5e.48xlarge` | Unknown | Confirm price before including it in a race |
| EC2 GPU instance | Depends on instance and Region | Compute while running; disk also while stopped |
| Fargate fetch task | About $0.40/hour, plus networking | While the copy task runs |
| S3 weights | About $0.023/GB-month | Until the model objects are deleted |

Run `bin/drip-ai status --json` to inspect running resources, and use [Cost Explorer and resource tags](docs/troubleshooting.md#check-what-you-spend) to check actual spend. `--yes` answers a command's confirmation after its plan prints; use it only for a specific run you have already approved.

## Documentation

The guides cover the complete workflow:

- [Setup](docs/setup.md): AWS access, permissions, quotas, and shared accounts.
- [Run a model](docs/running.md): download or upload, start, tunnel, test, and stop.
- [SageMaker](docs/sagemaker.md): configuration, endpoint races, monitoring, and cleanup.
- [EC2](docs/ec2.md): configuration, Session Manager, and pause or deletion.
- [Lane runner](lane-runner/README.md): always-on ranked build lanes beside a configurable model endpoint.
- [Model configuration](docs/models.md): memory sizing, profiles, parsers, and context length.
- [Connect clients](docs/connect-clients.md): temporary configuration and recovery.
- [Troubleshooting](docs/troubleshooting.md): failed starts, credentials, cleanup, and spend.
- [Architecture](docs/architecture.md) and [decisions](docs/DECISIONS.md): components, contracts, and rationale.

For contributions, read [CONTRIBUTING.md](CONTRIBUTING.md) and run `bin/drip-ai check`. Report security issues as described in [SECURITY.md](SECURITY.md). For agent-assisted installation, read [AGENTS.md](AGENTS.md).

## License

Project code is available under the [MIT License](LICENSE). Model weights, containers, libraries, and other external software retain their own licenses and terms. Check those terms before downloading, serving, or redistributing them; the project license does not grant rights to model weights.
