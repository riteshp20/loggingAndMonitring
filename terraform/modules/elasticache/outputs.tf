output "primary_endpoint"      { value = aws_elasticache_replication_group.this.primary_endpoint_address }
output "reader_endpoint"       { value = aws_elasticache_replication_group.this.reader_endpoint_address }
output "auth_secret_arn"       { value = aws_secretsmanager_secret.redis_auth.arn }
output "security_group_id"     { value = aws_security_group.redis.id }
