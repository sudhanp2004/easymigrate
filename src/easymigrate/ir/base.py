"""Cloud-neutral, intent-level IR: base types shared by every resource family (spec §3.1)."""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    ValidationError,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(StrictModel):
    cloud: Literal["aws"]
    type: str
    id: str
    region: str


class Finding(StrictModel):
    """Scan coverage and support notes. Cross-cloud differences are never Findings."""

    level: Literal["info", "unsupported", "scan_gap"]
    code: str
    message: str
    subject: str | None = None
    suggestion: str | None = None


class ResolutionRef(StrictModel):
    key: str
    strategy: str


_KINDS: dict[str, type[IntentObject]] = {}


class IntentObject(StrictModel):
    kind: str
    id: str
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    source: SourceRef
    # Populated only in resolved IR (spec §4.3).
    target_hints: dict[str, Any] = Field(default_factory=dict)
    resolution: list[ResolutionRef] = Field(default_factory=list)
    excluded: str | None = None

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        kind = cls.model_fields["kind"].default
        if not isinstance(kind, str):
            raise TypeError(f"{cls.__name__} must declare a default for 'kind'")
        existing = _KINDS.get(kind)
        same_class = existing is not None and (
            existing.__module__,
            existing.__qualname__,
        ) == (cls.__module__, cls.__qualname__)
        if existing is not None and not same_class:
            raise TypeError(
                f"intent kind {kind!r} is already registered by "
                f"{existing.__module__}.{existing.__qualname__}"
            )
        _KINDS[kind] = cls

    @model_validator(mode="after")
    def _id_starts_with_kind(self) -> IntentObject:
        prefix = f"{self.kind}:"
        if not self.id.startswith(prefix) or self.id == prefix:
            raise ValueError(f"id {self.id!r} must start with {prefix!r}")
        return self

    def references(self) -> list[str]:
        """IR ids this object points at. Subclasses with reference fields override this."""
        return []


def intent_class(kind: str) -> type[IntentObject]:
    try:
        return _KINDS[kind]
    except KeyError:
        raise ValueError(f"unknown intent kind {kind!r}") from None


T = TypeVar("T", bound=IntentObject)


class IRDocument(StrictModel):
    schema_version: Literal["1"] = "1"
    source: dict[str, Any]
    objects: list[SerializeAsAny[IntentObject]] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)

    @field_validator("objects", mode="before")
    @classmethod
    def _dispatch_kinds(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        parsed: list[Any] = []
        for item in value:
            if isinstance(item, dict):
                kind = item.get("kind")
                if not isinstance(kind, str):
                    raise ValueError("object is missing 'kind'")
                model = intent_class(kind)
                try:
                    item = model.model_validate(item)
                except ValidationError as exc:
                    raise ValueError(f"invalid {kind} object {item.get('id')!r}: {exc}") from None
            parsed.append(item)
        return parsed

    @model_validator(mode="after")
    def _check_integrity(self) -> IRDocument:
        ids: set[str] = set()
        for obj in self.objects:
            if obj.id in ids:
                raise ValueError(f"duplicate object id: {obj.id}")
            ids.add(obj.id)
        for obj in self.objects:
            for ref in obj.references():
                if ref not in ids:
                    raise ValueError(f"{obj.id} references missing object {ref}")
        return self

    def get(self, object_id: str) -> IntentObject:
        for obj in self.objects:
            if obj.id == object_id:
                return obj
        raise KeyError(object_id)

    def of_kind(self, cls: type[T]) -> list[T]:
        return [obj for obj in self.objects if isinstance(obj, cls)]
