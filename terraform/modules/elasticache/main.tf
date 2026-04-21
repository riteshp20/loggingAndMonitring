# ── Security group ────────────────────────────────────────────────────────────

resource "aws_security_group" "redis" {
  name        = "${var.name}-redis"
  description = "ElastiCache Redis — allow only EKS nodes"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Redis TLS from EKS nodes"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [var.eks_sg_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-redis-sg" }
}

# ── Subnet group ──────────────────────────────────────────────────────────────

resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name}-redis"
  subnet_ids = var.private_subnet_ids
  tags       = { Name = "${var.name}-redis-subnet-group" }
}

# ── Parameter group ───────────────────────────────────────────────────────────

resource "aws_elasticache_parameter_group" "this" {
  name   = "${var.name}-redis"
  family = "redis7"

  # Match the in-memory baseline for the anomaly detector
  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }

  parameter {
    name  = "activedefrag"
    value = "yes"
  }

  parameter {
    name  = "lazyfree-lazy-eviction"
    value = "yes"
  }
}

# ── Replication group (multi-AZ when num_cache_nodes > 1) ────────────────────

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = var.name
  description          = "${var.name} Redis for baseline state and rate limiting"

  node_type            = var.node_type
  engine_version       = var.engine_version
  parameter_group_name = aws_elasticache_parameter_group.this.name
  subnet_group_name    = aws_elasticache_subnet_group.this.name
  security_group_ids   = [aws_security_group.redis.id]
  port                 = 6379

  # Replica configuration
  num_cache_clusters         = var.num_cache_nodes
  automatic_failover_enabled = var.num_cache_nodes > 1
  multi_az_enabled           = var.num_cache_nodes > 1
  preferred_cache_cluster_azs = slice(var.availability_zones, 0, var.num_cache_nodes)

  # Encryption
  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn
  transit_encryption_enabled = true
  auth_token                 = random_password.redis_auth.result

  # Maintenance
  maintenance_window       = "sun:05:00-sun:06:00"
  snapshot_window          = "03:00-05:00"
  snapshot_retention_limit = 7
  auto_minor_version_upgrade = true

  apply_immediately = false

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }

  tags = { Name = var.name }
}

resource "random_password" "redis_auth" {
  length  = 32
  special = false   # ElastiCache auth token only allows alphanumeric + some specials
}

resource "aws_secretsmanager_secret" "redis_auth" {
  name       = "${var.name}/redis/auth-token"
  kms_key_id = var.kms_key_arn

  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id = aws_secretsmanager_secret.redis_auth.id
  secret_string = jsonencode({
    auth_token = random_password.redis_auth.result
    endpoint   = aws_elasticache_replication_group.this.primary_endpoint_address
    port       = 6379
  })
}

resource "aws_cloudwatch_log_group" "redis" {
  name              = "/aws/elasticache/${var.name}/slow-log"
  retention_in_days = 14
}
