# CloudTrail for change correlation.
#
# IMPORTANT - what this does and does not give you:
#
# OpsPilot's investigator reads change history through cloudtrail:LookupEvents,
# which queries the free 90-day CloudTrail *event history* that every AWS
# account has by default. That works with or without the trail below, which is
# why investigation still functions when enable_cloudtrail = false.
#
# The trail below adds durable, queryable retention in a project-owned bucket.
# It is deliberately narrow:
#
#   * single region        - no multi-region duplication
#   * management events    - no data events, which are the expensive ones
#   * write events only    - read-only API calls cannot change behaviour
#   * 30-day expiry        - storage cost stays negligible
#
# The first copy of management events in a region is free, so the running cost
# of this trail is S3 storage measured in cents.
#
# LIMITATION, stated plainly: CloudTrail delivery is NOT instantaneous. Event
# history typically lags real API activity by several minutes, and trail
# delivery to S3 can take up to ~15 minutes. A change made seconds before an
# incident may therefore not be visible to CloudTrail yet. This is exactly why
# OpsPilot correlates against two sources - CloudTrail and its own change log -
# and labels every change with which source it came from. OpsPilot does not
# claim complete visibility into every AWS change.

resource "aws_s3_bucket" "cloudtrail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket        = "${local.prefix}-cloudtrail-${local.suffix}"
  force_destroy = true

  tags = merge(local.tags, { Name = "${local.prefix}-cloudtrail" })
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket = aws_s3_bucket.cloudtrail[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket = aws_s3_bucket.cloudtrail[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket = aws_s3_bucket.cloudtrail[0].id

  rule {
    id     = "expire-trail-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "cloudtrail_bucket" {
  count = var.enable_cloudtrail ? 1 : 0

  statement {
    sid    = "AWSCloudTrailAclCheck"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.cloudtrail[0].arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:cloudtrail:${local.region}:${local.account_id}:trail/${local.prefix}-trail"]
    }
  }

  statement {
    sid    = "AWSCloudTrailWrite"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cloudtrail[0].arn}/AWSLogs/${local.account_id}/*"]

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:cloudtrail:${local.region}:${local.account_id}:trail/${local.prefix}-trail"]
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
      aws_s3_bucket.cloudtrail[0].arn,
      "${aws_s3_bucket.cloudtrail[0].arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket = aws_s3_bucket.cloudtrail[0].id
  policy = data.aws_iam_policy_document.cloudtrail_bucket[0].json

  depends_on = [aws_s3_bucket_public_access_block.cloudtrail]
}

resource "aws_cloudtrail" "opspilot" {
  count = var.enable_cloudtrail ? 1 : 0

  name                          = "${local.prefix}-trail"
  s3_bucket_name                = aws_s3_bucket.cloudtrail[0].id
  include_global_service_events = true
  is_multi_region_trail         = false
  enable_log_file_validation    = true
  enable_logging                = true

  # Management events only, and only the ones that can change behaviour.
  # Data events (S3 object access, Lambda invocations) are the expensive part of
  # CloudTrail and contribute nothing to change correlation.
  event_selector {
    read_write_type           = "WriteOnly"
    include_management_events = true
  }

  tags = merge(local.tags, { Name = "${local.prefix}-trail" })

  depends_on = [aws_s3_bucket_policy.cloudtrail]
}
