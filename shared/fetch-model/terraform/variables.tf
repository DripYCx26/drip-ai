variable "project" {
  description = "Project tag value and name prefix for every resource."
  type        = string
  default     = "drip-ai"
}

variable "region" {
  description = "AWS region. Must match the shared stack, which owns the models bucket."
  type        = string
  default     = "us-east-1"
}
