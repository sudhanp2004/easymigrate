"""IAM intent objects (spec 8 §4). Report only: nothing is generated from them."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from easymigrate.ir.base import IntentObject, StrictModel


class AttachedPolicy(StrictModel):
    name: str
    kind: Literal["inline", "customer", "aws"]


class ServiceAccess(StrictModel):
    service: str
    level: Literal["read", "write", "admin"]
    actions_sample: list[str] = Field(default_factory=list)


class IamRole(IntentObject):
    kind: Literal["iam_role"] = "iam_role"
    arn: str
    trusted_services: list[str] = Field(default_factory=list)
    trusted_oidc: list[str] = Field(default_factory=list)
    instance_profiles: list[str] = Field(default_factory=list)
    policies: list[AttachedPolicy] = Field(default_factory=list)
    access: list[ServiceAccess] = Field(default_factory=list)
    wildcard_resources: bool = False
    admin_everything: bool = False
