# Dashboard hosting: private S3 bucket behind CloudFront with Origin Access
# Control.
#
# S3 static website hosting would be simpler, but it requires a publicly
# readable bucket and serves over plain HTTP. OAC keeps the bucket private,
# gives the dashboard HTTPS, and stays inside the CloudFront free tier.

resource "aws_s3_bucket" "frontend" {
  bucket        = "${local.prefix}-frontend-${local.suffix}"
  force_destroy = true

  tags = merge(local.tags, { Name = "${local.prefix}-frontend" })
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${local.prefix}-frontend-oac"
  description                       = "OAC for the OpsPilot dashboard bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "OpsPilot ${var.environment} dashboard"
  price_class         = "PriceClass_100" # cheapest edge footprint

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "opspilot-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  default_cache_behavior {
    target_origin_id       = "opspilot-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # CachingOptimized. The dashboard is static; live data comes from the API,
    # which is never fronted by CloudFront.
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  # A single-page dashboard: unknown paths return the app, not an S3 error.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = merge(local.tags, { Name = "${local.prefix}-frontend" })
}

data "aws_iam_policy_document" "frontend_bucket" {
  statement {
    sid    = "AllowCloudFrontServicePrincipalReadOnly"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.frontend.arn,
      "${aws_s3_bucket.frontend.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

# --- Dashboard assets ---------------------------------------------------------
# The API URL is injected at apply time, so the dashboard needs no manual
# configuration after deployment.
locals {
  frontend_dir = "${path.module}/../frontend"

  frontend_config = templatefile("${path.module}/templates/config.js.tftpl", {
    api_url         = local.api_base_url
    region          = local.region
    environment     = var.environment
    bedrock_model   = var.bedrock_model_id
    demo_app_url    = "${local.api_base_url}/demo/app"
    resource_prefix = local.prefix
  })

  frontend_files = {
    "index.html" = { source = "${local.frontend_dir}/index.html", type = "text/html; charset=utf-8" }
    "styles.css" = { source = "${local.frontend_dir}/styles.css", type = "text/css; charset=utf-8" }
    "app.js"     = { source = "${local.frontend_dir}/app.js", type = "application/javascript; charset=utf-8" }
  }
}

resource "aws_s3_object" "frontend" {
  for_each = local.frontend_files

  bucket        = aws_s3_bucket.frontend.id
  key           = each.key
  source        = each.value.source
  etag          = filemd5(each.value.source)
  content_type  = each.value.type
  cache_control = "no-cache, must-revalidate"

  depends_on = [aws_s3_bucket_policy.frontend]
}

resource "aws_s3_object" "frontend_config" {
  bucket        = aws_s3_bucket.frontend.id
  key           = "config.js"
  content       = local.frontend_config
  etag          = md5(local.frontend_config)
  content_type  = "application/javascript; charset=utf-8"
  cache_control = "no-cache, must-revalidate"

  depends_on = [aws_s3_bucket_policy.frontend]
}

# Note on cache invalidation: the dashboard objects are uploaded with
# "Cache-Control: no-cache, must-revalidate", so CloudFront revalidates against
# S3 on every request and a redeploy is visible immediately. That avoids paying
# for invalidations, and avoids a local-exec dependency on the AWS CLI. If you
# switch to long-lived cache headers, add an explicit invalidation step.
