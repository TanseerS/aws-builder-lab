# OpsPilot Demo Lab.
#
# A deliberately small sample application that OpsPilot can break and repair
# safely. Everything here is named with the local.prefix so the remediation
# safety boundary is expressible in IAM.
#
# The demo application intentionally does NOT use the OpsPilot shared layer: it
# is the subject of the platform, not a part of it.

resource "aws_lambda_function" "demo_app" {
  function_name    = local.demo_function_name
  role             = aws_iam_role.demo_app.arn
  handler          = "handler.lambda_handler"
  runtime          = local.lambda_runtime
  architectures    = local.architectures
  filename         = data.archive_file.demo_app.output_path
  source_code_hash = data.archive_file.demo_app.output_base64sha256
  # Must exceed the injected latency (4s) so the latency scenario shows up as
  # slow responses rather than as timeouts.
  timeout     = 30
  memory_size = 256

  environment {
    # Fault flags start at the healthy baseline. The Demo Lab controller and the
    # remediation function are the only things permitted to change these, and
    # only these.
    variables = merge(local.demo_baseline_env, {
      SERVICE_NAME     = local.demo_function_name
      METRIC_NAMESPACE = local.demo_metric_namespace
    })
  }

  tags = merge(local.tags, {
    Name    = local.demo_function_name
    DemoLab = "true"
  })

  # Terraform owns the baseline, but the Demo Lab mutates these at run time.
  # Without this, every apply would fight with an in-flight demo scenario.
  lifecycle {
    ignore_changes = [environment]
  }

  depends_on = [aws_cloudwatch_log_group.demo_app]
}

# Asynchronous invocations of the demo app must NOT be retried.
#
# Lambda retries a failed async invocation twice by default, with delays. During
# a fault injection that would triple the error count and keep errors arriving
# for minutes after remediation - which would re-trip the alarm and open a
# spurious second incident. Disabling retries keeps the error signal an honest
# reflection of what the injected fault is actually doing.
resource "aws_lambda_function_event_invoke_config" "demo_app" {
  function_name                = aws_lambda_function.demo_app.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 60
}

# --- Synthetic traffic --------------------------------------------------------
# CloudWatch alarms can only evaluate datapoints that exist. A one-per-minute
# trickle keeps the demo application's metrics continuous, which is what makes
# detection take ~2 minutes instead of "whenever someone calls the service".
# 1 invocation/minute is comfortably inside the Lambda free tier.
resource "aws_cloudwatch_event_rule" "traffic" {
  count = var.enable_traffic_generator ? 1 : 0

  name                = "${local.prefix}-demo-traffic"
  description         = "Generates a baseline of demo application requests so alarms have data"
  schedule_expression = "rate(1 minute)"
  tags                = merge(local.tags, { Name = "${local.prefix}-demo-traffic" })
}

resource "aws_cloudwatch_event_target" "traffic" {
  count = var.enable_traffic_generator ? 1 : 0

  rule      = aws_cloudwatch_event_rule.traffic[0].name
  target_id = "traffic-generator"
  arn       = aws_lambda_function.traffic_generator.arn
}

resource "aws_lambda_permission" "traffic" {
  count = var.enable_traffic_generator ? 1 : 0

  statement_id  = "AllowExecutionFromEventBridgeSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.traffic_generator.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.traffic[0].arn
}
