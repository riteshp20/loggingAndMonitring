output "eks_cluster_name" {
  description = "EKS cluster name — use with: aws eks update-kubeconfig --name <value>"
  value       = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "msk_bootstrap_brokers_tls" {
  description = "MSK TLS bootstrap servers for Kafka clients"
  value       = module.msk.bootstrap_brokers_tls
  sensitive   = true
}

output "opensearch_endpoint" {
  description = "OpenSearch domain VPC endpoint"
  value       = module.opensearch.endpoint
}

output "redis_primary_endpoint" {
  description = "ElastiCache Redis primary endpoint"
  value       = module.elasticache.primary_endpoint
}

output "flink_checkpoint_bucket" {
  value = module.s3.flink_checkpoint_bucket_name
}

output "log_archive_bucket" {
  value = module.s3.log_archive_bucket_name
}

output "irsa_role_arns" {
  description = "IRSA role ARNs to set as Helm values (api.serviceAccount.annotations)"
  value       = module.iam.irsa_role_arns
}

output "helm_values" {
  description = "Paste these into values-prod.yaml or pass as --set flags"
  value = {
    kafka_bootstrap    = module.msk.bootstrap_brokers_tls
    opensearch_url     = "https://${module.opensearch.endpoint}"
    redis_url          = "rediss://${module.elasticache.primary_endpoint}:6379"
    checkpoint_bucket  = "s3://${module.s3.flink_checkpoint_bucket_name}/checkpoints"
    irsa_flink         = module.iam.irsa_role_arns["flink"]
    irsa_anomaly       = module.iam.irsa_role_arns["anomaly-detector"]
    irsa_reporter      = module.iam.irsa_role_arns["incident-reporter"]
    irsa_api           = module.iam.irsa_role_arns["api"]
    irsa_fluent_bit    = module.iam.irsa_role_arns["fluent-bit"]
  }
  sensitive = true
}
