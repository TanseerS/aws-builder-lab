# OpsPilot IAM - one execution role per function, least privilege throughout.
#
# There is no AdministratorAccess, no PowerUserAccess and no "*" resource on any
# mutating action. The remediation role in particular can modify exactly one
# Lambda function: the Demo Lab application.
#
# Wildcards appear only where an AWS API genuinely does not support resource
# level permissions; each such case is called out in a comment.

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

locals {
  log_group_arns = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-*"
  demo_log_group = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.demo_function_name}"

  incidents_table_arn = aws_dynamodb_table.incidents.arn
  changes_table_arn   = aws_dynamodb_table.changes.arn
}

# --- Reusable policy documents ------------------------------------------------

# CloudWatch Logs write access, scoped to this deployment's log groups.
data "aws_iam_policy_document" "logs_write" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [local.log_group_arns, "${local.log_group_arns}:*"]
  }
}

data "aws_iam_policy_document" "incidents_read" {
  statement {
    sid    = "ReadIncidents"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:DescribeTable",
    ]
    resources = [local.incidents_table_arn, "${local.incidents_table_arn}/index/*"]
  }
}

data "aws_iam_policy_document" "incidents_write" {
  statement {
    sid    = "WriteIncidents"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DescribeTable",
    ]
    resources = [local.incidents_table_arn, "${local.incidents_table_arn}/index/*"]
  }
}

data "aws_iam_policy_document" "changes_read" {
  statement {
    sid    = "ReadChangeLog"
    effect = "Allow"
    actions = [
      "dynamodb:Query",
      "dynamodb:GetItem",
    ]
    resources = [local.changes_table_arn, "${local.changes_table_arn}/index/*"]
  }
}

data "aws_iam_policy_document" "changes_write" {
  statement {
    sid    = "WriteChangeLog"
    effect = "Allow"
    actions = [
      "dynamodb:Query",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
    ]
    resources = [local.changes_table_arn, "${local.changes_table_arn}/index/*"]
  }
}

data "aws_iam_policy_document" "publish_events" {
  statement {
    sid       = "PublishOpsPilotEvents"
    effect    = "Allow"
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.opspilot.arn]
  }
}

data "aws_iam_policy_document" "invoke_bedrock" {
  statement {
    sid    = "InvokeBedrockTextModel"
    effect = "Allow"
    # The minimum permission the Converse API requires. No model management,
    # no provisioned throughput, no agent or knowledge base access.
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:${local.partition}:bedrock:${local.region}::foundation-model/*",
      "arn:${local.partition}:bedrock:${local.region}:${local.account_id}:inference-profile/*",
    ]
  }
}

# Reading CloudWatch alarm state and metric data. These APIs are account-scoped
# and do not accept a resource ARN, so a wildcard resource is unavoidable; both
# actions are strictly read-only.
data "aws_iam_policy_document" "cloudwatch_read" {
  statement {
    sid    = "ReadAlarmsAndMetrics"
    effect = "Allow"
    actions = [
      "cloudwatch:DescribeAlarms",
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:ListMetrics",
    ]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "demo_logs_read" {
  statement {
    sid    = "ReadDemoLogs"
    effect = "Allow"
    actions = [
      "logs:FilterLogEvents",
      "logs:GetLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [local.demo_log_group, "${local.demo_log_group}:*"]
  }
}

# CloudTrail LookupEvents reads the account event history and has no resource
# level permission model, so a wildcard is required. It is read-only.
data "aws_iam_policy_document" "cloudtrail_read" {
  statement {
    sid       = "LookupCloudTrailEvents"
    effect    = "Allow"
    actions   = ["cloudtrail:LookupEvents"]
    resources = ["*"]
  }
}

# Read-only view of the Demo Lab function's configuration.
data "aws_iam_policy_document" "demo_function_read" {
  statement {
    sid    = "ReadDemoFunction"
    effect = "Allow"
    actions = [
      "lambda:GetFunctionConfiguration",
      "lambda:GetFunction",
    ]
    resources = [aws_lambda_function.demo_app.arn]
  }
}

# Invoke the Demo Lab function - used for health probing and synthetic traffic.
data "aws_iam_policy_document" "demo_function_invoke" {
  statement {
    sid       = "InvokeDemoFunction"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.demo_app.arn]
  }
}

# THE DEMO LAB SAFETY BOUNDARY.
# The single grant in OpsPilot that can change AWS state, restricted to exactly
# one function ARN. Even if a model returned a malicious action name, and even
# if every application-level allowlist check were bypassed, IAM would still
# permit modification of nothing but the Demo Lab application.
data "aws_iam_policy_document" "demo_function_mutate" {
  statement {
    sid    = "MutateDemoLabFunctionOnly"
    effect = "Allow"
    actions = [
      "lambda:UpdateFunctionConfiguration",
      "lambda:GetFunctionConfiguration",
      "lambda:GetFunction",
    ]
    resources = [aws_lambda_function.demo_app.arn]
  }
}

# --- API Lambda role ----------------------------------------------------------
resource "aws_iam_role" "api" {
  name               = "${local.prefix}-api-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = merge(local.tags, { Name = "${local.prefix}-api-role" })
}

data "aws_iam_policy_document" "api" {
  source_policy_documents = [
    data.aws_iam_policy_document.logs_write.json,
    data.aws_iam_policy_document.incidents_write.json,
    data.aws_iam_policy_document.publish_events.json,
    data.aws_iam_policy_document.cloudwatch_read.json,
  ]

  statement {
    sid       = "ReadPostmortems"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/postmortems/*"]
  }

  # The API cannot modify the demo function itself; it delegates to the Demo Lab
  # controller, which holds that permission under its own role.
  statement {
    sid       = "InvokeDemoController"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.demo_controller.arn]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "${local.prefix}-api-policy"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

# --- Incident Detector role ---------------------------------------------------
resource "aws_iam_role" "incident_detector" {
  name               = "${local.prefix}-incident-detector-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = merge(local.tags, { Name = "${local.prefix}-incident-detector-role" })
}

data "aws_iam_policy_document" "incident_detector" {
  source_policy_documents = [
    data.aws_iam_policy_document.logs_write.json,
    data.aws_iam_policy_document.incidents_write.json,
    data.aws_iam_policy_document.publish_events.json,
  ]
}

resource "aws_iam_role_policy" "incident_detector" {
  name   = "${local.prefix}-incident-detector-policy"
  role   = aws_iam_role.incident_detector.id
  policy = data.aws_iam_policy_document.incident_detector.json
}

# --- Investigator role --------------------------------------------------------
# The broadest *read* surface in OpsPilot, and still no permission to change
# anything anywhere.
resource "aws_iam_role" "investigator" {
  name               = "${local.prefix}-investigator-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = merge(local.tags, { Name = "${local.prefix}-investigator-role" })
}

data "aws_iam_policy_document" "investigator" {
  source_policy_documents = [
    data.aws_iam_policy_document.logs_write.json,
    data.aws_iam_policy_document.incidents_write.json,
    data.aws_iam_policy_document.changes_read.json,
    data.aws_iam_policy_document.publish_events.json,
    data.aws_iam_policy_document.invoke_bedrock.json,
    data.aws_iam_policy_document.cloudwatch_read.json,
    data.aws_iam_policy_document.demo_logs_read.json,
    data.aws_iam_policy_document.cloudtrail_read.json,
    data.aws_iam_policy_document.demo_function_read.json,
  ]
}

resource "aws_iam_role_policy" "investigator" {
  name   = "${local.prefix}-investigator-policy"
  role   = aws_iam_role.investigator.id
  policy = data.aws_iam_policy_document.investigator.json
}

# --- Remediation role ---------------------------------------------------------
resource "aws_iam_role" "remediation" {
  name               = "${local.prefix}-remediation-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = merge(local.tags, { Name = "${local.prefix}-remediation-role" })
}

data "aws_iam_policy_document" "remediation" {
  source_policy_documents = [
    data.aws_iam_policy_document.logs_write.json,
    data.aws_iam_policy_document.incidents_write.json,
    data.aws_iam_policy_document.changes_write.json,
    data.aws_iam_policy_document.publish_events.json,
    data.aws_iam_policy_document.demo_function_mutate.json,
    data.aws_iam_policy_document.demo_function_invoke.json,
  ]
}

resource "aws_iam_role_policy" "remediation" {
  name   = "${local.prefix}-remediation-policy"
  role   = aws_iam_role.remediation.id
  policy = data.aws_iam_policy_document.remediation.json
}

# --- Verifier role ------------------------------------------------------------
resource "aws_iam_role" "verifier" {
  name               = "${local.prefix}-verifier-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = merge(local.tags, { Name = "${local.prefix}-verifier-role" })
}

data "aws_iam_policy_document" "verifier" {
  source_policy_documents = [
    data.aws_iam_policy_document.logs_write.json,
    data.aws_iam_policy_document.incidents_write.json,
    data.aws_iam_policy_document.publish_events.json,
    data.aws_iam_policy_document.cloudwatch_read.json,
    data.aws_iam_policy_document.demo_logs_read.json,
    data.aws_iam_policy_document.demo_function_invoke.json,
    data.aws_iam_policy_document.demo_function_read.json,
  ]
}

resource "aws_iam_role_policy" "verifier" {
  name   = "${local.prefix}-verifier-policy"
  role   = aws_iam_role.verifier.id
  policy = data.aws_iam_policy_document.verifier.json
}

# --- Postmortem role ----------------------------------------------------------
resource "aws_iam_role" "postmortem" {
  name               = "${local.prefix}-postmortem-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = merge(local.tags, { Name = "${local.prefix}-postmortem-role" })
}

data "aws_iam_policy_document" "postmortem" {
  source_policy_documents = [
    data.aws_iam_policy_document.logs_write.json,
    data.aws_iam_policy_document.incidents_write.json,
    data.aws_iam_policy_document.publish_events.json,
    data.aws_iam_policy_document.invoke_bedrock.json,
  ]

  statement {
    sid       = "WritePostmortems"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/postmortems/*"]
  }
}

resource "aws_iam_role_policy" "postmortem" {
  name   = "${local.prefix}-postmortem-policy"
  role   = aws_iam_role.postmortem.id
  policy = data.aws_iam_policy_document.postmortem.json
}

# --- Demo Lab controller role -------------------------------------------------
resource "aws_iam_role" "demo_controller" {
  name               = "${local.prefix}-demo-controller-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = merge(local.tags, { Name = "${local.prefix}-demo-controller-role" })
}

data "aws_iam_policy_document" "demo_controller" {
  source_policy_documents = [
    data.aws_iam_policy_document.logs_write.json,
    data.aws_iam_policy_document.changes_write.json,
    data.aws_iam_policy_document.demo_function_mutate.json,
    data.aws_iam_policy_document.demo_function_invoke.json,
    data.aws_iam_policy_document.cloudwatch_read.json,
  ]
}

resource "aws_iam_role_policy" "demo_controller" {
  name   = "${local.prefix}-demo-controller-policy"
  role   = aws_iam_role.demo_controller.id
  policy = data.aws_iam_policy_document.demo_controller.json
}

# --- Demo application role ----------------------------------------------------
# The sample application. It is the subject of OpsPilot, not part of it, so it
# has no access to any OpsPilot table, bucket or bus.
resource "aws_iam_role" "demo_app" {
  name               = "${local.prefix}-demo-app-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = merge(local.tags, { Name = "${local.prefix}-demo-app-role" })
}

data "aws_iam_policy_document" "demo_app" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [local.demo_log_group, "${local.demo_log_group}:*"]
  }

  statement {
    sid    = "UseDemoTable"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      # BatchWriteItem is how the database_throttle scenario drives load fast
      # enough to avoid also breaching the latency alarm.
      "dynamodb:BatchWriteItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.demo.arn]
  }
}

resource "aws_iam_role_policy" "demo_app" {
  name   = "${local.prefix}-demo-app-policy"
  role   = aws_iam_role.demo_app.id
  policy = data.aws_iam_policy_document.demo_app.json
}

# --- Traffic generator role ---------------------------------------------------
resource "aws_iam_role" "traffic_generator" {
  name               = "${local.prefix}-traffic-generator-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = merge(local.tags, { Name = "${local.prefix}-traffic-generator-role" })
}

data "aws_iam_policy_document" "traffic_generator" {
  source_policy_documents = [
    data.aws_iam_policy_document.logs_write.json,
    data.aws_iam_policy_document.demo_function_invoke.json,
  ]
}

resource "aws_iam_role_policy" "traffic_generator" {
  name   = "${local.prefix}-traffic-generator-policy"
  role   = aws_iam_role.traffic_generator.id
  policy = data.aws_iam_policy_document.traffic_generator.json
}
