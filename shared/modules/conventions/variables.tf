variable "project" {
  description = "Project tag value and name prefix for every resource."
  type        = string
}

variable "region" {
  description = "AWS region the calling root deploys into."
  type        = string
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
