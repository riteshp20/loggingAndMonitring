output "s3_key_arn"          { value = aws_kms_key.keys["s3"].arn }
output "msk_key_arn"         { value = aws_kms_key.keys["msk"].arn }
output "opensearch_key_arn"  { value = aws_kms_key.keys["opensearch"].arn }
output "elasticache_key_arn" { value = aws_kms_key.keys["elasticache"].arn }
output "eks_key_arn"         { value = aws_kms_key.keys["eks"].arn }
