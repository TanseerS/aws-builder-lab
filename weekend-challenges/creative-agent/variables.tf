variable "aws_region" {
  description = "AWS region to deploy into. Must have Bedrock access enabled for the text model (see image_model_region for the image model)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix used for naming all resources."
  type        = string
  default     = "weather-muse-agent"
}

variable "location_name" {
  description = "Human-readable location the agent themes its art/poems on."
  type        = string
  default     = "Nashik"
}

variable "latitude" {
  description = "Latitude for the Open-Meteo weather lookup."
  type        = string
  default     = "19.9975"
}

variable "longitude" {
  description = "Longitude for the Open-Meteo weather lookup."
  type        = string
  default     = "73.7898"
}

variable "enable_image_generation" {
  description = "Generate artwork alongside the poem. Off by default: every Bedrock text-to-image model requires a paid AWS Marketplace subscription, so the agent runs poem-only until that is set up."
  type        = bool
  default     = false
}

variable "image_model_id" {
  description = "Bedrock model id used for image generation."
  type        = string
  default     = "stability.stable-image-core-v1:1"
}

variable "image_model_region" {
  description = "Region to call the image model in. Bedrock does not offer the text-to-image models in every region, so this is separate from aws_region."
  type        = string
  default     = "us-west-2"
}

variable "image_aspect_ratio" {
  description = "Aspect ratio for generated art. Stability text-to-image models take a ratio rather than explicit pixel dimensions."
  type        = string
  default     = "1:1"
}

variable "text_model_id" {
  description = "Bedrock model id used for poem generation."
  type        = string
  default     = "amazon.nova-micro-v1:0"
}

variable "schedule_expression" {
  description = "EventBridge Scheduler expression controlling how often the agent runs autonomously."
  type        = string
  default     = "rate(1 day)"
}

variable "trigger_initial_run" {
  description = "If true, Terraform invokes the Lambda once right after deployment so the gallery isn't empty."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda function."
  type        = number
  default     = 14
}
