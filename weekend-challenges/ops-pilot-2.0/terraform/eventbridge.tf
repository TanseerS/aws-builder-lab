# OpsPilot event-driven orchestration.
#
# Stages are coupled through events rather than direct invocation, so any stage
# can fail, be retried or be replaced without the others knowing.
#
#   CloudWatch Alarm ──(default bus)──> Incident Detector
#                                            │
#                                     (opspilot bus)
#                                            ▼
#   Investigator ──> [human approval] ──> Remediation ──> Verifier ──> Postmortem

resource "aws_cloudwatch_event_bus" "opspilot" {
  name = "${local.prefix}-events"
  tags = merge(local.tags, { Name = "${local.prefix}-events" })
}

# --- Alarm ingress (default bus) ----------------------------------------------
# CloudWatch publishes alarm state changes to the default bus only, so this one
# rule necessarily lives there. It is filtered to this deployment's alarms.
resource "aws_cloudwatch_event_rule" "alarm_state_change" {
  name        = "${local.prefix}-alarm-state-change"
  description = "Routes OpsPilot demo alarm transitions to the incident detector"

  event_pattern = jsonencode({
    source      = ["aws.cloudwatch"]
    detail-type = ["CloudWatch Alarm State Change"]
    detail = {
      alarmName = local.demo_alarm_list
      state = {
        value = ["ALARM"]
      }
    }
  })

  tags = merge(local.tags, { Name = "${local.prefix}-alarm-state-change" })
}

resource "aws_cloudwatch_event_target" "alarm_state_change" {
  rule      = aws_cloudwatch_event_rule.alarm_state_change.name
  target_id = "incident-detector"
  arn       = aws_lambda_function.incident_detector.arn

  retry_policy {
    maximum_event_age_in_seconds = 300
    maximum_retry_attempts       = 3
  }
}

resource "aws_lambda_permission" "alarm_state_change" {
  statement_id  = "AllowExecutionFromCloudWatchAlarmRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.incident_detector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.alarm_state_change.arn
}

# --- Incident Detected -> Investigator ----------------------------------------
resource "aws_cloudwatch_event_rule" "incident_detected" {
  name           = "${local.prefix}-incident-detected"
  description    = "Starts an investigation when an incident is opened"
  event_bus_name = aws_cloudwatch_event_bus.opspilot.name

  event_pattern = jsonencode({
    source      = ["opspilot.core"]
    detail-type = ["OpsPilot Incident Detected", "OpsPilot Reinvestigation Requested"]
  })

  tags = merge(local.tags, { Name = "${local.prefix}-incident-detected" })
}

resource "aws_cloudwatch_event_target" "incident_detected" {
  rule           = aws_cloudwatch_event_rule.incident_detected.name
  event_bus_name = aws_cloudwatch_event_bus.opspilot.name
  target_id      = "investigator"
  arn            = aws_lambda_function.investigator.arn

  retry_policy {
    maximum_event_age_in_seconds = 600
    maximum_retry_attempts       = 2
  }
}

resource "aws_lambda_permission" "incident_detected" {
  statement_id  = "AllowExecutionFromIncidentDetectedRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.investigator.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.incident_detected.arn
}

# --- Remediation Approved -> Remediation --------------------------------------
# There is deliberately no rule from "Investigation Completed" to remediation.
# The only path into this rule is a human approving through the API.
resource "aws_cloudwatch_event_rule" "remediation_approved" {
  name           = "${local.prefix}-remediation-approved"
  description    = "Executes remediation after a human has approved it"
  event_bus_name = aws_cloudwatch_event_bus.opspilot.name

  event_pattern = jsonencode({
    source      = ["opspilot.core"]
    detail-type = ["OpsPilot Remediation Approved"]
  })

  tags = merge(local.tags, { Name = "${local.prefix}-remediation-approved" })
}

resource "aws_cloudwatch_event_target" "remediation_approved" {
  rule           = aws_cloudwatch_event_rule.remediation_approved.name
  event_bus_name = aws_cloudwatch_event_bus.opspilot.name
  target_id      = "remediation"
  arn            = aws_lambda_function.remediation.arn

  retry_policy {
    maximum_event_age_in_seconds = 300
    # Remediation mutates state: no automatic retries. A failed remediation
    # moves the incident to FAILED for a human to look at.
    maximum_retry_attempts = 0
  }
}

resource "aws_lambda_permission" "remediation_approved" {
  statement_id  = "AllowExecutionFromRemediationApprovedRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.remediation.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.remediation_approved.arn
}

# --- Remediation Completed -> Verifier ----------------------------------------
resource "aws_cloudwatch_event_rule" "remediation_completed" {
  name           = "${local.prefix}-remediation-completed"
  description    = "Verifies recovery once remediation has run"
  event_bus_name = aws_cloudwatch_event_bus.opspilot.name

  event_pattern = jsonencode({
    source      = ["opspilot.core"]
    detail-type = ["OpsPilot Remediation Completed"]
  })

  tags = merge(local.tags, { Name = "${local.prefix}-remediation-completed" })
}

resource "aws_cloudwatch_event_target" "remediation_completed" {
  rule           = aws_cloudwatch_event_rule.remediation_completed.name
  event_bus_name = aws_cloudwatch_event_bus.opspilot.name
  target_id      = "verifier"
  arn            = aws_lambda_function.verifier.arn

  retry_policy {
    maximum_event_age_in_seconds = 600
    maximum_retry_attempts       = 1
  }
}

resource "aws_lambda_permission" "remediation_completed" {
  statement_id  = "AllowExecutionFromRemediationCompletedRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.verifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.remediation_completed.arn
}

# --- Verification Completed -> Postmortem -------------------------------------
resource "aws_cloudwatch_event_rule" "verification_completed" {
  name           = "${local.prefix}-verification-completed"
  description    = "Generates a postmortem once an incident reaches a terminal state"
  event_bus_name = aws_cloudwatch_event_bus.opspilot.name

  event_pattern = jsonencode({
    source      = ["opspilot.core"]
    detail-type = ["OpsPilot Verification Completed"]
  })

  tags = merge(local.tags, { Name = "${local.prefix}-verification-completed" })
}

resource "aws_cloudwatch_event_target" "verification_completed" {
  rule           = aws_cloudwatch_event_rule.verification_completed.name
  event_bus_name = aws_cloudwatch_event_bus.opspilot.name
  target_id      = "postmortem"
  arn            = aws_lambda_function.postmortem.arn

  retry_policy {
    maximum_event_age_in_seconds = 600
    maximum_retry_attempts       = 2
  }
}

resource "aws_lambda_permission" "verification_completed" {
  statement_id  = "AllowExecutionFromVerificationCompletedRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.postmortem.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.verification_completed.arn
}
