# CloudWatch alarms for the Demo Lab.
#
# All five use a 60-second period with a single evaluation period, so an
# injected failure is detected in roughly two minutes rather than ten. That is
# aggressive for production but correct for a showcase, and it is only ever
# applied to Demo Lab resources.
#
# treat_missing_data = "notBreaching" means a quiet period never fabricates an
# incident; the traffic generator keeps datapoints flowing regardless.

# --- Lambda error rate --------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "demo_lambda_errors" {
  alarm_name        = local.alarm_names.lambda_error
  alarm_description = "Demo application Lambda is returning errors"

  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  dimensions          = local.lambda_dimensions
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  tags = merge(local.tags, {
    Name     = local.alarm_names.lambda_error
    DemoLab  = "true"
    Scenario = "lambda_error"
  })
}

# --- Lambda duration ----------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "demo_lambda_latency" {
  alarm_name        = local.alarm_names.lambda_latency
  alarm_description = "Demo application Lambda duration is far above normal"

  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  statistic           = "Average"
  dimensions          = local.lambda_dimensions
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  # Healthy requests finish in tens of milliseconds; the latency scenario
  # injects 4000ms.
  threshold           = 2000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  tags = merge(local.tags, {
    Name     = local.alarm_names.lambda_latency
    DemoLab  = "true"
    Scenario = "lambda_latency"
  })
}

# --- Application HTTP 500 rate ------------------------------------------------
# A custom metric published by the demo app via CloudWatch Embedded Metric
# Format. Handled 500s never appear in AWS/Lambda Errors, so the application's
# own error rate needs its own signal.
resource "aws_cloudwatch_metric_alarm" "demo_app_errors" {
  alarm_name        = local.alarm_names.application_error
  alarm_description = "Demo application is returning HTTP 500 responses"

  namespace           = local.demo_metric_namespace
  metric_name         = "HttpErrors"
  statistic           = "Sum"
  dimensions          = local.app_dimensions
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  tags = merge(local.tags, {
    Name     = local.alarm_names.application_error
    DemoLab  = "true"
    Scenario = "application_error"
  })
}

# --- DynamoDB throttling ------------------------------------------------------
# Real throttling from the demo table's 1 WCU provisioned capacity.
#
# This alarms on the demo application's own DbThrottles metric rather than on
# AWS/DynamoDB directly, for one specific reason: AWS/DynamoDB throttle metrics
# are *sparse*. When nothing is throttling they publish no datapoints at all,
# and CloudWatch then takes ~8 minutes to apply the notBreaching treatment and
# return the alarm to OK. Until it does, a repeat injection produces no
# OK -> ALARM transition, so no incident - which makes the scenario unusable in
# a live demo.
#
# DbThrottles is emitted on every request, including zeros, so the alarm clears
# within a minute like every other scenario. The value is not synthetic: it is
# the count of writes DynamoDB actually rejected, taken from UnprocessedItems
# and ProvisionedThroughputExceededException.
#
# The authoritative AWS/DynamoDB WriteThrottleEvents series is still collected
# as investigation evidence (see local.metric_probes), so the incident record
# carries the real DynamoDB numbers.
resource "aws_cloudwatch_metric_alarm" "demo_db_throttle" {
  alarm_name        = local.alarm_names.database_throttle
  alarm_description = "Demo database writes are being throttled"

  namespace   = local.demo_metric_namespace
  metric_name = "DbThrottles"
  statistic   = "Sum"
  dimensions  = local.app_dimensions

  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  tags = merge(local.tags, {
    Name     = local.alarm_names.database_throttle
    DemoLab  = "true"
    Scenario = "database_throttle"
  })
}

# --- Configuration errors -----------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "demo_config_errors" {
  alarm_name        = local.alarm_names.configuration_error
  alarm_description = "Demo application configuration is invalid and rejecting requests"

  namespace           = local.demo_metric_namespace
  metric_name         = "ConfigErrors"
  statistic           = "Sum"
  dimensions          = local.app_dimensions
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  tags = merge(local.tags, {
    Name     = local.alarm_names.configuration_error
    DemoLab  = "true"
    Scenario = "configuration_error"
  })
}

# --- OpsPilot's own observability ---------------------------------------------
# The platform must be observable too. These alarms watch OpsPilot itself, not
# the Demo Lab, and are deliberately excluded from the incident detection rule
# so OpsPilot cannot open incidents about itself.
resource "aws_cloudwatch_metric_alarm" "opspilot_investigator_errors" {
  alarm_name        = "${local.prefix}-opspilot-investigator-errors"
  alarm_description = "The OpsPilot investigator function is failing"

  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  dimensions          = { FunctionName = aws_lambda_function.investigator.function_name }
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  tags = merge(local.tags, { Name = "${local.prefix}-opspilot-investigator-errors" })
}

resource "aws_cloudwatch_metric_alarm" "opspilot_api_errors" {
  alarm_name        = "${local.prefix}-opspilot-api-errors"
  alarm_description = "The OpsPilot API function is failing"

  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  dimensions          = { FunctionName = aws_lambda_function.api.function_name }
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  tags = merge(local.tags, { Name = "${local.prefix}-opspilot-api-errors" })
}
