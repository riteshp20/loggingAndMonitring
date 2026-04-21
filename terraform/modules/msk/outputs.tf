output "cluster_arn"            { value = aws_msk_cluster.this.arn }
output "bootstrap_brokers_tls"  { value = aws_msk_cluster.this.bootstrap_brokers_tls  sensitive = true }
output "bootstrap_brokers_sasl" { value = aws_msk_cluster.this.bootstrap_brokers_sasl_scram  sensitive = true }
output "credentials_secret_arn" { value = aws_secretsmanager_secret.msk_credentials.arn }
output "security_group_id"      { value = aws_security_group.msk.id }
