module "conventions" {
  source = "../modules/conventions"

  project = var.project
  region  = var.region
}

# Weights uploaded from ./models or fetched from Hugging Face land here under models/<name>/.
resource "aws_s3_bucket" "models" {
  bucket = module.conventions.bucket_name
}

resource "aws_s3_bucket_public_access_block" "models" {
  bucket                  = aws_s3_bucket.models.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# An interrupted copy leaves multipart parts that bill as storage but list as no object.
resource "aws_s3_bucket_lifecycle_configuration" "models" {
  bucket = aws_s3_bucket.models.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Ephemeral and write-only: the key is generated and sent to SSM during apply
# but never stored in plan or state. Raising api_key_version writes a new one.
ephemeral "random_password" "api_key" {
  length  = 48
  special = false
}

resource "aws_ssm_parameter" "api_key" {
  name             = module.conventions.api_key_parameter
  type             = "SecureString"
  value_wo         = "sk-${ephemeral.random_password.api_key.result}"
  value_wo_version = var.api_key_version
}

# One console view of everything tagged Project=<project>, across every stack.
resource "aws_resourcegroups_group" "project" {
  name = var.project

  resource_query {
    query = jsonencode({
      ResourceTypeFilters = ["AWS::AllSupported"]
      TagFilters          = [{ Key = "Project", Values = [var.project] }]
    })
  }
}
