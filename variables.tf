# =============================
# Variáveis principais
# =============================

variable "aws_region" {
  description = "Região AWS onde os recursos serão criados"
  type        = string
  default     = "us-east-1"
}

# (Opcional) caso queira alterar os horários no futuro via variável
variable "collector_cron_expression" {
  description = "Agendamento da função OneVisionDataCollector (horário UTC)"
  type        = string
  default     = "cron(0 16 * * ? *)" # 13h Brasília
}

variable "cleaner_cron_expression" {
  description = "Agendamento da função OneVisionDataCleaner (horário UTC)"
  type        = string
  default     = "cron(0 17 * * ? *)" # 14h Brasília
}
