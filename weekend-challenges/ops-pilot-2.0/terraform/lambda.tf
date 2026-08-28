# OpsPilot Lambda functions.
#
# Packaging is done by Terraform's archive provider, so `terraform apply` is the
# only build step: there is no separate bundling stage to forget. Dependencies
# are boto3 plus the standard library, both already present in the runtime.

locals {
  lambda_runtime  = "python3.12"
  lambda_root     = "${path.module}/../lambda"
  build_dir       = "${path.module}/.build"
  architectures   = ["arm64"] # Graviton: cheaper per ms than x86_64
  log_group_names = { for k, v in local.lambda_functions : k => "/aws/lambda/${v}" }

  lambda_functions = {
    api               = "${local.prefix}-api"
    incident_detector = "${local.prefix}-incident-detector"
    investigator      = "${local.prefix}-investigator"
    remediation       = "${local.prefix}-remediation"
    verifier          = "${local.prefix}-verifier"
    postmortem        = "${local.prefix}-postmortem"
    demo_controller   = "${local.prefix}-demo-controller"
    traffic_generator = "${local.prefix}-traffic-generator"
  }
}

# --- Shared layer -------------------------------------------------------------
# One implementation of config, logging, clients, the data model, Bedrock access
# and change correlation, shared by every OpsPilot function.
data "archive_file" "shared_layer" {
  type        = "zip"
  source_dir  = "${local.lambda_root}/shared"
  output_path = "${local.build_dir}/shared_layer.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

resource "aws_lambda_layer_version" "shared" {
  layer_name          = "${local.prefix}-shared"
  description         = "OpsPilot shared runtime library"
  filename            = data.archive_file.shared_layer.output_path
  source_code_hash    = data.archive_file.shared_layer.output_base64sha256
  compatible_runtimes = [local.lambda_runtime]

  compatible_architectures = local.architectures
}

# --- Function packages --------------------------------------------------------
data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${local.lambda_root}/api"
  output_path = "${local.build_dir}/api.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

data "archive_file" "incident_detector" {
  type        = "zip"
  source_dir  = "${local.lambda_root}/incident_detector"
  output_path = "${local.build_dir}/incident_detector.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

data "archive_file" "investigator" {
  type        = "zip"
  source_dir  = "${local.lambda_root}/investigator"
  output_path = "${local.build_dir}/investigator.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

data "archive_file" "remediation" {
  type        = "zip"
  source_dir  = "${local.lambda_root}/remediation"
  output_path = "${local.build_dir}/remediation.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

data "archive_file" "verifier" {
  type        = "zip"
  source_dir  = "${local.lambda_root}/verifier"
  output_path = "${local.build_dir}/verifier.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

data "archive_file" "postmortem" {
  type        = "zip"
  source_dir  = "${local.lambda_root}/postmortem"
  output_path = "${local.build_dir}/postmortem.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

data "archive_file" "demo_app" {
  type        = "zip"
  source_dir  = "${local.lambda_root}/demo_app"
  output_path = "${local.build_dir}/demo_app.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

data "archive_file" "demo_controller" {
  type        = "zip"
  source_dir  = "${local.lambda_root}/demo_controller"
  output_path = "${local.build_dir}/demo_controller.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

data "archive_file" "traffic_generator" {
  type        = "zip"
  source_dir  = "${local.lambda_root}/traffic_generator"
  output_path = "${local.build_dir}/traffic_generator.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

# --- Log groups ---------------------------------------------------------------
# Created explicitly so retention is bounded and the group exists before its
# function first runs.
resource "aws_cloudwatch_log_group" "functions" {
  for_each = local.lambda_functions

  name              = "/aws/lambda/${each.value}"
  retention_in_days = var.log_retention_days
  tags              = merge(local.tags, { Name = "/aws/lambda/${each.value}" })
}

resource "aws_cloudwatch_log_group" "demo_app" {
  name              = "/aws/lambda/${local.demo_function_name}"
  retention_in_days = var.log_retention_days
  tags              = merge(local.tags, { Name = "/aws/lambda/${local.demo_function_name}", DemoLab = "true" })
}

# --- API Lambda ---------------------------------------------------------------
resource "aws_lambda_function" "api" {
  function_name = local.lambda_functions.api
  role          = aws_iam_role.api.arn
  handler       = "handler.lambda_handler"
  runtime       = local.lambda_runtime
  architectures = local.architectures
  filename      = data.archive_file.api.output_path
  # 29s matches the API Gateway integration timeout: the Demo Lab call is
  # synchronous and waits for a Lambda configuration update to settle, so the
  # API must not expire first and report a failure for work that succeeded.
  timeout          = 29
  memory_size      = 512
  source_code_hash = data.archive_file.api.output_base64sha256
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = merge(local.common_env, local.bedrock_env, {
      DEMO_CONTROLLER_FUNCTION = aws_lambda_function.demo_controller.function_name
    })
  }

  tags       = merge(local.tags, { Name = local.lambda_functions.api })
  depends_on = [aws_cloudwatch_log_group.functions]
}

# --- Incident Detector --------------------------------------------------------
resource "aws_lambda_function" "incident_detector" {
  function_name    = local.lambda_functions.incident_detector
  role             = aws_iam_role.incident_detector.arn
  handler          = "handler.lambda_handler"
  runtime          = local.lambda_runtime
  architectures    = local.architectures
  filename         = data.archive_file.incident_detector.output_path
  timeout          = 30
  memory_size      = 256
  source_code_hash = data.archive_file.incident_detector.output_base64sha256
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = merge(local.common_env, {
      ALARM_CATALOG    = jsonencode(local.alarm_catalog)
      DEFAULT_SEVERITY = "HIGH"
    })
  }

  tags       = merge(local.tags, { Name = local.lambda_functions.incident_detector })
  depends_on = [aws_cloudwatch_log_group.functions]
}

# --- Investigator -------------------------------------------------------------
resource "aws_lambda_function" "investigator" {
  function_name = local.lambda_functions.investigator
  role          = aws_iam_role.investigator.arn
  handler       = "handler.lambda_handler"
  runtime       = local.lambda_runtime
  architectures = local.architectures
  filename      = data.archive_file.investigator.output_path
  # Generous: evidence collection fans out across five AWS APIs and then waits
  # on Bedrock, which is retried with backoff.
  timeout          = 180
  memory_size      = 1024
  source_code_hash = data.archive_file.investigator.output_base64sha256
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = merge(local.common_env, local.bedrock_env, local.evidence_env)
  }

  tags       = merge(local.tags, { Name = local.lambda_functions.investigator })
  depends_on = [aws_cloudwatch_log_group.functions]
}

# --- Remediation --------------------------------------------------------------
resource "aws_lambda_function" "remediation" {
  function_name    = local.lambda_functions.remediation
  role             = aws_iam_role.remediation.arn
  handler          = "handler.lambda_handler"
  runtime          = local.lambda_runtime
  architectures    = local.architectures
  filename         = data.archive_file.remediation.output_path
  timeout          = 120
  memory_size      = 512
  source_code_hash = data.archive_file.remediation.output_base64sha256
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = merge(local.common_env, {})
  }

  tags       = merge(local.tags, { Name = local.lambda_functions.remediation })
  depends_on = [aws_cloudwatch_log_group.functions]
}

# --- Verifier -----------------------------------------------------------------
resource "aws_lambda_function" "verifier" {
  function_name = local.lambda_functions.verifier
  role          = aws_iam_role.verifier.arn
  handler       = "handler.lambda_handler"
  runtime       = local.lambda_runtime
  architectures = local.architectures
  filename      = data.archive_file.verifier.output_path
  # Must cover the whole verification window plus headroom to record the
  # verdict. Sleeping here avoids adding an orchestration service.
  timeout          = max(300, var.verification_checks * var.verification_interval_seconds + 120)
  memory_size      = 512
  source_code_hash = data.archive_file.verifier.output_base64sha256
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = merge(local.common_env, local.evidence_env, {
      VERIFICATION_CHECKS           = tostring(var.verification_checks)
      VERIFICATION_INTERVAL_SECONDS = tostring(var.verification_interval_seconds)
    })
  }

  tags       = merge(local.tags, { Name = local.lambda_functions.verifier })
  depends_on = [aws_cloudwatch_log_group.functions]
}

# --- Postmortem ---------------------------------------------------------------
resource "aws_lambda_function" "postmortem" {
  function_name    = local.lambda_functions.postmortem
  role             = aws_iam_role.postmortem.arn
  handler          = "handler.lambda_handler"
  runtime          = local.lambda_runtime
  architectures    = local.architectures
  filename         = data.archive_file.postmortem.output_path
  timeout          = 120
  memory_size      = 512
  source_code_hash = data.archive_file.postmortem.output_base64sha256
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = merge(local.common_env, local.bedrock_env)
  }

  tags       = merge(local.tags, { Name = local.lambda_functions.postmortem })
  depends_on = [aws_cloudwatch_log_group.functions]
}

# --- Demo Lab controller ------------------------------------------------------
resource "aws_lambda_function" "demo_controller" {
  function_name    = local.lambda_functions.demo_controller
  role             = aws_iam_role.demo_controller.arn
  handler          = "handler.lambda_handler"
  runtime          = local.lambda_runtime
  architectures    = local.architectures
  filename         = data.archive_file.demo_controller.output_path
  timeout          = 60
  memory_size      = 512
  source_code_hash = data.archive_file.demo_controller.output_base64sha256
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = merge(local.common_env, {
      INJECT_TRAFFIC = "8"
    })
  }

  tags       = merge(local.tags, { Name = local.lambda_functions.demo_controller, DemoLab = "true" })
  depends_on = [aws_cloudwatch_log_group.functions]
}

# --- Traffic generator --------------------------------------------------------
resource "aws_lambda_function" "traffic_generator" {
  function_name    = local.lambda_functions.traffic_generator
  role             = aws_iam_role.traffic_generator.arn
  handler          = "handler.lambda_handler"
  runtime          = local.lambda_runtime
  architectures    = local.architectures
  filename         = data.archive_file.traffic_generator.output_path
  timeout          = 30
  memory_size      = 256
  source_code_hash = data.archive_file.traffic_generator.output_base64sha256
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = merge(local.common_env, {
      REQUESTS_PER_RUN = "4"
    })
  }

  tags       = merge(local.tags, { Name = local.lambda_functions.traffic_generator, DemoLab = "true" })
  depends_on = [aws_cloudwatch_log_group.functions]
}
