terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state — use S3 backend so team + CI share the same state
  backend "s3" {
    bucket         = "resume-parser-tfstate"   # create this bucket manually first
    key            = "resume-parser/terraform.tfstate"
    region         = "us-east-2"
    encrypt        = true
    dynamodb_table = "resume-parser-tflock"    # create this table manually first
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name_prefix = "resume-parser-${var.environment}"
  common_tags = {
    Project     = "resume-parser"
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
