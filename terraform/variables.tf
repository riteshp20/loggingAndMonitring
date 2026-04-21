variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name — used in resource names and tags"
  type        = string
  default     = "log-monitor"
}

variable "environment" {
  description = "Deployment environment: prod | staging | dev"
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["prod", "staging", "dev"], var.environment)
    error_message = "environment must be prod, staging, or dev."
  }
}

# ── VPC ────────────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "AZs to deploy into — must be at least 3 for MSK and OpenSearch HA"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
}

variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]
}

# ── EKS ────────────────────────────────────────────────────────────────────────

variable "eks_kubernetes_version" {
  description = "Kubernetes version for EKS"
  type        = string
  default     = "1.30"
}

variable "eks_node_groups" {
  description = "EKS managed node group definitions"
  type = map(object({
    instance_types = list(string)
    min_size       = number
    max_size       = number
    desired_size   = number
    disk_size_gb   = number
    labels         = map(string)
    taints = list(object({
      key    = string
      value  = string
      effect = string
    }))
  }))
  default = {
    general = {
      instance_types = ["m6i.xlarge"]
      min_size       = 2
      max_size       = 10
      desired_size   = 3
      disk_size_gb   = 100
      labels         = { role = "general" }
      taints         = []
    }
    flink = {
      instance_types = ["r6i.2xlarge"]
      min_size       = 2
      max_size       = 8
      desired_size   = 2
      disk_size_gb   = 200
      labels         = { role = "flink" }
      taints = [{
        key    = "dedicated"
        value  = "flink"
        effect = "NO_SCHEDULE"
      }]
    }
  }
}

# ── MSK ────────────────────────────────────────────────────────────────────────

variable "msk_kafka_version" {
  type    = string
  default = "3.6.0"
}

variable "msk_broker_instance_type" {
  type    = string
  default = "kafka.m5.xlarge"
}

variable "msk_broker_storage_gb" {
  type    = number
  default = 1000
}

variable "msk_broker_count" {
  description = "Number of brokers — must be a multiple of the AZ count"
  type        = number
  default     = 3
}

# ── OpenSearch ─────────────────────────────────────────────────────────────────

variable "opensearch_engine_version" {
  type    = string
  default = "OpenSearch_2.13"
}

variable "opensearch_instance_type" {
  type    = string
  default = "m6g.large.search"
}

variable "opensearch_instance_count" {
  type    = number
  default = 3
}

variable "opensearch_volume_size_gb" {
  type    = number
  default = 500
}

# ── ElastiCache Redis ─────────────────────────────────────────────────────────

variable "redis_node_type" {
  type    = string
  default = "cache.r7g.large"
}

variable "redis_engine_version" {
  type    = string
  default = "7.1"
}

variable "redis_num_cache_nodes" {
  description = "Number of nodes (1 for single-node, >1 for multi-AZ replica group)"
  type        = number
  default     = 2
}

# ── S3 ─────────────────────────────────────────────────────────────────────────

variable "log_archive_retention_days" {
  description = "Days before raw logs transition to Glacier Instant Retrieval"
  type        = number
  default     = 30
}

variable "log_archive_glacier_days" {
  description = "Days before raw logs move to Glacier Deep Archive"
  type        = number
  default     = 365
}

variable "flink_checkpoint_retention_days" {
  description = "Days to retain old Flink checkpoints"
  type        = number
  default     = 7
}
