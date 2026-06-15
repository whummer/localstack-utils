terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  default = "eu-central-1"
}

variable "app_name" {
  default = "product-catalog"
}

# bucket_suffix must be unique across all AWS accounts; use your account ID on real AWS
variable "bucket_suffix" {
  default = "local"
}

# ── DynamoDB ───────────────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "products" {
  name         = "${var.app_name}-products"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }

  tags = { App = var.app_name }
}

# ── IAM ────────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "lambda_exec" {
  name = "${var.app_name}-lambda-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "dynamodb" {
  name = "dynamodb"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:Scan",
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
      ]
      Resource = aws_dynamodb_table.products.arn
    }]
  })
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ── Lambda ─────────────────────────────────────────────────────────────────────

data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/../lambda/handler.zip"
  source_file = "${path.module}/../lambda/handler.py"
}

resource "aws_lambda_function" "api" {
  function_name    = var.app_name
  role             = aws_iam_role.lambda_exec.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.products.name
    }
  }

  tags = { App = var.app_name }
}

resource "aws_lambda_permission" "apigw" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

# ── API Gateway v2 (HTTP API) ──────────────────────────────────────────────────

resource "aws_apigatewayv2_api" "api" {
  name          = var.app_name
  protocol_type = "HTTP"
  # CORS headers are returned by the Lambda itself so they work identically
  # on both AWS and LocalStack without relying on gateway-level injection.
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "get_products" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "GET /products"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "post_products" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /products"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "options_products" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "OPTIONS /products"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "put_product" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "PUT /products/{id}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "delete_product" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "DELETE /products/{id}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "options_product" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "OPTIONS /products/{id}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
}

# ── S3 website ─────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "frontend" {
  bucket = "${var.app_name}-frontend-${var.bucket_suffix}"
  tags   = { App = var.app_name }
}

resource "aws_s3_bucket_website_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  index_document { suffix = "index.html" }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket     = aws_s3_bucket.frontend.id
  depends_on = [aws_s3_bucket_public_access_block.frontend]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
    }]
  })
}

resource "aws_s3_object" "index" {
  bucket       = aws_s3_bucket.frontend.id
  key          = "index.html"
  source       = "${path.module}/../frontend/index.html"
  content_type = "text/html"
  etag         = filemd5("${path.module}/../frontend/index.html")
}

# config.json is fetched by index.html on load to auto-populate the API URL
resource "aws_s3_object" "config" {
  bucket       = aws_s3_bucket.frontend.id
  key          = "config.json"
  content      = jsonencode({ apiUrl = trim(aws_apigatewayv2_stage.default.invoke_url, "/") })
  content_type = "application/json"
  depends_on   = [aws_apigatewayv2_stage.default]
}

# ── Outputs ────────────────────────────────────────────────────────────────────

output "api_url" {
  value       = trim(aws_apigatewayv2_stage.default.invoke_url, "/")
  description = "API Gateway HTTP API endpoint"
}

output "table_name" {
  value = aws_dynamodb_table.products.name
}

output "function_name" {
  value = aws_lambda_function.api.function_name
}

output "frontend_bucket" {
  value = aws_s3_bucket.frontend.bucket
}

output "website_url_aws" {
  value       = "http://${aws_s3_bucket_website_configuration.frontend.website_endpoint}"
  description = "S3 website URL (AWS)"
}

output "website_url_local" {
  value       = "http://${aws_s3_bucket.frontend.bucket}.s3-website.localhost.localstack.cloud:4566"
  description = "S3 website URL (LocalStack)"
}
