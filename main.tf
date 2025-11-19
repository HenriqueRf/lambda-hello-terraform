terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  cloud {
    organization = "testess0102"
    workspaces {
      name = "lambda-hello-world"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# =============================
# IAM ROLES e POLÍTICAS
# =============================

# Role da Lambda 1
resource "aws_iam_role" "onevision_data_collector_role" {
  name = "OneVisionDataCollectorRole"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "onevision_data_collector_policy" {
  role       = aws_iam_role.onevision_data_collector_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Role da Lambda 2
resource "aws_iam_role" "onevision_data_cleaner_role" {
  name = "OneVisionDataCleanerRole"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "onevision_data_cleaner_policy" {
  role       = aws_iam_role.onevision_data_cleaner_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# =============================
# ARQUIVOS DAS LAMBDAS
# =============================

data "archive_file" "data_collector_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda/OneVisionDataCollector"
  output_path = "${path.module}/lambda/OneVisionDataCollector.zip"
}

data "archive_file" "data_cleaner_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda/OneVisionDataCleaner"
  output_path = "${path.module}/lambda/OneVisionDataCleaner.zip"
}

# =============================
# LAMBDA 1 - Data Collector
# =============================

resource "aws_lambda_function" "OneVisionDataCollectorFunction" {
  function_name     = "OneVisionDataCollectorFunction"
  role              = aws_iam_role.onevision_data_collector_role.arn
  handler           = "index.lambda_handler"

  filename          = data.archive_file.data_collector_zip.output_path
  source_code_hash  = data.archive_file.data_collector_zip.output_base64sha256
  publish           = true
}

# =============================
# LAMBDA 2 - Data Cleaner
# =============================

resource "aws_lambda_function" "OneVisionDataCleanerFunction" {
  function_name     = "OneVisionDataCleanerFunction"
  role              = aws_iam_role.onevision_data_cleaner_role.arn
  handler           = "index.lambda_handler"

  filename          = data.archive_file.data_cleaner_zip.output_path
  source_code_hash  = data.archive_file.data_cleaner_zip.output_base64sha256
  publish           = true
}

# =============================
# EVENTBRIDGE - LAMBDA 1
# =============================

resource "aws_cloudwatch_event_rule" "onevision_data_collector_schedule" {
  name                = "OneVisionDataCollectorSchedule"
  description         = "Executa a função de coleta diariamente às 13h"
  schedule_expression = "cron(0 16 * * ? *)"
}

resource "aws_cloudwatch_event_target" "onevision_data_collector_target" {
  rule      = aws_cloudwatch_event_rule.onevision_data_collector_schedule.name
  target_id = "OneVisionDataCollectorTarget"
  arn       = aws_lambda_function.OneVisionDataCollectorFunction.arn

  depends_on = [aws_lambda_permission.allow_eventbridge_data_collector]
}

resource "aws_lambda_permission" "allow_eventbridge_data_collector" {
  statement_id  = "AllowExecutionFromEventBridgeDataCollector"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.OneVisionDataCollectorFunction.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.onevision_data_collector_schedule.arn
}

# =============================
# EVENTBRIDGE - LAMBDA 2 (Data Cleaner)
# =============================

resource "aws_cloudwatch_event_rule" "onevision_data_cleaner_schedule" {
  name                = "OneVisionDataCleanerSchedule"
  description         = "Executa a função de limpeza de dados diariamente às 14h (horário de Brasília)"
  schedule_expression = var.cleaner_cron_expression
}

resource "aws_cloudwatch_event_target" "onevision_data_cleaner_target" {
  rule      = aws_cloudwatch_event_rule.onevision_data_cleaner_schedule.name
  target_id = "OneVisionDataCleanerTarget"
  arn       = aws_lambda_function.OneVisionDataCleanerFunction.arn

  depends_on = [aws_lambda_permission.allow_eventbridge_data_cleaner]
}

resource "aws_lambda_permission" "allow_eventbridge_data_cleaner" {
  statement_id  = "AllowExecutionFromEventBridgeDataCleaner"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.OneVisionDataCleanerFunction.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.onevision_data_cleaner_schedule.arn
}

