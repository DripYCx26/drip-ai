variable "project" {
  description = "Project tag value and name prefix for every resource. Every endpoint, model, configuration, and log group a race creates starts with <project>-."
  type        = string
  default     = "drip-ai"

  validation {
    condition     = var.project == "drip-ai"
    error_message = "project must be drip-ai: sagemaker/race.py names every endpoint drip-ai-..., and the execution role writes logs only under /aws/sagemaker/Endpoints/<project>-*."
  }
}

variable "region" {
  description = "AWS region. Must match the shared stack, which owns that region's models bucket and API key."
  type        = string
  default     = "us-east-1"
}
