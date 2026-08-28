output "opspilot_dashboard_url" {
  description = "Open this in a browser to use OpsPilot."
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "opspilot_api_url" {
  description = "Base URL of the OpsPilot API."
  value       = local.api_base_url
}

output "opspilot_region" {
  description = "AWS region hosting this deployment."
  value       = var.aws_region
}

output "opspilot_bedrock_model" {
  description = "Bedrock model used for incident analysis."
  value       = var.bedrock_model_id
}

output "opspilot_demo_instructions" {
  description = "How to run the OpsPilot demo."
  value       = <<-EOT

    OpsPilot is deployed. From incident detection to verified recovery:

      1. Open the dashboard:
         https://${aws_cloudfront_distribution.frontend.domain_name}

         CloudFront takes a few minutes to finish propagating after the first
         apply. If you get an error page, wait a moment and reload.

      2. Confirm the environment is healthy (header shows "System Healthy").

      3. Click "Inject Lambda Error" in the Demo Lab panel.

      4. Wait about 2-3 minutes. In order, OpsPilot will:
           - CloudWatch alarm ${local.alarm_names.lambda_error} enters ALARM
           - EventBridge delivers the transition to the incident detector
           - the incident appears on the dashboard as DETECTED
           - the investigator collects logs, metrics, CloudTrail and app state
           - change correlation identifies the configuration change
           - Bedrock (${var.bedrock_model_id}) produces the root-cause analysis
           - the incident moves to AWAITING_APPROVAL

      5. Open the incident and review the timeline, changes, evidence and
         recommended remediation.

      6. Click "Approve Remediation". OpsPilot will remediate, verify recovery
         against the live service, resolve the incident and write a postmortem
         to S3.

      7. Inject the same scenario a second time to see incident memory recall
         the earlier occurrence.

    Command line equivalent:

      curl ${local.api_base_url}/health
      curl -X POST ${local.api_base_url}/demo/inject \
        -H 'content-type: application/json' -d '{"scenario":"lambda_error"}'
      curl ${local.api_base_url}/incidents

    Full smoke test:

      ./scripts/smoke_test.sh ${local.api_base_url}

    Tear everything down:

      terraform destroy

  EOT
}

# --- Supporting detail --------------------------------------------------------
output "opspilot_demo_app_url" {
  description = "Public endpoint of the Demo Lab sample application."
  value       = "${local.api_base_url}/demo/app"
}

output "opspilot_incidents_table" {
  description = "DynamoDB table holding incidents."
  value       = aws_dynamodb_table.incidents.name
}

output "opspilot_artifacts_bucket" {
  description = "S3 bucket holding generated postmortems."
  value       = aws_s3_bucket.artifacts.bucket
}

output "opspilot_event_bus" {
  description = "Custom EventBridge bus carrying OpsPilot lifecycle events."
  value       = aws_cloudwatch_event_bus.opspilot.name
}

output "opspilot_demo_alarms" {
  description = "CloudWatch alarms that open OpsPilot incidents."
  value       = local.demo_alarm_list
}

output "opspilot_demo_scenarios" {
  description = "Failure scenarios the Demo Lab can inject."
  value = [
    "lambda_error",
    "lambda_latency",
    "application_error",
    "database_throttle",
    "configuration_error",
  ]
}

output "opspilot_cloudtrail_enabled" {
  description = "Whether a project-owned CloudTrail trail was created. Change correlation also reads the free 90-day CloudTrail event history regardless."
  value       = var.enable_cloudtrail
}
