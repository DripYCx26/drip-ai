# SageMaker real-time endpoints running AWS's vLLM SageMaker image, one per
# instance_types entry, which sagemaker/start.sh races. See docs/DECISIONS.md
# (2026-10-05, 2026-10-06) for the image, access, proxy, and race choices.
# Turning it off is `destroy` on this root.
#
# No Hugging Face token here: it would sit in the model environment and
# Terraform state. Gated models reach the endpoint through S3.

module "conventions" {
  source = "../../shared/modules/conventions"

  project = var.project
  region  = var.region
}

# Owned by the shared stack; looked up by name so this root reads no other state.
data "aws_s3_bucket" "models" {
  bucket = module.conventions.bucket_name
}

# Resolves the tag only to prove it still points at the pinned digest.
data "aws_ecr_image" "vllm" {
  registry_id     = module.conventions.sagemaker_image_registry
  repository_name = module.conventions.sagemaker_image_repository
  image_tag       = module.conventions.sagemaker_image_tag
}

locals {
  model_prefix = "models/${var.model_source}/"

  # Endpoint name per raced instance type: ml.p5en.48xlarge races as drip-ai-p5en-48xlarge.
  lanes = { for type in var.instance_types : type => "${var.project}-${replace(trimprefix(type, "ml."), ".", "-")}" }

  # GPUs per instance for the types this stack is sized for. Unlisted types skip
  # the tensor_parallel_size check.
  gpu_count = {
    "ml.p5.48xlarge"      = 8
    "ml.p5e.48xlarge"     = 8
    "ml.p5en.48xlarge"    = 8
    "ml.p6-b200.48xlarge" = 8
    "ml.p6-b300.48xlarge" = 8
    "ml.g6e.xlarge"       = 1
    "ml.g6e.2xlarge"      = 1
    "ml.g6e.4xlarge"      = 1
    "ml.g6e.8xlarge"      = 1
    "ml.g6e.16xlarge"     = 1
    "ml.g6e.12xlarge"     = 4
    "ml.g6e.24xlarge"     = 4
    "ml.g6e.48xlarge"     = 8
  }

  # The image turns SM_VLLM_<FLAG> into `--<flag> <value>`. A flag followed by a
  # non-flag token takes that token as its value; a bare flag becomes "true".
  # The model profile's args come first and the operator's vllm_extra_args last,
  # so merge() lets the operator win on a flag both set.
  vllm_arg_lists = [var.vllm_model_args, var.vllm_extra_args]
  vllm_env = merge([
    for args in local.vllm_arg_lists : {
      for i, token in args :
      "SM_VLLM_${upper(replace(trimprefix(token, "--"), "-", "_"))}" => (
        startswith(try(args[i + 1], "--"), "--") ? "true" : try(args[i + 1], "true")
      )
      if startswith(token, "--")
    }
  ]...)

  # No secrets: the environment is readable through DescribeModel and state.
  environment = merge({
    SM_VLLM_MODEL                = var.model_from_s3 ? "/opt/ml/model" : var.model_source
    SM_VLLM_SERVED_MODEL_NAME    = var.served_model_name
    SM_VLLM_TENSOR_PARALLEL_SIZE = tostring(var.tensor_parallel_size)
    SM_VLLM_MAX_MODEL_LEN        = tostring(var.max_model_len)
    VLLM_NO_USAGE_STATS          = "1"
    DO_NOT_TRACK                 = "1"
    # Stops the image pip-installing a requirements.txt found in the weights prefix at startup.
    STANDARD_AUTO_INSTALL_REQ = "false"
  }, local.vllm_env)

  # A model is immutable, so any change gets a new name and a blue/green swap.
  model_hash = substr(sha1(jsonencode({
    environment   = local.environment
    image         = module.conventions.sagemaker_image
    model_from_s3 = var.model_from_s3
    model_source  = var.model_source
  })), 0, 8)
}

# Created here so it gets a retention period; SageMaker would create it without one.
resource "aws_cloudwatch_log_group" "sagemaker" {
  for_each = local.lanes

  name              = "/aws/sagemaker/Endpoints/${each.value}"
  retention_in_days = 14
}

resource "aws_iam_role" "sagemaker" {
  name = "${var.project}-sagemaker-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sagemaker.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = module.conventions.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "sagemaker" {
  name = "pull-image-read-model-write-logs"
  role = aws_iam_role.sagemaker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect   = "Allow"
          Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
          Resource = flatten([for group in aws_cloudwatch_log_group.sagemaker : [group.arn, "${group.arn}:*"]])
        },
        # Neither action supports resource-level permissions.
        {
          Effect   = "Allow"
          Action   = ["cloudwatch:PutMetricData", "ecr:GetAuthorizationToken"]
          Resource = "*"
        },
        {
          Effect   = "Allow"
          Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
          Resource = module.conventions.sagemaker_image_repository_arn
        },
      ],
      # Weights from S3: list and read only this model's prefix.
      [for statement in [
        {
          Effect    = "Allow"
          Action    = ["s3:ListBucket"]
          Resource  = data.aws_s3_bucket.models.arn
          Condition = { StringLike = { "s3:prefix" = [local.model_prefix, "${local.model_prefix}*"] } }
        },
        {
          Effect   = "Allow"
          Action   = ["s3:GetObject"]
          Resource = "${data.aws_s3_bucket.models.arn}/${local.model_prefix}*"
        },
      ] : statement if var.model_from_s3],
    )
  })
}

resource "aws_sagemaker_model" "server" {
  name               = "${var.project}-${local.model_hash}"
  execution_role_arn = aws_iam_role.sagemaker.arn

  primary_container {
    image       = module.conventions.sagemaker_image
    environment = local.environment

    dynamic "model_data_source" {
      for_each = var.model_from_s3 ? [1] : []
      content {
        s3_data_source {
          s3_uri           = "s3://${data.aws_s3_bucket.models.id}/${local.model_prefix}"
          s3_data_type     = "S3Prefix"
          compression_type = "None"
        }
      }
    }
  }

  # CreateModel checks that the role can pull the image, so the grant must exist first.
  depends_on = [aws_iam_role_policy.sagemaker]

  lifecycle {
    create_before_destroy = true

    precondition {
      condition     = data.aws_ecr_image.vllm.image_digest == module.conventions.sagemaker_image_digest
      error_message = "The vllm:${module.conventions.sagemaker_image_tag} tag no longer resolves to the pinned digest. Review the new image and record it in docs/DECISIONS.md before changing the pin in shared/modules/conventions."
    }

    precondition {
      condition = alltrue(flatten([
        for args in local.vllm_arg_lists : [
          for i, token in args :
          startswith(token, "--") ? can(regex("^--[a-z0-9][a-z0-9-]*$", token)) : startswith(try(args[i - 1], ""), "--")
        ]
      ]))
      # A '--' token is always a flag, so '--api_key' cannot slip through as the value of the flag before it.
      error_message = "vllm_model_args and vllm_extra_args must be '--flag' or '--flag value' tokens with lowercase, hyphenated flag names; '--flag=value', underscores, and stray values cannot become SM_VLLM_* variables."
    }

    precondition {
      condition = alltrue([
        for token in flatten(local.vllm_arg_lists) :
        !can(regex("(?i)(api-key|secret|password|credential|(^--|-)token$)", token)) if startswith(token, "--")
      ])
      error_message = "vllm_model_args and vllm_extra_args must carry no secret-shaped flag on SageMaker: its value would sit in the model environment, DescribeModel, and Terraform state."
    }

    # The tunnel tells Codex the window from SM_VLLM_MAX_MODEL_LEN, so a flag must not change it.
    precondition {
      condition = length(setintersection(
        toset(flatten(local.vllm_arg_lists)),
        ["--model", "--served-model-name", "--tensor-parallel-size", "--max-model-len"],
      )) == 0
      error_message = "Set the model, served_model_name, tensor_parallel_size, and max_model_len through their variables, not as vLLM flags."
    }
  }
}

resource "aws_sagemaker_endpoint_configuration" "server" {
  for_each = local.lanes

  name_prefix = "${each.value}-"

  production_variants {
    variant_name                                      = "main"
    model_name                                        = aws_sagemaker_model.server.name
    instance_type                                     = each.key
    initial_instance_count                            = 1
    inference_ami_version                             = var.inference_ami_version
    container_startup_health_check_timeout_in_seconds = var.startup_timeout_seconds
    model_data_download_timeout_in_seconds            = var.download_timeout_seconds
    volume_size_in_gb                                 = var.volume_size_gb
  }

  lifecycle {
    create_before_destroy = true

    precondition {
      condition     = lookup(local.gpu_count, each.key, var.tensor_parallel_size) == var.tensor_parallel_size
      error_message = "tensor_parallel_size must equal the GPU count of ${each.key} (${lookup(local.gpu_count, each.key, "unknown")})."
    }
  }
}

resource "aws_sagemaker_endpoint" "server" {
  for_each = local.lanes

  name                 = each.value
  endpoint_config_name = aws_sagemaker_endpoint_configuration.server[each.key].name

  # The one-user-at-a-time checks in the start and stop scripts read this tag.
  tags = {
    StartedBy = var.started_by
  }
}
