output "lambda_1_name" {
  description = "Nome da primeira Lambda"
  value       = aws_lambda_function.lambda_1.function_name
}

output "lambda_2_name" {
  description = "Nome da segunda Lambda"
  value       = aws_lambda_function.lambda_2.function_name
}

output "lambda_1_arn" {
  description = "ARN da Lambda 1"
  value       = aws_lambda_function.lambda_1.arn
}

output "lambda_2_arn" {
  description = "ARN da Lambda 2"
  value       = aws_lambda_function.lambda_2.arn
}
