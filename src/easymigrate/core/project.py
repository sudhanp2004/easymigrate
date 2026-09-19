"""The .easymigrate/ project directory: every stage's inputs and outputs (spec §2.2)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from easymigrate import __version__
from easymigrate.ir.base import IRDocument, StrictModel

DIR_NAME = ".easymigrate"
_PRIVATE = 0o600
_PUBLIC = 0o644


class StageInputMissing(Exception):
    def __init__(self, artifact: Path, command: str) -> None:
        self.artifact = artifact
        self.command = command
        super().__init__(f"{artifact.name} not found. Run `easymigrate {command}` first.")


class StaleProjectState(Exception):
    """A saved artifact was written by an EasyMigrate whose IR had different fields."""

    def __init__(self, artifact: Path, command: str) -> None:
        self.artifact = artifact
        self.command = command
        super().__init__(
            f"{artifact.name} was written by a different version of EasyMigrate and can no "
            f"longer be read. Nothing is lost: run `easymigrate {command}` again to rebuild it "
            "from the scan already on disk."
        )


class SourceConfig(StrictModel):
    cloud: str
    account_id: str
    region: str


class TargetConfig(StrictModel):
    cloud: str
    project: str
    region: str


class ProjectConfig(StrictModel):
    tool_version: str
    source: SourceConfig | None = None
    target: TargetConfig | None = None


class Project:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.dir = self.root / DIR_NAME

    @property
    def config_path(self) -> Path:
        return self.dir / "project.yaml"

    def snapshot_dir(self, cloud: str) -> Path:
        return self.dir / "snapshot" / cloud

    @property
    def ir_path(self) -> Path:
        return self.dir / "ir.json"

    @property
    def differences_path(self) -> Path:
        return self.dir / "differences.json"

    @property
    def decisions_path(self) -> Path:
        return self.dir / "decisions.yaml"

    @property
    def resolved_ir_path(self) -> Path:
        return self.dir / "resolved_ir.json"

    @property
    def report_md_path(self) -> Path:
        return self.dir / "report.md"

    @property
    def report_json_path(self) -> Path:
        return self.dir / "report.json"

    @property
    def terraform_dir(self) -> Path:
        return self.dir / "terraform"

    @property
    def tfplan_path(self) -> Path:
        return self.dir / "tfplan"

    @property
    def plan_txt_path(self) -> Path:
        return self.dir / "plan.txt"

    @property
    def plan_lock_path(self) -> Path:
        return self.dir / "plan.lock.json"

    @property
    def plan_summary_path(self) -> Path:
        return self.dir / "plan_summary.json"

    @property
    def superseded_path(self) -> Path:
        return self.dir / "superseded.json"

    @property
    def jobs_dir(self) -> Path:
        """`.easymigrate/jobs/`: one `<job-id>.json` record and `<job-id>.log` file per
        background job (spec §4). Created on demand by whichever write touches it first,
        matching every other path property here."""
        return self.dir / "jobs"

    @property
    def secrets_path(self) -> Path:
        """`.easymigrate/terraform/secrets.auto.tfvars.json`: OpenTofu auto-loads any
        `*.auto.tfvars.json` file in the working directory, so this needs no `-var-file`
        wiring. It lives in `terraform/`, which is hashed wholesale by
        `pipeline._terraform_hashes`, so editing a password after `plan` makes `apply`
        correctly refuse as stale without any extra fingerprinting here."""
        return self.terraform_dir / "secrets.auto.tfvars.json"

    def write_secrets(self, values: dict[str, str]) -> Path:
        """Write the password tfvars file, keyed by the generator's own variable names
        (`database_password_variables`), mode 0600, atomically. Never call this with
        anything but password values: nothing else belongs in this file."""
        text = json.dumps(values, indent=2, sort_keys=True) + "\n"
        _atomic_write(self.secrets_path, text, mode=_PRIVATE)
        return self.secrets_path

    def init(self) -> bool:
        """Create the project directory. Returns True on the first run."""
        created = not self.dir.exists()
        _create_private_dirs(self.dir)
        if not self.config_path.exists():
            self.save_config(ProjectConfig(tool_version=__version__))
        self.ensure_gitignored()
        return created

    def ensure_gitignored(self) -> bool:
        git_root = _find_git_root(self.root)
        if git_root is None:
            return False
        relative = self.dir.relative_to(git_root).as_posix()
        entry = f"/{relative}/"
        covered = {entry, entry.rstrip("/"), relative, f"{relative}/", f"{DIR_NAME}/", DIR_NAME}
        gitignore = git_root / ".gitignore"
        text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if any(line.strip() in covered for line in text.splitlines()):
            return False
        if text and not text.endswith("\n"):
            text += "\n"
        _atomic_write(gitignore, f"{text}{entry}\n", mode=_PUBLIC)
        return True

    def load_config(self) -> ProjectConfig:
        if not self.config_path.exists():
            raise StageInputMissing(self.config_path, "scan")
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        return ProjectConfig.model_validate(data)

    def save_config(self, config: ProjectConfig) -> None:
        text = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
        _atomic_write(self.config_path, text)

    def write_text(self, path: Path, text: str) -> None:
        _atomic_write(path, text)

    def write_json(self, path: Path, data: BaseModel | dict[str, Any] | list[Any]) -> None:
        if isinstance(data, BaseModel):
            text = data.model_dump_json(indent=2)
        else:
            text = json.dumps(data, indent=2, sort_keys=True, default=str)
        _atomic_write(path, text + "\n")

    def read_json(self, path: Path, *, produced_by: str) -> Any:
        if not path.exists():
            raise StageInputMissing(path, produced_by)
        return json.loads(path.read_text(encoding="utf-8"))

    def write_ir(self, path: Path, doc: IRDocument) -> None:
        """Re-validate before writing, so a document mutated after construction (e.g. with
        `model_copy(update=...)`) can't be saved with dangling references."""
        validated = IRDocument.model_validate_json(doc.model_dump_json())
        self.write_json(path, validated)

    def read_ir(self, path: Path, *, produced_by: str) -> IRDocument:
        if not path.exists():
            raise StageInputMissing(path, produced_by)
        text = path.read_text(encoding="utf-8")
        try:
            return IRDocument.model_validate_json(text)
        except ValidationError as exc:
            if not _looks_like_an_older_schema(exc):
                raise
            raise StaleProjectState(path, produced_by) from None


def _looks_like_an_older_schema(exc: ValidationError) -> bool:
    """Whether this validation failure is a field the IR no longer has.

    The IR forbids unknown fields on purpose, so a document written by an older EasyMigrate
    that carried a since-removed field fails to load. That is a stale-state problem with an
    easy fix, not a corrupt file, and it deserves to say so (spec 4 §2.1 removed
    `FirewallPolicy.applies_to` without this, and the user met a raw pydantic dump).
    """
    for error in exc.errors():
        if error.get("type") == "extra_forbidden":
            return True
        # `IRDocument._dispatch_kinds` re-raises a per-object failure as a ValueError, so the
        # original extra_forbidden survives only inside the message.
        if "Extra inputs are not permitted" in str(error.get("msg", "")):
            return True
    return False


def _find_git_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _create_private_dirs(path: Path) -> None:
    """Create `path` and any missing parents. Directories under `.easymigrate/` are created
    with mode 0o700 (the umask would otherwise mask that), since they hold sensitive scan and
    plan data; directories outside it (e.g. the git root) are created with default permissions
    and never touched."""
    if DIR_NAME not in path.parts:
        path.mkdir(parents=True, exist_ok=True)
        return
    to_create: list[Path] = []
    current = path
    while not current.exists():
        to_create.append(current)
        current = current.parent
    for directory in reversed(to_create):
        directory.mkdir(exist_ok=True)
        if DIR_NAME in directory.parts:
            os.chmod(directory, 0o700)


def _atomic_write(path: Path, text: str, *, mode: int = _PRIVATE) -> None:
    _create_private_dirs(path.parent)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
