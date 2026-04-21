# GitHub Actions secrets reference

Configure these at **Settings → Secrets and variables → Actions**.

Secrets marked **Environment** must be set on the named GitHub Environment
(Settings → Environments), not at the repo level — this enforces the approval
gate before they are injected.

## Repository secrets (all workflows)

| Secret | Required | Description |
|--------|----------|-------------|
| `AWS_ACCOUNT_ID` | ✅ | 12-digit AWS account ID used to construct ECR registry URL |
| `AWS_DEPLOY_ROLE_ARN` | ✅ | OIDC role for staging deployments and ECR push (see below) |
| `ANTHROPIC_API_KEY_TEST` | optional | Low-quota key for integration tests; can be empty to skip AI tests |
| `CODECOV_TOKEN` | optional | Coverage upload token from codecov.io |
| `SLACK_DEPLOY_WEBHOOK` | optional | Incoming webhook URL for deploy notifications |

## Environment: `staging`

| Secret | Required | Description |
|--------|----------|-------------|
| `ANTHROPIC_API_KEY` | ✅ | Full Anthropic API key |
| `JWT_SECRET_STAGING` | ✅ | ≥32-char random string for staging JWT signing |
| `SLACK_BOT_TOKEN` | optional | Slack Bot token for alert routing |
| `PAGERDUTY_ROUTING_KEY_STAGING` | optional | PagerDuty Events v2 key |

## Environment: `production`

> ⚠️ This environment requires a **required reviewer** — configure at
> Settings → Environments → production → Required reviewers.

| Secret | Required | Description |
|--------|----------|-------------|
| `AWS_PROD_DEPLOY_ROLE_ARN` | ✅ | Separate OIDC role scoped only to the prod EKS cluster |
| `ANTHROPIC_API_KEY` | ✅ | Full Anthropic API key |
| `JWT_SECRET_PROD` | ✅ | ≥32-char random string for prod JWT signing |
| `SLACK_BOT_TOKEN` | optional | Slack Bot token |
| `PAGERDUTY_ROUTING_KEY_PROD` | optional | PagerDuty Events v2 key |

## AWS OIDC setup

Instead of long-lived `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, the
workflows use OIDC to assume IAM roles. One-time setup:

```bash
# 1. Create the OIDC identity provider in your AWS account
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# 2. Create the staging deploy role (trust policy below)
aws iam create-role \
  --role-name log-monitor-github-staging \
  --assume-role-policy-document file://github-oidc-trust.json

# 3. Attach minimum required policies
aws iam attach-role-policy \
  --role-name log-monitor-github-staging \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser

# Trust policy (github-oidc-trust.json):
# {
#   "Version": "2012-10-17",
#   "Statement": [{
#     "Effect": "Allow",
#     "Principal": { "Federated": "arn:aws:iam::ACCOUNT:oidc-provider/token.actions.githubusercontent.com" },
#     "Action": "sts:AssumeRoleWithWebIdentity",
#     "Condition": {
#       "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
#       "StringLike":   { "token.actions.githubusercontent.com:sub": "repo:YOUR_ORG/YOUR_REPO:*" }
#     }
#   }]
# }
```
