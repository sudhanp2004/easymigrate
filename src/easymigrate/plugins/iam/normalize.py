"""IAM normalizer: snapshot → `iam_role` (spec 8 §4). Faithful capture only."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import Finding, IntentObject, SourceRef
from easymigrate.ir.iam import AttachedPolicy, IamRole, ServiceAccess
from easymigrate.ir.naming import allocate_ids, clean_labels

# Receive is the consuming side of a queue: a subscriber, not a publisher.
_READ_VERBS = (
    "Get",
    "List",
    "Describe",
    "Head",
    "Select",
    "Scan",
    "Query",
    "BatchGet",
    "View",
    "Receive",
)
_LEVEL_ORDER = {"read": 0, "write": 1, "admin": 2}
_SAMPLE = 5


def _document(raw: Any) -> dict[str, Any]:
    """Policy documents arrive as dicts from boto3, or URL-encoded JSON on the raw wire."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(unquote(raw))
        except ValueError:
            return {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def action_level(action: str) -> tuple[str, str]:
    """(service, read|write|admin) for one IAM action pattern (spec 8 D3)."""
    if action == "*":
        return "*", "admin"
    service, _, verb = action.partition(":")
    if verb in {"*", ""}:
        return service, "admin"
    if verb.startswith(_READ_VERBS):
        return service, "read"
    return service, "write"


def normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
    data = snapshot.get("iam.authorization") or {}
    aws_managed: dict[str, Any] = snapshot.get("iam.aws_managed_policies") or {}
    customer = {p["Arn"]: p.get("Document") for p in data.get("Policies") or []}
    roles = data.get("Roles") or []
    users = data.get("Users") or []
    groups = data.get("Groups") or []

    if roles or users or groups:
        ctx.findings.append(
            Finding(
                level="info",
                code="IAM-ACCOUNT-SUMMARY",
                message=(
                    f"The account has {len(users)} IAM user(s), {len(groups)} group(s) and "
                    f"{len(roles)} role(s). None are migrated: people's access is re-granted "
                    "through Google Cloud IAM, and workload identities as service accounts."
                ),
            )
        )

    ids = allocate_ids("iam_role", [(r["RoleName"], r["Arn"]) for r in roles])
    objects: list[IntentObject] = []
    for role in roles:
        # Service-linked roles are AWS's own plumbing; they have no GCP counterpart to plan.
        if (role.get("Path") or "").startswith("/aws-service-role/"):
            continue
        documents: list[dict[str, Any]] = []
        attached: list[AttachedPolicy] = []
        for inline in role.get("RolePolicyList") or []:
            attached.append(AttachedPolicy(name=inline["PolicyName"], kind="inline"))
            documents.append(_document(inline.get("PolicyDocument")))
        for managed in role.get("AttachedManagedPolicies") or []:
            arn = managed["PolicyArn"]
            is_aws = arn.startswith("arn:aws:iam::aws:policy/")
            attached.append(
                AttachedPolicy(name=managed["PolicyName"], kind="aws" if is_aws else "customer")
            )
            documents.append(_document(aws_managed.get(arn) if is_aws else customer.get(arn)))

        access: dict[str, tuple[str, list[str]]] = {}
        wildcard = False
        everything = False
        for document in documents:
            for statement in _as_list(document.get("Statement")):
                if statement.get("Effect") != "Allow":
                    continue
                if "*" in _as_list(statement.get("Resource")):
                    wildcard = True
                for action in _as_list(statement.get("Action")):
                    service, level = action_level(action)
                    if service == "*":
                        everything = True
                    current, sample = access.get(service, ("read", []))
                    if _LEVEL_ORDER[level] > _LEVEL_ORDER[current]:
                        current = level
                    if action not in sample and len(sample) < _SAMPLE:
                        sample = [*sample, action]
                    access[service] = (current, sample)

        trust = _document(role.get("AssumeRolePolicyDocument"))
        services: set[str] = set()
        oidc: set[str] = set()
        for statement in _as_list(trust.get("Statement")):
            principal = statement.get("Principal") or {}
            if isinstance(principal, dict):
                services.update(_as_list(principal.get("Service")))
                oidc.update(p for p in _as_list(principal.get("Federated")) if "oidc-provider" in p)

        objects.append(
            IamRole(
                id=ids[role["Arn"]],
                name=role["RoleName"],
                labels=clean_labels({t["Key"]: t["Value"] for t in role.get("Tags") or []}),
                source=SourceRef(cloud="aws", type="iam-role", id=role["Arn"], region="global"),
                arn=role["Arn"],
                trusted_services=sorted(services),
                trusted_oidc=sorted(oidc),
                instance_profiles=sorted(p["Arn"] for p in role.get("InstanceProfileList") or []),
                policies=sorted(attached, key=lambda p: (p.kind, p.name)),
                access=[
                    ServiceAccess(service=s, level=level, actions_sample=sample)
                    for s, (level, sample) in sorted(access.items())
                ],
                wildcard_resources=wildcard,
                admin_everything=everything,
            )
        )
    return objects
