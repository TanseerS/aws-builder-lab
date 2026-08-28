# OpsPilot 2.0 - shared locals and account context.
#
# Every resource name derives from local.prefix so the Demo Lab safety boundary
# ("remediation may only touch resources named opspilot-<env>-*") is enforceable
# in IAM as well as in code.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_partition" "current" {}

resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  prefix     = "${var.project_name}-${var.environment}"
  suffix     = random_id.suffix.hex
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
  partition  = data.aws_partition.current.partition

  tags = {
    Project     = "OpsPilot"
    Environment = var.environment
    ManagedBy   = "Terraform"
    Purpose     = "AWS Incident Lifecycle"
  }

  # --- Demo Lab ---------------------------------------------------------------
  demo_function_name    = "${local.prefix}-demo-app"
  demo_table_name       = "${local.prefix}-demo-table"
  demo_metric_namespace = "OpsPilot/DemoApp"

  # The only environment variables OpsPilot is ever allowed to write on the demo
  # function. Everything else on that function is Terraform's alone.
  demo_mutable_env_keys = [
    "FAILURE_MODE",
    "LATENCY_MS",
    "ERROR_RATE",
    "CONFIG_PROFILE",
    "WRITE_BURST",
    "TARGET_TABLE",
  ]

  # The healthy baseline that reset and remediation restore.
  demo_baseline_env = {
    FAILURE_MODE   = "none"
    LATENCY_MS     = "0"
    ERROR_RATE     = "0"
    CONFIG_PROFILE = "default"
    WRITE_BURST    = "0"
    TARGET_TABLE   = local.demo_table_name
  }

  # --- Alarms -----------------------------------------------------------------
  alarm_names = {
    lambda_error        = "${local.prefix}-demo-lambda-errors"
    lambda_latency      = "${local.prefix}-demo-lambda-latency"
    application_error   = "${local.prefix}-demo-app-errors"
    database_throttle   = "${local.prefix}-demo-db-throttle"
    configuration_error = "${local.prefix}-demo-config-errors"
  }

  demo_alarm_list = values(local.alarm_names)

  # Alarm name -> incident metadata. The detector treats this as a lookup table
  # rather than inferring incident type from the alarm name.
  alarm_catalog = {
    (local.alarm_names.lambda_error) = {
      incident_type    = "lambda_error"
      affected_service = local.demo_function_name
      severity         = "HIGH"
      title            = "Demo application Lambda is returning errors"
      description      = "The demo Lambda function is failing invocations, so requests to the demo service are not being served."
      metric_namespace = "AWS/Lambda"
      metric_name      = "Errors"
    }
    (local.alarm_names.lambda_latency) = {
      incident_type    = "lambda_latency"
      affected_service = local.demo_function_name
      severity         = "MEDIUM"
      title            = "Demo application latency has degraded"
      description      = "The demo Lambda function's average duration is far above its normal execution time."
      metric_namespace = "AWS/Lambda"
      metric_name      = "Duration"
    }
    (local.alarm_names.application_error) = {
      incident_type    = "application_error"
      affected_service = local.demo_function_name
      severity         = "HIGH"
      title            = "Demo application is returning HTTP 500 responses"
      description      = "The demo application is serving server errors to callers at an elevated rate."
      metric_namespace = local.demo_metric_namespace
      metric_name      = "HttpErrors"
    }
    (local.alarm_names.database_throttle) = {
      incident_type    = "database_throttle"
      affected_service = local.demo_table_name
      severity         = "HIGH"
      title            = "Demo database writes are being throttled"
      description      = "DynamoDB is rejecting writes to the demo table because demand exceeds its provisioned write capacity."
      metric_namespace = local.demo_metric_namespace
      metric_name      = "DbThrottles"
    }
    (local.alarm_names.configuration_error) = {
      incident_type    = "configuration_error"
      affected_service = local.demo_function_name
      severity         = "CRITICAL"
      title            = "Demo application configuration is invalid"
      description      = "The demo application is rejecting every request because its configuration profile is invalid."
      metric_namespace = local.demo_metric_namespace
      metric_name      = "ConfigErrors"
    }
  }

  # --- Metric probes ----------------------------------------------------------
  # Which metric series the investigator and verifier collect per incident type.
  #
  # This is stored compactly because a Lambda's entire environment must fit in
  # 4 KB. Dimensions are referenced by key rather than repeated per probe, and
  # resolved at run time from DEMO_FUNCTION_NAME / DEMO_TABLE_NAME - see
  # opspilot.evidence.load_metric_catalog.
  #
  #   ns = namespace, m = metric, s = statistic, d = dimension set, e = error signal
  #   d: "fn" -> FunctionName, "svc" -> Service (EMF), "tbl" -> TableName
  metric_probes = {
    lambda_errors      = { ns = "AWS/Lambda", m = "Errors", s = "Sum", d = "fn", e = true }
    lambda_invocations = { ns = "AWS/Lambda", m = "Invocations", s = "Sum", d = "fn", e = false }
    lambda_duration    = { ns = "AWS/Lambda", m = "Duration", s = "Average", d = "fn", e = false }
    app_errors         = { ns = local.demo_metric_namespace, m = "HttpErrors", s = "Sum", d = "svc", e = true }
    app_latency        = { ns = local.demo_metric_namespace, m = "RequestLatency", s = "Average", d = "svc", e = false }
    config_errors      = { ns = local.demo_metric_namespace, m = "ConfigErrors", s = "Sum", d = "svc", e = true }
    db_throttles       = { ns = "AWS/DynamoDB", m = "WriteThrottleEvents", s = "Sum", d = "tbl", e = true }
    app_db_throttles   = { ns = local.demo_metric_namespace, m = "DbThrottles", s = "Sum", d = "svc", e = true }
    db_writes          = { ns = "AWS/DynamoDB", m = "ConsumedWriteCapacityUnits", s = "Sum", d = "tbl", e = false }
  }

  # Incident type -> the probes worth collecting for it.
  metric_scenarios = {
    default             = ["lambda_errors", "lambda_invocations", "lambda_duration"]
    lambda_error        = ["lambda_errors", "lambda_invocations", "lambda_duration", "app_errors"]
    lambda_latency      = ["lambda_duration", "lambda_invocations", "app_latency", "lambda_errors"]
    application_error   = ["app_errors", "lambda_invocations", "lambda_errors", "app_latency"]
    database_throttle   = ["app_db_throttles", "db_throttles", "db_writes", "lambda_duration"]
    configuration_error = ["config_errors", "app_errors", "lambda_invocations"]
  }

  metric_catalog = {
    p = local.metric_probes
    c = local.metric_scenarios
  }

  lambda_dimensions = { FunctionName = local.demo_function_name }
  app_dimensions    = { Service = local.demo_function_name }
  table_dimensions  = { TableName = local.demo_table_name }

  # --- Shared Lambda environment ---------------------------------------------
  common_env = {
    PROJECT_NAME    = var.project_name
    ENVIRONMENT     = var.environment
    RESOURCE_PREFIX = local.prefix
    LOG_LEVEL       = "INFO"

    INCIDENTS_TABLE = aws_dynamodb_table.incidents.name
    CHANGES_TABLE   = aws_dynamodb_table.changes.name
    STATUS_INDEX    = "status-detected_at-index"
    SIGNATURE_INDEX = "signature-detected_at-index"
    CHANGES_INDEX   = "scope-timestamp-index"

    ARTIFACTS_BUCKET = aws_s3_bucket.artifacts.bucket
    EVENT_BUS_NAME   = aws_cloudwatch_event_bus.opspilot.name
    EVENT_SOURCE     = "opspilot.core"

    DEMO_FUNCTION_NAME    = local.demo_function_name
    DEMO_FUNCTION_ARN     = aws_lambda_function.demo_app.arn
    DEMO_TABLE_NAME       = local.demo_table_name
    DEMO_METRIC_NAMESPACE = local.demo_metric_namespace
    DEMO_LOG_GROUP        = "/aws/lambda/${local.demo_function_name}"
    DEMO_ALARMS           = jsonencode(local.demo_alarm_list)
    DEMO_BASELINE_ENV     = jsonencode(local.demo_baseline_env)
    DEMO_MUTABLE_ENV_KEYS = jsonencode(local.demo_mutable_env_keys)
  }

  bedrock_env = {
    BEDROCK_MODEL_ID    = var.bedrock_model_id
    BEDROCK_MAX_TOKENS  = tostring(var.bedrock_max_tokens)
    BEDROCK_TEMPERATURE = tostring(var.bedrock_temperature)
  }

  evidence_env = {
    MAX_LOG_EVENTS          = tostring(var.max_log_events)
    MAX_CLOUDTRAIL_EVENTS   = tostring(var.max_cloudtrail_events)
    MAX_METRIC_POINTS       = tostring(var.max_metric_points)
    MAX_PROMPT_CHARS        = tostring(var.max_prompt_chars)
    CHANGE_LOOKBACK_MINUTES = tostring(var.change_lookback_minutes)
    METRIC_CATALOG          = jsonencode(local.metric_catalog)
  }
}
