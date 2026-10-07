variable "project" {
  description = "Project tag value and name prefix for every resource."
  type        = string
  default     = "drip-ai"
}

variable "region" {
  description = "AWS region for the models bucket and the API key."
  type        = string
  default     = "us-east-1"
}

variable "api_key_version" {
  description = "Raise by one to write a new random API key. Clients must fetch the new key afterwards."
  type        = number
  default     = 1

  validation {
    condition     = var.api_key_version >= 1 && floor(var.api_key_version) == var.api_key_version
    error_message = "api_key_version must be a whole number of at least 1."
  }
}
