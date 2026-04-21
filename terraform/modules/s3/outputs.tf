output "flink_checkpoint_bucket_arn"  { value = aws_s3_bucket.buckets["checkpoints"].arn }
output "flink_checkpoint_bucket_name" { value = aws_s3_bucket.buckets["checkpoints"].id }
output "log_archive_bucket_arn"       { value = aws_s3_bucket.buckets["archive"].arn }
output "log_archive_bucket_name"      { value = aws_s3_bucket.buckets["archive"].id }
