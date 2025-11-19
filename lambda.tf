# lambda.tf

# Empacota os códigos Python em zips
data "archive_file" "data_cleaner_zip" {
  type        = "zip"
  source_dir = "${path.module}/lambda/OneVisionDataCleaner"
  output_path = "${path.module}/lambda/OneVisionDataCleaner.zip"
}

data "archive_file" "data_collector_zip" {
  type        = "zip"
  source_dir = "${path.module}/lambda/OneVisionDataCollector"
  output_path = "${path.module}/lambda/OneVisionDataCollector.zip"
}

resource "aws_lambda_function" "OneVisionDataCleanerFunction" {
  function_name = "OneVisionDataCleanerFunction"
  role          = aws_iam_role.OneVisionDataCleanerRole.arn
  handler       = "index.lambda_handler"
  runtime       = var.lambda_runtime
  filename      = data.archive_file.data_cleaner_zip.output_path
  source_code_hash = data.archive_file.data_cleaner_zip.output_base64sha256
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_seconds
  publish       = true

  environment {
    variables = {
      TRIGGER_TIME_BRT = "08:50"
    }
  }
}

resource "aws_lambda_function" "OneVisionDataCollectorFunction" {
  function_name = "OneVisionDataCollectorFunction"
  role          = aws_iam_role.OneVisionDataCollectorRole.arn
  handler       = "index.lambda_handler"
  runtime       = var.lambda_runtime
  filename      = data.archive_file.data_collector_zip.output_path
  source_code_hash = data.archive_file.data_collector_zip.output_base64sha256
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_seconds
  publish       = true

  environment {
    variables = {
      TRIGGER_TIME_BRT = "09:00"
    }
  }
}

# (Opcional) Log Groups explícitos com retenção
resource "aws_cloudwatch_log_group" "lg_a" {
  name              = "/aws/lambda/${aws_lambda_function.OneVisionDataCleanerFunction.function_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "lg_b" {
  name              = "/aws/lambda/${aws_lambda_function.OneVisionDataCollectorFunction.function_name}"
  retention_in_days = 30
}