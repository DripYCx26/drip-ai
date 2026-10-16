# Offline proof for the lane runner. Every provider call is mocked, so these
# plans never contact AWS or create an instance.

variables {
  project                             = "drip-ai"
  region                              = "us-east-1"
  runner_instance_type                = "m7i.2xlarge"
  runner_root_volume_gb               = 200
  runner_subnet_id                    = ""
  runner_instance_profile_arn         = "arn:aws:iam::123456789012:instance-profile/drip-ai-lane-runner"
  runner_image                        = "registry.example.invalid/example/lane-runner:review"
  ranked_queue_uri                    = "s3://ranked-queue/seeds/library/build_rank.json"
  lane_runner_enabled                 = true
  lane_count                          = 30
  model_endpoint_url                  = "https://model.example.invalid/v1"
  model_name                          = "example-model"
  model_endpoint_secret_parameter_arn = "arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/lane-model-api-key"
  git_push_secret_parameter_arn       = "arn:aws:ssm:us-east-1:123456789012:parameter/drip-ai/git-push-token"
}

mock_provider "aws" {
  mock_resource "aws_instance" {
    defaults = {
      id = "i-0123456789abcdef0"
    }
  }
}

override_data {
  target = data.aws_ssm_parameter.runner_ami
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

run "default_lanes_and_endpoint_environment" {
  command = apply

  assert {
    condition = alltrue([
      strcontains(aws_instance.runner.user_data, "BUILD_LANES_ENABLED=1"),
      strcontains(aws_instance.runner.user_data, "BUILD_LANE_COUNT=30"),
      strcontains(aws_instance.runner.user_data, "ranked_queue_uri=\"s3://ranked-queue/seeds/library/build_rank.json\""),
      strcontains(aws_instance.runner.user_data, "model_endpoint_url=\"https://model.example.invalid/v1\""),
      strcontains(aws_instance.runner.user_data, "model_name=\"example-model\""),
      strcontains(aws_instance.runner.user_data, "BUILD_RANKED_QUEUE_URI=$ranked_queue_uri"),
      strcontains(aws_instance.runner.user_data, "BUILD_MODEL=$model_name"),
      strcontains(aws_instance.runner.user_data, "OPENAI_BASE_URL=$model_endpoint_url"),
      strcontains(aws_instance.runner.user_data, "rank_path=\"$kindness_state/seeds/library/build_rank.json\""),
      strcontains(aws_instance.runner.user_data, "aws s3 cp --region us-east-1 --only-show-errors"),
      strcontains(aws_instance.runner.user_data, "--mount type=bind,src=$kindness_state,dst=/opt/drip-kindness"),
      strcontains(aws_instance.runner.user_data, "git_push_secret_parameter="),
      strcontains(aws_instance.runner.user_data, "parameter/drip-ai/git-push-token"),
      strcontains(aws_instance.runner.user_data, "GIT_PUSH_TOKEN=$git_push_token"),
    ])
    error_message = "The runner must receive the enabled default, ranked queue, endpoint URL, model, and lane count."
  }

  assert {
    condition     = length(aws_security_group.runner.ingress) == 0
    error_message = "The lane runner security group must have no ingress rules."
  }
}

run "endpoint_secret_is_optional" {
  command = plan

  variables {
    model_endpoint_secret_parameter_arn = ""
    git_push_secret_parameter_arn       = ""
  }

  assert {
    condition = alltrue([
      strcontains(aws_instance.runner.user_data, "endpoint_secret_parameter=\"\""),
      strcontains(aws_instance.runner.user_data, "OPENAI_API_KEY=$endpoint_api_key"),
      strcontains(aws_instance.runner.user_data, "git_push_secret_parameter=\"\""),
    ])
    error_message = "An empty secret reference must leave the API key empty without a secret read."
  }
}

run "lane_count_must_be_positive" {
  command = plan

  variables {
    lane_count = 0
  }

  expect_failures = [var.lane_count]
}

run "queue_must_be_an_s3_uri" {
  command = plan

  variables {
    ranked_queue_uri = "https://queue.example.invalid/build_rank.json"
  }

  expect_failures = [var.ranked_queue_uri]
}
