# API Gateway HTTP API.
#
# HTTP API rather than REST API: it is cheaper, has native CORS, and OpsPilot
# needs none of the REST API features (usage plans, request validators, WAF
# integration at the API layer).

resource "aws_apigatewayv2_api" "opspilot" {
  name          = "${local.prefix}-api"
  protocol_type = "HTTP"
  description   = "OpsPilot incident lifecycle API"

  cors_configuration {
    # The dashboard is served from a CloudFront domain that is only known after
    # this API exists, so origins are open. Every route is read-mostly and the
    # only state-changing routes are guarded server side by the allowlist and
    # incident-state checks in the API Lambda.
    allow_origins  = ["*"]
    allow_methods  = ["GET", "POST", "OPTIONS"]
    allow_headers  = ["content-type"]
    expose_headers = ["content-type"]
    max_age        = 300
  }

  tags = merge(local.tags, { Name = "${local.prefix}-api" })
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = "/aws/apigateway/${local.prefix}-api"
  retention_in_days = var.log_retention_days
  tags              = merge(local.tags, { Name = "/aws/apigateway/${local.prefix}-api" })
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.opspilot.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId        = "$context.requestId"
      httpMethod       = "$context.httpMethod"
      path             = "$context.path"
      status           = "$context.status"
      responseLength   = "$context.responseLength"
      responseLatency  = "$context.responseLatency"
      integrationError = "$context.integrationErrorMessage"
      sourceIp         = "$context.identity.sourceIp"
    })
  }

  default_route_settings {
    # A hard ceiling on how fast the Demo Lab can be driven, and a cheap guard
    # against an accidental loop against a public endpoint.
    throttling_burst_limit = 50
    throttling_rate_limit  = 25
  }

  tags = merge(local.tags, { Name = "${local.prefix}-api-stage" })
}

# --- Integrations -------------------------------------------------------------
resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.opspilot.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}

# The demo application is reachable over real HTTP, so the application error
# scenario produces genuine HTTP 500 responses to a genuine caller.
resource "aws_apigatewayv2_integration" "demo_app" {
  api_id                 = aws_apigatewayv2_api.opspilot.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.demo_app.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}

# --- Routes -------------------------------------------------------------------
locals {
  api_routes = {
    health          = "GET /health"
    list_incidents  = "GET /incidents"
    get_incident    = "GET /incidents/{id}"
    approve         = "POST /incidents/{id}/approve"
    reject          = "POST /incidents/{id}/reject"
    reinvestigate   = "POST /incidents/{id}/reinvestigate"
    postmortem      = "GET /incidents/{id}/postmortem"
    metrics_summary = "GET /metrics/summary"
    demo_inject     = "POST /demo/inject"
    demo_reset      = "POST /demo/reset"
    demo_status     = "GET /demo/status"
  }
}

resource "aws_apigatewayv2_route" "api" {
  for_each = local.api_routes

  api_id    = aws_apigatewayv2_api.opspilot.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "demo_app" {
  api_id    = aws_apigatewayv2_api.opspilot.id
  route_key = "GET /demo/app"
  target    = "integrations/${aws_apigatewayv2_integration.demo_app.id}"
}

# --- Invoke permissions -------------------------------------------------------
locals {
  # invoke_url carries a trailing slash; every consumer wants it without one so
  # that "${base}/health" does not become "${base}//health".
  api_base_url = trimsuffix(aws_apigatewayv2_stage.default.invoke_url, "/")
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.opspilot.execution_arn}/*/*"
}

resource "aws_lambda_permission" "api_gateway_demo_app" {
  statement_id  = "AllowExecutionFromAPIGatewayDemo"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.demo_app.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.opspilot.execution_arn}/*/*"
}
