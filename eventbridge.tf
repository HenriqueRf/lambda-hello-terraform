# eventbridge.tf

# IMPORTANTE: EventBridge usa UTC. Brasil (sem DST) = UTC-3.
# 08:50 BRT = 11:50 UTC -> cron(50 11 * * ? *)
# 09:00 BRT = 12:00 UTC -> cron(0 12 * * ? *)

resource "aws_cloudwatch_event_rule" "OneVisionDataCleanerSchedule" {
  name                = "${local.resource_prefix}-data-cleaner-schedule"
  description         = "Trigger OneVision data cleaner at ${var.data_cleaner_trigger_time_brt} BRT"
  schedule_expression = var.data_cleaner_schedule_expression

  tags = {
    Name = "${local.resource_prefix}-data-cleaner-schedule"
  }
}

resource "aws_cloudwatch_event_rule" "OneVisionDataCollectorSchedule" {
  name                = "${local.resource_prefix}-data-collector-schedule"
  description         = "Trigger OneVision data collector daily at ${var.data_collector_trigger_time_brt} BRT"
  schedule_expression = var.data_collector_schedule_expression

  tags = {
    Name = "${local.resource_prefix}-data-collector-schedule"
  }
}

resource "aws_cloudwatch_event_target" "target_a" {
  rule      = aws_cloudwatch_event_rule.OneVisionDataCleanerSchedule.name
  target_id = "${local.resource_prefix}-lambda-a-target"
  arn       = aws_lambda_function.OneVisionDataCleanerFunction.arn
}

resource "aws_cloudwatch_event_target" "target_b" {
  rule      = aws_cloudwatch_event_rule.OneVisionDataCollectorSchedule.name
  target_id = "${local.resource_prefix}-lambda-b-target"
  arn       = aws_lambda_function.OneVisionDataCollectorFunction.arn
}

# Permissoes para o EventBridge invocar as Lambdas
resource "aws_lambda_permission" "allow_events_to_invoke_a" {
  statement_id  = "AllowExecutionFromEventBridgeA-${var.client_name}-${var.environment}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.OneVisionDataCleanerFunction.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.OneVisionDataCleanerSchedule.arn
}

resource "aws_lambda_permission" "allow_events_to_invoke_b" {
  statement_id  = "AllowExecutionFromEventBridgeB-${var.client_name}-${var.environment}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.OneVisionDataCollectorFunction.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.OneVisionDataCollectorSchedule.arn
}