"""The CloudFormation role template visitors deploy, and the Render blueprint."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = json.loads((ROOT / "deploy/aws/easymigrate-readonly-role.json").read_text())
ROLE = TEMPLATE["Resources"]["EasyMigrateReadOnlyRole"]["Properties"]


def test_template_actions_equal_scan_policy():
    policy = json.loads((ROOT / "docs/aws-scan-policy.json").read_text())
    expected = {a for st in policy["Statement"] for a in st["Action"]}
    statements = ROLE["Policies"][0]["PolicyDocument"]["Statement"]
    actual = {a for st in statements for a in st["Action"]}
    assert actual == expected
    assert all(st["Effect"] == "Allow" and st["Resource"] == "*" for st in statements)


def test_template_trust_requires_external_id():
    (statement,) = ROLE["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Principal"] == {"AWS": "arn:aws:iam::538342414628:user/easymigrate-web"}
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Condition"] == {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}


def test_role_name_uses_suffix_and_session_limit():
    assert ROLE["RoleName"] == {"Fn::Sub": "EasyMigrateReadOnly-${RoleSuffix}"}
    assert ROLE["MaxSessionDuration"] == 3600
    params = TEMPLATE["Parameters"]
    assert params["RoleSuffix"]["AllowedPattern"] == "^[a-f0-9]{8}$"
    assert params["ExternalId"]["MinLength"] == 16
    assert TEMPLATE["Outputs"]["RoleArn"]["Value"] == {
        "Fn::GetAtt": ["EasyMigrateReadOnlyRole", "Arn"]
    }


def test_render_yaml_starts_web_factory():
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    (service,) = blueprint["services"]
    assert "easymigrate.web.app:create_web_app --factory" in service["startCommand"]
    # Rate limits read CF-Connecting-IP; uvicorn must not rewrite the client address from the
    # spoofable X-Forwarded-For.
    assert "--proxy-headers" not in service["startCommand"]
    assert service["healthCheckPath"] == "/api/health"
    keys = {env["key"] for env in service["envVars"]}
    assert {
        "DATABASE_URL",
        "EASYMIGRATE_DATA_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "EASYMIGRATE_CFN_TEMPLATE_URL",
    } <= keys


def test_vercel_points_at_api():
    vercel = json.loads((ROOT / "frontend/vercel.json").read_text())
    assert vercel["buildCommand"] == "npm run build:hosted"
    assert vercel["outputDirectory"] == "dist"
    headers = {h["key"]: h["value"] for h in vercel["headers"][0]["headers"]}
    assert (
        "connect-src 'self' https://api.easymigrate.sudhan.tech"
        in headers["Content-Security-Policy"]
    )
