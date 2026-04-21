# ── Security group ────────────────────────────────────────────────────────────

resource "aws_security_group" "msk" {
  name        = "${var.name}-msk"
  description = "MSK Kafka cluster — allow only EKS nodes"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Kafka TLS from EKS nodes"
    from_port       = 9094
    to_port         = 9094
    protocol        = "tcp"
    security_groups = [var.eks_sg_id]
  }

  ingress {
    description     = "Kafka SASL/SCRAM from EKS nodes"
    from_port       = 9096
    to_port         = 9096
    protocol        = "tcp"
    security_groups = [var.eks_sg_id]
  }

  ingress {
    description     = "ZooKeeper (disabled in KRaft mode — kept for legacy clients)"
    from_port       = 2181
    to_port         = 2181
    protocol        = "tcp"
    security_groups = [var.eks_sg_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-msk-sg" }
}

# ── Subnet group ──────────────────────────────────────────────────────────────

resource "aws_msk_cluster" "this" {
  cluster_name           = var.name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = var.broker_count

  broker_node_group_info {
    instance_type   = var.instance_type
    client_subnets  = var.private_subnet_ids
    security_groups = [aws_security_group.msk.id]

    storage_info {
      ebs_storage_info {
        volume_size = var.storage_gb

        # Auto-scaling storage — avoids manual intervention during log spikes
        provisioned_throughput {
          enabled           = true
          volume_throughput = 250
        }
      }
    }
  }

  # TLS + SASL/SCRAM authentication
  client_authentication {
    sasl {
      scram = true
    }
    tls {}
    unauthenticated = false
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = var.kms_key_arn
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.this.arn
    revision = aws_msk_configuration.this.latest_revision
  }

  open_monitoring {
    prometheus {
      jmx_exporter  { enabled_in_broker = true }
      node_exporter { enabled_in_broker = true }
    }
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk.name
      }
    }
  }

  tags = { Name = var.name }
}

resource "aws_msk_configuration" "this" {
  name = "${var.name}-config"

  kafka_versions = [var.kafka_version]

  server_properties = <<-EOT
    auto.create.topics.enable=false
    default.replication.factor=3
    min.insync.replicas=2
    num.partitions=12
    log.retention.hours=168
    log.segment.bytes=536870912
    message.max.bytes=1048576
    unclean.leader.election.enable=false
    group.initial.rebalance.delay.ms=3000
    offsets.topic.replication.factor=3
    transaction.state.log.replication.factor=3
    transaction.state.log.min.isr=2
    # Enable log compaction for internal topics
    log.cleanup.policy=delete
  EOT
}

resource "aws_cloudwatch_log_group" "msk" {
  name              = "/aws/msk/${var.name}/broker"
  retention_in_days = 30
}

# ── MSK SCRAM credentials stored in Secrets Manager ──────────────────────────
# Rotate with: aws secretsmanager rotate-secret --secret-id <arn>

resource "aws_secretsmanager_secret" "msk_credentials" {
  name       = "${var.name}/msk/kafka-credentials"
  kms_key_id = var.kms_key_arn

  recovery_window_in_days = 7
}

resource "aws_msk_scram_secret_association" "this" {
  cluster_arn     = aws_msk_cluster.this.arn
  secret_arn_list = [aws_secretsmanager_secret.msk_credentials.arn]
}
