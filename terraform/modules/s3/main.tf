locals {
  buckets = {
    checkpoints = "${var.name}-flink-checkpoints"
    archive     = "${var.name}-log-archive"
  }
}

# ── Buckets ───────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "buckets" {
  for_each = local.buckets

  bucket        = each.value
  force_destroy = false

  tags = { Name = each.value, Purpose = each.key }
}

# Block all public access — these buckets are internal only
resource "aws_s3_bucket_public_access_block" "buckets" {
  for_each = aws_s3_bucket.buckets

  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning — required for Flink checkpoint recovery and log reprocessing
resource "aws_s3_bucket_versioning" "buckets" {
  for_each = aws_s3_bucket.buckets

  bucket = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-KMS encryption
resource "aws_s3_bucket_server_side_encryption_configuration" "buckets" {
  for_each = aws_s3_bucket.buckets

  bucket = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true   # reduces KMS API call cost by ~99%
  }
}

# Enforce TLS-only access
resource "aws_s3_bucket_policy" "tls_only" {
  for_each = aws_s3_bucket.buckets

  bucket = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyNonTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          each.value.arn,
          "${each.value.arn}/*"
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
      {
        Sid    = "AllowEKSNodes"
        Effect = "Allow"
        Principal = { AWS = var.eks_node_role_arn }
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [each.value.arn, "${each.value.arn}/*"]
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.buckets]
}

# ── Lifecycle policies ────────────────────────────────────────────────────────

resource "aws_s3_bucket_lifecycle_configuration" "checkpoints" {
  bucket = aws_s3_bucket.buckets["checkpoints"].id

  rule {
    id     = "expire-old-checkpoints"
    status = "Enabled"
    filter { prefix = "checkpoints/" }

    expiration {
      days = var.flink_checkpoint_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket = aws_s3_bucket.buckets["archive"].id

  rule {
    id     = "tiered-archival"
    status = "Enabled"
    filter { prefix = "" }

    transition {
      days          = var.log_archive_retention_days
      storage_class = "GLACIER_IR"
    }

    transition {
      days          = var.log_archive_glacier_days
      storage_class = "DEEP_ARCHIVE"
    }

    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "GLACIER_IR"
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }
}

# ── Access logging ────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "access_logs" {
  bucket        = "${var.name}-s3-access-logs"
  force_destroy = false
  tags          = { Name = "${var.name}-s3-access-logs", Purpose = "access-logs" }
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket                  = aws_s3_bucket.access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  rule {
    id     = "expire-access-logs"
    status = "Enabled"
    filter { prefix = "" }
    expiration { days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_s3_bucket_logging" "buckets" {
  for_each = aws_s3_bucket.buckets

  bucket        = each.value.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "${each.key}/"
}
