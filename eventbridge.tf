#eventbridge.tf

# IMPORTANTE: EventBridge usa UTC. Brasil (sem DST) = UTC-3.
# 08:50 BRT = 11:50 UTC → cron(50 11 * * ? *)
# 09:00 BRT = 12:00 UTC → cron(0 12 * * ? *)

resource "aws_cloudwatch_event_rule" "OneVisionDataCleanerSchedule" {
  name                = "OneVisionDataCleanerSchedule"
  description         = "Trigger OneVision data cleaner at 8:50 AM Brazil time"
  schedule_expression = "cron(50 11 * * ? *)"
}

resource "aws_cloudwatch_event_rule" "OneVisionDataCollectorSchedule" {
  name                = "OneVisionDataCollectorSchedule"
  description         = "Trigger OneVision data collector daily at 9 AM Brazil time"
  schedule_expression = "cron(0 12 * * ? *)"
}

resource "aws_cloudwatch_event_target" "target_a" {
  rule      = aws_cloudwatch_event_rule.OneVisionDataCleanerSchedule.name
  target_id = "lambda-a-target"
  arn       = aws_lambda_function.OneVisionDataCleanerFunction.arn
}

resource "aws_cloudwatch_event_target" "target_b" {
  rule      = aws_cloudwatch_event_rule.OneVisionDataCollectorSchedule.name
  target_id = "lambda-b-target"
  arn       = aws_lambda_function.OneVisionDataCollectorFunction.arn
}

# Permissões para o EventBridge invocar as Lambdas
resource "aws_lambda_permission" "allow_events_to_invoke_a" {
  statement_id  = "AllowExecutionFromEventBridgeA"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.OneVisionDataCleanerFunction.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.OneVisionDataCleanerSchedule.arn
}

resource "aws_lambda_permission" "allow_events_to_invoke_b" {
  statement_id  = "AllowExecutionFromEventBridgeB"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.OneVisionDataCollectorFunction.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.OneVisionDataCollectorSchedule.arn
}