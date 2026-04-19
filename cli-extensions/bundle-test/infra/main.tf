terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "bundle-test-artifacts"
}

resource "aws_sqs_queue" "events" {
  name = "bundle-test-events"
}

resource "aws_ssm_parameter" "version" {
  name  = "/bundle-test/version"
  type  = "String"
  value = "1.0.0"
}
