output "gallery_website_url" {
  description = "Public URL of the always-on creative agent's gallery site."
  value       = "http://${aws_s3_bucket_website_configuration.gallery.website_endpoint}"
}

output "bucket_name" {
  description = "S3 bucket storing generated art, poems and site."
  value       = aws_s3_bucket.gallery.id
}

output "lambda_function_name" {
  description = "Name of the agent's Lambda function (for manual invokes / logs)."
  value       = aws_lambda_function.agent.function_name
}

output "schedule_name" {
  description = "EventBridge Scheduler schedule driving autonomous runs."
  value       = aws_scheduler_schedule.daily_run.name
}

output "manual_invoke_command" {
  description = "Handy AWS CLI command to trigger the agent on demand for testing/screenshots."
  value       = "aws lambda invoke --function-name ${aws_lambda_function.agent.function_name} --region ${var.aws_region} out.json && cat out.json"
}
