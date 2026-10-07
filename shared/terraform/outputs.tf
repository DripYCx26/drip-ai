output "models_bucket" {
  value = aws_s3_bucket.models.id
}

output "api_key_parameter" {
  value = aws_ssm_parameter.api_key.name
}

output "region" {
  value = var.region
}
