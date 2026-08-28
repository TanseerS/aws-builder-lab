# --- Identity -----------------------------------------------------------------
variable "aws_region" {
  description = "AWS region for every OpsPilot resource."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name, used in every resource name."
  type        = string
  default     = "showcase"

  validation {
    condition     = can(regex("^[a-z0-9-]{1,20}$", var.environment))
    error_message = "environment must be 1-20 lowercase alphanumeric or hyphen characters."
  }
}

variable "project_name" {
  description = "Project name prefix."
  type        = string
  default     = "opspilot"
}

# --- Bedrock ------------------------------------------------------------------
variable "bedrock_model_id" {
  description = <<-EOT
    Amazon Bedrock text generation model ID. Any Converse-compatible text model
    works; the default is chosen for low cost and broad availability.
  EOT
  type        = string
  default     = "amazon.nova-lite-v1:0"
}

variable "bedrock_max_tokens" {
  description = "Maximum tokens Bedrock may generate per analysis."
  type        = number
  default     = 2000

  validation {
    condition     = var.bedrock_max_tokens >= 256 && var.bedrock_max_tokens <= 8192
    error_message = "bedrock_max_tokens must be between 256 and 8192."
  }
}

variable "bedrock_temperature" {
  description = "Sampling temperature. Low by default: this is operational diagnosis, not creative writing."
  type        = number
  default     = 0.1

  validation {
    condition     = var.bedrock_temperature >= 0 && var.bedrock_temperature <= 1
    error_message = "bedrock_temperature must be between 0 and 1."
  }
}

# --- Evidence collection bounds ----------------------------------------------
variable "max_log_events" {
  description = "Maximum CloudWatch log events collected per investigation."
  type        = number
  default     = 100
}

variable "max_cloudtrail_events" {
  description = "Maximum CloudTrail events collected per investigation."
  type        = number
  default     = 50
}

variable "max_metric_points" {
  description = "Maximum datapoints collected per metric series."
  type        = number
  default     = 100
}

variable "max_prompt_chars" {
  description = "Character budget for the evidence section of a Bedrock prompt."
  type        = number
  default     = 18000
}

variable "change_lookback_minutes" {
  description = "How far back to search for infrastructure changes preceding an incident."
  type        = number
  default     = 15
}

# --- Verification -------------------------------------------------------------
variable "verification_checks" {
  description = "Number of recovery probes run after remediation."
  type        = number
  default     = 6
}

variable "verification_interval_seconds" {
  description = "Seconds between recovery probes."
  type        = number
  default     = 30
}

# --- Feature toggles ----------------------------------------------------------
variable "enable_cloudtrail" {
  description = <<-EOT
    Create a project-owned, single-region, management-events-only CloudTrail
    trail. Change correlation also reads the free 90-day CloudTrail event
    history, so investigation still works when this is false.
  EOT
  type        = bool
  default     = true
}

variable "enable_traffic_generator" {
  description = <<-EOT
    Run a scheduled trickle of demo-app requests so CloudWatch alarms always
    have datapoints to evaluate. Disabling this makes incident detection much
    slower and less reliable.
  EOT
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for every OpsPilot log group."
  type        = number
  default     = 14
}

# --- Demo Lab -----------------------------------------------------------------
variable "demo_table_read_capacity" {
  description = "Demo table RCU. Deliberately tiny so throttling is reproducible inside the free tier."
  type        = number
  default     = 1
}

variable "demo_table_write_capacity" {
  description = "Demo table WCU. Deliberately tiny so throttling is reproducible inside the free tier."
  type        = number
  default     = 1
}
