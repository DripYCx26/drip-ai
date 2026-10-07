# ECS Fargate job that copies a Hugging Face model into the models bucket.
# shared/fetch-model/fetch-model.sh applies this root, then launches one task
# per copy. See docs/DECISIONS.md (2026-10-05).

module "conventions" {
  source = "../../modules/conventions"

  project = var.project
  region  = var.region
}

# Owned by the shared stack; looked up by name so this root reads no other state.
data "aws_s3_bucket" "models" {
  bucket = module.conventions.bucket_name
}

data "aws_vpc" "default" {
  default = true
}

# The task runs in the default VPC's public subnets.
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  filter {
    name   = "default-for-az"
    values = ["true"]
  }

  lifecycle {
    postcondition {
      condition     = length(self.ids) > 0
      error_message = "The default VPC has no default subnets for the model-fetch task."
    }
  }
}

# Resolves the tag only to prove it still points at the pinned digest.
data "aws_ecr_image" "vllm" {
  registry_id     = module.conventions.sagemaker_image_registry
  repository_name = module.conventions.sagemaker_image_repository
  image_tag       = module.conventions.sagemaker_image_tag
}

locals {
  # Built from known values, not aws_ssm_parameter.hf_token.arn: fetch-model.sh
  # deletes the parameter after each launch, so the next apply re-creates it and
  # its arn is unknown at plan, which would force a new task-definition revision
  # and execution policy on every fetch.
  token_parameter_arn = module.conventions.hf_token_parameter_arn
  copier_script       = file("${path.module}/../copier.py")

  network = jsonencode({
    awsvpcConfiguration = {
      subnets        = data.aws_subnets.default.ids
      securityGroups = [aws_security_group.fetch_model.id]
      # The default VPC has no NAT, so the task needs a public IP to reach
      # Hugging Face, ECR, S3, and CloudWatch Logs.
      assignPublicIp = "ENABLED"
    }
  })
}

resource "aws_ecs_cluster" "fetch_model" {
  name = "${var.project}-fetch-model"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_cloudwatch_log_group" "fetch_model" {
  name              = "/${var.project}/fetch-model"
  retention_in_days = 14
}

# No ingress: the task only calls out over HTTPS.
resource "aws_security_group" "fetch_model" {
  name        = "${var.project}-fetch-model"
  description = "Egress HTTPS only for the model-fetch task"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Write-only value: the real token is written later with put-parameter and
# never read into state. ignore_changes on `value` was rejected because the
# provider would still read the decrypted token back into state on refresh.
resource "aws_ssm_parameter" "hf_token" {
  name             = module.conventions.hf_token_parameter
  type             = "SecureString"
  value_wo         = "none"
  value_wo_version = 1
}

resource "aws_iam_role" "fetch_model_task" {
  name = "${var.project}-fetch-model-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = module.conventions.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${module.conventions.partition}:ecs:${var.region}:${module.conventions.account_id}:*" }
      }
    }]
  })
}

# Read and write objects under models/ only. No delete: the copier never removes
# or replaces a verified object. ListBucket carries no s3:prefix condition
# because HeadObject on a missing key returns 403 instead of 404 without it,
# which breaks the copier's resume check; the bucket holds only model weights.
resource "aws_iam_role_policy" "fetch_model_task" {
  name = "copy-into-models-prefix"
  role = aws_iam_role.fetch_model_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = data.aws_s3_bucket.models.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "${data.aws_s3_bucket.models.arn}/models/*"
      },
    ]
  })
}

resource "aws_iam_role" "fetch_model_execution" {
  name = "${var.project}-fetch-model-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = module.conventions.account_id }
      }
    }]
  })
}

# ECS itself uses this role to pull the image, write logs, and inject the token.
resource "aws_iam_role_policy" "fetch_model_execution" {
  name = "pull-image-write-logs-read-token"
  role = aws_iam_role.fetch_model_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Does not support resource-level permissions.
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = module.conventions.sagemaker_image_repository_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.fetch_model.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameters"]
        Resource = local.token_parameter_arn
      },
    ]
  })

  depends_on = [aws_ssm_parameter.hf_token]
}

resource "aws_ecs_task_definition" "fetch_model" {
  family                   = "${var.project}-fetch-model"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "8192"
  memory                   = "16384"
  task_role_arn            = aws_iam_role.fetch_model_task.arn
  execution_role_arn       = aws_iam_role.fetch_model_execution.arn

  # Holds the extracted image. The root filesystem is read-only, so the copier
  # streams weights to S3 instead of staging them on disk.
  ephemeral_storage {
    size_in_gib = 100
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # No command: run-task overrides pass the copier's arguments.
  container_definitions = jsonencode([{
    name                   = "fetch"
    image                  = module.conventions.sagemaker_image
    essential              = true
    entryPoint             = ["/opt/venv/bin/python3", "-I", "-c", local.copier_script]
    readonlyRootFilesystem = true
    stopTimeout            = 30
    environment = [
      { name = "DO_NOT_TRACK", value = "1" },
      { name = "HF_HUB_DISABLE_TELEMETRY", value = "1" },
      { name = "OPT_OUT_TRACKING", value = "true" },
    ]
    # ECS reads the parameter at launch; only its ARN is in the definition.
    secrets = [
      { name = "HF_TOKEN", valueFrom = local.token_parameter_arn },
    ]
    linuxParameters = {
      capabilities = { drop = ["ALL"] }
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.fetch_model.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "fetch"
      }
    }
  }])

  # The ARN above is a string, so this keeps the parameter created first.
  depends_on = [aws_ssm_parameter.hf_token]

  lifecycle {
    precondition {
      condition     = data.aws_ecr_image.vllm.image_digest == module.conventions.sagemaker_image_digest
      error_message = "The vllm:${module.conventions.sagemaker_image_tag} tag no longer resolves to the pinned digest. Review the new image and record it in docs/DECISIONS.md before changing the pin in shared/modules/conventions."
    }

    # JSON escaping grows the script inside the 64 KiB container-definitions limit.
    precondition {
      condition     = length(local.copier_script) < 32768
      error_message = "copier.py must stay under 32768 characters to fit in the task definition."
    }
  }
}

# Handoff record for fetch-model.sh: it depends on everything a task needs, so
# its output exists only once all of it is applied.
resource "terraform_data" "fetch_model" {
  input = {
    cluster             = aws_ecs_cluster.fetch_model.name
    task_definition_arn = aws_ecs_task_definition.fetch_model.arn
    network_json        = local.network
    log_group           = aws_cloudwatch_log_group.fetch_model.name
    bucket              = data.aws_s3_bucket.models.id
    region              = var.region
    token_parameter     = aws_ssm_parameter.hf_token.name
  }

  depends_on = [
    aws_ssm_parameter.hf_token,
    aws_iam_role_policy.fetch_model_task,
    aws_iam_role_policy.fetch_model_execution,
  ]
}
