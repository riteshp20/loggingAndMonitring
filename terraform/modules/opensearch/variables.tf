variable "name"                       { type = string }
variable "engine_version"             { type = string }
variable "instance_type"              { type = string }
variable "instance_count"             { type = number }
variable "volume_size_gb"             { type = number }
variable "vpc_id"                     { type = string }
variable "private_subnet_ids"         { type = list(string) }
variable "eks_sg_id"                  { type = string }
variable "kms_key_arn"                { type = string }
variable "account_id"                 { type = string }
variable "aws_region"                 { type = string }
variable "opensearch_master_role_arn" { type = string; default = "" }
