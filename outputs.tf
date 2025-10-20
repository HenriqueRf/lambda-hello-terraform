output "lambda_function_name" {
  description = "Name of the deployed Lambda function"
  value       = aws_lambda_function.hello_world.function_name
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket that triggers the Lambda"
  value       = aws_s3_bucket.event_bucket.bucket
}
