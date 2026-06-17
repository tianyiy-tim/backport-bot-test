#!/usr/bin/env bash
#
# One-time setup for the backport bot's AWS access.
#
# Creates (idempotently):
#   1. the GitHub OIDC identity provider in IAM (if missing),
#   2. an IAM role the workflow assumes at runtime, scoped to THIS repo only,
#   3. a Bedrock InvokeModel permission on that role,
# then registers the role ARN as the `AWS_BACKPORT_ROLE_ARN` repo variable.
#
# PREREQUISITE — your shell must be authenticated to AWS first. Either:
#   aws sso login                        # if you use IAM Identity Center, or
#   export AWS_ACCESS_KEY_ID=...          # paste the 3 values from the AWS console
#   export AWS_SECRET_ACCESS_KEY=...      # ("Command line or programmatic access")
#   export AWS_SESSION_TOKEN=...
#
# Verify with:  aws sts get-caller-identity   (must print your account, not an error)
#
# Then run:     bash scripts/setup_oidc_role.sh

set -euo pipefail

REPO="tianyiy-tim/backport-bot-test"
ROLE_NAME="BackportBotBedrockRole"
OIDC_HOST="token.actions.githubusercontent.com"

# 0. Require working AWS credentials.
if ! ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null); then
  echo "ERROR: no AWS credentials found in this shell." >&2
  echo "Authenticate first (e.g. 'aws sso login', or export temporary creds from" >&2
  echo "the AWS console), confirm with 'aws sts get-caller-identity', then re-run." >&2
  exit 1
fi
echo "AWS account: ${ACCOUNT_ID}"

PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_HOST}"

# 1. GitHub OIDC provider (create only if it doesn't exist).
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "${PROVIDER_ARN}" >/dev/null 2>&1; then
  echo "OIDC provider already exists."
else
  echo "Creating GitHub OIDC provider..."
  aws iam create-open-id-connect-provider \
    --url "https://${OIDC_HOST}" \
    --client-id-list sts.amazonaws.com \
    --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
fi

# 2. Trust policy: only GitHub Actions runs from THIS repo may assume the role.
TRUST=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "${PROVIDER_ARN}" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": { "${OIDC_HOST}:aud": "sts.amazonaws.com" },
      "StringLike":   { "${OIDC_HOST}:sub": "repo:${REPO}:*" }
    }
  }]
}
EOF
)

# 3. Create the role, or update its trust policy if it already exists.
if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  echo "Role ${ROLE_NAME} exists; updating trust policy..."
  aws iam update-assume-role-policy --role-name "${ROLE_NAME}" --policy-document "${TRUST}"
else
  echo "Creating role ${ROLE_NAME}..."
  aws iam create-role --role-name "${ROLE_NAME}" --assume-role-policy-document "${TRUST}"
fi

# 4. Bedrock invoke permission (Resource "*" for the POC; scope later if desired).
aws iam put-role-policy --role-name "${ROLE_NAME}" --policy-name BedrockInvoke \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"}]}'

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# 5. Point the workflow at the role.
gh variable set AWS_BACKPORT_ROLE_ARN --repo "${REPO}" --body "${ROLE_ARN}"

echo
echo "Done."
echo "  Role ARN: ${ROLE_ARN}"
echo "  AWS_BACKPORT_ROLE_ARN variable set on ${REPO}."
echo "Tell the assistant to re-run the smoke test."
