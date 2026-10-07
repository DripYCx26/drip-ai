# Offline proof for the model-fetch job. Every provider is mocked, so no run
# here reaches AWS, even the ones with command = apply.

# terraform test also loads an operator's terraform.tfvars in this directory.
# Pinning every variable keeps these runs independent of that file.
variables {
  project = "drip-ai"
  region  = "us-east-1"
}

# The provider checks ARN shape even when mocked, so ARNs that feed other
# resources get realistic defaults.
mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-east-1:123456789012:log-group:/drip-ai/fetch-model"
    }
  }

  mock_resource "aws_ecs_task_definition" {
    defaults = {
      arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/drip-ai-fetch-model:1"
    }
  }

  mock_resource "aws_security_group" {
    defaults = {
      id = "sg-0123456789abcdef0"
    }
  }
}

# A canary token value: a run fails if it leaks into the task definition.
override_resource {
  target = aws_ssm_parameter.hf_token
  values = {
    arn   = "arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/hf-token"
    value = "hf_CANARYtokenDoNotLeak0123456789"
  }
}

override_data {
  target = module.conventions.data.aws_caller_identity.current
  values = {
    account_id = "123456789012"
  }
}

override_data {
  target = module.conventions.data.aws_partition.current
  values = {
    partition = "aws"
  }
}

override_data {
  target = data.aws_s3_bucket.models
  values = {
    id  = "drip-ai-models-123456789012"
    arn = "arn:aws:s3:::drip-ai-models-123456789012"
  }
}

override_data {
  target = data.aws_vpc.default
  values = {
    id = "vpc-0123456789abcdef0"
  }
}

override_data {
  target = data.aws_subnets.default
  values = {
    ids = ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"]
  }
}

override_data {
  target = data.aws_ecr_image.vllm
  values = {
    id           = "sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"
    image_digest = "sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"
  }
}

run "rejects_moved_image_tag" {
  command = plan

  override_data {
    target = data.aws_ecr_image.vllm
    values = {
      id           = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
      image_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    }
  }

  expect_failures = [aws_ecs_task_definition.fetch_model]
}

# fetch-model.sh deletes the token parameter after each launch, so the next
# apply re-creates it and its `arn` is unknown at plan. The task definition and
# execution policy must still be known then, or every fetch makes a new
# task-definition revision. The file-level token override applies only at
# apply, so here the parameter's arn stays unknown; the log group is the one
# other computed input and is pinned at plan as an existing group would be.
run "token_arn_known_at_plan_while_parameter_is_recreated" {
  command = plan

  override_resource {
    target          = aws_cloudwatch_log_group.fetch_model
    override_during = plan
    values = {
      arn = "arn:aws:logs:us-east-1:123456789012:log-group:/drip-ai/fetch-model"
    }
  }

  assert {
    condition = jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0].secrets == [
      { name = "HF_TOKEN", valueFrom = "arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/hf-token" },
    ]
    error_message = "The HF_TOKEN valueFrom must be the constructed parameter ARN, known at plan."
  }

  assert {
    condition = flatten([
      for statement in jsondecode(aws_iam_role_policy.fetch_model_execution.policy).Statement :
      flatten([statement.Resource]) if anytrue([for action in flatten([statement.Action]) : startswith(action, "ssm:")])
    ]) == ["arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/hf-token"]
    error_message = "The execution policy's SSM resource must be the constructed parameter ARN, known at plan."
  }
}

# The runs below apply against the mocked provider so that ARNs are known.
# State persists from here on, so plan-only runs stay above.
run "task_definition_pins_digest_and_shape" {
  assert {
    condition = alltrue([
      aws_ecs_task_definition.fetch_model.cpu == "8192",
      aws_ecs_task_definition.fetch_model.memory == "16384",
      aws_ecs_task_definition.fetch_model.ephemeral_storage[0].size_in_gib == 100,
      aws_ecs_task_definition.fetch_model.network_mode == "awsvpc",
      aws_ecs_task_definition.fetch_model.requires_compatibilities == toset(["FARGATE"]),
      aws_ecs_task_definition.fetch_model.runtime_platform[0].cpu_architecture == "X86_64",
    ])
    error_message = "The task must be Fargate x86_64 with 8 vCPU, 16 GiB memory, and 100 GiB ephemeral storage."
  }

  assert {
    condition     = length(jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)) == 1
    error_message = "The task must have exactly one container."
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0].image == "763104351884.dkr.ecr.us-east-1.amazonaws.com/vllm@sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"
    error_message = "The fetch container must run the pinned vLLM image digest."
  }

  assert {
    condition = alltrue([
      jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0].name == "fetch",
      jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0].readonlyRootFilesystem == true,
      jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0].linuxParameters.capabilities.drop == ["ALL"],
      !contains(keys(jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0]), "command"),
      !contains(keys(jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0]), "privileged"),
    ])
    error_message = "The fetch container must be named fetch, read-only, drop every capability, and carry no command or privilege."
  }

  assert {
    condition = jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0].logConfiguration == {
      logDriver = "awslogs"
      options = {
        awslogs-group         = "/drip-ai/fetch-model"
        awslogs-region        = "us-east-1"
        awslogs-stream-prefix = "fetch"
      }
    }
    error_message = "Logs must go to /drip-ai/fetch-model with stream prefix fetch."
  }
}

run "entrypoint_embeds_copier_file" {
  assert {
    condition     = jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0].entryPoint == ["/opt/venv/bin/python3", "-I", "-c", file("../copier.py")]
    error_message = "The entry point must run shared/fetch-model/copier.py verbatim with an isolated python3."
  }
}

run "secret_is_arn_reference_not_value" {
  assert {
    condition = jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0].secrets == [
      { name = "HF_TOKEN", valueFrom = "arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/hf-token" },
    ]
    error_message = "HF_TOKEN must come only from the managed parameter's ARN."
  }

  assert {
    condition = alltrue([
      for entry in jsondecode(aws_ecs_task_definition.fetch_model.container_definitions)[0].environment :
      entry.name != "HF_TOKEN" && !strcontains(entry.value, "hf_CANARYtokenDoNotLeak0123456789")
    ])
    error_message = "No environment entry may carry the token or be named HF_TOKEN."
  }

  assert {
    condition     = !strcontains(aws_ecs_task_definition.fetch_model.container_definitions, "hf_CANARYtokenDoNotLeak0123456789")
    error_message = "The token value must not appear anywhere in the task definition."
  }

  assert {
    condition     = !strcontains(jsonencode(terraform_data.fetch_model.output), "hf_CANARYtokenDoNotLeak0123456789")
    error_message = "The token value must not reach the fetch handoff data."
  }
}

run "execution_role_reads_only_managed_token_arn" {
  assert {
    condition = sort(distinct(flatten([
      for statement in jsondecode(aws_iam_role_policy.fetch_model_execution.policy).Statement : flatten([statement.Action])
      ]))) == sort([
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetAuthorizationToken",
      "ecr:GetDownloadUrlForLayer",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "ssm:GetParameters",
    ])
    error_message = "The execution policy must grant exactly image pull, log write, and ssm:GetParameters."
  }

  assert {
    condition = flatten([
      for statement in jsondecode(aws_iam_role_policy.fetch_model_execution.policy).Statement :
      flatten([statement.Resource]) if anytrue([for action in flatten([statement.Action]) : startswith(action, "ssm:")])
    ]) == ["arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/hf-token"]
    error_message = "SSM reads must be limited to the managed /drip-ai/hf-token ARN."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.fetch_model_execution.policy).Statement :
      statement.Effect == "Allow" && !contains(keys(statement), "NotResource") && !contains(keys(statement), "NotAction")
    ])
    error_message = "The execution policy must use plain Allow statements only."
  }

  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_role.fetch_model_execution.assume_role_policy).Statement :
      statement.Principal == { Service = "ecs-tasks.amazonaws.com" } &&
      try(statement.Condition.StringEquals["aws:SourceAccount"], "") == "123456789012"
    ])
    error_message = "The execution role trust must admit only ecs-tasks.amazonaws.com from this account."
  }
}

run "execution_wildcard_only_get_authorization_token" {
  assert {
    condition = flatten([
      for statement in jsondecode(aws_iam_role_policy.fetch_model_execution.policy).Statement :
      flatten([statement.Action]) if contains(flatten([statement.Resource]), "*")
    ]) == ["ecr:GetAuthorizationToken"]
    error_message = "Only ecr:GetAuthorizationToken may use Resource \"*\"."
  }

  assert {
    condition = alltrue([
      for action in flatten([for statement in jsondecode(aws_iam_role_policy.fetch_model_execution.policy).Statement : statement.Action]) :
      !strcontains(action, "*")
    ])
    error_message = "The execution policy must name every action; no action wildcards."
  }
}

run "task_role_exact_s3_actions_no_delete" {
  assert {
    condition = sort(distinct(flatten([
      for statement in jsondecode(aws_iam_role_policy.fetch_model_task.policy).Statement : flatten([statement.Action])
    ]))) == sort(["s3:AbortMultipartUpload", "s3:GetObject", "s3:ListBucket", "s3:PutObject"])
    error_message = "The task policy must grant exactly list, get, put, and abort-multipart; no delete, ssm, or iam."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.fetch_model_task.policy).Statement :
      statement.Effect == "Allow" && !contains(keys(statement), "NotResource") && !contains(keys(statement), "NotAction") &&
      alltrue([
        for resource in flatten([statement.Resource]) :
        resource == (statement.Action == ["s3:ListBucket"] ? "arn:aws:s3:::drip-ai-models-123456789012" : "arn:aws:s3:::drip-ai-models-123456789012/models/*")
      ])
    ])
    error_message = "ListBucket must target the bucket and object actions only models/*, in plain Allow statements."
  }

  # HeadObject on a missing key returns 403 instead of 404 unless ListBucket is
  # unconditioned, which would break the copier's resume check.
  assert {
    condition = [
      for statement in jsondecode(aws_iam_role_policy.fetch_model_task.policy).Statement :
      statement if contains(flatten([statement.Action]), "s3:ListBucket")
      ] == [{
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::drip-ai-models-123456789012"
    }]
    error_message = "s3:ListBucket must be one unconditioned statement on the bucket ARN alone."
  }
}

run "task_trust_has_source_account_and_arn" {
  assert {
    condition = jsondecode(aws_iam_role.fetch_model_task.assume_role_policy).Statement == [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = "123456789012" }
        ArnLike      = { "aws:SourceArn" = "arn:aws:ecs:us-east-1:123456789012:*" }
      }
    }]
    error_message = "The task role trust must admit only ECS tasks in this account and region."
  }
}

run "security_group_no_ingress_egress_443_only" {
  assert {
    condition     = length(aws_security_group.fetch_model.ingress) == 0
    error_message = "The fetch security group must have no ingress."
  }

  assert {
    condition = length(aws_security_group.fetch_model.egress) == 1 && alltrue([
      for rule in aws_security_group.fetch_model.egress :
      rule.from_port == 443 && rule.to_port == 443 && rule.protocol == "tcp" &&
      tolist(rule.cidr_blocks) == tolist(["0.0.0.0/0"]) && try(length(rule.ipv6_cidr_blocks), 0) == 0
    ])
    error_message = "The fetch security group must allow only TCP 443 egress to 0.0.0.0/0."
  }
}

run "network_output_has_public_ip_and_subnets" {
  assert {
    condition = jsondecode(output.fetch_model_network) == {
      awsvpcConfiguration = {
        subnets        = ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"]
        securityGroups = ["sg-0123456789abcdef0"]
        assignPublicIp = "ENABLED"
      }
    }
    error_message = "The network output must be run-task awsvpcConfiguration JSON with the default subnets, the fetch group, and a public IP."
  }

  assert {
    condition = alltrue([
      output.fetch_model_cluster == "drip-ai-fetch-model",
      output.fetch_model_task_definition == "arn:aws:ecs:us-east-1:123456789012:task-definition/drip-ai-fetch-model:1",
      output.fetch_model_log_group == "/drip-ai/fetch-model",
      output.fetch_model_token_parameter == "/drip-ai/hf-token",
      output.models_bucket == "drip-ai-models-123456789012",
      output.region == "us-east-1",
      terraform_data.fetch_model.output.network_json == output.fetch_model_network,
      terraform_data.fetch_model.output.bucket == output.models_bucket,
    ])
    error_message = "The fetch outputs must name the cluster, the revisioned task definition, the log group, the token parameter, the looked-up bucket, and the region."
  }
}
