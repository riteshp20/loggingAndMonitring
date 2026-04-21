output "irsa_role_arns" {
  description = "Map of workload name → IRSA role ARN for Helm serviceAccount annotations"
  value = {
    for k, r in aws_iam_role.irsa : k => r.arn
  }
}
