# Offline proof for the EC2 server. Every provider is mocked, so no run here
# reaches AWS, even the ones with command = apply.

# terraform test also loads an operator's terraform.tfvars in this directory.
# Pinning every variable keeps these runs independent of that file; update this
# block when a default changes.
variables {
  project              = "drip-ai"
  region               = "us-east-1"
  instance_type        = "g6e.2xlarge"
  root_volume_gb       = 300
  model_source         = "Org/Model"
  model_from_s3        = false
  served_model_name    = "drip-coder"
  tensor_parallel_size = 1
  max_model_len        = 65536
  vllm_extra_args      = ["--enable-auto-tool-choice", "--tool-call-parser", "example_tool"]
  hf_token_parameter   = ""
}

# The provider checks ARN shape even when mocked, so ARNs that feed other
# resources get realistic defaults.
mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock"
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

override_data {
  target = data.aws_s3_bucket.models
  values = {
    id  = "drip-ai-models-123456789012"
    arn = "arn:aws:s3:::drip-ai-models-123456789012"
  }
}

override_data {
  target = data.aws_ssm_parameter.gpu_ami
  values = {
    value = "ami-0123456789abcdef0"
  }
}

override_data {
  target = data.aws_vpc.default
  values = {
    id = "vpc-0123456789abcdef0"
  }
}

# AmazonSSMManagedInstanceCore allows ssm:GetParameter on "*", so only this Deny
# keeps the instance from reading every parameter in the account.
run "ssm_deny_not_resource_is_api_key_and_hf_token" {
  command = plan

  variables {
    hf_token_parameter = "/drip-ai/ec2-hf-token"
  }

  assert {
    condition = [
      for statement in jsondecode(aws_iam_role_policy.server.policy).Statement :
      statement if statement.Effect == "Deny"
      ] == [{
        Effect = "Deny"
        Action = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
        NotResource = [
          "arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/api-key",
          "arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/ec2-hf-token",
        ]
    }]
    error_message = "The role must deny every SSM parameter read except the API key and the configured token."
  }

  assert {
    condition = flatten([
      for statement in jsondecode(aws_iam_role_policy.server.policy).Statement :
      flatten([statement.Resource]) if statement.Effect == "Allow" && contains(flatten([statement.Action]), "ssm:GetParameter")
      ]) == [
      "arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/api-key",
      "arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/ec2-hf-token",
    ]
    error_message = "The role may read only the API key and the configured token."
  }
}

run "ssm_deny_without_a_token_leaves_only_the_api_key" {
  command = plan

  assert {
    condition = flatten([
      for statement in jsondecode(aws_iam_role_policy.server.policy).Statement :
      statement.NotResource if statement.Effect == "Deny"
    ]) == ["arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/api-key"]
    error_message = "With no hf_token_parameter, the API key must be the only readable parameter."
  }
}

run "imds_hop_limit_1" {
  command = plan

  assert {
    condition = (
      aws_instance.server.metadata_options[0].http_tokens == "required" &&
      aws_instance.server.metadata_options[0].http_put_response_hop_limit == 1
    )
    error_message = "The instance must require IMDSv2 tokens with a hop limit of 1, so the container cannot reach the role credentials."
  }
}

# The boot script reads the key from SSM by name at boot; no key value is ever
# rendered here.
run "user_data_reads_the_api_key_by_name_and_pins_the_image" {
  command = plan

  assert {
    condition = alltrue([
      strcontains(aws_instance.server.user_data, "read_secret '/drip-ai/api-key'"),
      strcontains(aws_instance.server.user_data, "docker pull 'vllm/vllm-openai:v0.30.0@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90'"),
      strcontains(aws_instance.server.user_data, "@sha256:"),
      !strcontains(aws_instance.server.user_data, "HF_TOKEN="),
    ])
    error_message = "User data must read the API key by name, pull the pinned image, and carry no token line without hf_token_parameter."
  }
}

run "hf_token_parameter_cannot_alias_managed" {
  command = plan

  variables {
    hf_token_parameter = "/drip-ai/hf-token"
  }

  expect_failures = [var.hf_token_parameter]
}

# Each segment must start with a letter or digit, so a name segment cannot
# begin with '-' and read as a flag.
run "rejects_a_name_segment_starting_with_a_dash" {
  command = plan

  variables {
    model_source = "org/-x"
  }

  expect_failures = [var.model_source]
}

# The runs below apply against the mocked provider so that computed attributes
# are known. State persists from here on, so plan-only runs stay above.
run "no_ingress_rules" {
  assert {
    condition     = length(aws_security_group.server.ingress) == 0
    error_message = "The server security group must have no ingress; clients use the SSM tunnel."
  }

  assert {
    condition     = output.instance_id == aws_instance.server.id && output.region == "us-east-1"
    error_message = "The outputs must name the instance and the region."
  }
}
