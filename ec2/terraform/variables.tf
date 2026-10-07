variable "project" {
  description = "Project tag value and name prefix for every resource."
  type        = string
  default     = "drip-ai"
}

variable "region" {
  description = "AWS region. Must match the shared stack. The G and VT on-demand vCPU quota must be raised here first."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "GPU instance type. See docs/models.md for sizing."
  type        = string
  default     = "g6e.2xlarge"
}

variable "root_volume_gb" {
  description = "Root EBS size. Must hold the Docker image plus the model weights."
  type        = number
  default     = 300
}

variable "model_source" {
  description = "Hugging Face repo id (Org/Name), or with model_from_s3 the name of a folder under s3://<bucket>/models/."
  type        = string

  # The value is interpolated into the boot script and an S3 path.
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$", var.model_source))
    error_message = "model_source must be Org/Name or a folder name: letters, digits, '.', '_', '-', each part starting with a letter or digit."
  }
}

variable "model_from_s3" {
  description = "True when model_source names a folder under s3://<bucket>/models/ instead of a Hugging Face repo."
  type        = bool
  default     = false
}

variable "served_model_name" {
  description = "Model name clients send in requests."
  type        = string
  default     = "drip-coder"
}

variable "tensor_parallel_size" {
  description = "GPUs to shard the model across. Must equal the GPU count of instance_type."
  type        = number
  default     = 1
}

variable "max_model_len" {
  description = "Context window served. Larger values need more free VRAM for the KV cache."
  type        = number
  default     = 65536
}

variable "vllm_extra_args" {
  description = "Extra vLLM serve flags, such as the tool-call parser for the chosen model."
  type        = list(string)
  default     = []
}

variable "hf_token_parameter" {
  description = "Optional SSM SecureString name holding a Hugging Face token, for gated models. Empty means none. Must start with '/'."
  type        = string
  default     = ""

  # The name is concatenated into an ARN as :parameter${name}; a leading slash is required.
  validation {
    condition     = var.hf_token_parameter == "" || startswith(var.hf_token_parameter, "/")
    error_message = "hf_token_parameter must be empty or start with '/' (e.g. /drip-ai/ec2-hf-token)."
  }

  # The model-fetch job owns that name and deletes it after each launch, so an
  # EC2 server pointed at it would lose its token.
  validation {
    condition     = var.hf_token_parameter != "/${var.project}/hf-token"
    error_message = "hf_token_parameter must not be /${var.project}/hf-token; that name is reserved for the short-lived model-fetch token."
  }
}
