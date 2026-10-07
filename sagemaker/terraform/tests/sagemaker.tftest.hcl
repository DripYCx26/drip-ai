# Offline proof for the SageMaker endpoint. Every provider is mocked, so no run
# here reaches AWS, even the ones with command = apply.

# terraform test also loads an operator's terraform.tfvars in this directory.
# Pinning every variable keeps these runs independent of that file; update this
# block when a default changes.
variables {
  project                  = "drip-ai"
  region                   = "us-east-1"
  model_source             = "Org/Model"
  model_from_s3            = false
  started_by               = "arn:aws:sts::123456789012:assumed-role/Dev/alice@example.com"
  instance_types           = ["ml.p5en.48xlarge", "ml.p5e.48xlarge"]
  tensor_parallel_size     = 8
  inference_ami_version    = "al2023-ami-sagemaker-inference-gpu-4-1"
  volume_size_gb           = null
  startup_timeout_seconds  = 3600
  download_timeout_seconds = 3600
  served_model_name        = "drip-coder"
  max_model_len            = 131072
  vllm_model_args          = []
  vllm_extra_args          = ["--enable-auto-tool-choice", "--tool-call-parser", "override_tool", "--reasoning-parser", "example_reasoning"]
}

# The provider checks ARN shape even when mocked, so ARNs that feed other
# resources get realistic defaults.
mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/sagemaker/Endpoints/drip-ai-mock"
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
  target = data.aws_ecr_image.vllm
  values = {
    id           = "sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"
    image_digest = "sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"
  }
}

run "pins_the_image_and_keeps_secrets_out_of_env" {
  command = plan

  assert {
    condition = alltrue([
      for key in keys(aws_sagemaker_model.server.primary_container[0].environment) :
      !can(regex("(?i)(token|key|secret|password)", key))
    ])
    error_message = "The model environment must carry no secret-shaped key."
  }

  assert {
    condition     = lookup(aws_sagemaker_model.server.primary_container[0].environment, "STANDARD_AUTO_INSTALL_REQ", "") == "false"
    error_message = "The model must not pip-install a requirements.txt from the weights prefix at startup."
  }

  assert {
    condition     = aws_sagemaker_model.server.primary_container[0].image == "763104351884.dkr.ecr.us-east-1.amazonaws.com/vllm@sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"
    error_message = "The model image must be pinned by digest."
  }

  assert {
    condition     = aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_MODEL"] == "Org/Model"
    error_message = "A Hugging Face source must pass through as SM_VLLM_MODEL."
  }

  assert {
    condition = (
      aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_ENABLE_AUTO_TOOL_CHOICE"] == "true" &&
      aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_TOOL_CALL_PARSER"] == "override_tool" &&
      aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_REASONING_PARSER"] == "example_reasoning"
    )
    error_message = "vllm_extra_args must become SM_VLLM_* entries: a bare flag is \"true\", a flag with a value keeps it."
  }

  assert {
    condition     = length(aws_sagemaker_model.server.primary_container[0].model_data_source) == 0
    error_message = "A Hugging Face source must not mount S3 weights."
  }

  assert {
    condition     = alltrue([for config in aws_sagemaker_endpoint_configuration.server : config.production_variants[0].inference_ami_version == "al2023-ami-sagemaker-inference-gpu-4-1"])
    error_message = "Every endpoint must use the default inference AMI version."
  }

  assert {
    condition     = output.region == "us-east-1"
    error_message = "The region output must name the stack's region."
  }
}

run "races_one_named_endpoint_per_instance_type" {
  command = plan

  assert {
    condition = (
      aws_sagemaker_endpoint.server["ml.p5en.48xlarge"].name == "drip-ai-p5en-48xlarge" &&
      aws_sagemaker_endpoint.server["ml.p5e.48xlarge"].name == "drip-ai-p5e-48xlarge" &&
      length(aws_sagemaker_endpoint.server) == 2
    )
    error_message = "Each instance type must get its own endpoint, named drip-ai-<type without ml., dots as dashes>."
  }

  assert {
    condition = alltrue([
      for type in ["ml.p5en.48xlarge", "ml.p5e.48xlarge"] :
      aws_sagemaker_endpoint_configuration.server[type].production_variants[0].instance_type == type &&
      aws_cloudwatch_log_group.sagemaker[type].name == "/aws/sagemaker/Endpoints/${aws_sagemaker_endpoint.server[type].name}"
    ])
    error_message = "Each endpoint must run on its own instance type and log to its own /aws/sagemaker/Endpoints/<name> group."
  }

  assert {
    condition = alltrue([
      for endpoint in aws_sagemaker_endpoint.server :
      endpoint.tags == tomap({ StartedBy = "arn:aws:sts::123456789012:assumed-role/Dev/alice@example.com" })
    ])
    error_message = "Every endpoint must carry the starting identity as its StartedBy tag."
  }
}

run "rejects_an_empty_instance_types" {
  command = plan

  variables {
    instance_types = []
  }

  expect_failures = [var.instance_types]
}

run "rejects_a_repeated_instance_type" {
  command = plan

  variables {
    instance_types = ["ml.p5en.48xlarge", "ml.p5e.48xlarge", "ml.p5en.48xlarge"]
  }

  expect_failures = [var.instance_types]
}

run "rejects_more_than_three_instance_types" {
  command = plan

  variables {
    instance_types = ["ml.p5en.48xlarge", "ml.p5e.48xlarge", "ml.p5.48xlarge", "ml.p6-b200.48xlarge"]
  }

  expect_failures = [var.instance_types]
}

run "max_model_len_reaches_the_environment" {
  command = plan

  assert {
    condition     = aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_MAX_MODEL_LEN"] == "131072"
    error_message = "max_model_len must become SM_VLLM_MAX_MODEL_LEN."
  }
}

run "rejects_a_max_model_len_that_is_not_a_positive_whole_number" {
  command = plan

  variables {
    max_model_len = 0
  }

  expect_failures = [var.max_model_len]
}

run "rejects_a_fractional_max_model_len" {
  command = plan

  variables {
    max_model_len = 1024.5
  }

  expect_failures = [var.max_model_len]
}

run "profile_args_become_env_and_extra_args_win_on_a_shared_flag" {
  command = plan

  variables {
    vllm_model_args = ["--enable-auto-tool-choice", "--tool-call-parser", "profile_tool", "--reasoning-parser", "profile_reasoning"]
    vllm_extra_args = ["--tool-call-parser", "override_tool", "--enforce-eager"]
  }

  assert {
    condition = (
      aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_ENABLE_AUTO_TOOL_CHOICE"] == "true" &&
      aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_TOOL_CALL_PARSER"] == "override_tool" &&
      aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_REASONING_PARSER"] == "profile_reasoning" &&
      aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_ENFORCE_EAGER"] == "true"
    )
    error_message = "Profile args must reach the environment, and vllm_extra_args must win on a flag both set."
  }
}

run "rejects_a_stray_profile_arg" {
  command = plan

  variables {
    vllm_model_args = ["--tool-call-parser", "profile_tool", "extra"]
  }

  expect_failures = [aws_sagemaker_model.server]
}

run "rejects_a_secret_shaped_profile_arg" {
  command = plan

  variables {
    vllm_model_args = ["--hf-token", "x"]
  }

  expect_failures = [aws_sagemaker_model.server]
}

run "rejects_a_flag_that_a_variable_owns" {
  command = plan

  variables {
    vllm_extra_args = ["--max-model-len", "524288"]
  }

  expect_failures = [aws_sagemaker_model.server]
}

run "rejects_an_empty_started_by" {
  command = plan

  variables {
    started_by = " "
  }

  expect_failures = [var.started_by]
}

# Checked per lane: only the g6e.12xlarge lane has 4 GPUs, so the p5en lane fails.
run "rejects_tensor_parallel_below_a_lanes_gpu_count" {
  command = plan

  variables {
    instance_types       = ["ml.g6e.12xlarge", "ml.p5en.48xlarge"]
    tensor_parallel_size = 4
  }

  expect_failures = [aws_sagemaker_endpoint_configuration.server]
}

run "rejects_a_moved_image_tag" {
  command = plan

  override_data {
    target = data.aws_ecr_image.vllm
    values = {
      id           = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
      image_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    }
  }

  expect_failures = [aws_sagemaker_model.server]
}

run "rejects_a_folder_name_without_s3" {
  command = plan

  variables {
    model_source  = "example-model"
    model_from_s3 = false
  }

  expect_failures = [var.model_source]
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

run "rejects_a_stray_extra_arg" {
  command = plan

  variables {
    vllm_extra_args = ["--tool-call-parser", "override_tool", "extra"]
  }

  expect_failures = [aws_sagemaker_model.server]
}

run "rejects_a_secret_shaped_extra_arg" {
  command = plan

  variables {
    vllm_extra_args = ["--api-key", "x"]
  }

  expect_failures = [aws_sagemaker_model.server]
}

# Without the flag-shape rule, '--hf_token' would pass as the value of the flag before it and become SM_VLLM_HF_TOKEN.
run "rejects_an_underscore_secret_flag_hidden_as_a_value" {
  command = plan

  variables {
    vllm_extra_args = ["--enable-auto-tool-choice", "--hf_token", "x"]
  }

  expect_failures = [aws_sagemaker_model.server]
}

run "allows_token_named_flags_that_are_not_secrets" {
  command = plan

  variables {
    vllm_extra_args = ["--tokenizer-mode", "auto", "--max-num-batched-tokens", "8192", "--return-tokens-as-token-ids"]
  }

  assert {
    condition     = aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_RETURN_TOKENS_AS_TOKEN_IDS"] == "true"
    error_message = "A non-secret flag containing 'token' must pass through to the environment."
  }
}

# The runs below apply against the mocked provider so that ARNs are known.
# State persists from here on, so plan-only runs stay above.
run "s3_weights_mount_under_opt_ml_model" {
  variables {
    model_from_s3 = true
    model_source  = "example-model"
  }

  assert {
    condition     = aws_sagemaker_model.server.primary_container[0].model_data_source[0].s3_data_source[0].s3_uri == "s3://drip-ai-models-123456789012/models/example-model/"
    error_message = "S3 weights must come from models/<model_source>/ in the looked-up models bucket."
  }

  assert {
    condition     = aws_sagemaker_model.server.primary_container[0].environment["SM_VLLM_MODEL"] == "/opt/ml/model"
    error_message = "S3 weights must be served from /opt/ml/model."
  }
}

run "execution_policy_limits_wildcard_resources" {
  variables {
    model_from_s3 = true
    model_source  = "example-model"
  }

  assert {
    condition = sort(distinct(flatten([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement :
      flatten([statement.Action]) if contains(flatten([statement.Resource]), "*")
    ]))) == sort(["cloudwatch:PutMetricData", "ecr:GetAuthorizationToken"])
    error_message = "Only ecr:GetAuthorizationToken and cloudwatch:PutMetricData may use Resource \"*\"."
  }

  assert {
    condition = sort(distinct(flatten([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement : flatten([statement.Action])
      ]))) == sort([
      "cloudwatch:PutMetricData",
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetAuthorizationToken",
      "ecr:GetDownloadUrlForLayer",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
      "s3:GetObject",
      "s3:ListBucket",
    ])
    error_message = "The execution policy must grant exactly the listed read and log actions; no ssm, iam, or S3 write."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement :
      statement.Effect == "Allow" && !contains(keys(statement), "NotResource") && !contains(keys(statement), "NotAction")
    ])
    error_message = "The execution policy must use plain Allow statements only."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement :
      alltrue([for resource in flatten([statement.Resource]) : endswith(resource, "/models/example-model/*")])
      if contains(flatten([statement.Action]), "s3:GetObject")
    ])
    error_message = "s3:GetObject must be limited to models/<model_source>/*, not the whole bucket."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.sagemaker.policy).Statement :
      try(sort(statement.Condition.StringLike["s3:prefix"]), []) == sort(["models/example-model/", "models/example-model/*"])
      if contains(flatten([statement.Action]), "s3:ListBucket")
    ])
    error_message = "s3:ListBucket must carry an s3:prefix condition limited to models/<model_source>/."
  }

  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_role.sagemaker.assume_role_policy).Statement :
      statement.Effect == "Allow" &&
      statement.Action == "sts:AssumeRole" &&
      statement.Principal == { Service = "sagemaker.amazonaws.com" } &&
      try(statement.Condition.StringEquals["aws:SourceAccount"], "") == "123456789012"
    ])
    error_message = "The trust policy must admit only sagemaker.amazonaws.com, conditioned on aws:SourceAccount = this account."
  }
}
