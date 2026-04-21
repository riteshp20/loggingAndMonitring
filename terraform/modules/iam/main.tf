# ── IRSA helper: build a trust policy for a Kubernetes service account ────────

locals {
  oidc_url_stripped = replace(var.oidc_provider_url, "https://", "")

  # Map of workload → Kubernetes namespace/serviceaccount
  workloads = {
    flink             = { namespace = "log-monitor", sa = "flink" }
    anomaly-detector  = { namespace = "log-monitor", sa = "anomaly-detector" }
    incident-reporter = { namespace = "log-monitor", sa = "incident-reporter" }
    api               = { namespace = "log-monitor", sa = "api" }
    fluent-bit        = { namespace = "log-monitor", sa = "fluent-bit" }
  }
}

# ── IRSA trust policy (reusable) ─────────────────────────────────────────────

data "aws_iam_policy_document" "irsa_trust" {
  for_each = local.workloads

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_url_stripped}:sub"
      values   = ["system:serviceaccount:${each.value.namespace}:${each.value.sa}"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_url_stripped}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "irsa" {
  for_each = local.workloads

  name               = "${var.name}-irsa-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust[each.key].json

  tags = { Workload = each.key }
}

# ── KMS decrypt (shared by all workloads) ────────────────────────────────────

resource "aws_iam_policy" "kms_decrypt" {
  name = "${var.name}-kms-decrypt"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
      Resource = var.kms_key_arns
    }]
  })
}

resource "aws_iam_role_policy_attachment" "kms_all" {
  for_each = aws_iam_role.irsa

  role       = each.value.name
  policy_arn = aws_iam_policy.kms_decrypt.arn
}

# ── Flink: S3 read/write for checkpoints + log archive write ─────────────────

resource "aws_iam_policy" "flink" {
  name = "${var.name}-flink"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CheckpointReadWrite"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
                  "s3:GetBucketLocation", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = [var.flink_checkpoint_bucket, "${var.flink_checkpoint_bucket}/*"]
      },
      {
        Sid    = "LogArchiveWrite"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "${var.log_archive_bucket}/*"
      },
      {
        Sid    = "MSKDescribe"
        Effect = "Allow"
        Action = ["kafka:DescribeCluster", "kafka:GetBootstrapBrokers",
                  "kafka:ListClusters", "kafka-cluster:Connect",
                  "kafka-cluster:DescribeTopic", "kafka-cluster:ReadData",
                  "kafka-cluster:WriteData", "kafka-cluster:AlterGroup",
                  "kafka-cluster:DescribeGroup"]
        Resource = [var.msk_cluster_arn, "${var.msk_cluster_arn}/*"]
      },
      {
        Sid      = "MSKGetCredentials"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:*:${var.account_id}:secret:${var.name}/msk/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "flink" {
  role       = aws_iam_role.irsa["flink"].name
  policy_arn = aws_iam_policy.flink.arn
}

# ── Anomaly Detector: MSK consume + Secrets Manager ─────────────────────────

resource "aws_iam_policy" "anomaly_detector" {
  name = "${var.name}-anomaly-detector"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "MSKConsume"
        Effect = "Allow"
        Action = ["kafka-cluster:Connect", "kafka-cluster:DescribeTopic",
                  "kafka-cluster:ReadData", "kafka-cluster:WriteData",
                  "kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup",
                  "kafka:DescribeCluster", "kafka:GetBootstrapBrokers"]
        Resource = [var.msk_cluster_arn, "${var.msk_cluster_arn}/*"]
      },
      {
        Sid    = "MSKCredentials"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:*:${var.account_id}:secret:${var.name}/msk/*"
      },
      {
        Sid    = "RedisCredentials"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:*:${var.account_id}:secret:${var.name}/redis/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "anomaly_detector" {
  role       = aws_iam_role.irsa["anomaly-detector"].name
  policy_arn = aws_iam_policy.anomaly_detector.arn
}

# ── Incident Reporter: MSK consume + OpenSearch write + Secrets Manager ──────

resource "aws_iam_policy" "incident_reporter" {
  name = "${var.name}-incident-reporter"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "MSKConsume"
        Effect = "Allow"
        Action = ["kafka-cluster:Connect", "kafka-cluster:DescribeTopic",
                  "kafka-cluster:ReadData", "kafka-cluster:AlterGroup",
                  "kafka-cluster:DescribeGroup", "kafka:DescribeCluster",
                  "kafka:GetBootstrapBrokers"]
        Resource = [var.msk_cluster_arn, "${var.msk_cluster_arn}/*"]
      },
      {
        Sid    = "OpenSearchWrite"
        Effect = "Allow"
        Action = ["es:ESHttpPost", "es:ESHttpPut", "es:ESHttpGet",
                  "es:ESHttpHead", "es:DescribeElasticsearchDomain"]
        Resource = ["${var.opensearch_domain_arn}", "${var.opensearch_domain_arn}/*"]
      },
      {
        Sid    = "SecretsRead"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:*:${var.account_id}:secret:${var.name}/msk/*",
          "arn:aws:secretsmanager:*:${var.account_id}:secret:${var.name}/redis/*",
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "incident_reporter" {
  role       = aws_iam_role.irsa["incident-reporter"].name
  policy_arn = aws_iam_policy.incident_reporter.arn
}

# ── Dashboard API: OpenSearch read + MSK SSE consumer + Secrets Manager ──────

resource "aws_iam_policy" "api" {
  name = "${var.name}-api"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OpenSearchRead"
        Effect = "Allow"
        Action = ["es:ESHttpGet", "es:ESHttpPost", "es:ESHttpHead"]
        Resource = ["${var.opensearch_domain_arn}", "${var.opensearch_domain_arn}/*"]
      },
      {
        Sid    = "MSKSSEConsume"
        Effect = "Allow"
        Action = ["kafka-cluster:Connect", "kafka-cluster:DescribeTopic",
                  "kafka-cluster:ReadData", "kafka-cluster:AlterGroup",
                  "kafka-cluster:DescribeGroup", "kafka:GetBootstrapBrokers"]
        Resource = [var.msk_cluster_arn, "${var.msk_cluster_arn}/*"]
      },
      {
        Sid    = "SecretsRead"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:*:${var.account_id}:secret:${var.name}/redis/*",
          "arn:aws:secretsmanager:*:${var.account_id}:secret:${var.name}/msk/*",
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "api" {
  role       = aws_iam_role.irsa["api"].name
  policy_arn = aws_iam_policy.api.arn
}

# ── Fluent Bit: MSK produce + log archive write ───────────────────────────────

resource "aws_iam_policy" "fluent_bit" {
  name = "${var.name}-fluent-bit"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "MSKProduce"
        Effect = "Allow"
        Action = ["kafka-cluster:Connect", "kafka-cluster:DescribeTopic",
                  "kafka-cluster:WriteData", "kafka:GetBootstrapBrokers"]
        Resource = [var.msk_cluster_arn, "${var.msk_cluster_arn}/*"]
      },
      {
        Sid    = "LogArchiveWrite"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListBucket"]
        Resource = [var.log_archive_bucket, "${var.log_archive_bucket}/*"]
      },
      {
        Sid    = "MSKCredentials"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:*:${var.account_id}:secret:${var.name}/msk/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "fluent_bit" {
  role       = aws_iam_role.irsa["fluent-bit"].name
  policy_arn = aws_iam_policy.fluent_bit.arn
}
