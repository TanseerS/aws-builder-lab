# --- Incident store -----------------------------------------------------------
# On-demand billing: incident volume is spiky and low, and this table must never
# be the thing that throttles during an incident.
resource "aws_dynamodb_table" "incidents" {
  name         = "${local.prefix}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "incident_id"

  attribute {
    name = "incident_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "detected_at"
    type = "S"
  }

  attribute {
    name = "signature"
    type = "S"
  }

  # Drives the dashboard's active/historical views.
  global_secondary_index {
    name            = "status-detected_at-index"
    hash_key        = "status"
    range_key       = "detected_at"
    projection_type = "ALL"
  }

  # Incident memory: deterministic recall of past incidents that share a
  # failure signature, with no embeddings and no vector database.
  global_secondary_index {
    name            = "signature-detected_at-index"
    hash_key        = "signature"
    range_key       = "detected_at"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.tags, { Name = "${local.prefix}-incidents" })
}

# --- Change log ---------------------------------------------------------------
# Records configuration changes at the instant they are applied, which is what
# makes sub-minute change correlation possible. CloudTrail remains the
# independent, authoritative record.
resource "aws_dynamodb_table" "changes" {
  name         = "${local.prefix}-changes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "change_id"

  attribute {
    name = "change_id"
    type = "S"
  }

  attribute {
    name = "scope"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  # Single-partition time-range index: the volume is tiny and this avoids ever
  # scanning the table during an investigation.
  global_secondary_index {
    name            = "scope-timestamp-index"
    hash_key        = "scope"
    range_key       = "timestamp"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(local.tags, { Name = "${local.prefix}-changes" })
}

# --- Demo Lab table -----------------------------------------------------------
# Provisioned at 1 RCU / 1 WCU on purpose. That is inside the DynamoDB free
# tier and it is what allows the database_throttle scenario to produce *real*
# ProvisionedThroughputExceededException errors and real CloudWatch throttle
# metrics, rather than a simulated incident.
resource "aws_dynamodb_table" "demo" {
  name           = local.demo_table_name
  billing_mode   = "PROVISIONED"
  read_capacity  = var.demo_table_read_capacity
  write_capacity = var.demo_table_write_capacity
  hash_key       = "pk"
  range_key      = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(local.tags, {
    Name    = local.demo_table_name
    DemoLab = "true"
  })
}
