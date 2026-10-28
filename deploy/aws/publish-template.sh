#!/usr/bin/env bash
# Publish the visitor role template to a public-read S3 location (CloudFormation's
# quick-create link only accepts an S3 URL). Run with operator-account credentials.
#   deploy/aws/publish-template.sh <bucket-name>
set -euo pipefail
bucket="${1:?usage: publish-template.sh <bucket-name>}"
region=us-east-1
key=deploy/easymigrate-readonly-role.json
here="$(cd "$(dirname "$0")" && pwd)"

if ! aws s3api head-bucket --bucket "$bucket" 2>/dev/null; then
  aws s3api create-bucket --bucket "$bucket" --region "$region" >/dev/null
fi
# Public reads through the bucket policy only; ACL-based public access stays blocked.
aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false
aws s3api put-bucket-policy --bucket "$bucket" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{\"Sid\": \"PublicTemplateRead\", \"Effect\": \"Allow\", \"Principal\": \"*\",
    \"Action\": \"s3:GetObject\", \"Resource\": \"arn:aws:s3:::$bucket/deploy/*\"}]
}"
aws s3 cp "$here/easymigrate-readonly-role.json" "s3://$bucket/$key" \
  --content-type application/json --cache-control "max-age=300" >/dev/null
echo "https://$bucket.s3.amazonaws.com/$key"
