variable "project" {
  description = "Project tag value and name prefix for every resource."
  type        = string
  default     = "drip-ai"
}

variable "region" {
  description = "AWS Region for the runner and its S3 and SSM reads."
  type        = string
  default     = "us-east-1"
}

variable "runner_instance_type" {
  description = "CPU EC2 instance type for the always-on build-lane runner."
  type        = string
  default     = "m7i.2xlarge"
}

variable "runner_root_volume_gb" {
  description = "Encrypted root volume for the runner image, worktrees, and Docker layers."
  type        = number
  default     = 200

  validation {
    condition     = var.runner_root_volume_gb >= 30 && floor(var.runner_root_volume_gb) == var.runner_root_volume_gb
    error_message = "runner_root_volume_gb must be a whole number of at least 30."
  }
}

variable "runner_subnet_id" {
  description = "Optional subnet for the runner. Empty lets EC2 choose a default VPC subnet."
  type        = string
  default     = ""
}

variable "runner_instance_profile_arn" {
  description = "Existing IAM instance profile name or ARN with runner permissions already attached."
  type        = string

  validation {
    condition = can(regex(
      "^(arn:[A-Za-z0-9-]+:iam::[0-9]{12}:instance-profile/[^[:space:]]+|[A-Za-z0-9+=,.@_-]+)$",
      var.runner_instance_profile_arn,
    ))
    error_message = "runner_instance_profile_arn must be an existing profile name or IAM instance-profile ARN."
  }
}

variable "runner_image" {
  description = "Container image that contains the ranked build-lane runner."
  type        = string

  validation {
    condition     = length(trimspace(var.runner_image)) > 0 && !can(regex("[[:space:]]", var.runner_image))
    error_message = "runner_image must be a non-empty image reference without whitespace."
  }
}

variable "ranked_queue_uri" {
  description = "S3 URI of the ranked build queue consumed by the lane runner."
  type        = string

  validation {
    condition     = can(regex("^s3://[^/]+/.+$", var.ranked_queue_uri))
    error_message = "ranked_queue_uri must be an s3://bucket/key URI."
  }
}

variable "lane_runner_enabled" {
  description = "Whether the runner starts its build lanes. Set false to stop the consumer."
  type        = bool
  default     = true
}

variable "lane_count" {
  description = "Number of build lanes the runner image may execute concurrently."
  type        = number
  default     = 30

  validation {
    condition     = var.lane_count >= 1 && floor(var.lane_count) == var.lane_count
    error_message = "lane_count must be a positive whole number."
  }
}

variable "model_endpoint_url" {
  description = "OpenAI-compatible model endpoint URL, including its /v1 path when required."
  type        = string

  validation {
    condition     = can(regex("^https?://[^[:space:]\"']+$", var.model_endpoint_url))
    error_message = "model_endpoint_url must be an http:// or https:// URL without whitespace or quotes."
  }
}

variable "model_name" {
  description = "Model identifier sent to the OpenAI-compatible endpoint."
  type        = string

  validation {
    condition     = length(trimspace(var.model_name)) > 0 && !can(regex("[[:space:]]", var.model_name))
    error_message = "model_name must be a non-empty identifier without whitespace."
  }
}

variable "model_endpoint_secret_parameter_arn" {
  description = "Optional SSM SecureString ARN containing the model endpoint API key."
  type        = string
  default     = ""

  validation {
    condition = var.model_endpoint_secret_parameter_arn == "" || can(regex(
      "^arn:[A-Za-z0-9-]+:ssm:[A-Za-z0-9-]+:[0-9]{12}:parameter/[A-Za-z0-9_.+=,@-]+(/[A-Za-z0-9_.+=,@-]+)*$",
      var.model_endpoint_secret_parameter_arn,
    ))
    error_message = "model_endpoint_secret_parameter_arn must be empty or an SSM parameter ARN."
  }
}

variable "git_push_secret_parameter_arn" {
  description = "Optional SSM SecureString ARN for the image's Git remote push credential."
  type        = string
  default     = ""

  validation {
    condition = var.git_push_secret_parameter_arn == "" || can(regex(
      "^arn:[A-Za-z0-9-]+:ssm:[A-Za-z0-9-]+:[0-9]{12}:parameter/[A-Za-z0-9_.+=,@-]+(/[A-Za-z0-9_.+=,@-]+)*$",
      var.git_push_secret_parameter_arn,
    ))
    error_message = "git_push_secret_parameter_arn must be empty or an SSM parameter ARN."
  }
}
