output "lambda_function_name" {
  description = "Nome da função Lambda criada"
  value       = aws_lambda_function.hello_world.function_name
}

output "s3_bucket_name" {
  description = "Nome do bucket S3 onde os registros são armazenados"
  value       = aws_s3_bucket.lambda_records.bucket
}
