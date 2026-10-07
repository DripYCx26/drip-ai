variable "project" {
  description = "Project tag value and name prefix for every resource."
  type        = string
  default     = "drip-ai"
}

variable "region" {
  description = "AWS region for the state bucket. Every stack's backend uses this region."
  type        = string
  default     = "us-east-1"
}

variable "state_bucket_suffix" {
  description = "Optional suffix for the state bucket name, for when another account already owns the unsuffixed name. Empty keeps drip-ai-tfstate-<account>-<region>."
  type        = string
  default     = ""

  validation {
    condition     = var.state_bucket_suffix == "" || can(regex("^[a-z0-9]([a-z0-9-]{0,10}[a-z0-9])?$", var.state_bucket_suffix))
    error_message = "state_bucket_suffix must be empty, or 1 to 12 lowercase letters, digits, or inner hyphens."
  }
}
