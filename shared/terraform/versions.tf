terraform {
  required_version = ">= 1.11" # value_wo (write-only attributes) and ephemeral resources need 1.11.

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region

  # Every resource this stack creates carries these tags; see docs/DECISIONS.md.
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Stack     = "shared"
    }
  }
}
