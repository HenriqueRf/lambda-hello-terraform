# variable.tf

variable "region" {
  description = "AWS region where resources will be created"
  type        = string
  default     = "sa-east-1"
}

variable "project_name" {
  description = "Project name used for tagging"
  type        = string
  default     = "onevision"
}

variable "name_prefix" {
  description = "Base prefix for resource names"
  type        = string
  default     = "onevision"
}

variable "client_name" {
  description = "Client identifier used for naming and tagging"
  type        = string
  default     = "test"
}

variable "environment" {
  description = "Deployment environment (e.g., dev, test, prod, client name)"
  type        = string
  default     = "test"
}

variable "management_account_id" {
  description = "AWS Account ID of the management account that owns the StackSet"
  type        = string
}

variable "organizational_unit_ids" {
  description = "List of OU IDs in the organization that should receive the StackSet"
  type        = list(string)
  default     = []
}

variable "lambda_runtime" {
  description = "Runtime for the Lambda functions"
  type        = string
  default     = "python3.13"
}

variable "lambda_timeout_seconds" {
  description = "Timeout for the Lambda functions (seconds)"
  type        = number
  default     = 30
}

variable "lambda_memory_mb" {
  description = "Memory for the Lambda functions (MB)"
  type        = number
  default     = 128
}

variable "data_cleaner_trigger_time_brt" {
  description = "Trigger time in BRT for the DataCleaner Lambda environment variable"
  type        = string
  default     = "08:50"
}

variable "data_collector_trigger_time_brt" {
  description = "Trigger time in BRT for the DataCollector Lambda environment variable"
  type        = string
  default     = "09:00"
}

variable "data_cleaner_schedule_expression" {
  description = "CloudWatch EventBridge cron expression for the DataCleaner Lambda"
  type        = string
  default     = "cron(50 11 * * ? *)"
}

variable "data_collector_schedule_expression" {
  description = "CloudWatch EventBridge cron expression for the DataCollector Lambda"
  type        = string
  default     = "cron(0 12 * * ? *)"
}

variable "cloudwatch_log_retention_days" {
  description = "Retention in days for CloudWatch log groups"
  type        = number
  default     = 30
}