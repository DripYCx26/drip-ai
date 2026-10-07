# Local state on purpose: this root creates the bucket every other root keeps
# its state in. The state file stays on the operator's disk (gitignored); a lost
# copy is recovered by importing the bucket.
terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
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
      Stack     = "bootstrap"
    }
  }
}
