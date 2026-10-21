# Offline proof for the EC2 server. Every provider is mocked, so no run here
# reaches AWS, even the ones with command = apply.

# terraform test also loads an operator's terraform.tfvars in this directory.
# Pinning every variable keeps these runs independent of that file; update this
# block when a default changes.
variables {
  project                 = "drip-ai"
  region                  = "us-east-1"
  instance_type           = "g6e.2xlarge"
  root_volume_gb          = 300
  model_source            = "Org/Model"
  model_from_s3           = false
  served_model_name       = "drip-coder"
  tensor_parallel_size    = 1
  max_model_len           = 65536
  max_num_seqs            = null
  vllm_extra_args         = ["--enable-auto-tool-choice", "--tool-call-parser", "example_tool"]
  hf_token_parameter      = ""
  capacity_reservation_id = ""
  market_type             = ""
  availability_zone       = ""
  zone_subnets            = {}
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

run "max_num_seqs_is_optional_and_reaches_vllm" {
  command = plan

  variables {
    max_num_seqs = 48
  }

  assert {
    condition     = strcontains(aws_instance.server.user_data, "\"--max-num-seqs\",\"48\"")
    error_message = "max_num_seqs must be passed to the vLLM launch when configured."
  }
}

run "prefix_caching_cannot_be_disabled" {
  command = plan

  variables {
    vllm_extra_args = ["--no-enable-prefix-caching"]
  }

  expect_failures = [var.vllm_extra_args]
}

run "prefix_caching_cannot_be_disabled_with_a_value" {
  command = plan

  variables {
    vllm_extra_args = ["--no-enable-prefix-caching=true"]
  }

  expect_failures = [var.vllm_extra_args]
}

# Flags change in place through /etc/drip-ai/vllm.args; a new boot script never replaces the server.
run "user_data_writes_the_vllm_argv_once_and_changes_never_replace_the_instance" {
  command = plan

  assert {
    condition = alltrue([
      strcontains(aws_instance.server.user_data, jsonencode(["Org/Model", "--served-model-name", "drip-coder", "--tensor-parallel-size", "1", "--max-model-len", "65536", "--enable-auto-tool-choice", "--tool-call-parser", "example_tool"])),
      strcontains(aws_instance.server.user_data, "cat > /etc/drip-ai/vllm.args <<'EOF_ARGS'"),
      strcontains(aws_instance.server.user_data, "\"$${args[@]}\""),
      strcontains(aws_instance.server.user_data, "/usr/local/bin/drip-ai-vllm\n"),
    ])
    error_message = "User data must write the full vLLM argv as JSON and start the container through the launcher."
  }

  assert {
    condition     = aws_instance.server.user_data_replace_on_change == false
    error_message = "A changed boot script must not replace the instance; ec2 reconfigure changes flags in place."
  }

  assert {
    condition     = output.model_arg == "Org/Model" && output.tensor_parallel_size == 1
    error_message = "reconfigure rebuilds the argv from the model_arg and tensor_parallel_size outputs."
  }
}

run "a_capacity_block_launch_targets_its_reservation_and_zone" {
  command = plan

  variables {
    capacity_reservation_id = "cr-0123456789abcdef0"
    market_type             = "capacity-block"
    availability_zone       = "us-east-1-atl-2a"
    zone_subnets            = { "us-east-1-atl-2a" = "subnet-0123456789abcdef0" }
  }

  assert {
    condition = (
      aws_instance.server.instance_market_options[0].market_type == "capacity-block" &&
      aws_instance.server.capacity_reservation_specification[0].capacity_reservation_target[0].capacity_reservation_id == "cr-0123456789abcdef0" &&
      aws_instance.server.subnet_id == "subnet-0123456789abcdef0"
    )
    error_message = "A capacity-block launch must name its market type, its reservation, and the zone's subnet."
  }
}

run "a_capacity_block_market_needs_its_reservation" {
  command = plan

  variables {
    market_type = "capacity-block"
  }

  expect_failures = [aws_instance.server]
}

run "a_zone_needs_its_subnet" {
  command = plan

  variables {
    availability_zone = "us-east-1-atl-2a"
  }

  expect_failures = [aws_instance.server]
}

run "a_second_region_suffixes_the_global_iam_names" {
  command = plan

  variables {
    region = "us-east-2"
  }

  assert {
    condition     = aws_iam_role.server.name == "drip-ai-model-server-us-east-2" && aws_iam_instance_profile.server.name == "drip-ai-model-server-us-east-2"
    error_message = "IAM names are account-global, so the role and instance profile outside us-east-1 must end in -<region>."
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
