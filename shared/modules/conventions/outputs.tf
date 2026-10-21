output "account_id" {
  value = local.account_id
}

output "partition" {
  value = local.partition
}

# Weights uploaded or fetched land here under models/<name>/. Bucket names are global, so every
# region other than us-east-1 gets its own bucket with the region appended.
output "bucket_name" {
  value = "${var.project}-models-${local.account_id}${var.region == "us-east-1" ? "" : "-${var.region}"}"
}

# Terraform state for every stack; created by bootstrap/. The suffix is the
# operator's DRIP_AI_STATE_BUCKET_SUFFIX.
output "state_bucket_name" {
  value = "${var.project}-tfstate-${local.account_id}-${var.region}${var.state_bucket_suffix == "" ? "" : "-${var.state_bucket_suffix}"}"
}

output "api_key_parameter" {
  value = local.api_key_parameter
}

output "api_key_parameter_arn" {
  value = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${local.api_key_parameter}"
}

# Reserved for the short-lived model-fetch token.
output "hf_token_parameter" {
  value = local.hf_token_parameter
}

output "hf_token_parameter_arn" {
  value = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${local.hf_token_parameter}"
}

# The ECR registry (AWS account) id that hosts the image.
output "sagemaker_image_registry" {
  value = local.sagemaker_image_registry
}

output "sagemaker_image_repository" {
  value = local.sagemaker_image_repository
}

output "sagemaker_image_repository_arn" {
  value = "arn:${local.partition}:ecr:${var.region}:${local.sagemaker_image_registry}:repository/${local.sagemaker_image_repository}"
}

output "sagemaker_image_tag" {
  value = local.sagemaker_image_tag
}

output "sagemaker_image_digest" {
  value = local.sagemaker_image_digest
}

output "sagemaker_image" {
  value = "${local.sagemaker_image_registry}.dkr.ecr.${var.region}.amazonaws.com/${local.sagemaker_image_repository}@${local.sagemaker_image_digest}"
}

output "vllm_image" {
  value = local.vllm_image
}

# IAM names are account-global, so every region other than us-east-1 appends its region to them.
output "global_name_suffix" {
  value = var.region == "us-east-1" ? "" : "-${var.region}"
}
