# The per-region foundation every SageMaker race needs: the execution role and what it may do.
# sagemaker/race.py creates each lane's model, endpoint configuration, log group, and endpoint
# through the API, tagged ManagedBy=drip-ai and RunId, so this root holds nothing per model or per
# run. See docs/DECISIONS.md, 2026-10-09 "Plan, race, and learn in a Python control core".
#
# Apply it once per region with `bin/drip-ai setup` (DRIP_AI_REGION picks the region). No Hugging
# Face token here: the role reads weights from this region's models bucket, and an endpoint that
# downloads from Hugging Face gets no token either.

module "conventions" {
  source = "../../shared/modules/conventions"

  project = var.project
  region  = var.region
}

# Owned by the shared stack; looked up by name so this root reads no other state.
data "aws_s3_bucket" "models" {
  bucket = module.conventions.bucket_name
}

# Resolves the tag only to prove it still points at the pinned digest the races deploy.
data "aws_ecr_image" "vllm" {
  registry_id     = module.conventions.sagemaker_image_registry
  repository_name = module.conventions.sagemaker_image_repository
  image_tag       = module.conventions.sagemaker_image_tag
}

locals {
  # Every endpoint drip-ai creates is named drip-ai-..., so its log group is under this prefix;
  # sagemaker/race.py refuses a name outside it.
  log_group_arn = "arn:${module.conventions.partition}:logs:${var.region}:${module.conventions.account_id}:log-group:/aws/sagemaker/Endpoints/${var.project}-*"
}

resource "aws_iam_role" "sagemaker" {
  name = "${var.project}-sagemaker-execution${module.conventions.global_name_suffix}"
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
  name = "pull-image-read-models-write-logs"
  role = aws_iam_role.sagemaker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = [local.log_group_arn, "${local.log_group_arn}:*"]
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
      # Weights: list and read only the models/ folder of this region's bucket.
      {
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = data.aws_s3_bucket.models.arn
        Condition = { StringLike = { "s3:prefix" = ["models/", "models/*"] } }
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${data.aws_s3_bucket.models.arn}/models/*"
      },
    ]
  })

  lifecycle {
    precondition {
      condition     = data.aws_ecr_image.vllm.image_digest == module.conventions.sagemaker_image_digest
      error_message = "The vllm:${module.conventions.sagemaker_image_tag} tag no longer resolves to the pinned digest. Review the new image and record it in docs/DECISIONS.md before changing the pin in shared/modules/conventions."
    }
  }
}
