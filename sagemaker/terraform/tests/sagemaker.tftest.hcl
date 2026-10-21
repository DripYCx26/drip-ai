# Offline proof for the SageMaker foundation. Every provider is mocked, so no run
# here reaches AWS, even the ones with command = apply.

# terraform test also loads an operator's terraform.tfvars in this directory.
# Pinning every variable keeps these runs independent of that file; update this
# block when a default changes.
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
  target = data.aws_ecr_image.vllm
  values = {
    id           = "sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"
    image_digest = "sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"
  }
}

run "a_project_other_than_drip_ai_is_refused_so_the_log_grant_covers_every_race_name" {
  command = plan

  variables {
    project = "other"
  }

  expect_failures = [var.project]
}

run "logs_are_granted_on_the_drip_ai_prefix_wildcard" {
  command = plan

  assert {
    condition = [
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement :
      statement.Resource if contains(flatten([statement.Action]), "logs:PutLogEvents")
      ] == [[
        "arn:aws:logs:us-east-1:123456789012:log-group:/aws/sagemaker/Endpoints/drip-ai-*",
        "arn:aws:logs:us-east-1:123456789012:log-group:/aws/sagemaker/Endpoints/drip-ai-*:*",
    ]]
    error_message = "The role must write logs for every drip-ai-* endpoint's log group, whatever its name, and no other."
  }

  assert {
    condition     = output.region == "us-east-1"
    error_message = "The region output must name the stack's region."
  }
}

run "a_second_region_suffixes_the_global_role_name" {
  command = plan

  variables {
    region = "us-east-2"
  }

  assert {
    condition     = aws_iam_role.sagemaker.name == "drip-ai-sagemaker-execution-us-east-2"
    error_message = "IAM names are account-global, so the role outside us-east-1 must end in -<region>."
  }

  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement :
      contains(flatten([statement.Resource]), "arn:aws:logs:us-east-2:123456789012:log-group:/aws/sagemaker/Endpoints/drip-ai-*")
    ])
    error_message = "The log grant must name the role's own region."
  }
}

run "home_region_keeps_the_unsuffixed_role_name" {
  command = plan

  assert {
    condition     = aws_iam_role.sagemaker.name == "drip-ai-sagemaker-execution"
    error_message = "us-east-1 keeps the role name it had before regions were added."
  }
}

run "rejects_a_moved_image_tag" {
  command = plan

  override_data {
    target = data.aws_ecr_image.vllm
    values = {
      id           = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
      image_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    }
  }

  expect_failures = [aws_iam_role_policy.sagemaker]
}

# The runs below apply against the mocked provider so that ARNs are known.
run "execution_policy_limits_wildcard_resources" {
  assert {
    condition = sort(distinct(flatten([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement :
      flatten([statement.Action]) if contains(flatten([statement.Resource]), "*")
    ]))) == sort(["cloudwatch:PutMetricData", "ecr:GetAuthorizationToken"])
    error_message = "Only ecr:GetAuthorizationToken and cloudwatch:PutMetricData may use Resource \"*\"."
  }

  assert {
    condition = sort(distinct(flatten([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement : flatten([statement.Action])
      ]))) == sort([
      "cloudwatch:PutMetricData",
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetAuthorizationToken",
      "ecr:GetDownloadUrlForLayer",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
      "s3:GetObject",
      "s3:ListBucket",
    ])
    error_message = "The execution policy must grant exactly the listed read and log actions; no ssm, iam, or S3 write."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement :
      statement.Effect == "Allow" && !contains(keys(statement), "NotResource") && !contains(keys(statement), "NotAction")
    ])
    error_message = "The execution policy must use plain Allow statements only."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement :
      alltrue([for resource in flatten([statement.Resource]) : resource == "arn:aws:s3:::drip-ai-models-123456789012/models/*"])
      if contains(flatten([statement.Action]), "s3:GetObject")
    ])
    error_message = "s3:GetObject must be limited to models/* of the region's bucket."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement :
      try(sort(statement.Condition.StringLike["s3:prefix"]), []) == sort(["models/", "models/*"])
      if contains(flatten([statement.Action]), "s3:ListBucket")
    ])
    error_message = "s3:ListBucket must carry an s3:prefix condition limited to models/."
  }

  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_role.sagemaker.assume_role_policy).Statement :
      statement.Effect == "Allow" &&
      statement.Action == "sts:AssumeRole" &&
      statement.Principal == { Service = "sagemaker.amazonaws.com" } &&
      try(statement.Condition.StringEquals["aws:SourceAccount"], "") == "123456789012"
    ])
    error_message = "The trust policy must admit only sagemaker.amazonaws.com, conditioned on aws:SourceAccount = this account."
  }

  assert {
    condition     = output.execution_role_arn == "arn:aws:iam::123456789012:role/mock"
    error_message = "The execution_role_arn output must name the role."
  }
}
