# Offline proof for the state bucket. Every provider is mocked, so no run here
# reaches AWS. Runs are plan-only: prevent_destroy would fail the destroy that
# follows an applied run.

# terraform test also loads an operator's terraform.tfvars in this directory.
# Pinning every variable keeps these runs independent of that file.
variables {
  project             = "drip-ai"
  region              = "us-east-1"
  state_bucket_suffix = ""
}

mock_provider "aws" {}

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

# The bucket policy names the bucket ARN, which a plan would otherwise leave unknown.
override_resource {
  target          = aws_s3_bucket.state
  override_during = plan
  values = {
    id  = "drip-ai-tfstate-123456789012-us-east-1"
    arn = "arn:aws:s3:::drip-ai-tfstate-123456789012-us-east-1"
  }
}

run "state_bucket_versioned_encrypted_private_tls_only" {
  command = plan

  assert {
    condition     = aws_s3_bucket.state.bucket == "drip-ai-tfstate-123456789012-us-east-1" && output.state_bucket == aws_s3_bucket.state.id
    error_message = "The state bucket must be named drip-ai-tfstate-<account>-<region> and be the state_bucket output."
  }

  assert {
    condition     = aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "The state bucket must be versioned."
  }

  assert {
    condition = [
      for rule in aws_s3_bucket_server_side_encryption_configuration.state.rule :
      rule.apply_server_side_encryption_by_default[0].sse_algorithm
    ] == ["AES256"]
    error_message = "The state bucket must encrypt every object with SSE-S3."
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.state.block_public_acls,
      aws_s3_bucket_public_access_block.state.block_public_policy,
      aws_s3_bucket_public_access_block.state.ignore_public_acls,
      aws_s3_bucket_public_access_block.state.restrict_public_buckets,
    ])
    error_message = "The state bucket must turn on all four public access block settings."
  }

  assert {
    condition     = aws_s3_bucket_ownership_controls.state.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "The state bucket must disable ACLs with BucketOwnerEnforced."
  }

  assert {
    condition = jsondecode(aws_s3_bucket_policy.state.policy).Statement == [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        "arn:aws:s3:::drip-ai-tfstate-123456789012-us-east-1",
        "arn:aws:s3:::drip-ai-tfstate-123456789012-us-east-1/*",
      ]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
    error_message = "The bucket policy must be exactly one Deny of every S3 action over plain HTTP, on the bucket and its objects."
  }

  # Asserts address rule[0] only: Terraform 1.16.5 crashes printing a failed
  # assertion that shows the whole rule list, which carries a deprecation mark.
  assert {
    condition = alltrue([
      length(aws_s3_bucket_lifecycle_configuration.state.rule) == 1,
      aws_s3_bucket_lifecycle_configuration.state.rule[0].status == "Enabled",
      aws_s3_bucket_lifecycle_configuration.state.rule[0].noncurrent_version_expiration[0].noncurrent_days == 90,
      length(aws_s3_bucket_lifecycle_configuration.state.rule[0].expiration) == 0,
    ])
    error_message = "Noncurrent state versions must expire after 90 days, and current versions never."
  }
}

run "state_bucket_name_takes_an_optional_suffix" {
  command = plan

  variables {
    state_bucket_suffix = "team2"
  }

  assert {
    condition     = aws_s3_bucket.state.bucket == "drip-ai-tfstate-123456789012-us-east-1-team2" && module.conventions.state_bucket_name == aws_s3_bucket.state.bucket
    error_message = "With state_bucket_suffix set, the state bucket must be named drip-ai-tfstate-<account>-<region>-<suffix>."
  }
}

run "state_bucket_suffix_accepts_the_longest_name" {
  command = plan

  variables {
    state_bucket_suffix = "a-b-c-d-e-f1"
  }

  assert {
    condition     = aws_s3_bucket.state.bucket == "drip-ai-tfstate-123456789012-us-east-1-a-b-c-d-e-f1"
    error_message = "A 12-character suffix with inner hyphens must be accepted."
  }
}

run "state_bucket_suffix_rejects_a_malformed_value" {
  command = plan

  variables {
    state_bucket_suffix = "Team_2"
  }

  expect_failures = [var.state_bucket_suffix]
}

run "state_bucket_suffix_rejects_a_thirteen_character_value" {
  command = plan

  variables {
    state_bucket_suffix = "abcdefghijklm"
  }

  expect_failures = [var.state_bucket_suffix]
}

run "state_bucket_suffix_rejects_an_edge_hyphen" {
  command = plan

  variables {
    state_bucket_suffix = "team-"
  }

  expect_failures = [var.state_bucket_suffix]
}
