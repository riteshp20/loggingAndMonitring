locals {
  key_policy_principals = jsonencode([
    "arn:aws:iam::${var.account_id}:root"
  ])
}

# One CMK per service so key policies and audit trails are isolated
resource "aws_kms_key" "keys" {
  for_each = toset(["s3", "msk", "opensearch", "elasticache", "eks"])

  description             = "${var.name} — ${each.key} encryption key"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RootFullAccess"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid    = "AllowCloudWatchLogs"
        Effect = "Allow"
        Principal = {
          Service = "logs.${var.aws_region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*",
          "kms:GenerateDataKey*", "kms:DescribeKey"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "keys" {
  for_each = aws_kms_key.keys

  name          = "alias/${var.name}-${each.key}"
  target_key_id = each.value.key_id
}
