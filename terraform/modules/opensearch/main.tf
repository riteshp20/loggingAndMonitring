# ── Security group ────────────────────────────────────────────────────────────

resource "aws_security_group" "opensearch" {
  name        = "${var.name}-opensearch"
  description = "OpenSearch domain — allow only EKS nodes"
  vpc_id      = var.vpc_id

  ingress {
    description     = "HTTPS from EKS nodes"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [var.eks_sg_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-opensearch-sg" }
}

# ── Service-linked role (required once per account) ───────────────────────────

resource "aws_iam_service_linked_role" "opensearch" {
  aws_service_name = "opensearchservice.amazonaws.com"

  # Ignore if it already exists in the account
  lifecycle {
    ignore_errors = true
  }
}

# ── Domain ────────────────────────────────────────────────────────────────────

resource "aws_opensearch_domain" "this" {
  domain_name    = var.name
  engine_version = var.engine_version

  cluster_config {
    instance_type          = var.instance_type
    instance_count         = var.instance_count
    zone_awareness_enabled = var.instance_count > 1

    dynamic "zone_awareness_config" {
      for_each = var.instance_count > 1 ? [1] : []
      content {
        availability_zone_count = min(var.instance_count, 3)
      }
    }

    # Dedicated master nodes for stable cluster management
    dedicated_master_enabled = true
    dedicated_master_type    = "m6g.large.search"
    dedicated_master_count   = 3
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = var.volume_size_gb
    throughput  = 250
    iops        = 3000
  }

  vpc_options {
    subnet_ids         = slice(var.private_subnet_ids, 0, min(var.instance_count, length(var.private_subnet_ids)))
    security_group_ids = [aws_security_group.opensearch.id]
  }

  encrypt_at_rest {
    enabled    = true
    kms_key_id = var.kms_key_arn
  }

  node_to_node_encryption {
    enabled = true
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  advanced_security_options {
    enabled                        = true
    internal_user_database_enabled = false   # use IAM auth only

    master_user_options {
      master_user_arn = var.opensearch_master_role_arn != "" ? var.opensearch_master_role_arn : "arn:aws:iam::${var.account_id}:root"
    }
  }

  log_publishing_options {
    log_type                 = "INDEX_SLOW_LOGS"
    cloudwatch_log_group_arn = aws_cloudwatch_log_group.opensearch["INDEX_SLOW_LOGS"].arn
  }

  log_publishing_options {
    log_type                 = "SEARCH_SLOW_LOGS"
    cloudwatch_log_group_arn = aws_cloudwatch_log_group.opensearch["SEARCH_SLOW_LOGS"].arn
  }

  log_publishing_options {
    log_type                 = "ES_APPLICATION_LOGS"
    cloudwatch_log_group_arn = aws_cloudwatch_log_group.opensearch["ES_APPLICATION_LOGS"].arn
  }

  auto_tune_options {
    desired_state       = "ENABLED"
    rollback_on_disable = "NO_ROLLBACK"
  }

  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "*" }
      Action    = "es:*"
      Resource  = "arn:aws:es:${var.aws_region}:${var.account_id}:domain/${var.name}/*"
      Condition = {
        StringEquals = {
          "aws:PrincipalVpc" = var.vpc_id
        }
      }
    }]
  })

  depends_on = [aws_iam_service_linked_role.opensearch]

  tags = { Name = var.name }
}

resource "aws_cloudwatch_log_group" "opensearch" {
  for_each = toset(["INDEX_SLOW_LOGS", "SEARCH_SLOW_LOGS", "ES_APPLICATION_LOGS"])

  name              = "/aws/opensearch/${var.name}/${lower(each.key)}"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_resource_policy" "opensearch" {
  policy_name = "${var.name}-opensearch-logs"

  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "es.amazonaws.com" }
      Action    = ["logs:PutLogEvents", "logs:CreateLogStream"]
      Resource  = "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/opensearch/${var.name}/*"
    }]
  })
}
