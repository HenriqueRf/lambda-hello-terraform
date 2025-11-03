# =============================
# Outputs para facilitar consulta
# =============================

# Lambda 1 - OneVisionDataCollectorFunction
output "onevision_data_collector_function_name" {
  description = "Nome da função Lambda OneVisionDataCollectorFunction"
  value       = aws_lambda_function.onevision_data_collector_function.function_name
}

output "onevision_data_collector_arn" {
  description = "ARN da função Lambda OneVisionDataCollectorFunction"
  value       = aws_lambda_function.onevision_data_collector_function.arn
}

output "onevision_data_collector_role_name" {
  description = "Nome da IAM Role associada à Lambda de coleta de dados"
  value       = aws_iam_role.onevision_data_collector_role.name
}

output "onevision_data_collector_schedule_name" {
  description = "Nome da regra EventBridge que agenda a execução da Lambda de coleta"
  value       = aws_cloudwatch_event_rule.onevision_data_collector_schedule.name
}

# Lambda 2 - OneVisionDataCleanerFunction
output "onevision_data_cleaner_function_name" {
  description = "Nome da função Lambda OneVisionDataCleanerFunction"
  value       = aws_lambda_function.onevision_data_cleaner_function.function_name
}

output "onevision_data_cleaner_arn" {
  description = "ARN da função Lambda OneVisionDataCleanerFunction"
  value       = aws_lambda_function.onevision_data_cleaner_function.arn
}

output "onevision_data_cleaner_role_name" {
  description = "Nome da IAM Role associada à Lambda de limpeza de dados"
  value       = aws_iam_role.onevision_data_cleaner_role.name
}

output "onevision_data_cleaner_schedule_name" {
  description = "Nome da regra EventBridge que agenda a execução da Lambda de limpeza"
  value       = aws_cloudwatch_event_rule.onevision_data_cleaner_schedule.name
}
