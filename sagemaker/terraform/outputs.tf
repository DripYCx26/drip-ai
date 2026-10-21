output "region" {
  value = var.region
}

# sagemaker/race.py builds the same name from shared/drip_ai/conventions.py; it reads the role
# with iam get-role to check the foundation exists before it plans a lane in a region.
output "execution_role_arn" {
  value = aws_iam_role.sagemaker.arn
}
