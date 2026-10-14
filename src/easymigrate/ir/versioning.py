"""Saved-state schema version: one number for every file EasyMigrate writes and reads back.

`ir.json`, `resolved_ir.json`, `differences.json` and `decisions.yaml` all carry
`schema_version`. Any change to the models they are read into (IR kinds, `Finding`,
`Difference`, `DecisionSet`) bumps `SCHEMA_VERSION`; `tests/ir/test_schema_version.py` fails
until it is bumped, so a change cannot slip out unnoticed.

Reading a file:
- same version: load it.
- newer: refuse, and tell the user to upgrade EasyMigrate (this copy would misread it).
- older: run `MIGRATIONS` one step at a time. A missing step means old files are rebuilt
  instead: the user re-runs the command that wrote the file, from the scan already on disk.
  Prefer a migration whenever the change is mechanical; `decisions.yaml` holds the user's own
  choices and is the file most worth carrying forward.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

SCHEMA_VERSION = "1"

# from_version -> function turning that version's raw data into from_version + 1's.
# Example for a field added with a default:  MIGRATIONS["1"] = lambda data: data
MIGRATIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}


class SchemaError(Exception):
    """A saved file this EasyMigrate cannot read as it stands; the message names the fix."""


class StaleSchema(SchemaError):
    def __init__(self, artifact: str, command: str) -> None:
        self.artifact = artifact
        self.command = command
        super().__init__(
            f"{artifact} was written by a different version of EasyMigrate and can no longer be "
            f"read. Nothing is lost: run `easymigrate {command}` again to rebuild it from the "
            "scan already on disk."
        )


class NewerSchema(SchemaError):
    def __init__(self, artifact: str, version: str) -> None:
        super().__init__(
            f"{artifact} was written by a newer version of EasyMigrate (schema {version}; this "
            f"one reads {SCHEMA_VERSION}). Upgrade EasyMigrate to keep working on this project."
        )


def upgrade(data: Any, artifact: str, command: str) -> Any:
    """`data` brought to `SCHEMA_VERSION`, or StaleSchema/NewerSchema.

    Files from before versioning was enforced carry "1" or nothing; both read as "1".
    """
    if not isinstance(data, dict):
        return data
    version = str(data.get("schema_version", "1"))
    if not version.isdigit():
        raise StaleSchema(artifact, command)
    if int(version) > int(SCHEMA_VERSION):
        raise NewerSchema(artifact, version)
    while version != SCHEMA_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise StaleSchema(artifact, command)
        data = step(dict(data))
        version = str(int(version) + 1)
        data["schema_version"] = version
    data["schema_version"] = SCHEMA_VERSION
    return data
