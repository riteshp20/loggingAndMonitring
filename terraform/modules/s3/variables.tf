variable "name"                          { type = string }
variable "kms_key_arn"                   { type = string }
variable "eks_node_role_arn"             { type = string }
variable "log_archive_retention_days"    { type = number }
variable "log_archive_glacier_days"      { type = number }
variable "flink_checkpoint_retention_days" { type = number }
