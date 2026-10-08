"""Collect IAM roles and policies (spec 8 §3). IAM is global, so the region filters nothing.

Users and groups are reduced to names and memberships: the snapshot must never hold credentials.
"""

from __future__ import annotations

from typing import Any

from easymigrate.core.plugin import CollectContext
from easymigrate.providers.aws.source import AwsConnection, is_access_denied

IAM_SNAPSHOT_FILES = ("iam.authorization", "iam.aws_managed_policies")
_FILTER = ["User", "Group", "Role", "LocalManagedPolicy"]


def _without_trust_conditions(role: dict[str, Any]) -> dict[str, Any]:
    """Keep only who may assume the role. Trust conditions can hold sts:ExternalId values,
    shared secrets that have no place in a snapshot (review, 2026-10-08)."""
    trust = role.get("AssumeRolePolicyDocument")
    if not isinstance(trust, dict):
        return role
    statements = trust.get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    reduced = {
        "Statement": [
            {k: v for k, v in s.items() if k in {"Effect", "Principal", "Action"}}
            for s in statements
        ]
    }
    return {**role, "AssumeRolePolicyDocument": reduced}


def collect(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any]:
    iam = connection.client("iam")
    files: dict[str, Any] = {
        "iam.authorization": {"Roles": [], "Users": [], "Groups": [], "Policies": []},
        "iam.aws_managed_policies": {},
    }
    roles: list[dict[str, Any]] = []
    users: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    policies: list[dict[str, Any]] = []
    try:
        for page in iam.get_paginator("get_account_authorization_details").paginate(Filter=_FILTER):
            roles += page.get("RoleDetailList", [])
            users += page.get("UserDetailList", [])
            groups += page.get("GroupDetailList", [])
            policies += page.get("Policies", [])
    except Exception as exc:
        if is_access_denied(exc):
            ctx.access_denied(
                "GetAccountAuthorizationDetails", "iam:GetAccountAuthorizationDetails"
            )
            return files
        raise

    files["iam.authorization"] = {
        "Roles": sorted((_without_trust_conditions(r) for r in roles), key=lambda r: r["Arn"]),
        "Users": sorted(
            (
                {"UserName": u["UserName"], "Groups": sorted(u.get("GroupList") or [])}
                for u in users
            ),
            key=lambda u: u["UserName"],
        ),
        "Groups": sorted(g["GroupName"] for g in groups),
        "Policies": sorted(
            (
                {
                    "Arn": p["Arn"],
                    "PolicyName": p["PolicyName"],
                    "Document": next(
                        (
                            v.get("Document")
                            for v in p.get("PolicyVersionList") or []
                            if v.get("IsDefaultVersion")
                        ),
                        None,
                    ),
                }
                for p in policies
            ),
            key=lambda p: p["Arn"],
        ),
    }

    aws_managed = sorted(
        {
            a["PolicyArn"]
            for role in roles
            for a in role.get("AttachedManagedPolicies") or []
            if a["PolicyArn"].startswith("arn:aws:iam::aws:policy/")
        }
    )
    for arn in aws_managed:
        try:
            policy = iam.get_policy(PolicyArn=arn)["Policy"]
            document = iam.get_policy_version(PolicyArn=arn, VersionId=policy["DefaultVersionId"])[
                "PolicyVersion"
            ]["Document"]
        except Exception as exc:
            if is_access_denied(exc):
                ctx.access_denied("GetPolicyVersion", "iam:GetPolicyVersion")
                break
            continue  # a policy deleted since, or unreadable: the role still lists its name
        files["iam.aws_managed_policies"][arn] = document
    return files
