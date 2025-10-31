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
resource "aws_iam_role" "lambda_role_1" {
  name = "lambda_role_1"

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

resource "aws_iam_role_policy_attachment" "lambda_logs_1" {
  role       = aws_iam_role.lambda_role_1.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Role da Lambda 2
resource "aws_iam_role" "lambda_role_2" {
  name = "lambda_role_2"

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

resource "aws_iam_role_policy_attachment" "lambda_logs_2" {
  role       = aws_iam_role.lambda_role_2.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# =============================
# ARQUIVOS DAS LAMBDAS
# =============================

data "archive_file" "lambda1_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_function_1.py"
  output_path = "${path.module}/lambda_function_1.zip"
}

data "archive_file" "lambda2_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_function_2.py"
  output_path = "${path.module}/lambda_function_2.zip"
}

# =============================
# LAMBDA 1
# =============================

resource "aws_lambda_function" "lambda_1" {
  function_name = "lambda_1"
  role          = aws_iam_role.lambda_role_1.arn
  handler       = "lambda_function_1.lambda_handler"
  runtime       = "python3.9"

  filename         = data.archive_file.lambda1_zip.output_path
  source_code_hash = data.archive_file.lambda1_zip.output_base64sha256
}

# =============================
# LAMBDA 2
# =============================

resource "aws_lambda_function" "lambda_2" {
  function_name = "lambda_2"
  role          = aws_iam_role.lambda_role_2.arn
  handler       = "lambda_function_2.lambda_handler"
  runtime       = "python3.9"

  filename         = data.archive_file.lambda2_zip.output_path
  source_code_hash = data.archive_file.lambda2_zip.output_base64sha256
}

# =============================
# EVENTBRIDGE - LAMBDA 1
# =============================

resource "aws_cloudwatch_event_rule" "lambda1_schedule" {
  name                = "lambda1_daily_trigger"
  description         = "Executa a Lambda 1 todos os dias às 13h (horário de Brasília)"
  schedule_expression = "cron(0 16 * * ? *)"
}

resource "aws_cloudwatch_event_target" "lambda1_target" {
  rule      = aws_cloudwatch_event_rule.lambda1_schedule.name
  target_id = "lambda1-scheduled"
  arn       = aws_lambda_function.lambda_1.arn

  depends_on = [aws_lambda_permission.allow_eventbridge_lambda1]
}

resource "aws_lambda_permission" "allow_eventbridge_lambda1" {
  statement_id  = "AllowExecutionFromEventBridgeLambda1"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lambda_1.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lambda1_schedule.arn
}

# =============================
# EVENTBRIDGE - LAMBDA 2
# =============================

resource "aws_cloudwatch_event_rule" "lambda2_schedule" {
  name                = "lambda2_daily_trigger"
  description         = "Executa a Lambda 2 todos os dias às 14h (horário de Brasília)"
  schedule_expression = "cron(0 17 * * ? *)"
}

resource "aws_cloudwatch_event_target" "lambda2_target" {
  rule      = aws_cloudwatch_event_rule.lambda2_schedule.name
  target_id = "lambda2-scheduled"
  arn       = aws_lambda_function.lambda_2.arn

  depends_on = [aws_lambda_permission.allow_eventbridge_lambda2]
}

resource "aws_lambda_permission" "allow_eventbridge_lambda2" {
  statement_id  = "AllowExecutionFromEventBridgeLambda2"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lambda_2.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lambda2_schedule.arn
}
