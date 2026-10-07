output "fetch_model_cluster" {
  value = aws_ecs_cluster.fetch_model.name
}

# The full ARN with revision, so run-task launches exactly this definition.
output "fetch_model_task_definition" {
  value = aws_ecs_task_definition.fetch_model.arn
}

# JSON for `aws ecs run-task --network-configuration`.
output "fetch_model_network" {
  value = local.network
}

output "fetch_model_log_group" {
  value = aws_cloudwatch_log_group.fetch_model.name
}

output "fetch_model_token_parameter" {
  value = aws_ssm_parameter.hf_token.name
}

output "models_bucket" {
  value = data.aws_s3_bucket.models.id
}

output "region" {
  value = var.region
}
