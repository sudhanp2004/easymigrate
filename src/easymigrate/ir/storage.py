"""Object-storage intent objects (spec 6 §4). Faithful capture only."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from easymigrate.ir.base import IntentObject, StrictModel


class Transition(StrictModel):
    days: int = Field(ge=0)
    storage_class: str


class LifecycleRule(StrictModel):
    id: str
    enabled: bool = True
    prefix: str | None = None
    filtered_by_tags: bool = False
    filtered_by_size: bool = False
    transitions: list[Transition] = Field(default_factory=list)
    expiration_days: int | None = None
    noncurrent_transitions: list[Transition] = Field(default_factory=list)
    noncurrent_expiration_days: int | None = None
    newer_noncurrent_versions: int | None = None
    abort_multipart_days: int | None = None


class CorsRule(StrictModel):
    origins: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    allowed_headers: list[str] = Field(default_factory=list)
    expose_headers: list[str] = Field(default_factory=list)
    max_age: int | None = None


class Website(StrictModel):
    index: str | None = None
    error: str | None = None
    redirect_all: str | None = None
    routing_rules: int = 0


class ObjectLockRetention(StrictModel):
    mode: Literal["GOVERNANCE", "COMPLIANCE"]
    days: int = Field(ge=1)


class StorageBucket(IntentObject):
    kind: Literal["storage_bucket"] = "storage_bucket"
    bucket_name: str
    region: str
    versioning: Literal["enabled", "suspended", "disabled"] = "disabled"
    encryption: Literal["sse_s3", "sse_kms", "dsse_kms"] = "sse_s3"
    kms_key: str | None = None
    public_access_blocked: bool = False
    public_via_policy: bool = False
    public_via_acl: bool = False
    has_policy: bool = False
    object_ownership: str = "BucketOwnerEnforced"
    lifecycle_rules: list[LifecycleRule] = Field(default_factory=list)
    cors_rules: list[CorsRule] = Field(default_factory=list)
    website: Website | None = None
    logging_bucket: str | None = None
    logging_prefix: str | None = None
    object_lock_enabled: bool = False
    object_lock: ObjectLockRetention | None = None
    replication_destinations: list[str] = Field(default_factory=list)
    notifications: list[str] = Field(default_factory=list)
    requester_pays: bool = False
    acceleration: bool = False
    intelligent_tiering: bool = False
