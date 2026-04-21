output "domain_arn"         { value = aws_opensearch_domain.this.arn }
output "domain_id"          { value = aws_opensearch_domain.this.domain_id }
output "endpoint"           { value = aws_opensearch_domain.this.endpoint }
output "security_group_id"  { value = aws_security_group.opensearch.id }
