# Offline proof for the shared stack. The AWS provider is mocked, so no run here
# reaches AWS, even the ones with command = apply. The random provider runs for
# real because mock providers do not support ephemeral resources; it is local
# and needs no network or credentials.

# terraform test also loads an operator's terraform.tfvars in this directory.
# Pinning every variable keeps these runs independent of that file.
variables {
  project         = "drip-ai"
  region          = "us-east-1"
  api_key_version = 1
}

mock_provider "aws" {
  # An empty filter reads back as prefix "", which S3 applies to every object.
  mock_resource "aws_s3_bucket_lifecycle_configuration" {
    defaults = {
      rule = { filter = { prefix = "" } }
    }
  }
}

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

run "api_key_is_write_only" {
  command = plan

  assert {
    condition     = aws_ssm_parameter.api_key.name == "/drip-ai/api-key" && aws_ssm_parameter.api_key.type == "SecureString"
    error_message = "The API key must be the SecureString /drip-ai/api-key."
  }

  # Write-only attributes are always null in plan and state.
  assert {
    condition     = aws_ssm_parameter.api_key.value_wo == null && aws_ssm_parameter.api_key.value_wo_version == 1
    error_message = "The API key must be written through value_wo, versioned by api_key_version."
  }

  assert {
    condition     = output.api_key_parameter == "/drip-ai/api-key"
    error_message = "The api_key_parameter output must name the parameter."
  }
}

run "models_bucket_blocks_public_access" {
  command = plan

  assert {
    condition     = aws_s3_bucket.models.bucket == "drip-ai-models-123456789012"
    error_message = "The models bucket must be named drip-ai-models-<account>."
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.models.block_public_acls,
      aws_s3_bucket_public_access_block.models.block_public_policy,
      aws_s3_bucket_public_access_block.models.ignore_public_acls,
      aws_s3_bucket_public_access_block.models.restrict_public_buckets,
    ])
    error_message = "The models bucket must turn on all four public access block settings."
  }
}

run "rejects_an_api_key_version_below_one" {
  command = plan

  variables {
    api_key_version = 0
  }

  expect_failures = [var.api_key_version]
}

# The runs below apply against the mocked provider. State persists from here on,
# so plan-only runs stay above.

# The mock fills an unset `value` with its own short string. A config that set
# `value` from a password (the key this stack writes starts with "sk-") would
# keep that key in state, and this run would see it.
run "api_key_value_stays_out_of_state" {
  assert {
    condition     = !startswith(nonsensitive(aws_ssm_parameter.api_key.value), "sk-")
    error_message = "The API key must not reach the parameter's value attribute or state."
  }
}

# Asserts address rule[0] only: Terraform 1.16.5 crashes printing a failed
# assertion that shows the whole rule list, which carries a deprecation mark.
run "lifecycle_aborts_multipart_after_one_day" {
  assert {
    condition = alltrue([
      aws_s3_bucket_lifecycle_configuration.models.rule[0].status == "Enabled",
      aws_s3_bucket_lifecycle_configuration.models.rule[0].abort_incomplete_multipart_upload[0].days_after_initiation == 1,
      length(aws_s3_bucket_lifecycle_configuration.models.rule[0].filter) == 1,
      length(aws_s3_bucket_lifecycle_configuration.models.rule[0].filter[0].and) == 0,
      length(aws_s3_bucket_lifecycle_configuration.models.rule[0].filter[0].tag) == 0,
      aws_s3_bucket_lifecycle_configuration.models.rule[0].filter[0].prefix == "",
      length(aws_s3_bucket_lifecycle_configuration.models.rule[0].expiration) == 0,
      length(aws_s3_bucket_lifecycle_configuration.models.rule[0].transition) == 0,
    ])
    error_message = "The bucket must abort incomplete multipart uploads after one day for all objects, and expire nothing."
  }
}
