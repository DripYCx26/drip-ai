# Install drip-ai with an agent

Use this guide to help a user set up drip-ai in their own AWS account. Follow the [README walkthrough](README.md#get-started) and the linked backend guides for configuration and command details.

## Prepare the machine and account

Install Git, Bash, `curl`, Python 3.9 or newer (`python3`), AWS CLI v2.22 or newer, and Terraform 1.11 or newer. EC2 also requires the Session Manager plugin. See [prerequisites](README.md#prerequisites) for installation links and supported operating systems.

Use the user's AWS profile and chosen Region. Verify the caller with `aws sts get-caller-identity`, then check [permissions](docs/setup.md#permissions) and GPU quota for [SageMaker](docs/sagemaker.md#before-you-start) or [EC2](docs/ec2.md#before-you-start). Use SSO or IAM credentials rather than root keys. If access or quota is missing, report the requirement before proceeding.

From the repository root, inspect the checkout and existing resources first:

```bash
bin/drip-ai check
bin/drip-ai status --json
```

`check` uses mocked AWS calls and temporary Terraform directories; it needs no AWS credentials and creates no cloud resources. Provider downloads need internet access. The public profile map must be `{}` for this check; `bin/drip-ai check --public` also refuses a catalog or default file that names a `model_type` or a repo id. `status --json` and `bin/drip-ai doctor` only read; an unreadable section is unknown, not proof that nothing exists.

## Choose and configure the model

Ask the user which model and backend they want. Confirm their rights to use the weights and read [model configuration](docs/models.md) before any billable command. The public source includes no selected model or parser preset.

For SageMaker, add a local profile to `shared/model-profiles/profiles.json` for the selected model's `model_type`. Regions, GPU classes, lane counts, and the clients a tunnel links live in the user's config file; set them with `bin/drip-ai config set` as [configure drip-ai](docs/configuration.md) describes, and read `bin/drip-ai plan Org/Name --explain` for the derived context window, parallelism, and flags. If an older `sagemaker/terraform/terraform.tfvars` still holds tuning, `start` refuses until `bin/drip-ai config import-tfvars` moves it. For EC2, copy `ec2/terraform/terraform.tfvars.example` and set `model_source`, sizing, and `vllm_extra_args`; the public argument default is `[]`. Keep the user's model choices and deployment settings private. Check architecture support, parser flags, GPU memory, context-cache memory, and quota using the [SageMaker](docs/sagemaker.md) or [EC2](docs/ec2.md) guide.

## Approve and run the installation

Explain each command's changes and expected cost before running it. The user's explicit approval of an exact run remains valid; do not ask them to approve that same run again. Approval does not cover a different model, extra race lanes, additional runs, or unrelated deletion. Use the [cost estimates](docs/costs.md) as planning guidance and confirm costs for the chosen Region and configuration.

Run `setup`, `fetch`, `up`, `sagemaker start|stop|deploy`, `race resume|cancel`, `ec2 apply|destroy|start|reconfigure`, `capacity buy`, `doctor --fix`, and Terraform apply or destroy only with approval for that run. `plan`, `config show`, `status`, `doctor` without `--fix`, `capacity search|held|chain`, `race status`, and `learn show|explain` only read. Use `--yes` on supported commands after approval; it answers only that run's confirmation after the plan prints. Run Terraform through `bin/drip-ai` or the `tf` helper in `shared/lib/common.sh` so account and state guards apply.

For an approved SageMaker installation, replace `Org/Name` with the user's selection and run each separately approved step:

```bash
bin/drip-ai setup --yes
bin/drip-ai fetch Org/Name --yes
bin/drip-ai models list
bin/drip-ai sagemaker start Org/Name --yes --no-watch
```

`setup` also applies the SageMaker execution role for its Region; run it with `DRIP_AI_REGION` set for each further Region the race uses. Skip fetching if the S3 copy is already complete; each Region needs its own copy unless the profile sets `weights: hf`. For local weights, follow [upload instructions](docs/running.md#put-the-weights-in-s3). For EC2, use `bin/drip-ai ec2 apply --yes` after its configuration and approval. SageMaker races up to `race.max_lanes` endpoints across the configured Regions and GPU classes, so multiple lanes can bill during startup; keep `start` running until losing lanes are gone. If it is killed, `bin/drip-ai race resume <id>` (approval) or `sagemaker stop` cleans up its lanes.

A private or gated download stays in the user's own terminal: they run `bin/drip-ai fetch Org/Name` without `--yes` and enter the token at the hidden prompt. Never request a token in chat or put secrets in command arguments, logs, or committed files. Use the existing SSM credential and client-link mechanisms described in [client configuration](docs/connect-clients.md).

If `StartedBy` names another identity, stop and report the owner. Use `--take-over` or `--force` only when the user names the other person who approved; the user must type each endpoint name in their own terminal. Never bypass ownership checks or open public inbound ports.

## Connect and verify

Have the user run the tunnel command `start` prints (`bin/drip-ai sagemaker tunnel --region R --endpoint NAME`) or `bin/drip-ai ec2 tunnel` in a dedicated terminal and leave it open. Tunnels temporarily link the clients in `clients.link`, opencode only by default; Codex and Claude Code link only when the user lists them. Follow [client configuration](docs/connect-clients.md) to restart the relevant client, then run `bin/drip-ai smoke` with the tunnel open. A passing smoke test proves its API and parsed tool-call checks for this running model; it does not establish model quality or every client feature.

## Shut down and report remaining costs

Closing the tunnel restores client settings but leaves the server billing. With approval for shutdown, run `bin/drip-ai sagemaker stop --yes` to delete the user's SageMaker endpoints in every configured Region, `bin/drip-ai ec2 stop` to pause EC2, or `bin/drip-ai ec2 destroy --yes` to delete EC2 and its disk. These commands unlink clients. SageMaker cannot pause; its stop command retries transitional endpoints every 15 seconds for up to 2 hours. If cleanup fails or is interrupted, check status and report endpoints that may still bill rather than claiming shutdown succeeded.

Finish with `bin/drip-ai status --json`, `bin/drip-ai doctor`, and the [resource and spend checks](docs/troubleshooting.md#check-what-you-spend). Report remaining S3 weights, retained storage and logs, and EBS disks on stopped EC2 instances. Delete retained data only when the user explicitly requests that deletion. Use [troubleshooting](docs/troubleshooting.md) for failed starts or cleanup, [SECURITY.md](SECURITY.md) for sensitive reports, and [LICENSE](LICENSE) for the project's license; model weights retain their own terms.
