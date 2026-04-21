output "cluster_name"             { value = aws_eks_cluster.this.name }
output "cluster_endpoint"         { value = aws_eks_cluster.this.endpoint }
output "cluster_ca_certificate"   { value = aws_eks_cluster.this.certificate_authority[0].data }
output "cluster_auth_token" {
  value     = data.aws_eks_cluster_auth.this.token
  sensitive = true
}
output "oidc_provider_arn"        { value = aws_iam_openid_connect_provider.eks.arn }
output "oidc_provider_url"        { value = aws_iam_openid_connect_provider.eks.url }
output "node_security_group_id"   { value = aws_security_group.node.id }
output "node_role_arn"            { value = aws_iam_role.node.arn }

data "aws_eks_cluster_auth" "this" {
  name = aws_eks_cluster.this.name
}
