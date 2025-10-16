output "lambda_function_name" {
  description = "Name of the deployed Lambda function"
  value       = aws_lambda_function.hello_world.function_name
}
