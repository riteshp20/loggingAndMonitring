locals {
  name   = "${var.project}-${var.environment}"
  region = var.aws_region
}

# ── KMS customer-managed keys ─────────────────────────────────────────────────
module "kms" {
  source      = "./modules/kms"
  name        = local.name
  aws_region  = var.aws_region
  account_id  = data.aws_caller_identity.current.account_id
}

# ── VPC ────────────────────────────────────────────────────────────────────────
module "vpc" {
  source               = "./modules/vpc"
  name                 = local.name
  vpc_cidr             = var.vpc_cidr
  availability_zones   = var.availability_zones
  private_subnet_cidrs = var.private_subnet_cidrs
  public_subnet_cidrs  = var.public_subnet_cidrs
}

# ── EKS ────────────────────────────────────────────────────────────────────────
module "eks" {
  source             = "./modules/eks"
  name               = local.name
  kubernetes_version = var.eks_kubernetes_version
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  kms_key_arn        = module.kms.eks_key_arn
  node_groups        = var.eks_node_groups

  # IRSA role ARNs passed as annotations to service accounts in Helm
  irsa_roles = module.iam.irsa_role_arns
}

# ── MSK ────────────────────────────────────────────────────────────────────────
module "msk" {
  source             = "./modules/msk"
  name               = local.name
  kafka_version      = var.msk_kafka_version
  instance_type      = var.msk_broker_instance_type
  broker_count       = var.msk_broker_count
  storage_gb         = var.msk_broker_storage_gb
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  eks_sg_id          = module.eks.node_security_group_id
  kms_key_arn        = module.kms.msk_key_arn
}

# ── OpenSearch ─────────────────────────────────────────────────────────────────
module "opensearch" {
  source             = "./modules/opensearch"
  name               = local.name
  engine_version     = var.opensearch_engine_version
  instance_type      = var.opensearch_instance_type
  instance_count     = var.opensearch_instance_count
  volume_size_gb     = var.opensearch_volume_size_gb
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  eks_sg_id          = module.eks.node_security_group_id
  kms_key_arn        = module.kms.opensearch_key_arn
  account_id         = data.aws_caller_identity.current.account_id
  aws_region         = var.aws_region
}

# ── ElastiCache Redis ─────────────────────────────────────────────────────────
module "elasticache" {
  source             = "./modules/elasticache"
  name               = local.name
  node_type          = var.redis_node_type
  engine_version     = var.redis_engine_version
  num_cache_nodes    = var.redis_num_cache_nodes
  availability_zones = var.availability_zones
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  eks_sg_id          = module.eks.node_security_group_id
  kms_key_arn        = module.kms.elasticache_key_arn
}

# ── S3 ─────────────────────────────────────────────────────────────────────────
module "s3" {
  source                          = "./modules/s3"
  name                            = local.name
  kms_key_arn                     = module.kms.s3_key_arn
  log_archive_retention_days      = var.log_archive_retention_days
  log_archive_glacier_days        = var.log_archive_glacier_days
  flink_checkpoint_retention_days = var.flink_checkpoint_retention_days
  eks_node_role_arn               = module.eks.node_role_arn
}

# ── IAM / IRSA ────────────────────────────────────────────────────────────────
module "iam" {
  source                  = "./modules/iam"
  name                    = local.name
  account_id              = data.aws_caller_identity.current.account_id
  oidc_provider_arn       = module.eks.oidc_provider_arn
  oidc_provider_url       = module.eks.oidc_provider_url
  flink_checkpoint_bucket = module.s3.flink_checkpoint_bucket_arn
  log_archive_bucket      = module.s3.log_archive_bucket_arn
  msk_cluster_arn         = module.msk.cluster_arn
  opensearch_domain_arn   = module.opensearch.domain_arn
  kms_key_arns = [
    module.kms.s3_key_arn,
    module.kms.msk_key_arn,
    module.kms.opensearch_key_arn,
    module.kms.elasticache_key_arn,
  ]
}

data "aws_caller_identity" "current" {}
