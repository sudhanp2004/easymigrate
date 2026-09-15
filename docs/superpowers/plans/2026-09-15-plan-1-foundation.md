# EasyMigrate Plan 1 of 4: Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the tested foundation every later stage depends on:
- the Python package and CLI entry point
- CI
- the intent-level IR (base and networking models)
- ID and label naming
- the HCL builder and GCP naming
- the `.easymigrate/` project directory

**Architecture:** A `src/`-layout Python package managed by uv:
- The IR is Pydantic v2 models. Each kind registers itself, so an `IRDocument` can round-trip any kind through JSON and check that every reference points at an existing object.
- The HCL builder is a small, deterministic renderer. Generated Terraform never uses string templates.
- `core/project.py` owns every path under `.easymigrate/` and writes files atomically.

**Tech Stack:** Python ≥3.12, uv, Typer, Pydantic v2, PyYAML, Rich, pytest, ruff, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-15-core-pipeline-networking-design.md`

**Plan series** (each plan produces working, tested software):
1. **Foundation** (this plan)
2. Scan and analyze: AWS networking collectors, the normalizer, the unsupported inventory, and the `scan` and `analyze` commands
3. The difference engine, the networking rule catalogue, and `resolve`
4. The OpenTofu engine, the networking generator, the report, `plan` and `apply`, the testbed extension, and the live test

## Global Constraints

- Python `>=3.12`, `src/easymigrate/` layout, console script `easymigrate`.
- Dependency rules:
  - `cli` imports only `core`.
  - `core` never imports boto3 or GCP libraries.
  - Plugins never import `differences`.
  - Difference rules import only `ir`.
- IR object IDs have the form `"<kind>:<name>"` and are unique within a document.
- IR `Finding.level` is only one of `info`, `unsupported` or `scan_gap`. Cross-cloud differences are never Findings.
- Every IR object carries the resolved-IR fields `target_hints: dict`, `resolution: list[{key, strategy}]` and `excluded: str | None`, empty by default.
- GCP resource names must match `[a-z]([-a-z0-9]{0,61}[a-z0-9])?`.
- Terraform labels are the IR id cleaned to `[a-z0-9_]`, with a numeric suffix on collision.
- Generated HCL is never built with string templating. It always goes through `providers/gcp/hcl.py`, and output is deterministic.
- Credentials are never written to `.easymigrate/`. `.easymigrate/` is added to `.gitignore` when the project is inside a git repo.
- Exit codes: `0` success, `1` error, `2` declined or refused.
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Run all commands from the repository root: `/home/sudhan/cibiz/easymigrate`.

## File Structure (end of this plan)

```
.github/workflows/ci.yml          # lint + tests on every push
.python-version                   # 3.13
pyproject.toml / uv.lock
README.md
prototype/                        # old scanner/mapper/compiler (reference only, excluded from lint)
testbed/aws/                      # AWS test stack (main.tf, lock file, local state)
src/easymigrate/
├── __init__.py                   # __version__
├── cli.py                        # Typer app with --version
├── ir/
│   ├── __init__.py               # public IR exports; imports network to register its kinds
│   ├── base.py                   # StrictModel, SourceRef, Finding, ResolutionRef, IntentObject, IRDocument
│   ├── network.py                # Network, Subnet, Nat, FirewallPolicy, NetworkAcl, CustomRoute, rules, peers
│   └── naming.py                 # slug, IdAllocator, clean_labels
├── providers/gcp/
│   ├── __init__.py
│   ├── hcl.py                    # Ref, Block, HclFile, escape
│   └── naming.py                 # gcp_name, GcpNameAllocator, tf_label, TfLabelAllocator
└── core/
    ├── __init__.py
    └── project.py                # Project, ProjectConfig, StageInputMissing
tests/
├── test_cli.py
├── ir/test_base.py
├── ir/test_network.py
├── ir/test_naming.py
├── providers/gcp/test_hcl.py
├── providers/gcp/test_naming.py
└── core/test_project.py
```

---

### Task 1: Move the prototype and testbed

The live AWS state was destroyed on 2026-09-15 (`terraform.tfstate` holds 0 resources), so moving the state is safe. The state files still move with `main.tf`, so Terraform keeps an accurate record.

**Files:**
- Move: `scanner.py`, `mapper.py`, `compiler.py` → `prototype/`
- Move: `main.tf`, `.terraform.lock.hcl` → `testbed/aws/`
- Move (untracked, by hand): `terraform.tfstate`, `terraform.tfstate.backup`, `.terraform/` → `testbed/aws/`
- Move (untracked): `aws_state.json`, `migration_ir.json`, `gcp_output/` → `prototype/`
- Create: `prototype/README.md`, `testbed/aws/README.md`

**Interfaces:**
- Consumes: nothing
- Produces: `testbed/aws/` as the Terraform working directory for the AWS test stack

- [ ] **Step 1: Check the state is empty before moving**

Run: `terraform state list`
Expected: no output, exit code 0. If any resources are listed, STOP and ask the user before continuing.

- [ ] **Step 2: Move the files**

```bash
mkdir -p prototype testbed/aws
git mv scanner.py mapper.py compiler.py prototype/
git mv main.tf .terraform.lock.hcl testbed/aws/
mv terraform.tfstate terraform.tfstate.backup .terraform testbed/aws/
mv aws_state.json migration_ir.json gcp_output prototype/
```

- [ ] **Step 3: Write `prototype/README.md`**

```markdown
# Prototype (reference only)

The original proof of concept: `scanner.py` → `aws_state.json` → `mapper.py` → `migration_ir.json` → `compiler.py` → `gcp_output/gcp_main.tf`.

It is kept as a reference for the RDS and EKS plugins and will be deleted once those plugins replace it. It is excluded from linting and tests. Run scripts from inside this directory.
```

- [ ] **Step 4: Write `testbed/aws/README.md`**

```markdown
# AWS testbed

A small AWS environment used to test EasyMigrate end to end.

    export TF_VAR_db_password='<choose a password>'
    terraform -chdir=testbed/aws init
    terraform -chdir=testbed/aws apply
    # ... run the EasyMigrate tests ...
    terraform -chdir=testbed/aws destroy

**Cost:** EKS, RDS and NAT gateways are billed by the hour. Create the testbed only while testing and destroy it straight afterwards.
```

- [ ] **Step 5: Check Terraform still works from the new location**

Run: `terraform -chdir=testbed/aws state list && terraform -chdir=testbed/aws validate`
Expected: no resources listed, then `Success! The configuration is valid.`

- [ ] **Step 6: Check that git sees only renames and the new READMEs**

Run: `git status --short`
Expected: `R` entries for the 5 moved tracked files, `??` for `prototype/README.md` and `testbed/aws/README.md`, and nothing else. The tfstate files, `.terraform/`, `aws_state.json`, `migration_ir.json` and `gcp_output/` stay ignored.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore: move prototype scripts and AWS testbed into their own folders

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Package scaffold, CLI entry point and CI

**Files:**
- Create: `pyproject.toml`, `.python-version`, `README.md`, `src/easymigrate/__init__.py`, `src/easymigrate/cli.py`, `tests/test_cli.py`, `.github/workflows/ci.yml`
- Generated: `uv.lock`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `easymigrate.__version__: str` (`"0.1.0"`)
  - `easymigrate.cli.app: typer.Typer`, which later plans add commands to
  - `uv run pytest` and `uv run ruff check .` as the verification commands

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "easymigrate"
version = "0.1.0"
description = "Migrate cloud infrastructure from AWS to GCP through an intent-level IR."
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.7",
    "pyyaml>=6.0",
    "rich>=13.7",
    "typer>=0.12",
]

[project.scripts]
easymigrate = "easymigrate.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/easymigrate"]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "ruff>=0.6",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --import-mode=importlib"

[tool.ruff]
line-length = 100
target-version = "py312"
extend-exclude = ["prototype"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- [ ] **Step 2: Write `.python-version` and `README.md`**

`.python-version`:
```
3.13
```

`README.md`:
```markdown
# EasyMigrate

Migrates cloud infrastructure from AWS to GCP. It scans the live source cloud, builds an intent-level intermediate representation, shows every difference between the clouds with safe default handling, generates Terraform, and applies it only after you approve.

## Development

    uv sync
    uv run pytest
    uv run ruff check . && uv run ruff format --check .

Design: `docs/superpowers/specs/2026-09-15-core-pipeline-networking-design.md`
```

- [ ] **Step 3: Write the failing CLI test**

`tests/test_cli.py`:
```python
from typer.testing import CliRunner

from easymigrate import __version__
from easymigrate.cli import app

runner = CliRunner()


def test_version_flag_prints_version():
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"easymigrate {__version__}"


def test_no_arguments_shows_help():
    result = runner.invoke(app, [])

    assert "Usage" in result.output
```

- [ ] **Step 4: Run it and check it fails**

Run: `uv sync && uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate'` or an import error for `easymigrate.cli`.

- [ ] **Step 5: Write the package and CLI**

`src/easymigrate/__init__.py`:
```python
"""EasyMigrate: cloud infrastructure migration through an intent-level IR."""

__version__ = "0.1.0"
```

`src/easymigrate/cli.py`:
```python
"""Command-line entry point. Commands only call `easymigrate.core`."""

import typer

from easymigrate import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"easymigrate {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_print_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Migrate cloud infrastructure from AWS to GCP."""
```

- [ ] **Step 6: Run the tests and check they pass**

Run: `uv sync && uv run pytest tests/test_cli.py -v`
Expected: 2 passed. (With no arguments Typer may exit with code 0 or 2 depending on the version, so the test only checks for the help text.)

- [ ] **Step 7: Write the CI workflow**

`.github/workflows/ci.yml`:
```yaml
name: ci

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v6
      - name: Install
        run: uv sync --locked
      - name: Lint
        run: uv run ruff check . && uv run ruff format --check .
      - name: Test
        run: uv run pytest
```

- [ ] **Step 8: Check lint and formatting pass locally**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`
Expected: ruff reports no errors, and 2 tests pass.

- [ ] **Step 9: Commit and push, then check CI**

```bash
git add pyproject.toml uv.lock .python-version README.md src tests .github
git commit -m "$(cat <<'EOF'
feat: package scaffold with easymigrate CLI entry point and CI

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
git push
gh run watch --exit-status "$(gh run list --limit 1 --json databaseId --jq '.[0].databaseId')"
```
Expected: the CI run finishes successfully. If an action version tag doesn't exist, switch to the latest major version listed on its GitHub releases page, commit and push again.

---

### Task 3: IR base models

**Files:**
- Create: `src/easymigrate/ir/__init__.py`, `src/easymigrate/ir/base.py`, `tests/ir/test_base.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `StrictModel(BaseModel)`: rejects extra fields
  - `SourceRef(cloud: Literal["aws"], type: str, id: str, region: str)`
  - `Finding(level: Literal["info","unsupported","scan_gap"], code: str, message: str, subject: str | None = None, suggestion: str | None = None)`
  - `ResolutionRef(key: str, strategy: str)`
  - `IntentObject(kind, id, name, labels={}, source, target_hints={}, resolution=[], excluded=None)`. Subclasses declare `kind: Literal["x"] = "x"` and override `references() -> list[str]`.
  - `intent_class(kind: str) -> type[IntentObject]`: raises `ValueError` for unknown kinds
  - `IRDocument(schema_version="1", source: dict, objects: list[IntentObject], findings: list[Finding])`, with `.get(id) -> IntentObject` (raises `KeyError`) and `.of_kind(cls) -> list[cls]`

- [ ] **Step 1: Write the failing tests**

`tests/ir/test_base.py`:
```python
import json
from typing import Literal

import pytest
from pydantic import ValidationError

from easymigrate.ir.base import Finding, IntentObject, IRDocument, SourceRef, intent_class

SRC = {"cloud": "aws", "type": "widget", "id": "w-123", "region": "ap-south-1"}


class Widget(IntentObject):
    kind: Literal["test_widget"] = "test_widget"
    size: int
    parent: str | None = None

    def references(self) -> list[str]:
        return [self.parent] if self.parent else []


def widget(name: str = "a", **fields) -> Widget:
    return Widget(id=f"test_widget:{name}", name=name, source=SourceRef(**SRC), size=1, **fields)


def doc(*objects) -> IRDocument:
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=list(objects),
    )


def test_subclass_registers_its_kind():
    assert intent_class("test_widget") is Widget


def test_json_round_trip_keeps_subclass_and_its_fields():
    original = doc(widget("a"), widget("b", parent="test_widget:a"))

    restored = IRDocument.model_validate_json(original.model_dump_json())

    assert isinstance(restored.objects[1], Widget)
    assert restored.objects[1].parent == "test_widget:a"
    assert restored.model_dump() == original.model_dump()


def test_unknown_kind_is_rejected():
    data = json.loads(doc().model_dump_json())
    data["objects"] = [{"kind": "nope", "id": "nope:x", "name": "x", "source": SRC}]

    with pytest.raises(ValidationError, match="unknown intent kind 'nope'"):
        IRDocument.model_validate(data)


def test_invalid_object_inside_document_is_rejected():
    data = json.loads(doc().model_dump_json())
    data["objects"] = [{"kind": "test_widget", "id": "test_widget:x", "name": "x", "source": SRC}]

    with pytest.raises(ValidationError, match="invalid test_widget object 'test_widget:x'"):
        IRDocument.model_validate(data)


def test_id_must_start_with_kind():
    with pytest.raises(ValidationError, match="must start with 'test_widget:'"):
        Widget(id="other:a", name="a", source=SourceRef(**SRC), size=1)


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValidationError, match="duplicate object id: test_widget:a"):
        doc(widget("a"), widget("a"))


def test_dangling_reference_is_rejected():
    with pytest.raises(
        ValidationError, match="test_widget:b references missing object test_widget:zzz"
    ):
        doc(widget("b", parent="test_widget:zzz"))


def test_extra_fields_are_forbidden():
    with pytest.raises(ValidationError):
        Widget(id="test_widget:a", name="a", source=SourceRef(**SRC), size=1, bogus=True)


def test_subclass_must_declare_kind_default():
    with pytest.raises(TypeError, match="must declare a default for 'kind'"):

        class Broken(IntentObject):
            kind: str


def test_two_classes_cannot_share_a_kind():
    with pytest.raises(TypeError, match="intent kind 'test_widget' is already registered"):

        class Impostor(IntentObject):
            kind: Literal["test_widget"] = "test_widget"


def test_get_and_of_kind():
    d = doc(widget("a"))

    assert d.get("test_widget:a").name == "a"
    assert d.of_kind(Widget) == [d.objects[0]]
    with pytest.raises(KeyError):
        d.get("test_widget:missing")


def test_finding_levels_exclude_difference_levels():
    Finding(level="scan_gap", code="SCAN-ACCESS-DENIED", message="m")

    with pytest.raises(ValidationError):
        Finding(level="approximated", code="X", message="m")


def test_resolved_ir_fields_default_to_empty():
    w = widget()

    assert w.target_hints == {}
    assert w.resolution == []
    assert w.excluded is None
```

- [ ] **Step 2: Run the tests and check they fail**

Run: `uv run pytest tests/ir/test_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.ir'`.

- [ ] **Step 3: Write the implementation**

`src/easymigrate/ir/base.py`:
```python
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
```

`src/easymigrate/ir/__init__.py`:
```python
"""Intent-level intermediate representation."""

from easymigrate.ir.base import (
    Finding,
    IntentObject,
    IRDocument,
    ResolutionRef,
    SourceRef,
    StrictModel,
    intent_class,
)

__all__ = [
    "Finding",
    "IRDocument",
    "IntentObject",
    "ResolutionRef",
    "SourceRef",
    "StrictModel",
    "intent_class",
]
```

- [ ] **Step 4: Run the tests and check they pass**

Run: `uv run pytest tests/ir/test_base.py -v && uv run ruff check . && uv run ruff format --check .`
Expected: 13 passed, and ruff is clean. (Run `uv run ruff format .` first if the format check complains.)

- [ ] **Step 5: Commit**

```bash
git add src/easymigrate/ir tests/ir
git commit -m "$(cat <<'EOF'
feat(ir): intent object base, kind registry and IR document integrity checks

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Networking IR models

**Files:**
- Create: `src/easymigrate/ir/network.py`, `tests/ir/test_network.py`
- Modify: `src/easymigrate/ir/__init__.py` (import `network` so its kinds register)

**Interfaces:**
- Consumes: `IntentObject`, `StrictModel`, `IRDocument`, `SourceRef` from Task 3
- Produces (all in `easymigrate.ir.network`):
  - `Network(cidr_blocks: list[str] (≥1, IPv4), ipv6_cidr_blocks: list[str] = [], dns_hostnames: bool, dns_resolution: bool)`
  - `Subnet(network: "network:*", cidr: IPv4, ipv6_cidr: IPv6 | None = None, zone: str, internet: Literal["public","private_nat","isolated"], map_public_ip: bool)`
  - `Nat(network, serves_subnets: list["subnet:*"])`
  - `FirewallPolicy(network, rules: list[FirewallRule] = [], applies_to: list[str] = [])`
  - `FirewallRule(direction: Literal["ingress","egress"], protocol: str, ports: list[str] = [], peer: Peer, description: str | None = None)`
  - `Peer = CidrPeer(cidr) | PolicyPeer(policy: "firewall_policy:*") | ExternalPolicyPeer(group_id, owner_id=None, vpc_id=None) | SelfPeer()`, discriminated on `type`
  - `NetworkAcl(network, subnets: list["subnet:*"] = [], rules: list[AclRule] = [])`
  - `AclRule(number: 1..32766, direction, protocol: str, ports: list[str] = [], cidr: IPv4 or IPv6, action: Literal["allow","deny"])`
  - `CustomRoute(network, destination: str, target_type: Literal["peering","vpn","tgw","eni","other"], target_id: str, subnets: list["subnet:*"] = [])`. `subnets` lists the subnets whose route table holds the route; it is an addition to the spec so difference messages can name the affected subnets.
  - Protocol values: `tcp`, `udp`, `icmp`, `icmpv6`, `all`, or an IANA protocol number `0`–`255` as a string. Ports (`"443"` or `"8000-8080"`, 0–65535) are allowed only for `tcp` and `udp`.

- [ ] **Step 1: Write the failing tests**

`tests/ir/test_network.py`:
```python
import pytest
from pydantic import ValidationError

from easymigrate.ir import IRDocument, SourceRef, intent_class
from easymigrate.ir.network import (
    AclRule,
    CidrPeer,
    CustomRoute,
    ExternalPolicyPeer,
    FirewallPolicy,
    FirewallRule,
    Nat,
    Network,
    NetworkAcl,
    PolicyPeer,
    SelfPeer,
    Subnet,
)


def src(type_: str, id_: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=id_, region="ap-south-1")


def network(name: str = "main", **overrides) -> Network:
    fields = dict(cidr_blocks=["10.0.0.0/16"], dns_hostnames=True, dns_resolution=True)
    fields.update(overrides)
    return Network(id=f"network:{name}", name=name, source=src("vpc", "vpc-1"), **fields)


def subnet(name: str = "s1", **overrides) -> Subnet:
    fields = dict(
        network="network:main",
        cidr="10.0.1.0/24",
        zone="ap-south-1a",
        internet="public",
        map_public_ip=True,
    )
    fields.update(overrides)
    return Subnet(id=f"subnet:{name}", name=name, source=src("subnet", f"subnet-{name}"), **fields)


def policy(name: str, rules: list[FirewallRule]) -> FirewallPolicy:
    return FirewallPolicy(
        id=f"firewall_policy:{name}",
        name=name,
        source=src("security_group", f"sg-{name}"),
        network="network:main",
        rules=rules,
    )


def document(*objects) -> IRDocument:
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=list(objects),
    )


def test_networking_kinds_are_registered():
    assert intent_class("network") is Network
    assert intent_class("subnet") is Subnet
    assert intent_class("nat") is Nat
    assert intent_class("firewall_policy") is FirewallPolicy
    assert intent_class("network_acl") is NetworkAcl
    assert intent_class("custom_route") is CustomRoute


def test_full_networking_document_round_trips():
    web = policy(
        "web",
        [
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["443"], peer=CidrPeer(cidr="0.0.0.0/0")
            ),
            FirewallRule(direction="ingress", protocol="all", peer=SelfPeer()),
            FirewallRule(
                direction="egress",
                protocol="tcp",
                ports=["5432"],
                peer=PolicyPeer(policy="firewall_policy:db"),
            ),
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["22"],
                peer=ExternalPolicyPeer(group_id="sg-999", owner_id="210987654321"),
            ),
        ],
    )
    db = policy("db", [])
    original = document(
        network(ipv6_cidr_blocks=["2600:1f18:abcd:1200::/56"]),
        subnet("s1"),
        subnet("s2", cidr="10.0.2.0/24", internet="private_nat", map_public_ip=False),
        Nat(
            id="nat:nat-1",
            name="nat-1",
            source=src("nat_gateway", "nat-1"),
            network="network:main",
            serves_subnets=["subnet:s2"],
        ),
        web,
        db,
        NetworkAcl(
            id="network_acl:locked",
            name="locked",
            source=src("network_acl", "acl-1"),
            network="network:main",
            subnets=["subnet:s2"],
            rules=[
                AclRule(
                    number=100,
                    direction="ingress",
                    protocol="tcp",
                    ports=["443"],
                    cidr="10.0.0.0/16",
                    action="allow",
                )
            ],
        ),
        CustomRoute(
            id="custom_route:pcx-1",
            name="pcx-1",
            source=src("route", "rtb-1:172.16.0.0/16"),
            network="network:main",
            destination="172.16.0.0/16",
            target_type="peering",
            target_id="pcx-1",
            subnets=["subnet:s1"],
        ),
    )

    restored = IRDocument.model_validate_json(original.model_dump_json())

    assert restored.model_dump() == original.model_dump()
    assert isinstance(restored.get("firewall_policy:web").rules[2].peer, PolicyPeer)
    assert isinstance(restored.get("firewall_policy:web").rules[3].peer, ExternalPolicyPeer)


def test_subnet_must_point_at_existing_network():
    with pytest.raises(ValidationError, match="subnet:s1 references missing object network:main"):
        document(subnet("s1"))


def test_reference_fields_check_the_kind():
    with pytest.raises(ValidationError, match="expected a network reference"):
        subnet(network="subnet:oops")


def test_policy_peer_must_point_at_existing_policy():
    rule = FirewallRule(
        direction="ingress",
        protocol="tcp",
        ports=["80"],
        peer=PolicyPeer(policy="firewall_policy:gone"),
    )
    with pytest.raises(ValidationError, match="references missing object firewall_policy:gone"):
        document(network(), policy("web", [rule]))


@pytest.mark.parametrize(
    "cidrs, message",
    [
        (["10.0.0.0/33"], "not a valid CIDR"),
        (["10.0.0.0"], "not a valid CIDR"),
        (["10.0.1.5/24"], "not a valid CIDR"),
        (["2600:1f18::/56"], "not an IPv4 CIDR"),
        ([], "at least 1 item"),
    ],
)
def test_network_ipv4_cidrs_are_validated(cidrs, message):
    with pytest.raises(ValidationError, match=message):
        network(cidr_blocks=cidrs)


def test_network_ipv6_cidrs_must_be_ipv6():
    with pytest.raises(ValidationError, match="not an IPv6 CIDR"):
        network(ipv6_cidr_blocks=["10.0.0.0/16"])


@pytest.mark.parametrize(
    "protocol, ports",
    [("tcp", ["443"]), ("udp", ["8000-8080"]), ("tcp", ["0-65535"]), ("all", []), ("58", [])],
)
def test_valid_protocols_and_ports(protocol, ports):
    FirewallRule(direction="ingress", protocol=protocol, ports=ports, peer=SelfPeer())


@pytest.mark.parametrize(
    "protocol, ports, message",
    [
        ("tcp", ["70000"], "invalid port range"),
        ("tcp", ["90-80"], "invalid port range"),
        ("tcp", ["http"], "invalid port"),
        ("icmp", ["1"], "ports are only allowed for tcp/udp"),
        ("banana", [], "unsupported protocol"),
        ("300", [], "unsupported protocol"),
    ],
)
def test_invalid_protocols_and_ports(protocol, ports, message):
    with pytest.raises(ValidationError, match=message):
        FirewallRule(direction="ingress", protocol=protocol, ports=ports, peer=SelfPeer())


def test_acl_rule_number_range():
    with pytest.raises(ValidationError):
        AclRule(number=32767, direction="ingress", protocol="all", cidr="0.0.0.0/0", action="deny")
```

- [ ] **Step 2: Run the tests and check they fail**

Run: `uv run pytest tests/ir/test_network.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.ir.network'`.

- [ ] **Step 3: Write the implementation**

`src/easymigrate/ir/network.py`:
```python
"""Networking intent objects (spec §3.2). Faithful capture only; no translatability judgement."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from easymigrate.ir.base import IntentObject, StrictModel


def _cidr(version: int | None) -> Callable[[str], str]:
    def check(value: str) -> str:
        if "/" not in value:
            raise ValueError(f"{value!r} is not a valid CIDR: missing prefix length")
        try:
            net = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError(f"{value!r} is not a valid CIDR: {exc}") from None
        if version is not None and net.version != version:
            raise ValueError(f"{value!r} is not an IPv{version} CIDR")
        return value

    return check


def _ref(kind: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        prefix = f"{kind}:"
        if not value.startswith(prefix) or value == prefix:
            raise ValueError(f"expected a {kind} reference, got {value!r}")
        return value

    return check


Cidr = Annotated[str, AfterValidator(_cidr(None))]
Cidr4 = Annotated[str, AfterValidator(_cidr(4))]
Cidr6 = Annotated[str, AfterValidator(_cidr(6))]
NetworkRef = Annotated[str, AfterValidator(_ref("network"))]
SubnetRef = Annotated[str, AfterValidator(_ref("subnet"))]
PolicyRef = Annotated[str, AfterValidator(_ref("firewall_policy"))]
Direction = Literal["ingress", "egress"]

_PROTOCOL = re.compile(r"^(tcp|udp|icmp|icmpv6|all|\d{1,3})$")
_PORT = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")
_PORTED_PROTOCOLS = {"tcp", "udp"}


def _check_protocol_and_ports(protocol: str, ports: list[str]) -> None:
    if not _PROTOCOL.match(protocol) or (protocol.isdigit() and int(protocol) > 255):
        raise ValueError(f"unsupported protocol {protocol!r}")
    if ports and protocol not in _PORTED_PROTOCOLS:
        raise ValueError(f"ports are only allowed for tcp/udp, not {protocol!r}")
    for port in ports:
        match = _PORT.match(port)
        if not match:
            raise ValueError(f"invalid port {port!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not 0 <= start <= end <= 65535:
            raise ValueError(f"invalid port range {port!r}")


class CidrPeer(StrictModel):
    type: Literal["cidr"] = "cidr"
    cidr: Cidr


class PolicyPeer(StrictModel):
    """Another firewall policy in the same migration (AWS: security group in the same VPC)."""

    type: Literal["policy"] = "policy"
    policy: PolicyRef


class ExternalPolicyPeer(StrictModel):
    """A security group outside this migration: another VPC or another account."""

    type: Literal["external_policy"] = "external_policy"
    group_id: str
    owner_id: str | None = None
    vpc_id: str | None = None


class SelfPeer(StrictModel):
    type: Literal["self"] = "self"


Peer = Annotated[CidrPeer | PolicyPeer | ExternalPolicyPeer | SelfPeer, Field(discriminator="type")]


class FirewallRule(StrictModel):
    direction: Direction
    protocol: str
    ports: list[str] = Field(default_factory=list)
    peer: Peer
    description: str | None = None

    @model_validator(mode="after")
    def _valid_protocol_and_ports(self) -> FirewallRule:
        _check_protocol_and_ports(self.protocol, self.ports)
        return self


class AclRule(StrictModel):
    number: int = Field(ge=1, le=32766)
    direction: Direction
    protocol: str
    ports: list[str] = Field(default_factory=list)
    cidr: Cidr
    action: Literal["allow", "deny"]

    @model_validator(mode="after")
    def _valid_protocol_and_ports(self) -> AclRule:
        _check_protocol_and_ports(self.protocol, self.ports)
        return self


class Network(IntentObject):
    kind: Literal["network"] = "network"
    cidr_blocks: list[Cidr4] = Field(min_length=1)
    ipv6_cidr_blocks: list[Cidr6] = Field(default_factory=list)
    dns_hostnames: bool
    dns_resolution: bool


class Subnet(IntentObject):
    kind: Literal["subnet"] = "subnet"
    network: NetworkRef
    cidr: Cidr4
    ipv6_cidr: Cidr6 | None = None
    zone: str
    internet: Literal["public", "private_nat", "isolated"]
    map_public_ip: bool

    def references(self) -> list[str]:
        return [self.network]


class Nat(IntentObject):
    kind: Literal["nat"] = "nat"
    network: NetworkRef
    serves_subnets: list[SubnetRef] = Field(default_factory=list)

    def references(self) -> list[str]:
        return [self.network, *self.serves_subnets]


class FirewallPolicy(IntentObject):
    kind: Literal["firewall_policy"] = "firewall_policy"
    network: NetworkRef
    rules: list[FirewallRule] = Field(default_factory=list)
    applies_to: list[str] = Field(default_factory=list)  # native ids, filled by later plugins

    def references(self) -> list[str]:
        peers = [rule.peer.policy for rule in self.rules if isinstance(rule.peer, PolicyPeer)]
        return [self.network, *peers]


class NetworkAcl(IntentObject):
    kind: Literal["network_acl"] = "network_acl"
    network: NetworkRef
    subnets: list[SubnetRef] = Field(default_factory=list)
    rules: list[AclRule] = Field(default_factory=list)

    def references(self) -> list[str]:
        return [self.network, *self.subnets]


class CustomRoute(IntentObject):
    kind: Literal["custom_route"] = "custom_route"
    network: NetworkRef
    destination: str  # CIDR or AWS prefix-list id
    target_type: Literal["peering", "vpn", "tgw", "eni", "other"]
    target_id: str
    subnets: list[SubnetRef] = Field(default_factory=list)

    def references(self) -> list[str]:
        return [self.network, *self.subnets]
```

Modify `src/easymigrate/ir/__init__.py`: add the network import after the base import:
```python
"""Intent-level intermediate representation."""

from easymigrate.ir.base import (
    Finding,
    IntentObject,
    IRDocument,
    ResolutionRef,
    SourceRef,
    StrictModel,
    intent_class,
)
from easymigrate.ir import network  # registers networking kinds; import order does not matter

__all__ = [
    "Finding",
    "IRDocument",
    "IntentObject",
    "ResolutionRef",
    "SourceRef",
    "StrictModel",
    "intent_class",
    "network",
]
```

- [ ] **Step 4: Run the tests and check they pass**

Run: `uv run pytest tests/ir -v && uv run ruff format . && uv run ruff check .`
Expected: all tests in `tests/ir` pass (13 base + 23 network), and ruff is clean.

- [ ] **Step 5: Commit**

```bash
git add src/easymigrate/ir tests/ir
git commit -m "$(cat <<'EOF'
feat(ir): networking intent objects with CIDR, port and reference validation

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: IR IDs and GCP labels

**Files:**
- Create: `src/easymigrate/ir/naming.py`, `tests/ir/test_naming.py`

**Interfaces:**
- Consumes: nothing
- Produces (in `easymigrate.ir.naming`):
  - `slug(text: str) -> str`: lowercase, runs of anything other than `[a-z0-9]` become `-`, trimmed
  - `IdAllocator().allocate(kind: str, name: str | None, native_id: str) -> str`: `"<kind>:<slug>"`. It falls back to the native id when there is no usable name, then to `unnamed`. Collisions get `-2`, `-3` and so on. Callers must allocate in a stable order (sorted by native id) so ids are deterministic.
  - `clean_labels(tags: dict[str, str]) -> dict[str, str]`: follows GCP label rules. It skips `aws:*` keys; lowercases; turns runs of characters outside `[a-z0-9_-]` into `_`; limits keys and values to 63 characters; prefixes `tag_` to keys that don't start with a letter; keeps the first key after sorting when two clean to the same key; returns at most 64 labels.

- [ ] **Step 1: Write the failing tests**

`tests/ir/test_naming.py`:
```python
import re

import pytest

from easymigrate.ir.naming import IdAllocator, clean_labels, slug


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("migration-test-vpc", "migration-test-vpc"),
        ("Prod VPC", "prod-vpc"),
        ("  weird__Name!!", "weird-name"),
        ("日本", ""),
        ("subnet-09125e7961d295170", "subnet-09125e7961d295170"),
    ],
)
def test_slug(raw, expected):
    assert slug(raw) == expected


def test_allocate_uses_name():
    assert IdAllocator().allocate("subnet", "migration-test-subnet-1", "subnet-0a") == (
        "subnet:migration-test-subnet-1"
    )


@pytest.mark.parametrize("name", [None, "", "日本"])
def test_allocate_falls_back_to_native_id(name):
    assert IdAllocator().allocate("subnet", name, "subnet-0A") == "subnet:subnet-0a"


def test_allocate_last_resort_is_unnamed():
    assert IdAllocator().allocate("nat", None, "") == "nat:unnamed"


def test_collisions_get_numeric_suffixes_per_kind():
    ids = IdAllocator()

    assert ids.allocate("subnet", "public", "subnet-1") == "subnet:public"
    assert ids.allocate("subnet", "Public", "subnet-2") == "subnet:public-2"
    assert ids.allocate("subnet", "public", "subnet-3") == "subnet:public-3"
    assert ids.allocate("network", "public", "vpc-1") == "network:public"


def test_clean_labels_follows_gcp_rules():
    tags = {
        "Name": "migration-test-vpc",
        "Cost Center": "R&D",
        "aws:cloudformation:stack-name": "stack",
        "123abc": "V",
        "!!!": "dropped",
    }

    assert clean_labels(tags) == {
        "name": "migration-test-vpc",
        "cost_center": "r_d",
        "tag_123abc": "v",
    }


def test_clean_labels_collisions_keep_first_sorted_key():
    assert clean_labels({"env": "b", "Env": "a"}) == {"env": "a"}


def test_clean_labels_truncates_and_caps_count():
    long = "x" * 100
    labels = clean_labels({f"k{i:03d}": long for i in range(70)})

    assert len(labels) == 64
    assert all(len(k) <= 63 and len(v) == 63 for k, v in labels.items())
    assert all(re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", k) for k in labels)
```

- [ ] **Step 2: Run the tests and check they fail**

Run: `uv run pytest tests/ir/test_naming.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.ir.naming'`.

- [ ] **Step 3: Write the implementation**

`src/easymigrate/ir/naming.py`:
```python
"""Readable, deterministic IR ids and GCP-compatible labels from AWS names and tags."""

import re
import string

_MAX_LABEL_LENGTH = 63
_MAX_LABELS = 64


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class IdAllocator:
    """Allocates unique `<kind>:<name>` ids. Allocate in a stable order for stable ids."""

    def __init__(self) -> None:
        self._taken: set[str] = set()

    def allocate(self, kind: str, name: str | None, native_id: str) -> str:
        base = slug(name or "") or slug(native_id) or "unnamed"
        candidate = f"{kind}:{base}"
        suffix = 2
        while candidate in self._taken:
            candidate = f"{kind}:{base}-{suffix}"
            suffix += 1
        self._taken.add(candidate)
        return candidate


def _label_part(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", text.lower()).strip("_")[:_MAX_LABEL_LENGTH]


def clean_labels(tags: dict[str, str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for key in sorted(tags):
        if key.startswith("aws:"):
            continue
        clean_key = _label_part(key)
        if not clean_key:
            continue
        if clean_key[0] not in string.ascii_lowercase:
            clean_key = f"tag_{clean_key}"[:_MAX_LABEL_LENGTH]
        if clean_key in labels:
            continue
        labels[clean_key] = _label_part(tags[key])
        if len(labels) == _MAX_LABELS:
            break
    return labels
```

- [ ] **Step 4: Run the tests and check they pass**

Run: `uv run pytest tests/ir/test_naming.py -v && uv run ruff format . && uv run ruff check .`
Expected: all pass, and ruff is clean.

- [ ] **Step 5: Commit**

```bash
git add src/easymigrate/ir/naming.py tests/ir/test_naming.py
git commit -m "$(cat <<'EOF'
feat(ir): deterministic id allocation and GCP label cleaning

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: HCL builder

**Files:**
- Create: `src/easymigrate/providers/__init__.py`, `src/easymigrate/providers/gcp/__init__.py`, `src/easymigrate/providers/gcp/hcl.py`, `tests/providers/gcp/test_hcl.py`

**Interfaces:**
- Consumes: nothing
- Produces (in `easymigrate.providers.gcp.hcl`):
  - `Ref(expr: str)`: a raw expression such as `var.region` or `google_compute_network.x.id`
  - `Value = str | bool | int | float | None | Ref | list[Value] | dict[str, Value]`
  - `escape(text: str) -> str`
  - `Block(type, labels=[], comments=[], body=[])` with:
    - `.attr(name, value) -> Block` (chainable; raises `ValueError` on a duplicate name)
    - `.block(type, *labels) -> Block` (returns the child)
    - `.comment(text) -> Block`
  - `HclFile(blocks=[], header=[])` with `.block(type, *labels) -> Block` and `.render() -> str`

Rendering rules:
- 2-space indent.
- Consecutive attributes are aligned on `=`.
- A blank line separates an attribute group from a nested block, and a nested block from a following attribute group.
- Top-level blocks are separated by one blank line.
- Map keys are sorted and quoted when they aren't identifiers.
- Lists render on one line.
- `None` renders as `null`.
- The output ends with one newline, or is `""` when the file is empty.

- [ ] **Step 1: Write the failing tests**

`tests/providers/gcp/test_hcl.py`:
```python
import math

import pytest

from easymigrate.providers.gcp.hcl import Block, HclFile, Ref, escape


def test_resource_block_is_commented_and_aligned():
    f = HclFile()
    b = f.block("resource", "google_compute_subnetwork", "migration_test_subnet_1")
    b.comment(
        "from AWS subnet subnet-0912 (migration-test-subnet-1)\n"
        "decision NET-ZONE-REGIONAL → keep_1to1 (default)"
    )
    b.attr("name", "migration-test-subnet-1").attr("ip_cidr_range", "10.0.1.0/24")
    b.attr("region", Ref("var.region"))
    b.attr("network", Ref("google_compute_network.migration_test_vpc.id"))

    assert f.render() == (
        "# from AWS subnet subnet-0912 (migration-test-subnet-1)\n"
        "# decision NET-ZONE-REGIONAL → keep_1to1 (default)\n"
        'resource "google_compute_subnetwork" "migration_test_subnet_1" {\n'
        '  name          = "migration-test-subnet-1"\n'
        '  ip_cidr_range = "10.0.1.0/24"\n'
        "  region        = var.region\n"
        "  network       = google_compute_network.migration_test_vpc.id\n"
        "}\n"
    )


def test_nested_blocks_maps_lists_and_blank_lines():
    f = HclFile()
    r = f.block("resource", "google_sql_database_instance", "db")
    r.attr("name", "db").attr("deletion_protection", False)
    r.block("settings").attr("tier", "db-f1-micro")
    r.attr("labels", {"env": "prod", "cost-center": "r_d", "has space": "x"})
    r.attr("ports", ["80", "443"]).attr("count", 2).attr("nothing", None)

    assert f.render() == (
        'resource "google_sql_database_instance" "db" {\n'
        '  name                = "db"\n'
        "  deletion_protection = false\n"
        "\n"
        "  settings {\n"
        '    tier = "db-f1-micro"\n'
        "  }\n"
        "\n"
        "  labels  = {\n"
        '    cost-center = "r_d"\n'
        '    env         = "prod"\n'
        '    "has space" = "x"\n'
        "  }\n"
        '  ports   = ["80", "443"]\n'
        "  count   = 2\n"
        "  nothing = null\n"
        "}\n"
    )


def test_top_level_blocks_and_header_are_separated_by_blank_lines():
    f = HclFile(header=["Generated by EasyMigrate. Do not edit by hand."])
    f.block("variable", "project").attr("type", Ref("string"))
    f.block("variable", "region").attr("type", Ref("string"))

    assert f.render() == (
        "# Generated by EasyMigrate. Do not edit by hand.\n"
        "\n"
        'variable "project" {\n'
        "  type = string\n"
        "}\n"
        "\n"
        'variable "region" {\n'
        "  type = string\n"
        "}\n"
    )


def test_strings_and_labels_are_escaped():
    f = HclFile()
    f.block("locals").attr("text", 'say "hi"\\n ${var.x} %{if} \nnew')
    f.block("resource", "t", 'we"ird')

    assert f.render() == (
        "locals {\n"
        '  text = "say \\"hi\\"\\\\n $${var.x} %%{if} \\nnew"\n'
        "}\n"
        "\n"
        'resource "t" "we\\"ird" {\n'
        "}\n"
    )


def test_escape_handles_tabs_and_carriage_returns():
    assert escape("a\tb\rc") == "a\\tb\\rc"


def test_empty_file_renders_empty_string():
    assert HclFile().render() == ""


@pytest.mark.parametrize("bad", ["has space", "1starts_with_digit", "", "dot.ted"])
def test_invalid_identifiers_are_rejected(bad):
    with pytest.raises(ValueError, match="invalid HCL"):
        Block("resource").attr(bad, 1)
    with pytest.raises(ValueError, match="invalid HCL"):
        Block(bad)


def test_duplicate_attribute_is_rejected():
    with pytest.raises(ValueError, match="duplicate attribute 'name'"):
        Block("resource").attr("name", "a").attr("name", "b")


def test_unrenderable_values_are_rejected():
    with pytest.raises(TypeError, match="cannot render set as HCL"):
        f = HclFile()
        f.block("locals").attr("x", {1, 2})
        f.render()
    with pytest.raises(ValueError, match="non-finite"):
        f = HclFile()
        f.block("locals").attr("x", math.inf)
        f.render()
```

- [ ] **Step 2: Run the tests and check they fail**

Run: `uv run pytest tests/providers/gcp/test_hcl.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.providers'`.

- [ ] **Step 3: Write the implementation**

`src/easymigrate/providers/__init__.py` and `src/easymigrate/providers/gcp/__init__.py`: empty files.

`src/easymigrate/providers/gcp/hcl.py`:
```python
"""Minimal deterministic HCL writer (spec §5.2). Generated Terraform never uses string templates."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Union

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
INDENT = "  "


@dataclass(frozen=True)
class Ref:
    """A raw HCL expression, e.g. `var.region` or `google_compute_network.main.id`."""

    expr: str


Value = Union[str, bool, int, float, None, Ref, list["Value"], dict[str, "Value"]]  # noqa: UP007


def escape(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return escaped.replace("${", "$${").replace("%{", "%%{")


def _check_identifier(name: str, what: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"invalid HCL {what}: {name!r}")
    return name


@dataclass
class Block:
    type: str
    labels: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    body: list[tuple[str, Value] | Block] = field(default_factory=list)

    def __post_init__(self) -> None:
        _check_identifier(self.type, "block type")

    def attr(self, name: str, value: Value) -> Block:
        _check_identifier(name, "attribute name")
        if any(isinstance(item, tuple) and item[0] == name for item in self.body):
            raise ValueError(f"duplicate attribute {name!r} in {self.type} block")
        self.body.append((name, value))
        return self

    def block(self, type_: str, *labels: str) -> Block:
        child = Block(type_, list(labels))
        self.body.append(child)
        return child

    def comment(self, text: str) -> Block:
        self.comments.extend(text.splitlines() or [""])
        return self


@dataclass
class HclFile:
    blocks: list[Block] = field(default_factory=list)
    header: list[str] = field(default_factory=list)

    def block(self, type_: str, *labels: str) -> Block:
        child = Block(type_, list(labels))
        self.blocks.append(child)
        return child

    def render(self) -> str:
        parts: list[str] = []
        if self.header:
            parts.append("\n".join(_comment_lines(self.header, 0)))
        parts.extend("\n".join(_render_block(block, 0)) for block in self.blocks)
        return "\n\n".join(parts) + "\n" if parts else ""


def _comment_lines(comments: list[str], depth: int) -> list[str]:
    pad = INDENT * depth
    return [f"{pad}# {line}".rstrip() for line in comments]


def _render_block(block: Block, depth: int) -> list[str]:
    pad = INDENT * depth
    head = " ".join([block.type, *(f'"{escape(label)}"' for label in block.labels)])
    lines = _comment_lines(block.comments, depth)
    lines.append(f"{pad}{head} {{")
    lines.extend(_render_body(block.body, depth + 1))
    lines.append(f"{pad}}}")
    return lines


def _render_body(body: list[tuple[str, Value] | Block], depth: int) -> list[str]:
    lines: list[str] = []
    group: list[tuple[str, Value]] = []
    previous: str | None = None

    def flush() -> None:
        if not group:
            return
        width = max(len(name) for name, _ in group)
        for name, value in group:
            first, *rest = _render_value(value, depth).split("\n")
            lines.append(f"{INDENT * depth}{name.ljust(width)} = {first}")
            lines.extend(rest)
        group.clear()

    for item in body:
        if isinstance(item, Block):
            flush()
            if previous == "attr":
                lines.append("")
            lines.extend(_render_block(item, depth))
            previous = "block"
        else:
            if previous == "block":
                lines.append("")
            group.append(item)
            previous = "attr"
    flush()
    return lines


def _map_key(key: str) -> str:
    return key if _IDENT.match(key) else f'"{escape(key)}"'


def _render_value(value: Value, depth: int) -> str:
    if isinstance(value, Ref):
        return value.expr
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot render non-finite number {value!r} as HCL")
        return repr(value)
    if isinstance(value, str):
        return f'"{escape(value)}"'
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(item, depth) for item in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        entries = [(_map_key(k), v) for k, v in sorted(value.items())]
        width = max(len(key) for key, _ in entries)
        pad = INDENT * (depth + 1)
        inner = [f"{pad}{key.ljust(width)} = {_render_value(v, depth + 1)}" for key, v in entries]
        return "{\n" + "\n".join(inner) + "\n" + INDENT * depth + "}"
    raise TypeError(f"cannot render {type(value).__name__} as HCL")
```

- [ ] **Step 4: Run the tests and check they pass**

Run: `uv run pytest tests/providers/gcp/test_hcl.py -v && uv run ruff format . && uv run ruff check .`
Expected: all pass, and ruff is clean. If a whitespace mismatch appears, fix the **implementation** to match the rendering rules above; don't edit the expected strings.

- [ ] **Step 5: Commit**

```bash
git add src/easymigrate/providers tests/providers
git commit -m "$(cat <<'EOF'
feat(gcp): deterministic HCL builder with escaping and aligned attributes

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: GCP resource names and Terraform labels

**Files:**
- Create: `src/easymigrate/providers/gcp/naming.py`, `tests/providers/gcp/test_naming.py`

**Interfaces:**
- Consumes: nothing
- Produces (in `easymigrate.providers.gcp.naming`):
  - `GCP_NAME_PATTERN: re.Pattern`, equal to `^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$`
  - `gcp_name(name: str) -> str`: a valid GCP resource name
  - `GcpNameAllocator().allocate(resource_type: str, name: str) -> tuple[str, bool]`: returns (a unique valid name, whether it differs from the input). Uniqueness is per resource type, using suffixes `-2`, `-3` while staying within 63 characters. A `True` result means the generator must emit `info / GEN-RENAMED` (Plan 4).
  - `tf_label(ir_id: str) -> str`: removes the `kind:` prefix, cleans to `[a-z0-9_]`, and prefixes `r_` if the result starts with a digit or is empty
  - `TfLabelAllocator().allocate(resource_type: str, ir_id: str) -> str`: unique per resource type, using suffixes `_2`, `_3`

- [ ] **Step 1: Write the failing tests**

`tests/providers/gcp/test_naming.py`:
```python
import pytest

from easymigrate.providers.gcp.naming import (
    GCP_NAME_PATTERN,
    GcpNameAllocator,
    TfLabelAllocator,
    gcp_name,
    tf_label,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("migration-test-vpc", "migration-test-vpc"),
        ("Prod_VPC", "prod-vpc"),
        ("123", "r-123"),
        ("--edge--", "edge"),
        ("", "r"),
        ("日本", "r"),
    ],
)
def test_gcp_name(raw, expected):
    assert gcp_name(raw) == expected
    assert GCP_NAME_PATTERN.match(expected)


def test_gcp_name_truncates_to_63_without_trailing_dash():
    name = gcp_name("a" * 62 + "-bbbb")

    assert len(name) <= 63
    assert GCP_NAME_PATTERN.match(name)


def test_gcp_name_allocator_reports_renames_and_dedupes_per_type():
    names = GcpNameAllocator()

    assert names.allocate("google_compute_subnetwork", "public") == ("public", False)
    assert names.allocate("google_compute_subnetwork", "Public") == ("public-2", True)
    assert names.allocate("google_compute_network", "public") == ("public", False)


def test_gcp_name_allocator_suffix_respects_length_limit():
    names = GcpNameAllocator()
    long = "x" * 70

    first, _ = names.allocate("t", long)
    second, renamed = names.allocate("t", long)

    assert renamed is True
    assert first != second
    assert len(second) <= 63 and second.endswith("-2")
    assert GCP_NAME_PATTERN.match(second)


@pytest.mark.parametrize(
    "ir_id, expected",
    [
        ("subnet:migration-test-subnet-1", "migration_test_subnet_1"),
        ("network:10-net", "r_10_net"),
        ("firewall_policy:Web SG!", "web_sg"),
        ("nat:日本", "r"),
    ],
)
def test_tf_label(ir_id, expected):
    assert tf_label(ir_id) == expected


def test_tf_label_allocator_dedupes_per_resource_type():
    labels = TfLabelAllocator()

    assert labels.allocate("google_compute_subnetwork", "subnet:a-b") == "a_b"
    assert labels.allocate("google_compute_subnetwork", "subnet:a_b") == "a_b_2"
    assert labels.allocate("google_compute_router", "subnet:a-b") == "a_b"
```

- [ ] **Step 2: Run the tests and check they fail**

Run: `uv run pytest tests/providers/gcp/test_naming.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.providers.gcp.naming'`.

- [ ] **Step 3: Write the implementation**

`src/easymigrate/providers/gcp/naming.py`:
```python
"""GCP resource names and Terraform resource labels derived from IR names and ids (spec §5.2)."""

import re

GCP_NAME_PATTERN = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
_MAX_GCP_NAME = 63


def gcp_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"r-{cleaned}".rstrip("-")
    return cleaned[:_MAX_GCP_NAME].rstrip("-")


class GcpNameAllocator:
    def __init__(self) -> None:
        self._taken: dict[str, set[str]] = {}

    def allocate(self, resource_type: str, name: str) -> tuple[str, bool]:
        base = gcp_name(name)
        taken = self._taken.setdefault(resource_type, set())
        candidate = base
        n = 2
        while candidate in taken:
            suffix = f"-{n}"
            candidate = base[: _MAX_GCP_NAME - len(suffix)].rstrip("-") + suffix
            n += 1
        taken.add(candidate)
        return candidate, candidate != name


def tf_label(ir_id: str) -> str:
    _, _, rest = ir_id.rpartition(":") if ":" in ir_id else ("", "", ir_id)
    label = re.sub(r"[^a-z0-9_]+", "_", rest.lower())
    label = re.sub(r"_{2,}", "_", label).strip("_")
    if not label or label[0].isdigit():
        label = f"r_{label}".rstrip("_")
    return label


class TfLabelAllocator:
    def __init__(self) -> None:
        self._taken: dict[str, set[str]] = {}

    def allocate(self, resource_type: str, ir_id: str) -> str:
        base = tf_label(ir_id)
        taken = self._taken.setdefault(resource_type, set())
        candidate = base
        n = 2
        while candidate in taken:
            candidate = f"{base}_{n}"
            n += 1
        taken.add(candidate)
        return candidate
```

Note: `tf_label` uses `rpartition` on purpose, because IR ids contain exactly one `:`, and names are slugs with no colons.

- [ ] **Step 4: Run the tests and check they pass**

Run: `uv run pytest tests/providers/gcp -v && uv run ruff format . && uv run ruff check .`
Expected: all pass, and ruff is clean.

- [ ] **Step 5: Commit**

```bash
git add src/easymigrate/providers/gcp/naming.py tests/providers/gcp/test_naming.py
git commit -m "$(cat <<'EOF'
feat(gcp): GCP resource name and Terraform label allocation

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Project directory

**Files:**
- Create: `src/easymigrate/core/__init__.py`, `src/easymigrate/core/project.py`, `tests/core/test_project.py`

**Interfaces:**
- Consumes: `IRDocument`, `StrictModel` (Task 3); `easymigrate.ir.network.Network` (tests only); `easymigrate.__version__`
- Produces (in `easymigrate.core.project`):
  - `DIR_NAME = ".easymigrate"`
  - `StageInputMissing(artifact: Path, command: str)`: message `"<file> not found. Run \`easymigrate <command>\` first."`, with attributes `.artifact` and `.command`
  - `SourceConfig(cloud, account_id, region)`, `TargetConfig(cloud, project, region)`, `ProjectConfig(tool_version, source=None, target=None)`
  - `Project(root: Path)` with:
    - `.root`, `.dir`
    - path properties: `config_path`, `ir_path`, `differences_path`, `decisions_path`, `resolved_ir_path`, `report_md_path`, `report_json_path`, `terraform_dir`, `tfplan_path`, `plan_txt_path`, `plan_lock_path`
    - `snapshot_dir(cloud: str) -> Path`
    - `init() -> bool`: True on first run
    - `ensure_gitignored() -> bool`: True when it added an entry
    - `load_config() -> ProjectConfig`, `save_config(config) -> None`
    - `write_json(path, data: BaseModel | dict | list) -> None`, `read_json(path, *, produced_by: str) -> Any`
    - `write_ir(path, doc: IRDocument) -> None`, `read_ir(path, *, produced_by: str) -> IRDocument`
  - All writes are atomic. Artifacts are created with mode `0o600`; `.gitignore` is written with mode `0o644`.

- [ ] **Step 1: Write the failing tests**

`tests/core/test_project.py`:
```python
import json
import stat

import pytest

from easymigrate import __version__
from easymigrate.core.project import (
    DIR_NAME,
    Project,
    ProjectConfig,
    SourceConfig,
    StageInputMissing,
    TargetConfig,
)
from easymigrate.ir import IRDocument, SourceRef
from easymigrate.ir.network import Network


def make_ir() -> IRDocument:
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=[
            Network(
                id="network:main",
                name="main",
                source=SourceRef(cloud="aws", type="vpc", id="vpc-1", region="ap-south-1"),
                cidr_blocks=["10.0.0.0/16"],
                dns_hostnames=True,
                dns_resolution=True,
            )
        ],
    )


def test_paths_live_under_dot_easymigrate(tmp_path):
    p = Project(tmp_path)

    assert p.dir == tmp_path.resolve() / DIR_NAME
    assert p.ir_path == p.dir / "ir.json"
    assert p.decisions_path == p.dir / "decisions.yaml"
    assert p.snapshot_dir("aws") == p.dir / "snapshot" / "aws"
    assert p.terraform_dir == p.dir / "terraform"
    assert p.plan_lock_path == p.dir / "plan.lock.json"


def test_init_creates_directory_and_config_once(tmp_path):
    p = Project(tmp_path)

    assert p.init() is True
    assert p.init() is False
    assert p.load_config() == ProjectConfig(tool_version=__version__)


def test_config_round_trip(tmp_path):
    p = Project(tmp_path)
    p.init()
    config = ProjectConfig(
        tool_version=__version__,
        source=SourceConfig(cloud="aws", account_id="123456789012", region="ap-south-1"),
        target=TargetConfig(cloud="gcp", project="my-proj", region="asia-south1"),
    )

    p.save_config(config)

    assert Project(tmp_path).load_config() == config


def test_missing_config_names_the_command(tmp_path):
    with pytest.raises(StageInputMissing) as info:
        Project(tmp_path).load_config()

    assert str(info.value) == "project.yaml not found. Run `easymigrate scan` first."


def test_ir_round_trip(tmp_path):
    p = Project(tmp_path)
    p.init()

    p.write_ir(p.ir_path, make_ir())

    assert p.read_ir(p.ir_path, produced_by="analyze").model_dump() == make_ir().model_dump()


def test_read_missing_artifact_names_the_command(tmp_path):
    p = Project(tmp_path)

    with pytest.raises(StageInputMissing) as info:
        p.read_ir(p.ir_path, produced_by="analyze")

    assert str(info.value) == "ir.json not found. Run `easymigrate analyze` first."
    assert info.value.command == "analyze"


def test_write_json_dict_is_sorted_with_trailing_newline(tmp_path):
    p = Project(tmp_path)
    target = p.snapshot_dir("aws") / "vpcs.json"

    p.write_json(target, {"b": 1, "a": [1, 2]})

    assert target.read_text() == '{\n  "a": [\n    1,\n    2\n  ],\n  "b": 1\n}\n'
    assert p.read_json(target, produced_by="scan") == {"a": [1, 2], "b": 1}


def test_writes_are_atomic_and_private(tmp_path):
    p = Project(tmp_path)
    p.init()
    p.write_json(p.differences_path, [])

    assert not list(p.dir.glob("*.tmp"))
    assert stat.S_IMODE(p.differences_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.config_path.stat().st_mode) == 0o600


def test_init_adds_gitignore_entry_in_git_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    project_root = tmp_path / "work"
    project_root.mkdir()
    p = Project(project_root)

    p.init()
    p.init()

    gitignore = tmp_path / ".gitignore"
    assert gitignore.read_text().splitlines().count("/work/.easymigrate/") == 1
    assert stat.S_IMODE(gitignore.stat().st_mode) == 0o644


def test_existing_generic_entry_is_respected(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("__pycache__/\n.easymigrate/")

    assert Project(tmp_path).ensure_gitignored() is False
    assert (tmp_path / ".gitignore").read_text() == "__pycache__/\n.easymigrate/"


def test_appends_to_gitignore_without_trailing_newline(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("__pycache__/")

    assert Project(tmp_path).ensure_gitignored() is True
    assert (tmp_path / ".gitignore").read_text() == "__pycache__/\n/.easymigrate/\n"


def test_no_gitignore_outside_git(tmp_path):
    Project(tmp_path).init()

    assert not (tmp_path / ".gitignore").exists()


def test_write_json_accepts_models(tmp_path):
    p = Project(tmp_path)

    p.write_json(p.report_json_path, make_ir())

    assert json.loads(p.report_json_path.read_text())["objects"][0]["id"] == "network:main"
```

Note for the implementer: `tmp_path` sits under `/tmp`, which is not inside this repository's git tree, so "no git" cases behave correctly. If the machine's `/tmp` were itself inside a git repo, `test_no_gitignore_outside_git` would fail; in that case fix the environment, not the test.

- [ ] **Step 2: Run the tests and check they fail**

Run: `uv run pytest tests/core/test_project.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.core'`.

- [ ] **Step 3: Write the implementation**

`src/easymigrate/core/__init__.py`: empty file.

`src/easymigrate/core/project.py`:
```python
"""The .easymigrate/ project directory: every stage's inputs and outputs (spec §2.2)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

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

    def init(self) -> bool:
        """Create the project directory. Returns True on the first run."""
        created = not self.dir.exists()
        self.dir.mkdir(parents=True, exist_ok=True)
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
        self.write_json(path, doc)

    def read_ir(self, path: Path, *, produced_by: str) -> IRDocument:
        if not path.exists():
            raise StageInputMissing(path, produced_by)
        return IRDocument.model_validate_json(path.read_text(encoding="utf-8"))


def _find_git_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _atomic_write(path: Path, text: str, *, mode: int = _PRIVATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
```

- [ ] **Step 4: Run the tests and check they pass**

Run: `uv run pytest tests/core/test_project.py -v && uv run ruff format . && uv run ruff check .`
Expected: 13 passed, and ruff is clean.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: every test passes, and ruff is clean.

- [ ] **Step 6: Commit, push and check CI**

```bash
git add src/easymigrate/core tests/core
git commit -m "$(cat <<'EOF'
feat(core): .easymigrate project directory with atomic writes and gitignore protection

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
git push
gh run watch --exit-status "$(gh run list --limit 1 --json databaseId --jq '.[0].databaseId')"
```
Expected: the CI run finishes successfully.
