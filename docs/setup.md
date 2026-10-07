# Get started

Set up your AWS access once, then create or join the account's shared stack. The tool list is in the [README](../README.md#prerequisites).

## Prepare your AWS access

1. Sign in with your own IAM Identity Center (SSO) or IAM user, never root keys. Configure a named profile with `aws configure sso` or `aws configure --profile <name>`, then `export AWS_PROFILE=<name>`.
2. Check who you are with `aws sts get-caller-identity`.
3. Pick one Region and keep it. Every command reads `DRIP_AI_REGION`, else `AWS_REGION`, else `AWS_DEFAULT_REGION`, else `us-east-1`, and the account's state and shared stack live there.
4. Check your backend's GPU quota in that Region under Service Quotas. The [SageMaker](sagemaker.md#before-you-start) and [EC2](ec2.md#before-you-start) pages name the quota.
5. Ask an admin for the [permissions](#permissions) below.

SSO logins expire, and a start can run for hours, so log in again right before `setup` and `sagemaker start`. [Troubleshooting](troubleshooting.md#credentials-expire) has the fix when Terraform loses them.

## Create the shared stack

The first person in an account and Region runs this once:

```bash
bin/drip-ai setup
```

It creates the Terraform state bucket `drip-ai-tfstate-<account>-<region>` when missing, prepares every Terraform root, and applies the shared stack: the models bucket `drip-ai-models-<account>`, the API key in SSM, and the `drip-ai` resource group. Terraform shows each plan and waits for `yes`. An admin role is simplest for this one run.

## Join a team

1. Use the same Region as the rest of the team.
2. If the team set `DRIP_AI_STATE_BUCKET_SUFFIX`, set the same value.
3. Run `bin/drip-ai setup`. The shared plan shows no changes, and you are ready to [run a model](running.md).

## Permissions

Terraform and the scripts run as you. Scope each permission to the `drip-ai` names where AWS allows it.

| For | Needs |
| --- | --- |
| Every member | `s3:ListBucket` on the state bucket `drip-ai-tfstate-<account>-<region>` (plus `-<suffix>` if you set `DRIP_AI_STATE_BUCKET_SUFFIX`), and `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on its `*.tfstate`, `*.tflock`, and `leases/*` objects; `s3:ListBucket`, `s3:GetObject`, `s3:PutObject` (with the multipart actions) on the models bucket `drip-ai-models-<account>`; `ssm:GetParameter` on `/drip-ai/api-key` |
| Hugging Face fetch | `ecs:RunTask` on the fetch task definition, plus `ecs:DescribeTasks`, `ecs:ListTasks`, `ecs:StopTask`, `ecs:TagResource`; `iam:PassRole` on `drip-ai-fetch-model-task` and `drip-ai-fetch-model-execution` with `iam:PassedToService` set to `ecs-tasks.amazonaws.com`; `ssm:PutParameter` and `ssm:DeleteParameter` on `/drip-ai/hf-token`; `logs:GetLogEvents` on `/drip-ai/fetch-model`; `ecr:DescribeImages` on `repository/vllm` in registry `763104351884` and `ec2:DescribeVpcs`, `ec2:DescribeSubnets` (Terraform reads these at plan time); Terraform rights for ECS, EC2 security groups, IAM roles (including `iam:TagRole` and `iam:ListRolePolicies`), CloudWatch Logs (including `logs:TagResource` and `logs:PutRetentionPolicy`), and SSM |
| SageMaker backend | `sagemaker:` Create, Delete, Describe, and Update for Model, EndpointConfig, and Endpoint, plus `sagemaker:AddTags` and `sagemaker:ListTags`; `sagemaker:ListEndpoints`; `sagemaker:InvokeEndpoint` on `endpoint/drip-ai-*` (this one action also authorizes the streaming call the proxy makes); `iam:` CreateRole, DeleteRole, GetRole, TagRole, ListRolePolicies, PutRolePolicy, DeleteRolePolicy, GetRolePolicy, and `iam:PassRole` on `role/drip-ai-sagemaker-execution`; `ecr:DescribeImages` on `repository/vllm` in registry `763104351884`; CloudWatch Logs on `/aws/sagemaker/Endpoints/drip-ai-*` (including `logs:TagResource` and `logs:PutRetentionPolicy`); `cloudwatch:GetMetricData` for the dashboard |
| EC2 backend | EC2 instance, security group, and volume actions; IAM role and instance profile rights with `iam:PassRole` on `role/drip-ai-model-server`; `ssm:StartSession`, `ssm:SendCommand`, and `ssm:GetCommandInvocation`; `ssm:GetParameter` on the AMI parameter Terraform reads |
| First setup | S3 bucket creation and configuration, `ssm:PutParameter` and tagging for the API key, and resource-group rights |

Every command also calls `sts:GetCallerIdentity`, which needs no permission.

## Share an account

Everyone in an account shares one set of stacks. These guards keep teammates from colliding:

- Shared state: each stack keeps its Terraform state in the state bucket with locking. A second apply on a stack someone is applying fails with `someone else is applying <root> right now` and names the holder; wait for them.
- Account guard: the state bucket name comes from your identity and Region. If this checkout was set up for another account or Region, `bin/drip-ai` refuses and names both buckets. `bin/drip-ai init --reconfigure` switches on purpose and never moves state.
- Endpoint owner: every SageMaker endpoint carries a `StartedBy` tag. `sagemaker start` refuses someone else's endpoint, or one whose tag it cannot read, unless you pass `--take-over` and type each endpoint name; `sagemaker stop` needs `--force` the same way. Taking over deletes their endpoints, so ask first.
- One EC2 server: the account has one EC2 server in shared state, so the next `ec2 apply` replaces whatever model is on it. Agree before you apply.
- Fetch lease: `fetch` holds the S3 lease `leases/fetch-model.json` for up to 30 minutes while the Hugging Face token is in SSM, because every fetch shares one token parameter. It also refuses while another identity's fetch is running, unless you pass `--allow-concurrent`.
- Client links are per machine: your tunnel links and unlinks only your own tools.

These guards stop accidents between people who trust each other, not attacks:

- Any member can change the `StartedBy` tag, an endpoint, a lease, or any state file, and `bin/drip-ai tf` skips the guards on purpose. Give state-bucket write access only to people you would trust with the whole account.
- Tampered state cannot install new code on your machine, because `bin/drip-ai` runs `terraform init` with `-lockfile=readonly`. Review lockfile changes in pull requests as you would code.
- The state bucket name is predictable, so the scripts check that your account owns it. If another account took the name, they stop; set `DRIP_AI_STATE_BUCKET_SUFFIX` (the same value for everyone in the account) and run `bin/drip-ai setup` again.
- Whoever runs the server chooses its weights, flags, and logs, so they can read every prompt and file you send while it is theirs.
- While a fetch runs, your Hugging Face token sits in `/drip-ai/hf-token`, where a member with the fetch permissions could read it. Use a fine-grained, read-only token.
- If members share one IAM role, anyone can pick the session name that `StartedBy` records. Prefer SSO users, or pin `sts:RoleSessionName` to `${aws:username}` in the role's trust policy.

## Use Linux or Windows

Any Linux distribution works with the tools from the [prerequisites](../README.md#prerequisites). On Windows, use WSL:

1. Clone the repo inside the WSL filesystem.
2. Install the tools inside WSL.
3. Run every command from a WSL shell, and run the tunnel where your coding tools run, because it edits their config under that shell's `$HOME`.

`.gitattributes` keeps LF line endings, so the scripts survive a Windows `core.autocrlf` checkout.
