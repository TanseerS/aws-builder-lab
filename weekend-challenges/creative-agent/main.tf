data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# S3 bucket: hosts the static gallery site AND stores the agent's output
# (daily PNGs + manifest.json + state.json). Public-read so the gallery is
# viewable without any backend.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "gallery" {
  bucket = "${var.project_name}-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "gallery" {
  bucket                  = aws_s3_bucket.gallery.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_website_configuration" "gallery" {
  bucket = aws_s3_bucket.gallery.id
  index_document {
    suffix = "index.html"
  }
}

resource "aws_s3_bucket_policy" "public_read" {
  bucket = aws_s3_bucket.gallery.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Sid       = "PublicReadGetObject",
      Effect    = "Allow",
      Principal = "*",
      Action    = "s3:GetObject",
      Resource  = "${aws_s3_bucket.gallery.arn}/*"
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.gallery]
}

resource "aws_s3_object" "site_index" {
  bucket       = aws_s3_bucket.gallery.id
  key          = "index.html"
  source       = "${path.module}/site/index.html"
  etag         = filemd5("${path.module}/site/index.html")
  content_type = "text/html"
}

# Seed files so the site never 404s before the agent's first run.
# Ignored on subsequent applies since the Lambda owns these afterwards.
resource "aws_s3_object" "manifest_seed" {
  bucket       = aws_s3_bucket.gallery.id
  key          = "manifest.json"
  content      = "[]"
  content_type = "application/json"

  lifecycle {
    ignore_changes = [content, etag]
  }
}

resource "aws_s3_object" "state_seed" {
  bucket       = aws_s3_bucket.gallery.id
  key          = "state.json"
  content      = jsonencode({ style_index = 0 })
  content_type = "application/json"

  lifecycle {
    ignore_changes = [content, etag]
  }
}

# ---------------------------------------------------------------------------
# Lambda function: the agent's brain
# ---------------------------------------------------------------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/build/lambda.zip"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "lambda_role" {
  name = "${var.project_name}-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Service = "lambda.amazonaws.com" },
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid    = "Logs",
        Effect = "Allow",
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ],
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        Sid    = "GalleryBucket",
        Effect = "Allow",
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ],
        Resource = "${aws_s3_bucket.gallery.arn}/*"
      },
      {
        Sid    = "Bedrock",
        Effect = "Allow",
        Action = [
          "bedrock:InvokeModel"
        ],
        Resource = [
          "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.image_model_id}",
          "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.text_model_id}"
        ]
      }
    ]
  })
}

resource "aws_lambda_function" "agent" {
  function_name    = var.project_name
  role             = aws_iam_role.lambda_role.arn
  handler          = "index.handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 512
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      BUCKET_NAME    = aws_s3_bucket.gallery.id
      LOCATION_NAME  = var.location_name
      LATITUDE       = var.latitude
      LONGITUDE      = var.longitude
      IMAGE_MODEL_ID = var.image_model_id
      TEXT_MODEL_ID  = var.text_model_id
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler: makes the agent autonomous / always-on
# ---------------------------------------------------------------------------
resource "aws_iam_role" "scheduler_role" {
  name = "${var.project_name}-scheduler-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Service = "scheduler.amazonaws.com" },
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_policy" {
  name = "${var.project_name}-scheduler-policy"
  role = aws_iam_role.scheduler_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect   = "Allow",
      Action   = "lambda:InvokeFunction",
      Resource = aws_lambda_function.agent.arn
    }]
  })
}

resource "aws_scheduler_schedule" "daily_run" {
  name                         = "${var.project_name}-daily"
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.agent.arn
    role_arn = aws_iam_role.scheduler_role.arn
  }
}

resource "aws_lambda_permission" "allow_scheduler" {
  statement_id  = "AllowEventBridgeSchedulerInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.agent.function_name
  principal     = "scheduler.amazonaws.com"
  source_arn    = aws_scheduler_schedule.daily_run.arn
}

# ---------------------------------------------------------------------------
# Optional: run the agent once immediately after deploy so the gallery has
# proof of autonomous output straight away (useful for challenge screenshots).
# ---------------------------------------------------------------------------
resource "null_resource" "initial_invoke" {
  count = var.trigger_initial_run ? 1 : 0

  triggers = {
    lambda_version = aws_lambda_function.agent.source_code_hash
  }

  provisioner "local-exec" {
    command = "aws lambda invoke --function-name ${aws_lambda_function.agent.function_name} --region ${var.aws_region} --cli-read-timeout 90 /tmp/${var.project_name}-invoke.json"
  }

  depends_on = [
    aws_lambda_permission.allow_scheduler,
    aws_iam_role_policy.lambda_policy,
    aws_s3_object.manifest_seed,
    aws_s3_object.state_seed
  ]
}
