"""Plugin interface: what the core hands plugins and what it expects back (spec §2.1).

Plugins may import this module; it must stay free of cloud SDKs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from easymigrate.ir.base import Finding, IntentObject

SCAN_FILE = "_scan.json"


class UnmodelledData(Exception):
    """Raised in strict mode when a normalizer meets data the IR cannot represent."""


class Snapshot:
    def __init__(self, files: dict[str, Any]) -> None:
        self._files = dict(files)

    @classmethod
    def from_dir(cls, path: Path) -> Snapshot:
        files: dict[str, Any] = {}
        if path.is_dir():
            for file in sorted(path.glob("*.json")):
                if file.name == SCAN_FILE:
                    continue
                files[file.name.removesuffix(".json")] = json.loads(
                    file.read_text(encoding="utf-8")
                )
        return cls(files)

    def get(self, name: str) -> Any | None:
        return self._files.get(name)

    def names(self) -> list[str]:
        return sorted(self._files)


@dataclass
class CollectContext:
    plugin: str
    findings: list[Finding] = field(default_factory=list)

    def access_denied(self, api: str, iam_action: str) -> None:
        suggestion = f"Grant {iam_action}"
        if any(f.suggestion == suggestion for f in self.findings):
            return
        self.findings.append(
            Finding(
                level="scan_gap",
                code="SCAN-ACCESS-DENIED",
                message=f"Access denied calling {api}; {self.plugin} data from it is missing.",
                suggestion=suggestion,
            )
        )


@dataclass
class NormalizeContext:
    account_id: str
    region: str
    strict: bool = False
    findings: list[Finding] = field(default_factory=list)
    objects: dict[str, IntentObject] = field(default_factory=dict)

    def lookup(self, object_id: str) -> IntentObject:
        return self.objects[object_id]

    def info(
        self, code: str, message: str, subject: str | None = None, suggestion: str | None = None
    ) -> None:
        self.findings.append(
            Finding(
                level="info", code=code, message=message, subject=subject, suggestion=suggestion
            )
        )

    def unmodelled(self, message: str, subject: str | None = None) -> None:
        if self.strict:
            raise UnmodelledData(message)
        self.info(
            "INTERNAL-UNMODELLED",
            message,
            subject=subject,
            suggestion="Report this to the EasyMigrate team so it can be modelled.",
        )


class Plugin(Protocol):
    name: str
    aws_resource_types: frozenset[str]

    def collect_aws(self, connection: Any, ctx: CollectContext) -> dict[str, Any]: ...

    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]: ...
