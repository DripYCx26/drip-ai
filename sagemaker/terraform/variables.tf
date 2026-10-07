variable "project" {
  description = "Project tag value and name prefix for every resource. Each endpoint is named <project>-<instance type without ml., dots as dashes>."
  type        = string
  default     = "drip-ai"
}

variable "region" {
  description = "AWS region. Must match the shared stack, which owns the models bucket and the API key."
  type        = string
  default     = "us-east-1"
}

variable "model_source" {
  description = "Hugging Face repo id (Org/Name), or with model_from_s3 the name of a folder under s3://<bucket>/models/. Passed per run by sagemaker/start.sh."
  type        = string

  # The value is interpolated into the model environment, an S3 path, and an IAM resource.
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$", var.model_source))
    error_message = "model_source must be Org/Name or a folder name: letters, digits, '.', '_', '-', each part starting with a letter or digit."
  }

  # SageMaker downloads a Hugging Face model itself, so a bare folder name
  # would be sent to the Hub instead of read from S3.
  validation {
    condition     = var.model_from_s3 || can(regex("^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$", var.model_source))
    error_message = "With model_from_s3 = false, model_source must be a Hugging Face id (Org/Name)."
  }
}

variable "model_from_s3" {
  description = "True when model_source names a folder under s3://<bucket>/models/ instead of a Hugging Face repo."
  type        = bool
  default     = false
}

variable "started_by" {
  description = "Identity that started the endpoint, recorded as its StartedBy tag. Passed per run by sagemaker/start.sh."
  type        = string

  validation {
    condition     = length(trimspace(var.started_by)) > 0
    error_message = "started_by must not be empty."
  }

  # AWS tag value limits, checked here so a bad value fails at plan rather than mid-apply.
  validation {
    condition     = length(var.started_by) <= 256 && can(regex("^[\\p{L}\\p{Z}\\p{N}_.:/=+@-]*$", var.started_by))
    error_message = "started_by must be at most 256 characters of letters, digits, spaces, and _ . : / = + - @."
  }
}

variable "instance_types" {
  description = "SageMaker instance types raced in order of preference: one endpoint each, the first InService is kept and the others are deleted. Each bills per instance hour from creation until deleted."
  type        = list(string)
  default     = ["ml.p5en.48xlarge", "ml.p5e.48xlarge"]

  # Every entry is an endpoint billing at once while the race runs, so the count is capped.
  validation {
    condition     = length(var.instance_types) >= 1 && length(var.instance_types) <= 3
    error_message = "instance_types must list 1 to 3 instance types."
  }

  validation {
    condition     = length(distinct(var.instance_types)) == length(var.instance_types)
    error_message = "instance_types must not repeat an instance type."
  }

  validation {
    condition     = alltrue([for type in var.instance_types : can(regex("^ml\\.[a-z0-9-]+\\.[a-z0-9]+$", type))])
    error_message = "Each instance_types entry must be an ml.<family>.<size> instance type."
  }
}

variable "tensor_parallel_size" {
  description = "GPUs to shard the model across. Must equal the GPU count of every instance_types entry."
  type        = number
  default     = 8
}

variable "inference_ami_version" {
  description = "SageMaker inference AMI version; sets the host driver and CUDA version."
  type        = string
  default     = "al2023-ami-sagemaker-inference-gpu-4-1"
}

variable "volume_size_gb" {
  description = "Endpoint EBS volume size. Leave null on instance types with local NVMe storage, such as p5 and p5en."
  type        = number
  nullable    = true
  default     = null
}

variable "startup_timeout_seconds" {
  description = "Seconds SageMaker waits for the container health check to pass."
  type        = number
  default     = 3600

  validation {
    condition     = var.startup_timeout_seconds >= 60 && var.startup_timeout_seconds <= 3600
    error_message = "startup_timeout_seconds must be between 60 and 3600."
  }
}

variable "download_timeout_seconds" {
  description = "Seconds SageMaker allows for downloading S3 model data."
  type        = number
  default     = 3600

  validation {
    condition     = var.download_timeout_seconds >= 60 && var.download_timeout_seconds <= 3600
    error_message = "download_timeout_seconds must be between 60 and 3600."
  }
}

variable "served_model_name" {
  description = "Model name clients send in requests."
  type        = string
  default     = "drip-coder"
}

variable "max_model_len" {
  description = "Context window served, in tokens. Larger values need more free VRAM for the KV cache. Codex gets this window when the tunnel links it."
  type        = number
  default     = 131072

  validation {
    condition     = var.max_model_len >= 1 && floor(var.max_model_len) == var.max_model_len
    error_message = "max_model_len must be a positive whole number of tokens."
  }
}

variable "vllm_model_args" {
  description = "vLLM serve flags from the model's profile in shared/model-profiles/profiles.json. Passed per run by sagemaker/start.sh; vllm_extra_args wins on a flag both set."
  type        = list(string)
  default     = []
}

variable "vllm_extra_args" {
  description = "Your extra vLLM serve flags. They override the model profile's flag of the same name; see docs/models.md."
  type        = list(string)
  default     = []
}
