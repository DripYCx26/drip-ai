# Passed to every other root as `-backend-config=bucket=...`.
output "state_bucket" {
  value = aws_s3_bucket.state.id
}

output "region" {
  value = var.region
}
