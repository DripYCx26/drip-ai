# SageMaker CLI

Run a model on AWS SageMaker from your terminal.

- Download weights to S3, start endpoints, open a local tunnel, and stop them.
- Serve vLLM at `http://127.0.0.1:8000` with no inbound security-group rules.
- Connect Codex, Claude Code CLI, or opencode through temporary client settings.
- Use the [EC2 backend](docs/ec2.md) if you need an instance you can pause.
- Bring your own model and serving configuration. No weights or model presets are included.

The command is `bin/drip-ai`; existing scripts and AWS resource names use that name.

## Prerequisites

- Linux, macOS, or [WSL](docs/setup.md#use-linux-or-windows).
- Git, Bash, `curl`, and Python 3.9+ as `python3`.
- [AWS CLI v2.22+](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) and [Terraform 1.11+](https://developer.hashicorp.com/terraform/install).
- An AWS profile with the required [permissions](docs/setup.md#permissions) and GPU quota in your Region.
- For EC2, the [Session Manager plugin](docs/ec2.md#before-you-start).

## Get started

### 1. Clone and sign in

Replace `your-profile` with your AWS profile. For SSO, run `aws sso login --profile your-profile` first.

```bash
git clone https://github.com/DripYCx26/drip-ai.git
cd drip-ai
export AWS_PROFILE=your-profile
export DRIP_AI_REGION=us-east-1
aws sts get-caller-identity
```

### 2. Set up storage and download a model

```bash
bin/drip-ai setup
bin/drip-ai fetch Org/Name
bin/drip-ai models list
```

- Review each plan before typing `yes`.
- `setup` creates the state bucket, models bucket, SSM API key, and resource group. It does not start a GPU server.
- Replace `Org/Name` with a model you have permission to use. Enter a Hugging Face token at the hidden prompt, or press Enter for a public model.
- `fetch` runs a Fargate copy task. Wait for it to finish before starting an endpoint. For local weights, see [upload instructions](docs/running.md#put-the-weights-in-s3).

### 3. Configure serving

- Add your model's `model_type` to `shared/model-profiles/profiles.json` using the [model configuration guide](docs/models.md). The public profile map is empty.
- Check architecture support, parser flags, GPU memory, context length, and AWS quota before starting.
- To change instance types or serving settings, copy and edit the example below. See [SageMaker configuration](docs/sagemaker.md#before-you-start).

```bash
cp sagemaker/terraform/terraform.tfvars.example sagemaker/terraform/terraform.tfvars
```

### 4. Start and connect

```bash
bin/drip-ai sagemaker start Org/Name --no-watch
bin/drip-ai sagemaker tunnel
```

- The start command races configured instance types. Several endpoints can bill at once; review the plan and let it finish deleting losing endpoints.
- Leave the tunnel terminal open. It temporarily links installed coding clients.
- After a successful link, restart Codex or opencode, or open a new Claude Code session. The Claude desktop app is unsupported. See [client configuration](docs/connect-clients.md).

### 5. Test and stop

Run the smoke test in a second terminal while the tunnel is open:

```bash
bin/drip-ai smoke
```

The test checks API responses and a parsed tool call. Model quality and other client features need separate testing.

Close the tunnel with Ctrl-C, then delete the endpoints:

```bash
bin/drip-ai sagemaker stop
bin/drip-ai status --json
```

**Closing the tunnel does not stop GPU billing.** Endpoints bill until deleted.

For later runs, `bin/drip-ai up Org/Name` combines start, tunnel, and smoke. On macOS, `drip-ai.command` opens a Terminal menu.

## Costs

- Check AWS pricing for your Region and the command's plan before starting. `--yes` confirms only the current command after its plan prints.
- SageMaker endpoints bill until deleted, including overlapping endpoints during a race.
- EC2 compute bills while running; EBS disks also bill while stopped.
- Fargate copies, S3 weights, logs, requests, and networking can add charges. Retained storage bills after shutdown.
- Use `bin/drip-ai status --json` to inspect resources and [Cost Explorer](docs/troubleshooting.md#check-what-you-spend) to check spend.

## Documentation

- [Setup](docs/setup.md): permissions, quotas, and shared accounts.
- [Run a model](docs/running.md): download, start, connect, and stop.
- [SageMaker](docs/sagemaker.md) and [EC2](docs/ec2.md): backend configuration and cleanup.
- [Model configuration](docs/models.md): sizing, profiles, and parser flags.
- [Connect clients](docs/connect-clients.md): credentials, temporary settings, and recovery.
- [Troubleshooting](docs/troubleshooting.md): failed starts and cleanup.
- [Architecture](docs/architecture.md) and [decisions](docs/DECISIONS.md): implementation details.
- [Contributing](CONTRIBUTING.md), [security reports](SECURITY.md), and [agent setup](AGENTS.md).

## License

[MIT](LICENSE) for project code. Model weights and third-party software retain their own licenses.
