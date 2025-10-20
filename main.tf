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
# IAM ROLE e POLÍTICAS
# =============================

resource "aws_iam_role" "lambda_role" {
  name = "lambda_hello_world_role"

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

# Permissão básica de logs
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# =============================
# S3 BUCKET para armazenar registros da Lambda
# =============================

resource "aws_s3_bucket" "lambda_records" {
  bucket = "lambda-records-${random_id.suffix.hex}"
  tags = {
    Name = "lambda-records"
  }
}

# Sufixo aleatório pro nome do bucket (S3 exige nomes únicos globalmente)
resource "random_id" "suffix" {
  byte_length = 4
}

# Permissão para a Lambda escrever no S3
resource "aws_iam_policy" "lambda_s3_policy" {
  name        = "lambda_s3_write_policy"
  description = "Permite que a Lambda grave no bucket S3"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:PutObject"],
        Resource = ["${aws_s3_bucket.lambda_records.arn}/*"]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_s3_policy_attach" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = aws_iam_policy.lambda_s3_policy.arn
}

# =============================
# ARQUIVO E LAMBDA FUNCTION
# =============================

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_function.py"
  output_path = "${path.module}/lambda_function.zip"
}

resource "aws_lambda_function" "hello_world" {
  function_name = "hello_world_lambda"
  role          = aws_iam_role.lambda_role.arn
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.9"

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      BUCKET_NAME = aws_s3_bucket.lambda_records.bucket
    }
  }
}

# =============================
# TRIGGER: EXECUÇÃO AGENDADA (CRON 13h BRASÍLIA = 16h UTC)
# =============================

resource "aws_cloudwatch_event_rule" "lambda_schedule" {
  name                = "lambda_daily_trigger"
  description         = "Executa a Lambda todos os dias às 13h (horário de Brasília)"
  schedule_expression = "cron(0 12 * * ? *)"
}

resource "aws_cloudwatch_event_target" "lambda_target" {
  rule      = aws_cloudwatch_event_rule.lambda_schedule.name
  target_id = "lambda-scheduled"
  arn       = aws_lambda_function.hello_world.arn
}

# Permissão para o EventBridge invocar a Lambda
resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.hello_world.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lambda_schedule.arn
}

