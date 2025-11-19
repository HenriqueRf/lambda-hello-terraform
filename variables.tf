# variable.tf

variable "region" {
  description = "Região AWS onde os recursos serão criados"
  type        = string
  default     = "sa-east-1"
}

variable "project_name" {
  description = "Prefixo de nomes"
  type        = string
  default     = "eventbridge-lambda-schedules"
}

variable "lambda_runtime" {
  description = "Runtime das Lambdas"
  type        = string
  default     = "python3.13"
}

variable "lambda_timeout_seconds" {
  description = "Timeout das Lambdas (segundos)"
  type        = number
  default     = 30
}

variable "lambda_memory_mb" {
  description = "Memória das Lambdas (MB)"
  type        = number
  default     = 128
}