client_name            = "test"
environment            = "test"
name_prefix            = "onevision"
project_name           = "onevision"
region                 = "sa-east-1"
management_account_id  = "123456789012"
organizational_unit_ids = ["r-cbdf"]

# Optional overrides for schedules/retention (defaults already set in variables.tf)
# data_cleaner_schedule_expression   = "cron(50 11 * * ? *)"
# data_collector_schedule_expression = "cron(0 12 * * ? *)"
# data_cleaner_trigger_time_brt      = "08:50"
# data_collector_trigger_time_brt    = "09:00"
# cloudwatch_log_retention_days      = 30