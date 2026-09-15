# EasyMigrate Plan 2 of 4: Scan and Analyze Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `easymigrate scan` reads a live AWS region's networking, plus an inventory of everything else, into `.easymigrate/snapshot/aws/`. `easymigrate analyze` turns that snapshot into a validated, faithful intent-level `ir.json`.

**Architecture:**
- A network **plugin** owns two parts:
  - an AWS **collector**, which saves raw paginated API responses
  - a **normalizer**, which turns the snapshot into IR objects
- An AWS **source** (`providers/aws/source.py`) owns credentials, the account id and the tag-based inventory of unsupported resources.
- `core/registry.py` is the only composition point that imports plugins and providers. `core/pipeline.py` runs the stages, and `cli.py` only calls core.
- Scan gaps, unsupported resources and unmodelled data become IR `Finding`s. Cross-cloud differences are **not** part of this plan; they belong to plan 3.

**Tech Stack:** Python ≥3.12, boto3, moto 5 (tests), Pydantic v2, Typer, Rich, pytest.

**Spec:** `docs/superpowers/specs/2026-09-15-core-pipeline-networking-design.md` (§2, §3, §6, §7)

**Plan series:**
1. Foundation ✅
2. **Scan and analyze** (this plan)
3. Difference engine and `resolve`
4. OpenTofu, generator, report, `plan` and `apply`

## Global Constraints

**Dependency rules** (spec §2.1), refined, and enforced by `tests/test_dependency_rules.py`:
- `easymigrate.cli` imports only `easymigrate` (for `__version__`) and `easymigrate.core.*`.
- No `easymigrate.core.*` module imports `boto3`, `botocore`, `moto` or `google`. Only `easymigrate.core.registry` may import `easymigrate.plugins.*` or `easymigrate.providers.*`.
- `easymigrate.plugins.*` import only `easymigrate.ir.*`, `easymigrate.providers.*`, `easymigrate.core.plugin`, third-party libraries and the stdlib. They never import `easymigrate.differences`.
- `easymigrate.ir.*` imports nothing from `easymigrate` outside `easymigrate.ir`.

**IR and naming**
- IR ids come only from `easymigrate.ir.naming.allocate_ids(kind, items)`, which is order-independent. There is no `IdAllocator`.
- The normalizer is faithful: it never decides translatability (spec §3.3).
- Data it cannot represent → `Finding(level="info", code="INTERNAL-UNMODELLED")`. In strict mode (all tests) it raises `UnmodelledData`.
- CIDRs are canonicalized with `str(ipaddress.ip_network(value, strict=False))`.

**AWS value mapping**
- Protocol mapping: `-1`→`all`, `6`/`tcp`→`tcp`, `17`/`udp`→`udp`, `1`/`icmp`→`icmp`, `58`/`icmpv6`→`icmpv6`. Any other number stays as its string.
- ICMP `FromPort`/`ToPort` (security groups) and `IcmpTypeCode.Type`/`Code` (NACLs) → `icmp_type`/`icmp_code`, where `-1` means none.
- tcp/udp ports: `0-65535` → `[]` (all ports); a single port → `"443"`; a range → `"8000-8080"`.

**Scan behavior**
- `scan` calls `Project.init()` before any write.
- Snapshot files live at `.easymigrate/snapshot/aws/<plugin>.<resource>.json`, plus `.easymigrate/snapshot/aws/_scan.json` holding `account_id`, `region`, `scanned_at` and `findings`.
- `scan` deletes old `*.json` files in `snapshot/aws/` before writing new ones.
- AWS clients use `botocore.config.Config(retries={"mode": "adaptive", "max_attempts": 10})`, and every list call uses a paginator.
- Access denied (error codes `UnauthorizedOperation`, `AccessDenied`, `AccessDeniedException`) → `Finding(level="scan_gap", code="SCAN-ACCESS-DENIED", message=..., suggestion="Grant <iam action>")`. The scan continues, and that snapshot file is not written.

**CLI**
- Exit codes: `0` success, `1` error.
- Missing or invalid credentials, an unknown or missing region, `StageInputMissing`, pydantic `ValidationError` and `json.JSONDecodeError` → a one-line red message plus exit 1. No traceback.
- On the first run (when `Project.init()` returns True), print once: `Note: .easymigrate/ contains account metadata (account ID, ARNs, endpoints). Keep it private.`

**Housekeeping**
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Run all commands from `/home/sudhan/cibiz/easymigrate` using `uv run ...`. If a new module isn't importable, run `uv sync --reinstall-package easymigrate`.

## File Structure (end of this plan)

```
src/easymigrate/
├── cli.py                              # + scan, analyze commands
├── core/
│   ├── errors.py                       # EasyMigrateError, SourceConnectionError
│   ├── plugin.py                       # Snapshot, CollectContext, NormalizeContext, UnmodelledData, Plugin
│   ├── registry.py                     # PLUGINS, source_for(cloud) — the only importer of plugins/providers
│   └── pipeline.py                     # scan(), analyze(), ScanSummary
├── providers/aws/
│   ├── __init__.py
│   └── source.py                       # AwsConnection, connect(), inventory(), is_access_denied(), CLIENT_CONFIG
└── plugins/
    ├── __init__.py
    └── network/
        ├── __init__.py                 # NetworkPlugin
        ├── collect_aws.py              # collect(connection, ctx) -> dict[str, Any]
        ├── aws_values.py               # protocol/port/icmp/cidr/name/tag helpers
        ├── normalize_topology.py       # networks, subnets, nats, custom routes
        ├── normalize_security.py       # firewall policies, network ACLs
        └── normalize.py                # normalize(snapshot, ctx) entry point
scripts/
├── anonymize_fixture.py
└── make_network_fixtures.py            # moto scenarios → anonymized snapshot fixtures
tests/
├── test_dependency_rules.py
├── conftest.py                         # aws_credentials fixture, --update-golden option
├── providers/aws/test_source.py
├── core/test_plugin.py
├── core/test_pipeline.py
├── test_cli_scan_analyze.py
└── plugins/network/
    ├── aws_builders.py                 # tiny AWS-shaped dict builders for unit tests
    ├── test_collect_aws.py
    ├── test_aws_values.py
    ├── test_normalize_topology.py
    ├── test_normalize_security.py
    ├── test_golden.py
    └── fixtures/<case>/{snapshot/*.json, expected_ir.json}
```

---
### Task 1: Dependencies, test fixtures and dependency-rule guard

**Files:**
- Modify: `pyproject.toml` and `uv.lock` (through `uv add`)
- Create: `tests/conftest.py`, `tests/test_dependency_rules.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - pytest fixture `aws_credentials`: fake AWS env with no real profiles and region `ap-south-1`
  - pytest fixture `update_golden -> bool`, from the `--update-golden` flag
  - `tests/test_dependency_rules.py::violations(module: str, imports: set[str]) -> list[str]`, which guards every later task

- [ ] **Step 1: Add dependencies**

```bash
uv add "boto3>=1.34"
uv add --dev "moto[ec2,resourcegroupstaggingapi,sts]>=5.0"
```
Expected: `pyproject.toml` lists `boto3>=1.34` under `dependencies` and `moto[...]>=5.0` under `[dependency-groups] dev`, and `uv.lock` is updated.

- [ ] **Step 2: Write `tests/conftest.py`**

```python
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Rewrite golden expected files instead of comparing against them.",
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Fake AWS credentials; real profiles and config files on this machine are never read."""
    for key in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    empty = tmp_path_factory.mktemp("aws") / "empty"
    empty.write_text("")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(empty))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(empty))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
```

- [ ] **Step 3: Write the dependency-rule test**

`tests/test_dependency_rules.py`:
```python
"""Enforces the dependency rules from plan 2's Global Constraints (spec §2.1)."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
CLOUD_SDKS = ("boto3", "botocore", "moto", "google")


def _within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imported_modules(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def violations(module: str, imports: set[str]) -> list[str]:
    internal = {name for name in imports if _within(name, "easymigrate")}
    problems: list[str] = []

    def bad(name: str, why: str) -> None:
        problems.append(f"{module} imports {name}: {why}")

    if module == "easymigrate.cli":
        for name in internal:
            if name not in ("easymigrate", "easymigrate.__version__") and not _within(
                name, "easymigrate.core"
            ):
                bad(name, "cli may only import easymigrate.core")
    if _within(module, "easymigrate.core"):
        for name in imports:
            if any(_within(name, sdk) for sdk in CLOUD_SDKS):
                bad(name, "core must not import cloud SDKs")
        if module != "easymigrate.core.registry":
            for name in internal:
                if _within(name, "easymigrate.plugins") or _within(name, "easymigrate.providers"):
                    bad(name, "only easymigrate.core.registry may import plugins or providers")
    if _within(module, "easymigrate.plugins"):
        allowed = ("easymigrate.ir", "easymigrate.providers", "easymigrate.core.plugin")
        for name in internal:
            if not any(_within(name, prefix) for prefix in (*allowed, "easymigrate.plugins")):
                bad(name, "plugins may import only ir, providers and core.plugin")
    if _within(module, "easymigrate.differences"):
        for name in internal:
            if not (_within(name, "easymigrate.ir") or _within(name, "easymigrate.differences")):
                bad(name, "difference rules may import only ir")
    if _within(module, "easymigrate.ir"):
        for name in internal:
            if not _within(name, "easymigrate.ir"):
                bad(name, "ir must not import other easymigrate packages")
    return sorted(problems)


def test_source_tree_follows_dependency_rules():
    problems = []
    for path in sorted(SRC.rglob("*.py")):
        problems += violations(module_name(path), imported_modules(path.read_text()))

    assert problems == []


@pytest.mark.parametrize(
    "module, source, allowed",
    [
        ("easymigrate.core.pipeline", "import boto3", False),
        ("easymigrate.core.pipeline", "from botocore.config import Config", False),
        ("easymigrate.core.pipeline", "from easymigrate.providers.aws.source import connect", False),
        ("easymigrate.core.registry", "from easymigrate.plugins.network import NetworkPlugin", True),
        ("easymigrate.core.registry", "from easymigrate.providers.aws import source", True),
        ("easymigrate.core.registry", "import boto3", False),
        ("easymigrate.cli", "from easymigrate.core.pipeline import scan", True),
        ("easymigrate.cli", "from easymigrate import __version__", True),
        ("easymigrate.cli", "from easymigrate.ir import IRDocument", False),
        ("easymigrate.plugins.network.normalize", "from easymigrate.core.plugin import Snapshot", True),
        ("easymigrate.plugins.network.normalize", "from easymigrate.core.project import Project", False),
        ("easymigrate.plugins.network.normalize", "from easymigrate.differences.model import X", False),
        ("easymigrate.plugins.network.collect_aws", "from easymigrate.providers.aws.source import x", True),
        ("easymigrate.differences.engine", "from easymigrate.ir import IRDocument", True),
        ("easymigrate.differences.engine", "from easymigrate.core.project import Project", False),
        ("easymigrate.ir.network", "from easymigrate.core.project import Project", False),
    ],
)
def test_rule_checker(module, source, allowed):
    assert (violations(module, imported_modules(source)) == []) is allowed
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_dependency_rules.py -v`
Expected: all pass. This test is a guard, so there's no RED phase: current code already follows the rules. Run a quick sanity check that it fails on a real violation: temporarily add `import boto3` to `src/easymigrate/core/project.py`, run `uv run pytest tests/test_dependency_rules.py::test_source_tree_follows_dependency_rules`, confirm FAIL with `core must not import cloud SDKs`, then remove the line and confirm PASS.

- [ ] **Step 5: Run the full suite, lint and commit**

Run: `uv run pytest && uv run ruff format . && uv run ruff check .`
Expected: all pass, ruff clean.

```bash
git add pyproject.toml uv.lock tests/conftest.py tests/test_dependency_rules.py
git commit -m "$(cat <<'EOF'
test: dependency-rule guard, AWS test fixtures, boto3 and moto dependencies

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: AWS source (connection, identity, inventory)

**Files:**
- Create: `src/easymigrate/providers/aws/__init__.py` (empty), `src/easymigrate/providers/aws/source.py`, `tests/providers/aws/test_source.py`

**Interfaces:**
- Consumes: `aws_credentials` fixture (Task 1)
- Produces (in `easymigrate.providers.aws.source`):
  - `CLIENT_CONFIG: botocore.config.Config`, with adaptive retries and 10 attempts
  - `ACCESS_DENIED_CODES: frozenset[str]`
  - `class AwsConnectionError(Exception)`: the message is shown to the user as-is
  - `@dataclass(frozen=True) class AwsConnection(session: boto3.Session, account_id: str, region: str)` with `.client(service: str)`
  - `connect(*, region: str, profile: str | None = None, role_arn: str | None = None) -> AwsConnection`
  - `is_access_denied(exc: BaseException) -> bool`
  - `arn_resource_type(arn: str) -> str`: gives `"ec2:vpc"`, `"s3"`, `"lambda:function"`, or `"unknown"`
  - `@dataclass(frozen=True) class Inventory(arns_by_type: dict[str, list[str]], access_denied: bool = False)`
  - `inventory(connection: AwsConnection) -> Inventory`: keys and ARNs are sorted

- [ ] **Step 1: Write the failing tests**

`tests/providers/aws/test_source.py`:
```python
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from easymigrate.providers.aws import source
from easymigrate.providers.aws.source import (
    AwsConnectionError,
    Inventory,
    arn_resource_type,
    connect,
    inventory,
    is_access_denied,
)


def test_connect_returns_account_and_region(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")

    assert conn.account_id == "123456789012"
    assert conn.region == "ap-south-1"


def test_client_uses_region_and_adaptive_retries(aws_credentials):
    with mock_aws():
        client = connect(region="ap-south-1").client("ec2")

    assert client.meta.region_name == "ap-south-1"
    # botocore stores max_attempts=10 retries as 11 total attempts
    assert client.meta.config.retries == {"mode": "adaptive", "total_max_attempts": 11}


def test_connect_assumes_role(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1", role_arn="arn:aws:iam::123456789012:role/scanner")
        creds = conn.session.get_credentials().get_frozen_credentials()

    assert creds.access_key != "testing"
    assert creds.token


def test_unknown_region_is_rejected(aws_credentials):
    with pytest.raises(AwsConnectionError, match="unknown AWS region 'mars-1'"):
        connect(region="mars-1")


def test_missing_profile_is_rejected(aws_credentials):
    with pytest.raises(AwsConnectionError, match="AWS profile 'nope' not found"):
        connect(region="ap-south-1", profile="nope")


def test_missing_credentials_are_rejected(aws_credentials, monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    with pytest.raises(AwsConnectionError, match="no AWS credentials found"):
        connect(region="ap-south-1")


@pytest.mark.parametrize(
    "arn, expected",
    [
        ("arn:aws:ec2:ap-south-1:123456789012:vpc/vpc-1", "ec2:vpc"),
        ("arn:aws:ec2:ap-south-1:123456789012:security-group/sg-1", "ec2:security-group"),
        ("arn:aws:lambda:ap-south-1:123456789012:function:handler", "lambda:function"),
        ("arn:aws:rds:ap-south-1:123456789012:db:test-postgres-db", "rds:db"),
        ("arn:aws:s3:::my-bucket", "s3"),
        ("not-an-arn", "unknown"),
    ],
)
def test_arn_resource_type(arn, expected):
    assert arn_resource_type(arn) == expected


def test_inventory_groups_tagged_resources_by_type(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        ec2 = conn.client("ec2")
        for name in ("b", "a"):
            ec2.create_vpc(
                CidrBlock="10.0.0.0/16",
                TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": name}]}],
            )
        result = inventory(conn)

    assert result.access_denied is False
    assert list(result.arns_by_type) == ["ec2:vpc"]
    assert len(result.arns_by_type["ec2:vpc"]) == 2
    assert result.arns_by_type["ec2:vpc"] == sorted(result.arns_by_type["ec2:vpc"])


def test_inventory_reports_access_denied(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        client = conn.session.client("resourcegroupstaggingapi", region_name="ap-south-1")
        stubber = Stubber(client)
        stubber.add_client_error("get_resources", service_error_code="AccessDeniedException")
        monkeypatch.setattr(source.AwsConnection, "client", lambda self, service: client)
        with stubber:
            result = inventory(conn)

    assert result == Inventory(arns_by_type={}, access_denied=True)


def test_is_access_denied_recognises_codes():
    from botocore.exceptions import ClientError

    def error(code: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": "m"}}, "DescribeVpcs")

    assert is_access_denied(error("UnauthorizedOperation"))
    assert is_access_denied(error("AccessDenied"))
    assert is_access_denied(error("AccessDeniedException"))
    assert not is_access_denied(error("Throttling"))
    assert not is_access_denied(ValueError("x"))
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/providers/aws/test_source.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.providers.aws'`.

- [ ] **Step 3: Implement**

`src/easymigrate/providers/aws/__init__.py`: empty file.

`src/easymigrate/providers/aws/source.py`:
```python
"""AWS connection, account identity and tag-based resource inventory (spec D7, §6.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound

CLIENT_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 10})
ACCESS_DENIED_CODES = frozenset({"UnauthorizedOperation", "AccessDenied", "AccessDeniedException"})


class AwsConnectionError(Exception):
    """Credentials, role or region problem. The message is shown to the user as-is."""


@dataclass(frozen=True)
class AwsConnection:
    session: boto3.Session
    account_id: str
    region: str

    def client(self, service: str) -> Any:
        return self.session.client(service, region_name=self.region, config=CLIENT_CONFIG)


def connect(*, region: str, profile: str | None = None, role_arn: str | None = None) -> AwsConnection:
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
    except ProfileNotFound:
        raise AwsConnectionError(f"AWS profile {profile!r} not found") from None
    known_regions = set(session.get_available_regions("ec2")) | set(
        session.get_available_regions("ec2", partition_name="aws-cn")
    )
    if region not in known_regions:
        raise AwsConnectionError(f"unknown AWS region {region!r}")
    try:
        if role_arn:
            sts = session.client("sts", region_name=region, config=CLIENT_CONFIG)
            credentials = sts.assume_role(RoleArn=role_arn, RoleSessionName="easymigrate-scan")[
                "Credentials"
            ]
            session = boto3.Session(
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
                region_name=region,
            )
        identity = session.client("sts", region_name=region, config=CLIENT_CONFIG).get_caller_identity()
    except NoCredentialsError:
        raise AwsConnectionError(
            "no AWS credentials found; pass --aws-profile or --aws-role-arn, "
            "or configure the AWS CLI"
        ) from None
    except ClientError as exc:
        message = exc.response.get("Error", {}).get("Message") or str(exc)
        raise AwsConnectionError(f"AWS rejected the credentials: {message}") from None
    except BotoCoreError as exc:
        raise AwsConnectionError(f"could not connect to AWS: {exc}") from None
    return AwsConnection(session=session, account_id=identity["Account"], region=region)


def is_access_denied(exc: BaseException) -> bool:
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") in ACCESS_DENIED_CODES
    )


def arn_resource_type(arn: str) -> str:
    parts = arn.split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        return "unknown"
    service, resource = parts[2], parts[5]
    for separator in ("/", ":"):
        if separator in resource:
            return f"{service}:{resource.split(separator, 1)[0]}"
    return service


@dataclass(frozen=True)
class Inventory:
    arns_by_type: dict[str, list[str]] = field(default_factory=dict)
    access_denied: bool = False


def inventory(connection: AwsConnection) -> Inventory:
    """Every tagged resource in the region, grouped by type. Untagged resources are invisible
    to this API; callers must say so to the user."""
    client = connection.client("resourcegroupstaggingapi")
    by_type: dict[str, list[str]] = {}
    try:
        for page in client.get_paginator("get_resources").paginate():
            for item in page.get("ResourceTagMappingList", []):
                arn = item["ResourceARN"]
                by_type.setdefault(arn_resource_type(arn), []).append(arn)
    except ClientError as exc:
        if is_access_denied(exc):
            return Inventory(arns_by_type={}, access_denied=True)
        raise
    return Inventory(arns_by_type={key: sorted(by_type[key]) for key in sorted(by_type)})
```

- [ ] **Step 4: Run and confirm it passes**

Run: `uv run pytest tests/providers/aws/test_source.py -v && uv run pytest tests/test_dependency_rules.py`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`

```bash
git add src/easymigrate/providers/aws tests/providers/aws
git commit -m "$(cat <<'EOF'
feat(aws): connection with profile or role, account identity and tag inventory

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Plugin interface (snapshot and contexts)

**Files:**
- Create: `src/easymigrate/core/plugin.py`, `tests/core/test_plugin.py`

**Interfaces:**
- Consumes: `easymigrate.ir.base.Finding`, `IntentObject`
- Produces (in `easymigrate.core.plugin`; plugins may import this module):
  - `SCAN_FILE = "_scan.json"`
  - `class UnmodelledData(Exception)`
  - `class Snapshot(files: dict[str, Any])` with:
    - `.get(name) -> Any | None`
    - `.names() -> list[str]` (sorted)
    - `Snapshot.from_dir(path: Path) -> Snapshot`: loads every `*.json` except `_scan.json`; the name is the filename without `.json`, e.g. `"network.vpcs"`
  - `@dataclass class CollectContext(plugin: str, findings: list[Finding] = [])` with `.access_denied(api: str, iam_action: str) -> None`, which is de-duplicated per IAM action
  - `@dataclass class NormalizeContext(account_id: str, region: str, strict: bool = False, findings: list[Finding] = [], objects: dict[str, IntentObject] = {})` with:
    - `.lookup(object_id) -> IntentObject` (raises `KeyError`)
    - `.info(code, message, subject=None, suggestion=None)`
    - `.unmodelled(message, subject=None)`: raises `UnmodelledData` when strict, otherwise records `info / INTERNAL-UNMODELLED`
  - `class Plugin(Protocol)` with:
    - `name: str`
    - `aws_resource_types: frozenset[str]` (values in `arn_resource_type` form, such as `"ec2:vpc"`)
    - `collect_aws(connection: Any, ctx: CollectContext) -> dict[str, Any]`: keys are snapshot names such as `"network.vpcs"`
    - `normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_plugin.py`:
```python
import json

import pytest

from easymigrate.core.plugin import (
    SCAN_FILE,
    CollectContext,
    NormalizeContext,
    Snapshot,
    UnmodelledData,
)
from easymigrate.ir import SourceRef
from easymigrate.ir.network import Network


def test_snapshot_from_dir_skips_scan_file_and_non_json(tmp_path):
    (tmp_path / "network.vpcs.json").write_text(json.dumps([{"VpcId": "vpc-1"}]))
    (tmp_path / "network.subnets.json").write_text("[]")
    (tmp_path / SCAN_FILE).write_text("{}")
    (tmp_path / "notes.txt").write_text("ignore me")

    snapshot = Snapshot.from_dir(tmp_path)

    assert snapshot.names() == ["network.subnets", "network.vpcs"]
    assert snapshot.get("network.vpcs") == [{"VpcId": "vpc-1"}]
    assert snapshot.get("network.nat_gateways") is None


def test_snapshot_from_missing_dir_is_empty(tmp_path):
    assert Snapshot.from_dir(tmp_path / "absent").names() == []


def test_collect_context_records_access_denied_once_per_action():
    ctx = CollectContext(plugin="network")

    ctx.access_denied("DescribeVpcs", "ec2:DescribeVpcs")
    ctx.access_denied("DescribeVpcs", "ec2:DescribeVpcs")
    ctx.access_denied("DescribeSubnets", "ec2:DescribeSubnets")

    assert [f.suggestion for f in ctx.findings] == [
        "Grant ec2:DescribeVpcs",
        "Grant ec2:DescribeSubnets",
    ]
    first = ctx.findings[0]
    assert first.level == "scan_gap"
    assert first.code == "SCAN-ACCESS-DENIED"
    assert first.message == "Access denied calling DescribeVpcs; network data from it is missing."


def test_normalize_context_unmodelled_non_strict_records_info():
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1")

    ctx.unmodelled("IPv6-only subnet has no IPv4 CIDR", subject="subnet-1")

    assert len(ctx.findings) == 1
    finding = ctx.findings[0]
    assert (finding.level, finding.code, finding.subject) == (
        "info",
        "INTERNAL-UNMODELLED",
        "subnet-1",
    )
    assert finding.message == "IPv6-only subnet has no IPv4 CIDR"


def test_normalize_context_unmodelled_strict_raises():
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)

    with pytest.raises(UnmodelledData, match="IPv6-only subnet"):
        ctx.unmodelled("IPv6-only subnet has no IPv4 CIDR", subject="subnet-1")
    assert ctx.findings == []


def test_normalize_context_info_and_lookup():
    net = Network(
        id="network:main",
        name="main",
        source=SourceRef(cloud="aws", type="vpc", id="vpc-1", region="ap-south-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )
    ctx = NormalizeContext(account_id="1", region="r", objects={net.id: net})

    ctx.info("NET-DEFAULT-VPC", "Default VPC", subject="network:main")

    assert ctx.lookup("network:main") is net
    assert ctx.findings[0].code == "NET-DEFAULT-VPC"
    with pytest.raises(KeyError):
        ctx.lookup("network:missing")
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/core/test_plugin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.core.plugin'`.

- [ ] **Step 3: Implement**

`src/easymigrate/core/plugin.py`:
```python
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
                files[file.name.removesuffix(".json")] = json.loads(file.read_text(encoding="utf-8"))
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
            Finding(level="info", code=code, message=message, subject=subject, suggestion=suggestion)
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
```

- [ ] **Step 4: Run and confirm it passes**

Run: `uv run pytest tests/core/test_plugin.py tests/test_dependency_rules.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`

```bash
git add src/easymigrate/core/plugin.py tests/core/test_plugin.py
git commit -m "$(cat <<'EOF'
feat(core): plugin interface with snapshot, collect and normalize contexts

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Network collector

**Files:**
- Create: `src/easymigrate/plugins/__init__.py` (empty), `src/easymigrate/plugins/network/__init__.py` (empty for now; Task 8 adds `NetworkPlugin`), `src/easymigrate/plugins/network/collect_aws.py`, `tests/plugins/network/test_collect_aws.py`

**Interfaces:**
- Consumes: `AwsConnection`, `connect`, `is_access_denied` (Task 2); `CollectContext` (Task 3); `aws_credentials` fixture (Task 1)
- Produces:
  - `easymigrate.plugins.network.collect_aws.collect(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any]`, which returns snapshot names mapped to data:
    - `network.vpcs`, `network.subnets`, `network.route_tables`, `network.internet_gateways`, `network.nat_gateways`, `network.security_groups`, `network.network_acls`: each a list of raw AWS dicts, sorted by their id field
    - `network.vpc_attributes`: a `{vpc_id: {"enableDnsSupport": bool, "enableDnsHostnames": bool}}` dict
  - A file whose API was denied is left out, and the denial is recorded in `ctx`.
  - `NETWORK_SNAPSHOT_FILES: tuple[str, ...]`: all 8 names above

- [ ] **Step 1: Write the failing tests**

`tests/plugins/network/test_collect_aws.py`:
```python
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from easymigrate.core.plugin import CollectContext
from easymigrate.plugins.network.collect_aws import NETWORK_SNAPSHOT_FILES, collect
from easymigrate.providers.aws import source
from easymigrate.providers.aws.source import connect


def _build_network(ec2):
    vpc = ec2.create_vpc(
        CidrBlock="10.0.0.0/16",
        TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
    )["Vpc"]["VpcId"]
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value": True})
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")[
        "Subnet"
    ]["SubnetId"]
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ec2.create_nat_gateway(SubnetId=subnet, AllocationId=eip)
    ec2.create_security_group(GroupName="web", Description="web", VpcId=vpc)
    ec2.create_network_acl(VpcId=vpc)
    return vpc


def _deny(connection_cls, monkeypatch, operation: str, code: str = "UnauthorizedOperation"):
    original = connection_cls.client

    def client(self, service):
        c = original(self, service)

        def raise_error(**kwargs):
            raise ClientError({"Error": {"Code": code, "Message": "denied"}}, operation)

        c.meta.events.register(f"before-call.ec2.{operation}", raise_error)
        return c

    monkeypatch.setattr(connection_cls, "client", client)


def test_collects_every_network_file(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        vpc = _build_network(conn.client("ec2"))
        ctx = CollectContext(plugin="network")
        files = collect(conn, ctx)

    assert sorted(files) == sorted(NETWORK_SNAPSHOT_FILES)
    assert ctx.findings == []
    assert vpc in [v["VpcId"] for v in files["network.vpcs"]]
    assert files["network.vpc_attributes"][vpc] == {
        "enableDnsSupport": True,
        "enableDnsHostnames": True,
    }
    assert len(files["network.nat_gateways"]) == 1
    assert "web" in [g["GroupName"] for g in files["network.security_groups"]]


def test_lists_are_sorted_by_id(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        ec2 = conn.client("ec2")
        for cidr in ("10.1.0.0/16", "10.2.0.0/16", "10.3.0.0/16"):
            ec2.create_vpc(CidrBlock=cidr)
        files = collect(conn, CollectContext(plugin="network"))

    ids = [v["VpcId"] for v in files["network.vpcs"]]
    assert ids == sorted(ids)


def test_access_denied_skips_only_that_file(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        _build_network(conn.client("ec2"))
        _deny(source.AwsConnection, monkeypatch, "DescribeSubnets")
        ctx = CollectContext(plugin="network")
        files = collect(conn, ctx)

    assert "network.subnets" not in files
    assert "network.vpcs" in files
    assert [f.suggestion for f in ctx.findings] == ["Grant ec2:DescribeSubnets"]


def test_denied_vpc_attributes_drops_the_whole_attribute_file(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        _build_network(conn.client("ec2"))
        _deny(source.AwsConnection, monkeypatch, "DescribeVpcAttribute", code="AccessDenied")
        ctx = CollectContext(plugin="network")
        files = collect(conn, ctx)

    assert "network.vpc_attributes" not in files
    assert "network.vpcs" in files
    assert [f.suggestion for f in ctx.findings] == ["Grant ec2:DescribeVpcAttribute"]


def test_other_errors_propagate(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        _deny(source.AwsConnection, monkeypatch, "DescribeVpcs", code="InvalidParameterValue")

        with pytest.raises(ClientError, match="InvalidParameterValue"):
            collect(conn, CollectContext(plugin="network"))
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/plugins/network/test_collect_aws.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.plugins'`.

- [ ] **Step 3: Implement**

`src/easymigrate/plugins/__init__.py` and `src/easymigrate/plugins/network/__init__.py`: empty files.

`src/easymigrate/plugins/network/collect_aws.py`:
```python
"""Collect raw AWS networking API responses for one region (spec §2.1, collector)."""

from typing import Any

from easymigrate.core.plugin import CollectContext
from easymigrate.providers.aws.source import AwsConnection, is_access_denied

# (snapshot name, boto3 method, response list key, id field, IAM action)
_LISTS = (
    ("network.vpcs", "describe_vpcs", "Vpcs", "VpcId", "ec2:DescribeVpcs"),
    ("network.subnets", "describe_subnets", "Subnets", "SubnetId", "ec2:DescribeSubnets"),
    (
        "network.route_tables",
        "describe_route_tables",
        "RouteTables",
        "RouteTableId",
        "ec2:DescribeRouteTables",
    ),
    (
        "network.internet_gateways",
        "describe_internet_gateways",
        "InternetGateways",
        "InternetGatewayId",
        "ec2:DescribeInternetGateways",
    ),
    (
        "network.nat_gateways",
        "describe_nat_gateways",
        "NatGateways",
        "NatGatewayId",
        "ec2:DescribeNatGateways",
    ),
    (
        "network.security_groups",
        "describe_security_groups",
        "SecurityGroups",
        "GroupId",
        "ec2:DescribeSecurityGroups",
    ),
    (
        "network.network_acls",
        "describe_network_acls",
        "NetworkAcls",
        "NetworkAclId",
        "ec2:DescribeNetworkAcls",
    ),
)
_VPC_ATTRIBUTES = (("enableDnsSupport", "EnableDnsSupport"), ("enableDnsHostnames", "EnableDnsHostnames"))

NETWORK_SNAPSHOT_FILES = (*(entry[0] for entry in _LISTS), "network.vpc_attributes")


def _api_name(method: str) -> str:
    return "".join(part.title() for part in method.split("_"))


def collect(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any]:
    ec2 = connection.client("ec2")
    files: dict[str, Any] = {}
    for name, method, key, id_field, action in _LISTS:
        items: list[dict[str, Any]] = []
        try:
            for page in ec2.get_paginator(method).paginate():
                items.extend(page.get(key, []))
        except Exception as exc:
            if is_access_denied(exc):
                ctx.access_denied(_api_name(method), action)
                continue
            raise
        files[name] = sorted(items, key=lambda item: item[id_field])

    if "network.vpcs" in files:
        attributes = _collect_vpc_attributes(ec2, files["network.vpcs"], ctx)
        if attributes is not None:
            files["network.vpc_attributes"] = attributes
    return files


def _collect_vpc_attributes(
    ec2: Any, vpcs: list[dict[str, Any]], ctx: CollectContext
) -> dict[str, dict[str, bool]] | None:
    attributes: dict[str, dict[str, bool]] = {}
    for vpc in vpcs:
        vpc_id = vpc["VpcId"]
        entry: dict[str, bool] = {}
        for attribute, response_key in _VPC_ATTRIBUTES:
            try:
                response = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute=attribute)
            except Exception as exc:
                if is_access_denied(exc):
                    ctx.access_denied("DescribeVpcAttribute", "ec2:DescribeVpcAttribute")
                    return None
                raise
            entry[attribute] = bool(response[response_key]["Value"])
        attributes[vpc_id] = entry
    return attributes
```

- [ ] **Step 4: Run and confirm it passes**

Run: `uv run pytest tests/plugins/network/test_collect_aws.py tests/test_dependency_rules.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`

```bash
git add src/easymigrate/plugins tests/plugins
git commit -m "$(cat <<'EOF'
feat(network): AWS networking collector with paginated lists and scan-gap handling

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: AWS value helpers

**Files:**
- Create: `src/easymigrate/plugins/network/aws_values.py`, `tests/plugins/network/test_aws_values.py`

**Interfaces:**
- Consumes: nothing
- Produces (in `easymigrate.plugins.network.aws_values`; pure functions):
  - `protocol(value: str | int) -> str`: `-1`→`all`, `6`/`tcp`→`tcp`, `17`/`udp`→`udp`, `1`/`icmp`→`icmp`, `58`/`icmpv6`→`icmpv6`; any other value is lowercased and kept
  - `ports(protocol_name: str, from_port: int | None, to_port: int | None) -> list[str]`: only for tcp/udp. `0-65535`, `None` or negative values give `[]`; otherwise `["443"]` or `["8000-8080"]`
  - `icmp(protocol_name: str, type_value: int | None, code_value: int | None) -> tuple[int | None, int | None]`: only for icmp/icmpv6. `-1` or `None` gives `None`, and there is no code without a type
  - `cidr(value: str) -> str`: canonical form via `str(ipaddress.ip_network(value, strict=False))`
  - `tags(resource: dict) -> dict[str, str]`
  - `name_tag(resource: dict) -> str | None`: the `Name` tag stripped of surrounding whitespace, or `None` when it is empty or missing
  - `associated_cidrs(associations: list[dict] | None, cidr_key: str, state_key: str) -> list[str]`: canonical CIDRs whose `<state_key>.State == "associated"`

- [ ] **Step 1: Write the failing tests**

`tests/plugins/network/test_aws_values.py`:
```python
import pytest

from easymigrate.plugins.network.aws_values import (
    associated_cidrs,
    cidr,
    icmp,
    name_tag,
    ports,
    protocol,
    tags,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("-1", "all"),
        (-1, "all"),
        ("6", "tcp"),
        ("tcp", "tcp"),
        ("TCP", "tcp"),
        ("17", "udp"),
        ("udp", "udp"),
        ("1", "icmp"),
        ("icmp", "icmp"),
        ("58", "icmpv6"),
        ("icmpv6", "icmpv6"),
        ("50", "50"),
    ],
)
def test_protocol(raw, expected):
    assert protocol(raw) == expected


@pytest.mark.parametrize(
    "proto, from_port, to_port, expected",
    [
        ("tcp", 443, 443, ["443"]),
        ("udp", 8000, 8080, ["8000-8080"]),
        ("tcp", 0, 65535, []),
        ("tcp", None, None, []),
        ("tcp", -1, -1, []),
        ("tcp", 22, None, ["22"]),
        ("icmp", 8, 0, []),
        ("all", None, None, []),
        ("50", 1, 2, []),
    ],
)
def test_ports(proto, from_port, to_port, expected):
    assert ports(proto, from_port, to_port) == expected


@pytest.mark.parametrize(
    "proto, type_value, code_value, expected",
    [
        ("icmp", 8, 0, (8, 0)),
        ("icmp", 8, -1, (8, None)),
        ("icmp", -1, -1, (None, None)),
        ("icmp", None, None, (None, None)),
        ("icmp", -1, 3, (None, None)),
        ("icmpv6", 128, None, (128, None)),
        ("tcp", 443, 443, (None, None)),
    ],
)
def test_icmp(proto, type_value, code_value, expected):
    assert icmp(proto, type_value, code_value) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("10.0.0.0/16", "10.0.0.0/16"),
        ("10.0.1.5/24", "10.0.1.0/24"),
        ("2600:1F18:ABCD:1200:0000::/56", "2600:1f18:abcd:1200::/56"),
        ("0.0.0.0/0", "0.0.0.0/0"),
    ],
)
def test_cidr_is_canonical(raw, expected):
    assert cidr(raw) == expected


def test_tags_and_name_tag():
    resource = {"Tags": [{"Key": "Name", "Value": "  web  "}, {"Key": "env", "Value": "prod"}]}

    assert tags(resource) == {"Name": "  web  ", "env": "prod"}
    assert name_tag(resource) == "web"
    assert name_tag({"Tags": [{"Key": "Name", "Value": "   "}]}) is None
    assert name_tag({}) is None
    assert tags({"Tags": None}) == {}


def test_associated_cidrs_keeps_only_associated_and_canonicalises():
    associations = [
        {"CidrBlock": "10.0.0.0/16", "CidrBlockState": {"State": "associated"}},
        {"CidrBlock": "10.1.0.0/16", "CidrBlockState": {"State": "disassociated"}},
        {"CidrBlock": "10.2.0.5/16", "CidrBlockState": {"State": "associated"}},
    ]

    assert associated_cidrs(associations, "CidrBlock", "CidrBlockState") == [
        "10.0.0.0/16",
        "10.2.0.0/16",
    ]
    assert associated_cidrs(None, "CidrBlock", "CidrBlockState") == []
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/plugins/network/test_aws_values.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.plugins.network.aws_values'`.

- [ ] **Step 3: Implement**

`src/easymigrate/plugins/network/aws_values.py`:
```python
"""Pure helpers that turn raw AWS field values into IR values (plan 2 Global Constraints)."""

import ipaddress
from typing import Any

_PROTOCOLS = {
    "-1": "all",
    "6": "tcp",
    "tcp": "tcp",
    "17": "udp",
    "udp": "udp",
    "1": "icmp",
    "icmp": "icmp",
    "58": "icmpv6",
    "icmpv6": "icmpv6",
}
_PORTED = ("tcp", "udp")
_ICMP = ("icmp", "icmpv6")


def protocol(value: str | int) -> str:
    text = str(value).strip().lower()
    return _PROTOCOLS.get(text, text)


def ports(protocol_name: str, from_port: int | None, to_port: int | None) -> list[str]:
    if protocol_name not in _PORTED or from_port is None or from_port < 0:
        return []
    end = from_port if to_port is None or to_port < 0 else to_port
    if from_port == 0 and end == 65535:
        return []
    return [str(from_port)] if from_port == end else [f"{from_port}-{end}"]


def icmp(
    protocol_name: str, type_value: int | None, code_value: int | None
) -> tuple[int | None, int | None]:
    if protocol_name not in _ICMP or type_value is None or type_value < 0:
        return None, None
    if code_value is None or code_value < 0:
        return type_value, None
    return type_value, code_value


def cidr(value: str) -> str:
    return str(ipaddress.ip_network(value, strict=False))


def tags(resource: dict[str, Any]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in resource.get("Tags") or []}


def name_tag(resource: dict[str, Any]) -> str | None:
    return tags(resource).get("Name", "").strip() or None


def associated_cidrs(
    associations: list[dict[str, Any]] | None, cidr_key: str, state_key: str
) -> list[str]:
    return [
        cidr(association[cidr_key])
        for association in associations or []
        if (association.get(state_key) or {}).get("State") == "associated"
    ]
```

- [ ] **Step 4: Run and confirm it passes**

Run: `uv run pytest tests/plugins/network/test_aws_values.py tests/test_dependency_rules.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`

```bash
git add src/easymigrate/plugins/network/aws_values.py tests/plugins/network/test_aws_values.py
git commit -m "$(cat <<'EOF'
feat(network): AWS value helpers for protocols, ports, ICMP, CIDRs and tags

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Topology normalizer (networks, subnets, NAT gateways, custom routes)

**Files:**
- Create: `tests/plugins/network/aws_builders.py`, `src/easymigrate/plugins/network/normalize_topology.py`, `tests/plugins/network/test_normalize_topology.py`

**Interfaces:**
- Consumes: `Snapshot`, `NormalizeContext`, `UnmodelledData` (Task 3); `aws_values` (Task 5); `allocate_ids`, `clean_labels` (plan 1); `Network`, `Subnet`, `Nat`, `CustomRoute`, `IRDocument`, `SourceRef`
- Produces (in `easymigrate.plugins.network.normalize_topology`):
  - `@dataclass class Topology(networks: list[Network], subnets: list[Subnet], nats: list[Nat], routes: list[CustomRoute], network_ids: dict[str, str], subnet_ids: dict[str, str])`: the two maps go from native id to IR id, for VPCs and subnets
  - `build_topology(snapshot: Snapshot, ctx: NormalizeContext) -> Topology`
- Produces (test helper `tests/plugins/network/aws_builders.py`): `vpc`, `subnet`, `route_table`, `route`, `nat_gateway`, `security_group`, `permission`, `network_acl`, `acl_entry`, `snapshot`. Every call returns AWS-shaped dicts, and `snapshot(...)` returns a `Snapshot`.

**Rules implemented** (spec §3.3 plus plan 2 Global Constraints):
- **Networks:**
  - `cidr_blocks` are the associated entries of `CidrBlockAssociationSet`, falling back to `CidrBlock`. `ipv6_cidr_blocks` are the associated entries of `Ipv6CidrBlockAssociationSet`.
  - DNS settings come from `network.vpc_attributes`. If that data is missing, AWS defaults apply (support `True`, hostnames `False`) and `info / NET-DNS-ATTRIBUTES-UNKNOWN` is recorded.
  - `IsDefault` → `info / NET-DEFAULT-VPC`.
- **A subnet's route table** is its explicitly associated table, otherwise its VPC's main table.
- **Subnet `internet`**, from the active `0.0.0.0/0` route:
  - `GatewayId` starting `igw-` → `public`
  - `NatGatewayId` → `private_nat`
  - anything else, or no such route → `isolated`
- **Route tables unreadable** (the file is missing) → one `info / NET-ROUTES-UNKNOWN` finding, and every subnet is `isolated`.
- **Subnet with no `CidrBlock`** (IPv6-only) → `ctx.unmodelled(...)`, and the subnet is skipped. Subnets whose VPC isn't in the snapshot are skipped silently, because the scan gap is already recorded.
- **NAT gateways:** only those with `State` `pending` or `available`. `serves_subnets` lists the subnets whose default route targets that NAT.
- **Custom routes:** every route except:
  - `GatewayId == "local"`
  - `0.0.0.0/0` via `igw-*` or a NAT gateway
  - `::/0` via `igw-*` or an egress-only gateway

  `target_type` rules:
  - `VpcPeeringConnectionId` → `peering`
  - `TransitGatewayId` → `tgw`
  - `NetworkInterfaceId` or `InstanceId` → `eni`
  - `GatewayId` starting `vgw-` → `vpn`
  - anything else → `other`

  Other fields:
  - `destination`: the canonical CIDR, or `DestinationPrefixListId` as-is
  - `subnets`: every subnet using that table
  - native id: `"<RouteTableId>:<destination>"`
  - name: `"<table Name tag or RouteTableId> <destination>"`
- **Names** are the `Name` tag, otherwise the native id. `labels = clean_labels(tags)`. Ids come from `allocate_ids` per kind.

- [ ] **Step 1: Write the test builders**

`tests/plugins/network/aws_builders.py`:
```python
"""Tiny builders for AWS-shaped API dicts, used by normalizer unit tests."""

from typing import Any

from easymigrate.core.plugin import Snapshot

OWNER = "123456789012"


def _tags(name: str | None, extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    tags = dict(extra or {})
    if name is not None:
        tags["Name"] = name
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def vpc(
    vpc_id: str,
    cidr: str = "10.0.0.0/16",
    *,
    name: str | None = None,
    is_default: bool = False,
    extra_cidrs: tuple[str, ...] = (),
    ipv6: tuple[str, ...] = (),
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "VpcId": vpc_id,
        "CidrBlock": cidr,
        "CidrBlockAssociationSet": [
            {"CidrBlock": block, "CidrBlockState": {"State": "associated"}}
            for block in (cidr, *extra_cidrs)
        ],
        "Ipv6CidrBlockAssociationSet": [
            {"Ipv6CidrBlock": block, "Ipv6CidrBlockState": {"State": "associated"}}
            for block in ipv6
        ],
        "IsDefault": is_default,
        "OwnerId": OWNER,
        "State": "available",
        "Tags": _tags(name, tags),
    }


def subnet(
    subnet_id: str,
    vpc_id: str,
    cidr: str | None = "10.0.1.0/24",
    *,
    zone: str = "ap-south-1a",
    name: str | None = None,
    map_public_ip: bool = False,
    ipv6: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "SubnetId": subnet_id,
        "VpcId": vpc_id,
        "AvailabilityZone": zone,
        "MapPublicIpOnLaunch": map_public_ip,
        "Ipv6CidrBlockAssociationSet": (
            [{"Ipv6CidrBlock": ipv6, "Ipv6CidrBlockState": {"State": "associated"}}] if ipv6 else []
        ),
        "State": "available",
        "Tags": _tags(name),
    }
    if cidr is not None:
        data["CidrBlock"] = cidr
    return data


def route(destination: str, **target: str) -> dict[str, Any]:
    key = "DestinationPrefixListId" if destination.startswith("pl-") else (
        "DestinationIpv6CidrBlock" if ":" in destination else "DestinationCidrBlock"
    )
    return {key: destination, "State": "active", "Origin": "CreateRoute", **target}


def route_table(
    table_id: str,
    vpc_id: str,
    routes: list[dict[str, Any]] = (),
    *,
    subnets: tuple[str, ...] = (),
    main: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    associations = [
        {"Main": False, "SubnetId": sid, "RouteTableId": table_id,
         "AssociationState": {"State": "associated"}}
        for sid in subnets
    ]
    if main:
        associations.append(
            {"Main": True, "RouteTableId": table_id, "AssociationState": {"State": "associated"}}
        )
    local = {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"}
    return {
        "RouteTableId": table_id,
        "VpcId": vpc_id,
        "Associations": associations,
        "Routes": [local, *routes],
        "Tags": _tags(name),
    }


def nat_gateway(
    nat_id: str, vpc_id: str, subnet_id: str, *, state: str = "available", name: str | None = None
) -> dict[str, Any]:
    return {
        "NatGatewayId": nat_id,
        "VpcId": vpc_id,
        "SubnetId": subnet_id,
        "State": state,
        "Tags": _tags(name),
    }


def permission(
    protocol: str,
    from_port: int | None = None,
    to_port: int | None = None,
    *,
    cidrs: tuple[str, ...] = (),
    ipv6: tuple[str, ...] = (),
    groups: tuple[dict[str, str], ...] = (),
    prefix_lists: tuple[str, ...] = (),
    description: str | None = None,
) -> dict[str, Any]:
    def described(entry: dict[str, Any]) -> dict[str, Any]:
        return {**entry, "Description": description} if description else entry

    data: dict[str, Any] = {
        "IpProtocol": protocol,
        "IpRanges": [described({"CidrIp": c}) for c in cidrs],
        "Ipv6Ranges": [described({"CidrIpv6": c}) for c in ipv6],
        "UserIdGroupPairs": [described(dict(g)) for g in groups],
        "PrefixListIds": [described({"PrefixListId": p}) for p in prefix_lists],
    }
    if from_port is not None:
        data["FromPort"] = from_port
    if to_port is not None:
        data["ToPort"] = to_port
    return data


ALLOW_ALL_EGRESS = permission("-1", cidrs=("0.0.0.0/0",))


def security_group(
    group_id: str,
    vpc_id: str,
    group_name: str,
    *,
    ingress: list[dict[str, Any]] = (),
    egress: list[dict[str, Any]] | None = None,
    name: str | None = None,
    owner: str = OWNER,
) -> dict[str, Any]:
    return {
        "GroupId": group_id,
        "GroupName": group_name,
        "VpcId": vpc_id,
        "OwnerId": owner,
        "Description": group_name,
        "IpPermissions": list(ingress),
        "IpPermissionsEgress": [ALLOW_ALL_EGRESS] if egress is None else list(egress),
        "Tags": _tags(name),
    }


def acl_entry(
    number: int,
    *,
    protocol: str = "-1",
    action: str = "allow",
    egress: bool = False,
    cidr: str = "0.0.0.0/0",
    ports: tuple[int, int] | None = None,
    icmp: tuple[int, int] | None = None,
) -> dict[str, Any]:
    key = "Ipv6CidrBlock" if ":" in cidr else "CidrBlock"
    data: dict[str, Any] = {
        "RuleNumber": number,
        "Protocol": protocol,
        "RuleAction": action,
        "Egress": egress,
        key: cidr,
    }
    if ports is not None:
        data["PortRange"] = {"From": ports[0], "To": ports[1]}
    if icmp is not None:
        data["IcmpTypeCode"] = {"Type": icmp[0], "Code": icmp[1]}
    return data


DEFAULT_ACL_ENTRIES = [
    acl_entry(100, egress=False),
    acl_entry(32767, action="deny", egress=False),
    acl_entry(100, egress=True),
    acl_entry(32767, action="deny", egress=True),
]


def network_acl(
    acl_id: str,
    vpc_id: str,
    entries: list[dict[str, Any]],
    *,
    subnets: tuple[str, ...] = (),
    is_default: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "NetworkAclId": acl_id,
        "VpcId": vpc_id,
        "IsDefault": is_default,
        "Entries": entries,
        "Associations": [{"NetworkAclId": acl_id, "SubnetId": sid} for sid in subnets],
        "Tags": _tags(name),
    }


def snapshot(
    *,
    vpcs: list[dict[str, Any]] = (),
    subnets: list[dict[str, Any]] = (),
    route_tables: list[dict[str, Any]] | None = (),
    nat_gateways: list[dict[str, Any]] = (),
    security_groups: list[dict[str, Any]] = (),
    network_acls: list[dict[str, Any]] = (),
    vpc_attributes: dict[str, dict[str, bool]] | None = None,
    omit: tuple[str, ...] = (),
) -> Snapshot:
    """Build a Snapshot. `vpc_attributes` defaults to support on / hostnames on for every VPC.
    Pass `route_tables=None` or `omit=("network.vpc_attributes",)` to simulate scan gaps."""
    if vpc_attributes is None:
        vpc_attributes = {
            v["VpcId"]: {"enableDnsSupport": True, "enableDnsHostnames": True} for v in vpcs
        }
    files: dict[str, Any] = {
        "network.vpcs": list(vpcs),
        "network.subnets": list(subnets),
        "network.nat_gateways": list(nat_gateways),
        "network.security_groups": list(security_groups),
        "network.network_acls": list(network_acls),
        "network.internet_gateways": [],
        "network.vpc_attributes": vpc_attributes,
    }
    if route_tables is not None:
        files["network.route_tables"] = list(route_tables)
    for name in omit:
        files.pop(name, None)
    return Snapshot(files)
```

- [ ] **Step 2: Write the failing tests**

`tests/plugins/network/test_normalize_topology.py`:
```python
import pytest

from easymigrate.core.plugin import NormalizeContext, UnmodelledData
from easymigrate.ir import IRDocument
from easymigrate.plugins.network.normalize_topology import build_topology
from tests.plugins.network import aws_builders as aws


def ctx(strict: bool = True) -> NormalizeContext:
    return NormalizeContext(account_id="123456789012", region="ap-south-1", strict=strict)


def as_document(topology) -> IRDocument:
    objects = [*topology.networks, *topology.subnets, *topology.nats, *topology.routes]
    return IRDocument(source={"cloud": "aws"}, objects=objects)


def test_public_subnet_via_explicit_route_table():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", name="main", tags={"Cost Center": "R&D"})],
        subnets=[aws.subnet("subnet-1", "vpc-1", name="web", map_public_ip=True)],
        route_tables=[
            aws.route_table(
                "rtb-1", "vpc-1", [aws.route("0.0.0.0/0", GatewayId="igw-1")], subnets=("subnet-1",)
            ),
            aws.route_table("rtb-main", "vpc-1", main=True),
        ],
    )
    c = ctx()

    topo = build_topology(snap, c)

    (network,) = topo.networks
    assert network.id == "network:main"
    assert network.cidr_blocks == ["10.0.0.0/16"]
    assert (network.dns_resolution, network.dns_hostnames) == (True, True)
    assert network.labels == {"cost_center": "r_d", "name": "main"}
    assert network.source.id == "vpc-1" and network.source.type == "vpc"
    (sub,) = topo.subnets
    assert (sub.id, sub.network, sub.cidr, sub.zone) == (
        "subnet:web",
        "network:main",
        "10.0.1.0/24",
        "ap-south-1a",
    )
    assert sub.internet == "public" and sub.map_public_ip is True
    assert topo.routes == [] and topo.nats == [] and c.findings == []
    assert topo.network_ids == {"vpc-1": "network:main"}
    assert topo.subnet_ids == {"subnet-1": "subnet:web"}
    as_document(topo)


def test_main_route_table_fallback_and_nat():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", name="main")],
        subnets=[
            aws.subnet("subnet-pub", "vpc-1", "10.0.1.0/24", name="public"),
            aws.subnet("subnet-app", "vpc-1", "10.0.2.0/24", name="app"),
        ],
        route_tables=[
            aws.route_table(
                "rtb-pub", "vpc-1", [aws.route("0.0.0.0/0", GatewayId="igw-1")],
                subnets=("subnet-pub",),
            ),
            aws.route_table(
                "rtb-main", "vpc-1", [aws.route("0.0.0.0/0", NatGatewayId="nat-1")], main=True
            ),
        ],
        nat_gateways=[
            aws.nat_gateway("nat-1", "vpc-1", "subnet-pub", name="egress"),
            aws.nat_gateway("nat-old", "vpc-1", "subnet-pub", state="deleted"),
        ],
    )

    topo = build_topology(snap, ctx())

    internet = {s.id: s.internet for s in topo.subnets}
    assert internet == {"subnet:public": "public", "subnet:app": "private_nat"}
    (nat,) = topo.nats
    assert (nat.id, nat.network, nat.serves_subnets) == ("nat:egress", "network:main", ["subnet:app"])
    assert topo.routes == []
    as_document(topo)


def test_isolated_when_no_default_route():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1")],
        subnets=[aws.subnet("subnet-1", "vpc-1")],
        route_tables=[aws.route_table("rtb-main", "vpc-1", main=True)],
    )

    (sub,) = build_topology(snap, ctx()).subnets

    assert sub.internet == "isolated"
    assert sub.name == "subnet-1" and sub.id == "subnet:subnet-1"


def test_custom_routes_by_target_type():
    routes = [
        aws.route("172.16.0.0/16", VpcPeeringConnectionId="pcx-1"),
        aws.route("192.168.0.0/16", GatewayId="vgw-1"),
        aws.route("10.50.0.0/16", TransitGatewayId="tgw-1"),
        aws.route("10.60.0.0/16", NetworkInterfaceId="eni-1", InstanceId="i-1"),
        aws.route("pl-1234", VpcEndpointId="vpce-1"),
        aws.route("10.70.0.5/16", NatGatewayId="nat-1"),
        aws.route("::/0", GatewayId="igw-1"),
        aws.route("::/0", EgressOnlyInternetGatewayId="eigw-1"),
    ]
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", name="main")],
        subnets=[aws.subnet("subnet-1", "vpc-1", name="app")],
        route_tables=[
            aws.route_table("rtb-1", "vpc-1", routes, subnets=("subnet-1",), name="app-routes"),
            aws.route_table("rtb-main", "vpc-1", main=True),
        ],
    )

    topo = build_topology(snap, ctx())

    found = {(r.destination, r.target_type, r.target_id) for r in topo.routes}
    assert found == {
        ("172.16.0.0/16", "peering", "pcx-1"),
        ("192.168.0.0/16", "vpn", "vgw-1"),
        ("10.50.0.0/16", "tgw", "tgw-1"),
        ("10.60.0.0/16", "eni", "eni-1"),
        ("pl-1234", "other", "vpce-1"),
        ("10.70.0.0/16", "other", "nat-1"),
    }
    peering = next(r for r in topo.routes if r.target_type == "peering")
    assert peering.id == "custom_route:app-routes-172-16-0-0-16"
    assert peering.source.id == "rtb-1:172.16.0.0/16"
    assert peering.subnets == ["subnet:app"]
    assert all(r.network == "network:main" for r in topo.routes)
    as_document(topo)


def test_main_table_custom_route_lists_implicit_subnets():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1")],
        subnets=[
            aws.subnet("subnet-a", "vpc-1", "10.0.1.0/24", name="a"),
            aws.subnet("subnet-b", "vpc-1", "10.0.2.0/24", name="b"),
        ],
        route_tables=[
            aws.route_table("rtb-b", "vpc-1", subnets=("subnet-b",)),
            aws.route_table(
                "rtb-main", "vpc-1", [aws.route("172.16.0.0/16", VpcPeeringConnectionId="pcx-1")],
                main=True,
            ),
        ],
    )

    (custom,) = build_topology(snap, ctx()).routes

    assert custom.subnets == ["subnet:a"]


def test_ipv6_cidrs_are_captured_and_canonical():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", ipv6=("2600:1F18:ABCD:1200::/56",))],
        subnets=[aws.subnet("subnet-1", "vpc-1", ipv6="2600:1f18:abcd:1201::/64")],
        route_tables=[aws.route_table("rtb-main", "vpc-1", main=True)],
    )

    topo = build_topology(snap, ctx())

    assert topo.networks[0].ipv6_cidr_blocks == ["2600:1f18:abcd:1200::/56"]
    assert topo.subnets[0].ipv6_cidr == "2600:1f18:abcd:1201::/64"


def test_same_named_subnets_get_native_id_suffixes():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1")],
        subnets=[
            aws.subnet("subnet-2", "vpc-1", "10.0.2.0/24", name="public"),
            aws.subnet("subnet-1", "vpc-1", "10.0.1.0/24", name="public"),
        ],
        route_tables=[aws.route_table("rtb-main", "vpc-1", main=True)],
    )

    ids = sorted(s.id for s in build_topology(snap, ctx()).subnets)

    assert ids == ["subnet:public-subnet-1", "subnet:public-subnet-2"]


def test_ipv6_only_subnet_is_unmodelled():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1")],
        subnets=[aws.subnet("subnet-v6", "vpc-1", cidr=None, ipv6="2600:1f18::/64")],
        route_tables=[aws.route_table("rtb-main", "vpc-1", main=True)],
    )

    with pytest.raises(UnmodelledData, match="subnet-v6"):
        build_topology(snap, ctx(strict=True))

    lenient = ctx(strict=False)
    topo = build_topology(snap, lenient)
    assert topo.subnets == []
    assert [f.code for f in lenient.findings] == ["INTERNAL-UNMODELLED"]


def test_missing_vpc_attributes_use_aws_defaults():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", name="main", is_default=True)],
        route_tables=[],
        omit=("network.vpc_attributes",),
    )
    c = ctx()

    (network,) = build_topology(snap, c).networks

    assert (network.dns_resolution, network.dns_hostnames) == (True, False)
    assert sorted(f.code for f in c.findings) == ["NET-DEFAULT-VPC", "NET-DNS-ATTRIBUTES-UNKNOWN"]
    assert all(f.subject == "network:main" for f in c.findings)


def test_missing_route_tables_are_reported_once():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1")],
        subnets=[
            aws.subnet("subnet-1", "vpc-1", "10.0.1.0/24"),
            aws.subnet("subnet-2", "vpc-1", "10.0.2.0/24"),
        ],
        route_tables=None,
    )
    c = ctx()

    topo = build_topology(snap, c)

    assert {s.internet for s in topo.subnets} == {"isolated"}
    assert [f.code for f in c.findings] == ["NET-ROUTES-UNKNOWN"]


def test_subnets_of_unknown_vpcs_are_skipped():
    snap = aws.snapshot(
        vpcs=[],
        subnets=[aws.subnet("subnet-1", "vpc-missing")],
        route_tables=[],
    )

    topo = build_topology(snap, ctx())

    assert topo.subnets == [] and topo.networks == []


def test_secondary_vpc_cidrs():
    snap = aws.snapshot(vpcs=[aws.vpc("vpc-1", extra_cidrs=("100.64.0.0/16",))], route_tables=[])

    assert build_topology(snap, ctx()).networks[0].cidr_blocks == ["10.0.0.0/16", "100.64.0.0/16"]
```

Note: `tests/plugins/network/aws_builders.py` is imported as `tests.plugins.network.aws_builders`. Create empty `tests/__init__.py`, `tests/plugins/__init__.py` and `tests/plugins/network/__init__.py` if they don't exist. Pytest's importlib mode then resolves the package from the repository root.

- [ ] **Step 3: Run and confirm it fails**

Run: `uv run pytest tests/plugins/network/test_normalize_topology.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.plugins.network.normalize_topology'`.

- [ ] **Step 4: Make `tests` importable as a package**

Create empty `tests/__init__.py`, `tests/plugins/__init__.py` and `tests/plugins/network/__init__.py` if they're missing. In `pyproject.toml`, under `[tool.pytest.ini_options]`, add `pythonpath = ["."]`, so `from tests.plugins.network import aws_builders` resolves.

- [ ] **Step 5: Implement**

`src/easymigrate/plugins/network/normalize_topology.py`:
```python
"""Networks, subnets, NAT gateways and custom routes from an AWS snapshot (spec §3.2–§3.3).

Faithful capture only: nothing here decides whether GCP can reproduce what it records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import SourceRef
from easymigrate.ir.naming import allocate_ids, clean_labels
from easymigrate.ir.network import CustomRoute, Nat, Network, Subnet
from easymigrate.plugins.network import aws_values as av

_LIVE_NAT_STATES = frozenset({"pending", "available"})
# Route keys naming a target, checked in order; the first present wins.
_TYPED_TARGETS = (
    ("VpcPeeringConnectionId", "peering"),
    ("TransitGatewayId", "tgw"),
    ("NetworkInterfaceId", "eni"),
    ("InstanceId", "eni"),
)
_OTHER_TARGETS = (
    "GatewayId",
    "NatGatewayId",
    "EgressOnlyInternetGatewayId",
    "VpcEndpointId",
    "CarrierGatewayId",
    "LocalGatewayId",
    "CoreNetworkArn",
)


@dataclass
class Topology:
    networks: list[Network]
    subnets: list[Subnet]
    nats: list[Nat]
    routes: list[CustomRoute]
    network_ids: dict[str, str]
    subnet_ids: dict[str, str]


def build_topology(snapshot: Snapshot, ctx: NormalizeContext) -> Topology:
    vpcs = snapshot.get("network.vpcs") or []
    route_tables_raw = snapshot.get("network.route_tables")
    route_tables = route_tables_raw or []

    network_ids = allocate_ids("network", [(av.name_tag(v), v["VpcId"]) for v in vpcs])
    networks = [
        _network(v, network_ids[v["VpcId"]], snapshot.get("network.vpc_attributes"), ctx)
        for v in vpcs
    ]

    subnets_raw = [
        s
        for s in snapshot.get("network.subnets") or []
        if s["VpcId"] in network_ids and _has_ipv4(s, ctx)
    ]
    subnet_ids = allocate_ids("subnet", [(av.name_tag(s), s["SubnetId"]) for s in subnets_raw])

    if route_tables_raw is None and subnets_raw:
        ctx.info(
            "NET-ROUTES-UNKNOWN",
            "Route tables could not be read, so every subnet is recorded as isolated.",
            suggestion="Grant ec2:DescribeRouteTables and scan again.",
        )
    tables = _RouteTables(route_tables)

    subnets = [
        _subnet(s, subnet_ids[s["SubnetId"]], network_ids[s["VpcId"]], tables.of(s), ctx)
        for s in subnets_raw
    ]
    nats = _nats(snapshot, network_ids, subnet_ids, subnets_raw, tables, ctx)
    routes = _custom_routes(route_tables, network_ids, subnet_ids, subnets_raw, tables, ctx)
    return Topology(networks, subnets, nats, routes, network_ids, subnet_ids)


class _RouteTables:
    def __init__(self, tables: list[dict[str, Any]]) -> None:
        self.by_subnet: dict[str, dict[str, Any]] = {}
        self.main: dict[str, dict[str, Any]] = {}
        for table in tables:
            for association in table.get("Associations") or []:
                state = (association.get("AssociationState") or {}).get("State", "associated")
                if state != "associated":
                    continue
                if association.get("Main"):
                    self.main[table["VpcId"]] = table
                elif association.get("SubnetId"):
                    self.by_subnet[association["SubnetId"]] = table

    def of(self, subnet: dict[str, Any]) -> dict[str, Any] | None:
        return self.by_subnet.get(subnet["SubnetId"]) or self.main.get(subnet["VpcId"])


def _source(ctx: NormalizeContext, type_: str, native_id: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native_id, region=ctx.region)


def _network(
    vpc: dict[str, Any],
    ir_id: str,
    attributes: dict[str, dict[str, bool]] | None,
    ctx: NormalizeContext,
) -> Network:
    vpc_id = vpc["VpcId"]
    cidrs = av.associated_cidrs(
        vpc.get("CidrBlockAssociationSet"), "CidrBlock", "CidrBlockState"
    ) or [av.cidr(vpc["CidrBlock"])]
    if attributes is not None and vpc_id in attributes:
        dns_support = attributes[vpc_id]["enableDnsSupport"]
        dns_hostnames = attributes[vpc_id]["enableDnsHostnames"]
    else:
        dns_support, dns_hostnames = True, False
        ctx.info(
            "NET-DNS-ATTRIBUTES-UNKNOWN",
            f"DNS settings for {vpc_id} could not be read; assuming AWS defaults "
            "(DNS resolution on, DNS hostnames off).",
            subject=ir_id,
            suggestion="Grant ec2:DescribeVpcAttribute and scan again.",
        )
    if vpc.get("IsDefault"):
        ctx.info(
            "NET-DEFAULT-VPC",
            f"{vpc_id} is this region's default VPC. Default VPCs are often unused; "
            "check whether it needs migrating.",
            subject=ir_id,
        )
    return Network(
        id=ir_id,
        name=av.name_tag(vpc) or vpc_id,
        labels=clean_labels(av.tags(vpc)),
        source=_source(ctx, "vpc", vpc_id),
        cidr_blocks=cidrs,
        ipv6_cidr_blocks=av.associated_cidrs(
            vpc.get("Ipv6CidrBlockAssociationSet"), "Ipv6CidrBlock", "Ipv6CidrBlockState"
        ),
        dns_hostnames=dns_hostnames,
        dns_resolution=dns_support,
    )


def _has_ipv4(subnet: dict[str, Any], ctx: NormalizeContext) -> bool:
    if subnet.get("CidrBlock"):
        return True
    ctx.unmodelled(
        f"subnet {subnet['SubnetId']} has no IPv4 CIDR; IPv6-only subnets are not modelled",
        subject=subnet["SubnetId"],
    )
    return False


def _default_route(table: dict[str, Any] | None) -> dict[str, Any] | None:
    for route in (table or {}).get("Routes") or []:
        if route.get("DestinationCidrBlock") == "0.0.0.0/0" and route.get("State", "active") == "active":
            return route
    return None


def _internet(table: dict[str, Any] | None) -> str:
    route = _default_route(table)
    if route is None:
        return "isolated"
    if (route.get("GatewayId") or "").startswith("igw-"):
        return "public"
    if route.get("NatGatewayId"):
        return "private_nat"
    return "isolated"


def _subnet(
    subnet: dict[str, Any],
    ir_id: str,
    network_id: str,
    table: dict[str, Any] | None,
    ctx: NormalizeContext,
) -> Subnet:
    subnet_id = subnet["SubnetId"]
    ipv6 = av.associated_cidrs(
        subnet.get("Ipv6CidrBlockAssociationSet"), "Ipv6CidrBlock", "Ipv6CidrBlockState"
    )
    return Subnet(
        id=ir_id,
        name=av.name_tag(subnet) or subnet_id,
        labels=clean_labels(av.tags(subnet)),
        source=_source(ctx, "subnet", subnet_id),
        network=network_id,
        cidr=av.cidr(subnet["CidrBlock"]),
        ipv6_cidr=ipv6[0] if ipv6 else None,
        zone=subnet["AvailabilityZone"],
        internet=_internet(table),
        map_public_ip=bool(subnet.get("MapPublicIpOnLaunch")),
    )


def _nats(
    snapshot: Snapshot,
    network_ids: dict[str, str],
    subnet_ids: dict[str, str],
    subnets_raw: list[dict[str, Any]],
    tables: _RouteTables,
    ctx: NormalizeContext,
) -> list[Nat]:
    live = [
        n
        for n in snapshot.get("network.nat_gateways") or []
        if n.get("State") in _LIVE_NAT_STATES and n["VpcId"] in network_ids
    ]
    nat_ids = allocate_ids("nat", [(av.name_tag(n), n["NatGatewayId"]) for n in live])
    served: dict[str, list[str]] = {}
    for subnet in subnets_raw:
        route = _default_route(tables.of(subnet))
        if route and route.get("NatGatewayId"):
            served.setdefault(route["NatGatewayId"], []).append(subnet_ids[subnet["SubnetId"]])
    return [
        Nat(
            id=nat_ids[n["NatGatewayId"]],
            name=av.name_tag(n) or n["NatGatewayId"],
            labels=clean_labels(av.tags(n)),
            source=_source(ctx, "nat_gateway", n["NatGatewayId"]),
            network=network_ids[n["VpcId"]],
            serves_subnets=sorted(served.get(n["NatGatewayId"], [])),
        )
        for n in live
    ]


def _is_internet_default(route: dict[str, Any]) -> bool:
    gateway = route.get("GatewayId") or ""
    if route.get("DestinationCidrBlock") == "0.0.0.0/0":
        return gateway.startswith("igw-") or bool(route.get("NatGatewayId"))
    if route.get("DestinationIpv6CidrBlock") == "::/0":
        return gateway.startswith("igw-") or bool(route.get("EgressOnlyInternetGatewayId"))
    return False


def _destination(route: dict[str, Any]) -> str | None:
    if route.get("DestinationCidrBlock"):
        return av.cidr(route["DestinationCidrBlock"])
    if route.get("DestinationIpv6CidrBlock"):
        return av.cidr(route["DestinationIpv6CidrBlock"])
    return route.get("DestinationPrefixListId")


def _target(route: dict[str, Any]) -> tuple[str, str]:
    for key, target_type in _TYPED_TARGETS:
        if route.get(key):
            return target_type, route[key]
    gateway = route.get("GatewayId") or ""
    if gateway.startswith("vgw-"):
        return "vpn", gateway
    for key in _OTHER_TARGETS:
        if route.get(key):
            return "other", route[key]
    return "other", "unknown"


def _custom_routes(
    route_tables: list[dict[str, Any]],
    network_ids: dict[str, str],
    subnet_ids: dict[str, str],
    subnets_raw: list[dict[str, Any]],
    tables: _RouteTables,
    ctx: NormalizeContext,
) -> list[CustomRoute]:
    candidates: list[tuple[str, str, dict[str, Any], dict[str, Any], str]] = []
    for table in route_tables:
        if table["VpcId"] not in network_ids:
            continue
        table_name = av.name_tag(table) or table["RouteTableId"]
        for route in table.get("Routes") or []:
            if route.get("GatewayId") == "local" or _is_internet_default(route):
                continue
            destination = _destination(route)
            if destination is None:
                ctx.unmodelled(
                    f"route in {table['RouteTableId']} has no destination", subject=table["RouteTableId"]
                )
                continue
            native_id = f"{table['RouteTableId']}:{destination}"
            candidates.append((native_id, f"{table_name} {destination}", table, route, destination))

    route_ids = allocate_ids("custom_route", [(name, native) for native, name, *_ in candidates])
    routes: list[CustomRoute] = []
    for native_id, name, table, route, destination in candidates:
        target_type, target_id = _target(route)
        using = sorted(
            subnet_ids[s["SubnetId"]]
            for s in subnets_raw
            if tables.of(s) is table
        )
        routes.append(
            CustomRoute(
                id=route_ids[native_id],
                name=name,
                source=_source(ctx, "route", native_id),
                network=network_ids[table["VpcId"]],
                destination=destination,
                target_type=target_type,
                target_id=target_id,
                subnets=using,
            )
        )
    return routes
```

- [ ] **Step 6: Run and confirm it passes**

Run: `uv run pytest tests/plugins/network/test_normalize_topology.py tests/test_dependency_rules.py -v`
Expected: all pass.

- [ ] **Step 7: Lint and commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`

```bash
git add pyproject.toml tests/__init__.py tests/plugins src/easymigrate/plugins/network/normalize_topology.py
git commit -m "$(cat <<'EOF'
feat(network): normalize VPCs, subnets, NAT gateways and custom routes into IR

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Security normalizer (firewall policies and network ACLs)

**Files:**
- Create: `src/easymigrate/plugins/network/normalize_security.py`, `tests/plugins/network/test_normalize_security.py`

**Interfaces:**
- Consumes: `build_topology`, `Topology` (Task 6); `aws_values` (Task 5); `aws_builders` (Task 6); `NormalizeContext`, `Snapshot` (Task 3); `allocate_ids`, `clean_labels`; `FirewallPolicy`, `FirewallRule`, `NetworkAcl`, `AclRule`, `CidrPeer`, `PolicyPeer`, `ExternalPolicyPeer`, `SelfPeer`, `PrefixListPeer`
- Produces (in `easymigrate.plugins.network.normalize_security`):
  - `build_security(snapshot: Snapshot, ctx: NormalizeContext, topology: Topology) -> tuple[list[FirewallPolicy], list[NetworkAcl]]`

**Rules implemented:**
- **Security groups:** only groups in a VPC present in `topology.network_ids`. The name is the `Name` tag, otherwise `GroupName`. Ids come from `allocate_ids("firewall_policy", ...)`, so two VPCs' `default` groups get native-id suffixes.
- **Rules:** one `FirewallRule` per (permission × peer entry) in `IpPermissions` (ingress) and `IpPermissionsEgress` (egress).
  - protocol, ports and ICMP values come through `aws_values`
  - `description` is the peer entry's `Description`
- **Peers:**
  - `IpRanges[].CidrIp` and `Ipv6Ranges[].CidrIpv6` → `CidrPeer` (canonical)
  - `PrefixListIds[].PrefixListId` → `PrefixListPeer`
  - `UserIdGroupPairs[]`:
    - its own `GroupId` → `SelfPeer`
    - a scanned group in the **same VPC** whose `UserId` is absent or equal to the group's `OwnerId` → `PolicyPeer`
    - anything else → `ExternalPolicyPeer(group_id, owner_id=UserId, vpc_id=VpcId or the referenced group's VpcId)`
- **Ordering:** rules are sorted by `(direction, protocol, ports, icmp_type, icmp_code, peer JSON, description)` so output is deterministic. `applies_to` stays `[]`.
- **Network ACLs:** only for known VPCs. A NACL is **omitted** when it is allow-all, meaning its entries other than rule 32767:
  - cover both directions, and
  - are all `allow`, protocol `-1`, with CIDR `0.0.0.0/0` or `::/0`

  This applies whether or not `IsDefault` is set.
- **NACL rules:** rule 32767 entries are skipped, since that implicit deny is identical in every NACL. Each rule is built from `RuleNumber`, `Egress`, `Protocol`, `PortRange`, `IcmpTypeCode`, `CidrBlock`/`Ipv6CidrBlock` and `RuleAction`, and rules are sorted by `(direction, number)`. `subnets` are the associated subnets in `topology.subnet_ids`, sorted.

- [ ] **Step 1: Write the failing tests**

`tests/plugins/network/test_normalize_security.py`:
```python
from easymigrate.core.plugin import NormalizeContext
from easymigrate.ir import IRDocument
from easymigrate.ir.network import (
    CidrPeer,
    ExternalPolicyPeer,
    PolicyPeer,
    PrefixListPeer,
    SelfPeer,
)
from easymigrate.plugins.network.normalize_security import build_security
from easymigrate.plugins.network.normalize_topology import build_topology
from tests.plugins.network import aws_builders as aws


def run(snap):
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)
    topology = build_topology(snap, ctx)
    policies, acls = build_security(snap, ctx, topology)
    objects = [*topology.networks, *topology.subnets, *policies, *acls]
    IRDocument(source={"cloud": "aws"}, objects=objects)  # validates references
    return policies, acls, ctx


def base(**files):
    files.setdefault("vpcs", [aws.vpc("vpc-1", name="main")])
    files.setdefault("route_tables", [aws.route_table("rtb-main", "vpc-1", main=True)])
    return aws.snapshot(**files)


def test_cidr_rules_ports_descriptions_and_sorting():
    web = aws.security_group(
        "sg-web",
        "vpc-1",
        "web",
        ingress=[
            aws.permission("tcp", 443, 443, cidrs=("0.0.0.0/0",), description="https"),
            aws.permission("6", 8000, 8080, ipv6=("2600:1F18::/32",)),
            aws.permission("tcp", 0, 65535, cidrs=("10.0.0.0/16",)),
        ],
    )

    (policy,), acls, _ = run(base(security_groups=[web]))

    assert (policy.id, policy.name, policy.network) == ("firewall_policy:web", "web", "network:main")
    assert policy.source.id == "sg-web" and policy.source.type == "security_group"
    assert policy.applies_to == []
    summary = [(r.direction, r.protocol, r.ports, r.peer, r.description) for r in policy.rules]
    assert summary == [
        ("egress", "all", [], CidrPeer(cidr="0.0.0.0/0"), None),
        ("ingress", "tcp", [], CidrPeer(cidr="10.0.0.0/16"), None),
        ("ingress", "tcp", ["443"], CidrPeer(cidr="0.0.0.0/0"), "https"),
        ("ingress", "tcp", ["8000-8080"], CidrPeer(cidr="2600:1f18::/32"), None),
    ]
    assert acls == []


def test_group_references_self_same_vpc_other_account_and_other_vpc():
    db = aws.security_group(
        "sg-db",
        "vpc-1",
        "db",
        ingress=[
            aws.permission("tcp", 5432, 5432, groups=({"GroupId": "sg-web", "UserId": aws.OWNER},)),
            aws.permission("-1", groups=({"GroupId": "sg-db"},)),
            aws.permission("tcp", 22, 22, groups=({"GroupId": "sg-999", "UserId": "210987654321"},)),
            aws.permission("tcp", 9000, 9000, groups=({"GroupId": "sg-other-vpc"},)),
        ],
        egress=[],
    )
    web = aws.security_group("sg-web", "vpc-1", "web", egress=[])
    other = aws.security_group("sg-other-vpc", "vpc-2", "other", egress=[])
    snap = base(
        vpcs=[aws.vpc("vpc-1", name="main"), aws.vpc("vpc-2", "10.1.0.0/16", name="second")],
        route_tables=[],
        security_groups=[db, web, other],
    )

    policies, _, _ = run(snap)

    rules = {r.ports[0] if r.ports else r.protocol: r.peer for r in next(
        p for p in policies if p.name == "db"
    ).rules}
    assert rules["5432"] == PolicyPeer(policy="firewall_policy:web")
    assert rules["all"] == SelfPeer()
    assert rules["22"] == ExternalPolicyPeer(group_id="sg-999", owner_id="210987654321")
    assert rules["9000"] == ExternalPolicyPeer(group_id="sg-other-vpc", vpc_id="vpc-2")


def test_prefix_lists_icmp_and_other_protocols():
    group = aws.security_group(
        "sg-1",
        "vpc-1",
        "mixed",
        ingress=[
            aws.permission("icmp", 8, -1, cidrs=("10.0.0.0/8",)),
            aws.permission("icmp", -1, -1, cidrs=("10.0.0.0/8",)),
            aws.permission("58", 128, 0, ipv6=("::/0",)),
            aws.permission("50", cidrs=("10.9.0.0/16",)),
        ],
        egress=[aws.permission("tcp", 443, 443, prefix_lists=("pl-123",))],
    )

    (policy,), _, _ = run(base(security_groups=[group]))

    by_protocol = {(r.protocol, r.icmp_type, r.icmp_code) for r in policy.rules if r.direction == "ingress"}
    assert by_protocol == {("icmp", 8, None), ("icmp", None, None), ("icmpv6", 128, 0), ("50", None, None)}
    (egress,) = [r for r in policy.rules if r.direction == "egress"]
    assert egress.peer == PrefixListPeer(prefix_list_id="pl-123")


def test_default_groups_in_two_vpcs_get_distinct_ids_and_unknown_vpcs_are_skipped():
    snap = base(
        vpcs=[aws.vpc("vpc-1"), aws.vpc("vpc-2", "10.1.0.0/16")],
        route_tables=[],
        security_groups=[
            aws.security_group("sg-a", "vpc-1", "default"),
            aws.security_group("sg-b", "vpc-2", "default"),
            aws.security_group("sg-c", "vpc-missing", "stray"),
        ],
    )

    policies, _, _ = run(snap)

    assert sorted(p.id for p in policies) == [
        "firewall_policy:default-sg-a",
        "firewall_policy:default-sg-b",
    ]


def test_allow_all_acls_are_omitted_but_restrictive_ones_kept():
    custom = aws.network_acl(
        "acl-db",
        "vpc-1",
        [
            aws.acl_entry(200, protocol="6", egress=False, cidr="10.0.0.0/16", ports=(5432, 5432)),
            aws.acl_entry(100, protocol="1", egress=False, cidr="10.0.0.0/16", icmp=(8, -1)),
            aws.acl_entry(100, protocol="-1", action="deny", egress=True, cidr="::/0"),
            aws.acl_entry(32767, action="deny", egress=False),
            aws.acl_entry(32767, action="deny", egress=True),
        ],
        subnets=("subnet-db",),
        name="db-acl",
    )
    snap = base(
        subnets=[aws.subnet("subnet-db", "vpc-1", name="db"), aws.subnet("subnet-web", "vpc-1", "10.0.2.0/24", name="web")],
        network_acls=[
            aws.network_acl("acl-default", "vpc-1", aws.DEFAULT_ACL_ENTRIES, is_default=True, subnets=("subnet-web",)),
            aws.network_acl("acl-open", "vpc-1", aws.DEFAULT_ACL_ENTRIES),
            aws.network_acl("acl-deny", "vpc-1", [aws.acl_entry(32767, action="deny")]),
            custom,
        ],
    )

    _, acls, _ = run(snap)

    assert sorted(a.id for a in acls) == ["network_acl:acl-deny", "network_acl:db-acl"]
    db = next(a for a in acls if a.id == "network_acl:db-acl")
    assert db.subnets == ["subnet:db"]
    assert [(r.direction, r.number, r.protocol, r.ports, r.icmp_type, r.cidr, r.action) for r in db.rules] == [
        ("egress", 100, "all", [], None, "::/0", "deny"),
        ("ingress", 100, "icmp", [], 8, "10.0.0.0/16", "allow"),
        ("ingress", 200, "tcp", ["5432"], None, "10.0.0.0/16", "allow"),
    ]
    deny = next(a for a in acls if a.id == "network_acl:acl-deny")
    assert deny.rules == []
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/plugins/network/test_normalize_security.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.plugins.network.normalize_security'`.

- [ ] **Step 3: Implement**

`src/easymigrate/plugins/network/normalize_security.py`:
```python
"""Security groups → firewall policies and network ACLs from an AWS snapshot (spec §3.2–§3.3).

Faithful capture only: nothing here decides whether GCP can reproduce what it records."""

from __future__ import annotations

from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import SourceRef
from easymigrate.ir.naming import allocate_ids, clean_labels
from easymigrate.ir.network import (
    AclRule,
    CidrPeer,
    ExternalPolicyPeer,
    FirewallPolicy,
    FirewallRule,
    NetworkAcl,
    PolicyPeer,
    PrefixListPeer,
    SelfPeer,
)
from easymigrate.plugins.network import aws_values as av
from easymigrate.plugins.network.normalize_topology import Topology

_IMPLICIT_DENY = 32767
_EVERYWHERE = frozenset({"0.0.0.0/0", "::/0"})


def build_security(
    snapshot: Snapshot, ctx: NormalizeContext, topology: Topology
) -> tuple[list[FirewallPolicy], list[NetworkAcl]]:
    return _policies(snapshot, ctx, topology), _acls(snapshot, ctx, topology)


def _source(ctx: NormalizeContext, type_: str, native_id: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native_id, region=ctx.region)


def _policies(snapshot: Snapshot, ctx: NormalizeContext, topology: Topology) -> list[FirewallPolicy]:
    groups = [
        g for g in snapshot.get("network.security_groups") or [] if g["VpcId"] in topology.network_ids
    ]
    names = {g["GroupId"]: av.name_tag(g) or g["GroupName"] for g in groups}
    policy_ids = allocate_ids("firewall_policy", [(names[g["GroupId"]], g["GroupId"]) for g in groups])
    vpc_of = {g["GroupId"]: g["VpcId"] for g in groups}

    policies = []
    for group in groups:
        rules = [
            rule
            for direction, key in (("ingress", "IpPermissions"), ("egress", "IpPermissionsEgress"))
            for permission in group.get(key) or []
            for rule in _rules(direction, permission, group, policy_ids, vpc_of)
        ]
        rules.sort(key=_rule_sort_key)
        policies.append(
            FirewallPolicy(
                id=policy_ids[group["GroupId"]],
                name=names[group["GroupId"]],
                labels=clean_labels(av.tags(group)),
                source=_source(ctx, "security_group", group["GroupId"]),
                network=topology.network_ids[group["VpcId"]],
                rules=rules,
            )
        )
    return policies


def _rules(
    direction: str,
    permission: dict[str, Any],
    group: dict[str, Any],
    policy_ids: dict[str, str],
    vpc_of: dict[str, str],
) -> list[FirewallRule]:
    protocol = av.protocol(permission["IpProtocol"])
    from_port, to_port = permission.get("FromPort"), permission.get("ToPort")
    ports = av.ports(protocol, from_port, to_port)
    icmp_type, icmp_code = av.icmp(protocol, from_port, to_port)

    peers: list[tuple[Any, str | None]] = []
    for entry in permission.get("IpRanges") or []:
        peers.append((CidrPeer(cidr=av.cidr(entry["CidrIp"])), entry.get("Description")))
    for entry in permission.get("Ipv6Ranges") or []:
        peers.append((CidrPeer(cidr=av.cidr(entry["CidrIpv6"])), entry.get("Description")))
    for entry in permission.get("PrefixListIds") or []:
        peers.append((PrefixListPeer(prefix_list_id=entry["PrefixListId"]), entry.get("Description")))
    for entry in permission.get("UserIdGroupPairs") or []:
        peers.append((_group_peer(entry, group, policy_ids, vpc_of), entry.get("Description")))

    return [
        FirewallRule(
            direction=direction,
            protocol=protocol,
            ports=ports,
            peer=peer,
            description=description,
            icmp_type=icmp_type,
            icmp_code=icmp_code,
        )
        for peer, description in peers
    ]


def _group_peer(
    pair: dict[str, Any],
    group: dict[str, Any],
    policy_ids: dict[str, str],
    vpc_of: dict[str, str],
) -> SelfPeer | PolicyPeer | ExternalPolicyPeer:
    group_id = pair.get("GroupId") or pair.get("GroupName") or "unknown"
    if group_id == group["GroupId"]:
        return SelfPeer()
    same_owner = pair.get("UserId") in (None, group.get("OwnerId"))
    same_vpc = vpc_of.get(group_id) == group["VpcId"] and pair.get("VpcId") in (None, group["VpcId"])
    if group_id in policy_ids and same_owner and same_vpc:
        return PolicyPeer(policy=policy_ids[group_id])
    return ExternalPolicyPeer(
        group_id=group_id,
        owner_id=pair.get("UserId"),
        vpc_id=pair.get("VpcId") or vpc_of.get(group_id),
    )


def _rule_sort_key(rule: FirewallRule) -> tuple[Any, ...]:
    return (
        rule.direction,
        rule.protocol,
        rule.ports,
        -1 if rule.icmp_type is None else rule.icmp_type,
        -1 if rule.icmp_code is None else rule.icmp_code,
        rule.peer.model_dump_json(),
        rule.description or "",
    )


def _acls(snapshot: Snapshot, ctx: NormalizeContext, topology: Topology) -> list[NetworkAcl]:
    kept = [
        acl
        for acl in snapshot.get("network.network_acls") or []
        if acl["VpcId"] in topology.network_ids and not _is_allow_all(acl.get("Entries") or [])
    ]
    acl_ids = allocate_ids("network_acl", [(av.name_tag(a), a["NetworkAclId"]) for a in kept])
    return [
        NetworkAcl(
            id=acl_ids[acl["NetworkAclId"]],
            name=av.name_tag(acl) or acl["NetworkAclId"],
            labels=clean_labels(av.tags(acl)),
            source=_source(ctx, "network_acl", acl["NetworkAclId"]),
            network=topology.network_ids[acl["VpcId"]],
            subnets=sorted(
                topology.subnet_ids[a["SubnetId"]]
                for a in acl.get("Associations") or []
                if a.get("SubnetId") in topology.subnet_ids
            ),
            rules=sorted(
                (_acl_rule(entry) for entry in acl.get("Entries") or [] if entry["RuleNumber"] != _IMPLICIT_DENY),
                key=lambda rule: (rule.direction, rule.number),
            ),
        )
        for acl in kept
    ]


def _is_allow_all(entries: list[dict[str, Any]]) -> bool:
    rules = [e for e in entries if e["RuleNumber"] != _IMPLICIT_DENY]
    if {bool(e.get("Egress")) for e in rules} != {True, False}:
        return False
    return all(
        e.get("RuleAction") == "allow"
        and str(e.get("Protocol")) == "-1"
        and (e.get("CidrBlock") or e.get("Ipv6CidrBlock")) in _EVERYWHERE
        for e in rules
    )


def _acl_rule(entry: dict[str, Any]) -> AclRule:
    protocol = av.protocol(entry["Protocol"])
    port_range = entry.get("PortRange") or {}
    icmp_type_code = entry.get("IcmpTypeCode") or {}
    icmp_type, icmp_code = av.icmp(protocol, icmp_type_code.get("Type"), icmp_type_code.get("Code"))
    return AclRule(
        number=entry["RuleNumber"],
        direction="egress" if entry.get("Egress") else "ingress",
        protocol=protocol,
        ports=av.ports(protocol, port_range.get("From"), port_range.get("To")),
        cidr=av.cidr(entry.get("CidrBlock") or entry["Ipv6CidrBlock"]),
        action=entry["RuleAction"],
        icmp_type=icmp_type,
        icmp_code=icmp_code,
    )
```

- [ ] **Step 4: Run and confirm it passes**

Run: `uv run pytest tests/plugins/network/test_normalize_security.py tests/test_dependency_rules.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`

```bash
git add src/easymigrate/plugins/network/normalize_security.py tests/plugins/network/test_normalize_security.py
git commit -m "$(cat <<'EOF'
feat(network): normalize security groups and network ACLs into IR

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Network plugin entry point

**Files:**
- Create: `src/easymigrate/plugins/network/normalize.py`, `tests/plugins/network/test_plugin.py`
- Modify: `src/easymigrate/plugins/network/__init__.py` (currently empty)

**Interfaces:**
- Consumes: `collect` (Task 4), `build_topology` (Task 6), `build_security` (Task 7), `Plugin` protocol (Task 3)
- Produces:
  - `easymigrate.plugins.network.normalize.normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]`: objects are ordered networks, subnets, NATs, firewall policies, network ACLs, then custom routes
  - `easymigrate.plugins.network.NetworkPlugin`, with:
    - `name = "network"`
    - `aws_resource_types = frozenset({"ec2:vpc", "ec2:subnet", "ec2:route-table", "ec2:internet-gateway", "ec2:natgateway", "ec2:security-group", "ec2:network-acl"})`
    - `collect_aws(connection, ctx)` and `normalize(snapshot, ctx)`

- [ ] **Step 1: Write the failing tests**

`tests/plugins/network/test_plugin.py`:
```python
from moto import mock_aws

from easymigrate.core.plugin import CollectContext, NormalizeContext, Plugin, Snapshot
from easymigrate.ir import IRDocument
from easymigrate.plugins.network import NetworkPlugin
from easymigrate.providers.aws.source import connect
from tests.plugins.network import aws_builders as aws


def test_plugin_satisfies_protocol_and_declares_resource_types():
    plugin: Plugin = NetworkPlugin()

    assert plugin.name == "network"
    assert "ec2:vpc" in plugin.aws_resource_types
    assert "ec2:natgateway" in plugin.aws_resource_types
    assert "rds:db" not in plugin.aws_resource_types


def test_normalize_returns_every_kind_in_stable_order():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", name="main")],
        subnets=[aws.subnet("subnet-1", "vpc-1", name="app")],
        route_tables=[
            aws.route_table(
                "rtb-1",
                "vpc-1",
                [
                    aws.route("0.0.0.0/0", NatGatewayId="nat-1"),
                    aws.route("172.16.0.0/16", VpcPeeringConnectionId="pcx-1"),
                ],
                subnets=("subnet-1",),
            ),
        ],
        nat_gateways=[aws.nat_gateway("nat-1", "vpc-1", "subnet-1")],
        security_groups=[aws.security_group("sg-1", "vpc-1", "web")],
        network_acls=[aws.network_acl("acl-1", "vpc-1", [aws.acl_entry(10, action="deny")])],
    )
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)

    objects = NetworkPlugin().normalize(snap, ctx)

    assert [o.kind for o in objects] == [
        "network",
        "subnet",
        "nat",
        "firewall_policy",
        "network_acl",
        "custom_route",
    ]
    IRDocument(source={"cloud": "aws"}, objects=objects)


def test_normalize_empty_snapshot():
    ctx = NormalizeContext(account_id="1", region="ap-south-1", strict=True)

    assert NetworkPlugin().normalize(Snapshot({}), ctx) == []


def test_collect_aws_delegates_to_collector(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        files = NetworkPlugin().collect_aws(conn, CollectContext(plugin="network"))

    assert "network.vpcs" in files
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/plugins/network/test_plugin.py -v`
Expected: FAIL with `ImportError: cannot import name 'NetworkPlugin' from 'easymigrate.plugins.network'`.

- [ ] **Step 3: Implement**

`src/easymigrate/plugins/network/normalize.py`:
```python
"""Network plugin normalizer entry point: snapshot → networking IR objects."""

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject
from easymigrate.plugins.network.normalize_security import build_security
from easymigrate.plugins.network.normalize_topology import build_topology


def normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
    topology = build_topology(snapshot, ctx)
    policies, acls = build_security(snapshot, ctx, topology)
    return [
        *topology.networks,
        *topology.subnets,
        *topology.nats,
        *policies,
        *acls,
        *topology.routes,
    ]
```

`src/easymigrate/plugins/network/__init__.py`:
```python
"""Networking plugin: VPCs, subnets, routes, NAT gateways, security groups and network ACLs."""

from typing import Any

from easymigrate.core.plugin import CollectContext, NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject
from easymigrate.plugins.network.collect_aws import collect
from easymigrate.plugins.network.normalize import normalize


class NetworkPlugin:
    name = "network"
    aws_resource_types = frozenset(
        {
            "ec2:vpc",
            "ec2:subnet",
            "ec2:route-table",
            "ec2:internet-gateway",
            "ec2:natgateway",
            "ec2:security-group",
            "ec2:network-acl",
        }
    )

    def collect_aws(self, connection: Any, ctx: CollectContext) -> dict[str, Any]:
        return collect(connection, ctx)

    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
        return normalize(snapshot, ctx)
```

- [ ] **Step 4: Run and confirm it passes**

Run: `uv run pytest tests/plugins/network tests/test_dependency_rules.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`

```bash
git add src/easymigrate/plugins/network/__init__.py src/easymigrate/plugins/network/normalize.py tests/plugins/network/test_plugin.py
git commit -m "$(cat <<'EOF'
feat(network): network plugin combining collector and normalizers

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Registry and pipeline (`scan`, `analyze`)

**Files:**
- Create: `src/easymigrate/core/errors.py`, `src/easymigrate/core/registry.py`, `src/easymigrate/core/pipeline.py`, `tests/core/test_pipeline.py`

**Interfaces:**
- Consumes: `Project`, `SourceConfig`, `StageInputMissing` (plan 1); `Snapshot`, `CollectContext`, `NormalizeContext`, `Plugin`, `SCAN_FILE` (Task 3); `NetworkPlugin` (Task 8); `easymigrate.providers.aws.source` (Task 2); `IRDocument`, `Finding`
- Produces:
  - `easymigrate.core.errors`:
    - `EasyMigrateError(Exception)`: an expected, user-facing failure
    - `SourceConnectionError(EasyMigrateError)`
  - `easymigrate.core.registry`:
    - `PLUGINS: tuple[Plugin, ...]` (`(NetworkPlugin(),)`)
    - `class AwsSource` with:
      - `connect(*, region, profile=None, role_arn=None) -> Any`, which raises `SourceConnectionError`
      - `account_id(connection) -> str`
      - `inventory(connection) -> tuple[dict[str, list[str]], bool]`
    - `source_for(cloud: str) -> AwsSource`
  - `easymigrate.core.pipeline`:
    - `@dataclass(frozen=True) class ScanSummary(account_id: str, region: str, files: list[str], findings: list[Finding], first_run: bool)`
    - `scan(project: Project, *, region: str, profile: str | None = None, role_arn: str | None = None, plugins: Sequence[Plugin] | None = None, source: Any = None) -> ScanSummary`
    - `analyze(project: Project, *, plugins: Sequence[Plugin] | None = None, strict: bool = False) -> IRDocument`

**Behavior:**
- **`scan`:**
  1. `project.init()`
  2. connect
  3. collect every plugin, keeping collect findings
  4. build the inventory findings
  5. **only then** delete the old `snapshot/aws/*.json` and write the new files and `_scan.json`, so a failed scan leaves the previous snapshot intact
  6. set `config.source`
- **Inventory findings:**
  - Access denied → `scan_gap / SCAN-ACCESS-DENIED` with suggestion `Grant tag:GetResources`.
  - Otherwise, one `unsupported / UNSUPPORTED-RESOURCE` finding per resource type that no plugin supports, with `subject=<type>`. The message lists at most 3 sample ARNs plus `and N more`.
  - Always one `info / INVENTORY-TAGGED-ONLY` finding (unless access was denied).
- **`analyze`:** reads `_scan.json` (a missing file raises `StageInputMissing(..., "scan")`) and runs plugins in order, passing earlier objects through `ctx.objects`. `IRDocument.source` is `{cloud, account_id, region, scanned_at}`, and its findings are the scan findings plus the normalize findings. The result is written with `project.write_ir(project.ir_path, doc)`.

- [ ] **Step 1: Write the failing tests**

`tests/core/test_pipeline.py`:
```python
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from moto import mock_aws

from easymigrate.core.errors import SourceConnectionError
from easymigrate.core.pipeline import analyze, scan
from easymigrate.core.plugin import SCAN_FILE
from easymigrate.core.project import Project, StageInputMissing
from easymigrate.core.registry import PLUGINS, source_for
from easymigrate.ir import SourceRef
from easymigrate.ir.network import Network


class FakeSource:
    def __init__(self, arns=None, denied=False, error=None):
        self.arns, self.denied, self.error = arns or {}, denied, error
        self.connected = None

    def connect(self, *, region, profile=None, role_arn=None):
        if self.error:
            raise self.error
        self.connected = (region, profile, role_arn)
        return SimpleNamespace(account_id="123456789012")

    def account_id(self, connection):
        return connection.account_id

    def inventory(self, connection):
        return self.arns, self.denied


class FakePlugin:
    aws_resource_types = frozenset({"ec2:vpc"})

    def __init__(self, name="fake", files=None, deny=None, make_objects=None):
        self.name, self.files, self.deny, self.make_objects = name, files or {}, deny, make_objects
        self.seen_objects = None
        self.snapshot_names = None

    def collect_aws(self, connection, ctx):
        if self.deny:
            ctx.access_denied(*self.deny)
        return dict(self.files)

    def normalize(self, snapshot, ctx):
        self.seen_objects = dict(ctx.objects)
        self.snapshot_names = snapshot.names()
        return self.make_objects(ctx) if self.make_objects else []


def network(name="main"):
    return Network(
        id=f"network:{name}",
        name=name,
        source=SourceRef(cloud="aws", type="vpc", id=f"vpc-{name}", region="ap-south-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )


def test_registry_exposes_network_plugin_and_aws_source():
    assert [p.name for p in PLUGINS] == ["network"]
    assert source_for("aws").__class__.__name__ == "AwsSource"
    with pytest.raises(ValueError, match="unsupported source cloud 'azure'"):
        source_for("azure")


def test_scan_writes_snapshot_scan_metadata_and_config(tmp_path):
    project = Project(tmp_path)
    source = FakeSource()
    plugin = FakePlugin(files={"fake.things": [{"id": 1}]}, deny=("DescribeX", "ec2:DescribeX"))

    summary = scan(project, region="ap-south-1", profile="prod", plugins=[plugin], source=source)

    assert source.connected == ("ap-south-1", "prod", None)
    assert summary.first_run is True
    assert summary.account_id == "123456789012" and summary.region == "ap-south-1"
    assert summary.files == ["fake.things"]
    snapshot_dir = project.snapshot_dir("aws")
    assert json.loads((snapshot_dir / "fake.things.json").read_text()) == [{"id": 1}]
    meta = json.loads((snapshot_dir / SCAN_FILE).read_text())
    assert meta["account_id"] == "123456789012" and meta["region"] == "ap-south-1"
    datetime.fromisoformat(meta["scanned_at"])
    assert [f["code"] for f in meta["findings"]] == ["SCAN-ACCESS-DENIED", "INVENTORY-TAGGED-ONLY"]
    config = project.load_config()
    assert (config.source.cloud, config.source.account_id, config.source.region) == (
        "aws",
        "123456789012",
        "ap-south-1",
    )
    assert scan(project, region="ap-south-1", plugins=[plugin], source=source).first_run is False


def test_scan_replaces_stale_snapshot_files(tmp_path):
    project = Project(tmp_path)
    project.init()
    stale = project.snapshot_dir("aws") / "network.gone.json"
    project.write_json(stale, [])

    scan(project, region="ap-south-1", plugins=[FakePlugin(files={"fake.new": []})], source=FakeSource())

    assert not stale.exists()
    assert (project.snapshot_dir("aws") / "fake.new.json").exists()


def test_failed_connection_keeps_previous_snapshot(tmp_path):
    project = Project(tmp_path)
    project.init()
    previous = project.snapshot_dir("aws") / "network.vpcs.json"
    project.write_json(previous, [])

    with pytest.raises(SourceConnectionError, match="no AWS credentials"):
        scan(
            project,
            region="ap-south-1",
            plugins=[FakePlugin()],
            source=FakeSource(error=SourceConnectionError("no AWS credentials found")),
        )

    assert previous.exists()


def test_inventory_findings_for_unsupported_types(tmp_path):
    arns = {
        "ec2:vpc": ["arn:aws:ec2:ap-south-1:1:vpc/vpc-1"],
        "lambda:function": [f"arn:aws:lambda:ap-south-1:1:function:f{i}" for i in range(4)],
    }

    summary = scan(Project(tmp_path), region="ap-south-1", plugins=[FakePlugin()], source=FakeSource(arns))

    unsupported = [f for f in summary.findings if f.level == "unsupported"]
    assert len(unsupported) == 1
    finding = unsupported[0]
    assert (finding.code, finding.subject) == ("UNSUPPORTED-RESOURCE", "lambda:function")
    assert finding.message.startswith("4 lambda:function resource(s)")
    assert finding.message.endswith("and 1 more")
    assert "function:f3" not in finding.message


def test_inventory_access_denied_is_a_scan_gap(tmp_path):
    summary = scan(
        Project(tmp_path), region="ap-south-1", plugins=[FakePlugin()], source=FakeSource(denied=True)
    )

    assert [(f.level, f.code, f.suggestion) for f in summary.findings] == [
        ("scan_gap", "SCAN-ACCESS-DENIED", "Grant tag:GetResources")
    ]


def test_analyze_requires_scan(tmp_path):
    with pytest.raises(StageInputMissing, match="Run `easymigrate scan` first"):
        analyze(Project(tmp_path), plugins=[FakePlugin()])


def test_analyze_merges_findings_and_passes_objects_to_later_plugins(tmp_path):
    project = Project(tmp_path)
    first = FakePlugin(
        name="first",
        files={"first.data": []},
        deny=("DescribeX", "ec2:DescribeX"),
        make_objects=lambda ctx: (ctx.info("NET-DEFAULT-VPC", "default"), [network()])[1],
    )
    second = FakePlugin(name="second")
    scan(project, region="ap-south-1", plugins=[first, second], source=FakeSource())

    doc = analyze(project, plugins=[first, second])

    assert [o.id for o in doc.objects] == ["network:main"]
    assert second.seen_objects == {"network:main": doc.objects[0]}
    assert first.snapshot_names == ["first.data"]
    assert doc.source["account_id"] == "123456789012" and doc.source["cloud"] == "aws"
    assert "scanned_at" in doc.source
    assert [f.code for f in doc.findings] == [
        "SCAN-ACCESS-DENIED",
        "INVENTORY-TAGGED-ONLY",
        "NET-DEFAULT-VPC",
    ]
    assert project.read_ir(project.ir_path, produced_by="analyze").model_dump() == doc.model_dump()


def test_scan_and_analyze_end_to_end_with_moto(tmp_path, aws_credentials):
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")
        summary = scan(project, region="ap-south-1")

    doc = analyze(project, strict=True)

    assert "network.vpcs" in summary.files
    kinds = {o.kind for o in doc.objects}
    assert {"network", "subnet", "firewall_policy"} <= kinds
    assert "network:main" in [o.id for o in doc.objects]
    assert "NET-DEFAULT-VPC" in [f.code for f in doc.findings]
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/core/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'easymigrate.core.errors'`.

- [ ] **Step 3: Implement**

`src/easymigrate/core/errors.py`:
```python
"""Expected, user-facing failures. The CLI prints the message and exits with code 1."""


class EasyMigrateError(Exception):
    pass


class SourceConnectionError(EasyMigrateError):
    """The source cloud could not be reached: credentials, role or region problem."""
```

`src/easymigrate/core/registry.py`:
```python
"""Composition root: the only core module allowed to import plugins and providers."""

from typing import Any

from easymigrate.core.errors import SourceConnectionError
from easymigrate.core.plugin import Plugin
from easymigrate.plugins.network import NetworkPlugin
from easymigrate.providers.aws import source as aws_source

PLUGINS: tuple[Plugin, ...] = (NetworkPlugin(),)


class AwsSource:
    """Adapts the AWS provider to the plain types the pipeline uses."""

    def connect(
        self, *, region: str, profile: str | None = None, role_arn: str | None = None
    ) -> Any:
        try:
            return aws_source.connect(region=region, profile=profile, role_arn=role_arn)
        except aws_source.AwsConnectionError as exc:
            raise SourceConnectionError(str(exc)) from None

    def account_id(self, connection: Any) -> str:
        return connection.account_id

    def inventory(self, connection: Any) -> tuple[dict[str, list[str]], bool]:
        result = aws_source.inventory(connection)
        return result.arns_by_type, result.access_denied


def source_for(cloud: str) -> AwsSource:
    if cloud != "aws":
        raise ValueError(f"unsupported source cloud {cloud!r}")
    return AwsSource()
```

`src/easymigrate/core/pipeline.py`:
```python
"""Stage orchestration for `scan` and `analyze` (spec §2.2, §2.3)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from easymigrate.core import registry
from easymigrate.core.plugin import SCAN_FILE, CollectContext, NormalizeContext, Plugin, Snapshot
from easymigrate.core.project import Project, SourceConfig
from easymigrate.ir.base import Finding, IntentObject, IRDocument

_SAMPLE_ARNS = 3


@dataclass(frozen=True)
class ScanSummary:
    account_id: str
    region: str
    files: list[str]
    findings: list[Finding]
    first_run: bool


def scan(
    project: Project,
    *,
    region: str,
    profile: str | None = None,
    role_arn: str | None = None,
    plugins: Sequence[Plugin] | None = None,
    source: Any = None,
) -> ScanSummary:
    first_run = project.init()
    plugins = registry.PLUGINS if plugins is None else plugins
    source = source if source is not None else registry.source_for("aws")
    connection = source.connect(region=region, profile=profile, role_arn=role_arn)
    account_id = source.account_id(connection)

    files: dict[str, Any] = {}
    findings: list[Finding] = []
    for plugin in plugins:
        ctx = CollectContext(plugin=plugin.name)
        files.update(plugin.collect_aws(connection, ctx))
        findings.extend(ctx.findings)
    findings.extend(_inventory_findings(source, connection, plugins))

    snapshot_dir = project.snapshot_dir("aws")
    if snapshot_dir.is_dir():
        for old in snapshot_dir.glob("*.json"):
            old.unlink()
    for name in sorted(files):
        project.write_json(snapshot_dir / f"{name}.json", files[name])
    project.write_json(
        snapshot_dir / SCAN_FILE,
        {
            "account_id": account_id,
            "region": region,
            "scanned_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "findings": [f.model_dump(mode="json") for f in findings],
        },
    )
    config = project.load_config()
    project.save_config(
        config.model_copy(
            update={"source": SourceConfig(cloud="aws", account_id=account_id, region=region)}
        )
    )
    return ScanSummary(account_id, region, sorted(files), findings, first_run)


def _inventory_findings(source: Any, connection: Any, plugins: Sequence[Plugin]) -> list[Finding]:
    arns_by_type, access_denied = source.inventory(connection)
    if access_denied:
        return [
            Finding(
                level="scan_gap",
                code="SCAN-ACCESS-DENIED",
                message=(
                    "Access denied calling GetResources; resources from unsupported services "
                    "are not listed."
                ),
                suggestion="Grant tag:GetResources",
            )
        ]
    supported: set[str] = set()
    for plugin in plugins:
        supported |= plugin.aws_resource_types
    findings = []
    for resource_type, arns in arns_by_type.items():
        if resource_type in supported:
            continue
        sample = ", ".join(arns[:_SAMPLE_ARNS])
        if len(arns) > _SAMPLE_ARNS:
            sample += f" and {len(arns) - _SAMPLE_ARNS} more"
        findings.append(
            Finding(
                level="unsupported",
                code="UNSUPPORTED-RESOURCE",
                message=(
                    f"{len(arns)} {resource_type} resource(s) found that EasyMigrate does not "
                    f"migrate yet: {sample}"
                ),
                subject=resource_type,
                suggestion=(
                    "Plan to migrate these by hand, or wait for a release that supports them."
                ),
            )
        )
    findings.append(
        Finding(
            level="info",
            code="INVENTORY-TAGGED-ONLY",
            message=(
                "Resources from unsupported services are found through the AWS tagging API, "
                "which only lists tagged resources; untagged ones may be missing."
            ),
        )
    )
    return findings


def analyze(
    project: Project, *, plugins: Sequence[Plugin] | None = None, strict: bool = False
) -> IRDocument:
    plugins = registry.PLUGINS if plugins is None else plugins
    snapshot_dir = project.snapshot_dir("aws")
    meta = project.read_json(snapshot_dir / SCAN_FILE, produced_by="scan")
    snapshot = Snapshot.from_dir(snapshot_dir)
    ctx = NormalizeContext(account_id=meta["account_id"], region=meta["region"], strict=strict)

    objects: list[IntentObject] = []
    for plugin in plugins:
        produced = plugin.normalize(snapshot, ctx)
        objects.extend(produced)
        ctx.objects.update({obj.id: obj for obj in produced})

    doc = IRDocument(
        source={
            "cloud": "aws",
            "account_id": meta["account_id"],
            "region": meta["region"],
            "scanned_at": meta["scanned_at"],
        },
        objects=objects,
        findings=[*(Finding.model_validate(f) for f in meta["findings"]), *ctx.findings],
    )
    project.write_ir(project.ir_path, doc)
    return doc
```

- [ ] **Step 4: Run and confirm it passes**

Run: `uv run pytest tests/core/test_pipeline.py tests/test_dependency_rules.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`

```bash
git add src/easymigrate/core/errors.py src/easymigrate/core/registry.py src/easymigrate/core/pipeline.py tests/core/test_pipeline.py
git commit -m "$(cat <<'EOF'
feat(core): plugin registry and scan/analyze pipeline with inventory findings

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: `scan` and `analyze` CLI commands

**Files:**
- Modify: `src/easymigrate/cli.py`
- Create: `tests/test_cli_scan_analyze.py`

**Interfaces:**
- Consumes: `scan`, `analyze` (Task 9); `EasyMigrateError` (Task 9); `Project`, `StageInputMissing` (plan 1)
- Produces: the CLI commands
  - `easymigrate scan --region R [--aws-profile P] [--aws-role-arn ARN]`
  - `easymigrate analyze`

  Both use the current working directory as the project root, and both exit 0 on success and 1 on a user-facing error.

**Behavior:**
- **`scan`:**
  - Without `--region` → `Error: pass --region, e.g. --region ap-south-1` and exit 1.
  - On the first run it prints the metadata note from Global Constraints.
  - It prints `Scanned AWS account <id> in <region>: <n> snapshot file(s).`, then the findings, then `Next: run `easymigrate analyze`.`
- **`analyze`:** prints a Rich table titled `Intent objects` (columns Kind, Count), the findings, then `Wrote .easymigrate/ir.json.`
- **Findings output:** scan gaps and unsupported findings are listed one per line as `  CODE: message → suggestion`. Info findings are only counted: `<n> note(s) recorded in the project files.`
- **Errors:** `EasyMigrateError`, `StageInputMissing`, `pydantic.ValidationError` and `json.JSONDecodeError` are printed as `Error: ...` on stderr with exit 1, with no traceback.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_scan_analyze.py`:
```python
import json

from moto import mock_aws
from typer.testing import CliRunner

from easymigrate.cli import METADATA_NOTE, app

runner = CliRunner()


def test_scan_requires_region(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["scan"])

    assert result.exit_code == 1
    assert "pass --region" in result.output
    assert not (tmp_path / ".easymigrate").exists()


def test_scan_reports_connection_errors_without_traceback(tmp_path, monkeypatch, aws_credentials):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["scan", "--region", "mars-1"])

    assert result.exit_code == 1
    assert "Error: unknown AWS region 'mars-1'" in result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_analyze_before_scan_names_the_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["analyze"])

    assert result.exit_code == 1
    assert "_scan.json not found. Run `easymigrate scan` first." in result.output


def test_corrupt_scan_file_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = tmp_path / ".easymigrate" / "snapshot" / "aws"
    snapshot.mkdir(parents=True)
    (snapshot / "_scan.json").write_text("{not json")

    result = runner.invoke(app, ["analyze"])

    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_invalid_scan_findings_are_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = tmp_path / ".easymigrate" / "snapshot" / "aws"
    snapshot.mkdir(parents=True)
    (snapshot / "_scan.json").write_text(
        json.dumps(
            {
                "account_id": "1",
                "region": "ap-south-1",
                "scanned_at": "2026-09-15T00:00:00+00:00",
                "findings": [{"level": "approximated", "code": "X", "message": "m"}],
            }
        )
    )

    result = runner.invoke(app, ["analyze"])

    assert result.exit_code == 1
    assert "invalid" in result.output
    assert "Traceback" not in result.output


def test_scan_then_analyze(tmp_path, monkeypatch, aws_credentials):
    monkeypatch.chdir(tmp_path)

    with mock_aws():
        first = runner.invoke(app, ["scan", "--region", "ap-south-1"])
        second = runner.invoke(app, ["scan", "--region", "ap-south-1"])

    assert first.exit_code == 0, first.output
    assert METADATA_NOTE in first.output
    assert METADATA_NOTE not in second.output
    assert "Scanned AWS account 123456789012 in ap-south-1" in first.output
    assert "Next: run `easymigrate analyze`." in first.output

    result = runner.invoke(app, ["analyze"])

    assert result.exit_code == 0, result.output
    assert "Intent objects" in result.output
    assert "network" in result.output
    assert "Wrote .easymigrate/ir.json." in result.output
    assert (tmp_path / ".easymigrate" / "ir.json").exists()
```

- [ ] **Step 2: Run and confirm it fails**

Run: `uv run pytest tests/test_cli_scan_analyze.py -v`
Expected: FAIL with `ImportError: cannot import name 'METADATA_NOTE' from 'easymigrate.cli'`.

- [ ] **Step 3: Implement**

Replace `src/easymigrate/cli.py` with:
```python
"""Command-line entry point. Commands only call `easymigrate.core`."""

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from easymigrate import __version__
from easymigrate.core.errors import EasyMigrateError
from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import scan as run_scan
from easymigrate.core.project import Project, StageInputMissing

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console(soft_wrap=True, highlight=False)
error_console = Console(stderr=True, soft_wrap=True, highlight=False)

METADATA_NOTE = (
    "Note: .easymigrate/ contains account metadata (account ID, ARNs, endpoints). Keep it private."
)
_LISTED_LEVELS = (("scan_gap", "Scan gaps"), ("unsupported", "Not yet supported"))


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


def _fail(message: str) -> NoReturn:
    error_console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code=1)


@contextmanager
def _user_errors() -> Iterator[None]:
    try:
        yield
    except (EasyMigrateError, StageInputMissing) as exc:
        _fail(str(exc))
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        _fail(
            f"a project file is invalid or from an incompatible version "
            f"({exc.error_count()} error(s); first: {location}: {first['msg']})"
        )
    except json.JSONDecodeError as exc:
        _fail(f"a project file is not valid JSON: {exc.msg} (line {exc.lineno})")


def _print_findings(findings: Iterable[Any]) -> None:
    findings = list(findings)
    for level, title in _LISTED_LEVELS:
        selected = [f for f in findings if f.level == level]
        if not selected:
            continue
        console.print(f"[bold]{title} ({len(selected)})[/bold]")
        for finding in selected:
            line = f"  {finding.code}: {finding.message}"
            if finding.suggestion:
                line += f" → {finding.suggestion}"
            console.print(line, markup=False)
    notes = sum(1 for f in findings if f.level == "info")
    if notes:
        console.print(f"{notes} note(s) recorded in the project files.")


@app.command()
def scan(
    region: str | None = typer.Option(None, "--region", help="AWS region to scan, e.g. ap-south-1."),
    aws_profile: str | None = typer.Option(None, "--aws-profile", help="AWS CLI profile to use."),
    aws_role_arn: str | None = typer.Option(
        None, "--aws-role-arn", help="Read-only IAM role to assume for the scan."
    ),
) -> None:
    """Scan a live AWS region into .easymigrate/snapshot/aws/."""
    if not region:
        _fail("pass --region, e.g. --region ap-south-1")
    project = Project(Path.cwd())
    with _user_errors():
        summary = run_scan(project, region=region, profile=aws_profile, role_arn=aws_role_arn)
    if summary.first_run:
        console.print(METADATA_NOTE, markup=False)
    console.print(
        f"Scanned AWS account {summary.account_id} in {summary.region}: "
        f"{len(summary.files)} snapshot file(s)."
    )
    _print_findings(summary.findings)
    console.print("Next: run `easymigrate analyze`.", markup=False)


@app.command()
def analyze() -> None:
    """Turn the latest scan into .easymigrate/ir.json."""
    project = Project(Path.cwd())
    with _user_errors():
        doc = run_analyze(project)
    counts = Counter(obj.kind for obj in doc.objects)
    table = Table("Kind", "Count", title="Intent objects")
    for kind in sorted(counts):
        table.add_row(kind, str(counts[kind]))
    console.print(table)
    _print_findings(doc.findings)
    console.print(f"Wrote {project.ir_path.relative_to(project.root)}.", markup=False)
```

- [ ] **Step 4: Run and confirm it passes**

Run: `uv run pytest tests/test_cli_scan_analyze.py tests/test_cli.py tests/test_dependency_rules.py -v`
Expected: all pass.

- [ ] **Step 5: Try it by hand**

Run `uv run easymigrate scan --help` and check that `--region`, `--aws-profile` and `--aws-role-arn` are listed. Then run `uv run easymigrate analyze` in an empty temporary directory and check you get `Error: _scan.json not found. Run `easymigrate scan` first.` with exit code 1 (`echo $?`).

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`

```bash
git add src/easymigrate/cli.py tests/test_cli_scan_analyze.py
git commit -m "$(cat <<'EOF'
feat(cli): scan and analyze commands with clean user-facing errors

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Golden fixtures (anonymizer, moto scenarios, golden tests)

**Files:**
- Create: `scripts/anonymize_fixture.py`, `tests/test_anonymize_fixture.py`, `tests/plugins/network/test_golden.py`, `scripts/make_network_fixtures.py`
- Generated (committed): `tests/plugins/network/fixtures/<case>/snapshot/*.json` and `tests/plugins/network/fixtures/<case>/expected_ir.json`, for the 8 spec cases `basic_public`, `private_with_nat`, `sg_references_sg`, `custom_nacl`, `peering_route`, `multi_vpc`, `ipv6` and `external_sg_ref`

**Interfaces:**
- Consumes: `NetworkPlugin` (Task 8); `collect` (Task 4); `connect` (Task 2); `Snapshot`, `NormalizeContext`, `CollectContext` (Task 3); the `update_golden` fixture (Task 1)
- Produces:
  - `scripts/anonymize_fixture.py`:
    - `FIXTURE_ACCOUNT = "123456789012"`
    - `anonymize_files(files: dict[str, Any], account_id: str) -> dict[str, Any]`
    - a CLI: `SRC DEST --account-id`
  - `scripts/make_network_fixtures.py`, which regenerates every fixture snapshot
  - golden tests that fail on any change to normalizer output

**Why moto scenarios:** they drive the **real collector**, so the fixtures have real AWS response shapes. `fix_moto_quirks` corrects the one place where moto 5 differs from AWS: a subnet's IPv6 association state. `keep_vpcs` drops moto's always-present default VPC. Each case also has one **key fact** assertion, so a wrong golden file can't pass review unnoticed.

- [ ] **Step 1: Write the failing anonymizer test**

`tests/test_anonymize_fixture.py`:
```python
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from anonymize_fixture import FIXTURE_ACCOUNT, anonymize_files  # noqa: E402


def test_ids_accounts_ips_and_times_are_replaced_consistently():
    files = {
        "network.vpcs": [
            {
                "VpcId": "vpc-0a1b2c3d4e5f60718",
                "OwnerId": "538342414628",
                "CidrBlock": "10.0.0.0/16",
            }
        ],
        "network.subnets": [
            {
                "SubnetId": "subnet-09125e7961d295170",
                "VpcId": "vpc-0a1b2c3d4e5f60718",
                "SubnetArn": "arn:aws:ec2:ap-south-1:538342414628:subnet/subnet-09125e7961d295170",
            }
        ],
        "network.nat_gateways": [
            {
                "NatGatewayId": "nat-0123456789abcdef0",
                "CreateTime": datetime(2026, 9, 15, tzinfo=UTC),
                "NatGatewayAddresses": [{"PublicIp": "13.232.1.2"}],
            }
        ],
        "network.vpc_attributes": {"vpc-0a1b2c3d4e5f60718": {"enableDnsSupport": True}},
    }

    result = anonymize_files(files, "538342414628")

    vpc_id = result["network.vpcs"][0]["VpcId"]
    assert vpc_id == "vpc-00000000000000001"
    assert result["network.subnets"][0]["VpcId"] == vpc_id
    assert result["network.vpc_attributes"] == {vpc_id: {"enableDnsSupport": True}}
    assert result["network.vpcs"][0]["OwnerId"] == FIXTURE_ACCOUNT
    assert result["network.vpcs"][0]["CidrBlock"] == "10.0.0.0/16"
    assert result["network.subnets"][0]["SubnetArn"] == (
        "arn:aws:ec2:ap-south-1:123456789012:subnet/subnet-00000000000000001"
    )
    nat = result["network.nat_gateways"][0]
    assert nat["CreateTime"] == "2026-01-01T00:00:00+00:00"
    assert nat["NatGatewayAddresses"] == [{"PublicIp": "203.0.113.1"}]
```

Run: `uv run pytest tests/test_anonymize_fixture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anonymize_fixture'`.

- [ ] **Step 2: Implement the anonymizer**

`scripts/anonymize_fixture.py`:
```python
"""Anonymize a real AWS snapshot before it is saved as a test fixture (spec §7).

    uv run python scripts/anonymize_fixture.py SRC_DIR DEST_DIR --account-id 538342414628

Replaces the account id, every AWS resource id (consistently across files), public IPs and
timestamps. Only `*.json` files other than `_scan.json` are copied."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

FIXTURE_ACCOUNT = "123456789012"
_RESOURCE_ID = re.compile(
    r"\b(vpc-cidr-assoc|subnet-cidr-assoc|rtbassoc|aclassoc|eipalloc|eipassoc|vpce|vpc|subnet"
    r"|rtb|eigw|igw|nat|sg|acl|eni|pcx|dopt|pl|tgw|vgw|i)-[0-9a-f]{8,17}\b"
)
_TIMESTAMP_KEYS = frozenset({"CreateTime", "DeleteTime", "LaunchTime"})
_PUBLIC_IP_KEYS = frozenset({"PublicIp", "CarrierIp"})


class Anonymizer:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self._ids: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self._ips: dict[str, str] = {}

    def _resource_id(self, match: re.Match[str]) -> str:
        original = match.group(0)
        if original not in self._ids:
            prefix = match.group(1)
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            self._ids[original] = f"{prefix}-{self._counters[prefix]:017x}"
        return self._ids[original]

    def _public_ip(self, value: str) -> str:
        if value not in self._ips:
            self._ips[value] = f"203.0.113.{len(self._ips) + 1}"
        return self._ips[value]

    def value(self, data: Any, key: str | None = None) -> Any:
        if isinstance(data, dict):
            return {k: self.value(data[k], k) for k in sorted(data)}
        if isinstance(data, list):
            return [self.value(item) for item in data]
        if key in _TIMESTAMP_KEYS:
            return "2026-01-01T00:00:00+00:00"
        if isinstance(data, datetime):
            return data.isoformat()
        if isinstance(data, str):
            if key in _PUBLIC_IP_KEYS:
                return self._public_ip(data)
            text = data.replace(self.account_id, FIXTURE_ACCOUNT)
            return _RESOURCE_ID.sub(self._resource_id, text)
        return data

    def mapping_key(self, key: str) -> str:
        return _RESOURCE_ID.sub(self._resource_id, key.replace(self.account_id, FIXTURE_ACCOUNT))


def anonymize_files(files: dict[str, Any], account_id: str) -> dict[str, Any]:
    """Anonymize snapshot files together so resource ids stay consistent across them."""
    anonymizer = Anonymizer(account_id)
    result: dict[str, Any] = {}
    for name in sorted(files):
        data = files[name]
        if name.endswith("vpc_attributes") and isinstance(data, dict):
            result[name] = {
                anonymizer.mapping_key(k): anonymizer.value(v) for k, v in sorted(data.items())
            }
        else:
            result[name] = anonymizer.value(data)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("dest", type=Path)
    parser.add_argument("--account-id", required=True)
    args = parser.parse_args()
    files = {
        path.name.removesuffix(".json"): json.loads(path.read_text())
        for path in sorted(args.source.glob("*.json"))
        if path.name != "_scan.json"
    }
    args.dest.mkdir(parents=True, exist_ok=True)
    for name, data in anonymize_files(files, args.account_id).items():
        (args.dest / f"{name}.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
```

Run: `uv run pytest tests/test_anonymize_fixture.py -v`
Expected: PASS.

- [ ] **Step 3: Write the golden test**

`tests/plugins/network/test_golden.py`:
```python
"""Golden regression tests: anonymized snapshots → expected IR (spec §7).

Regenerate snapshots with `uv run python scripts/make_network_fixtures.py`, then expected files
with `uv run pytest --update-golden tests/plugins/network/test_golden.py`, and review the diff."""

from collections.abc import Callable
from pathlib import Path

import pytest

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir import IRDocument
from easymigrate.ir.network import ExternalPolicyPeer, PolicyPeer
from easymigrate.plugins.network import NetworkPlugin

FIXTURES = Path(__file__).parent / "fixtures"
CASES = sorted(p.name for p in FIXTURES.glob("*") if (p / "snapshot").is_dir())


def _kinds(doc: IRDocument, kind: str) -> list:
    return [o for o in doc.objects if o.kind == kind]


def _peers(doc: IRDocument) -> list:
    return [rule.peer for policy in _kinds(doc, "firewall_policy") for rule in policy.rules]


# One key fact per case, so a wrong golden file cannot silently pass review.
KEY_FACTS: dict[str, Callable[[IRDocument], bool]] = {
    "basic_public": lambda doc: {s.internet for s in _kinds(doc, "subnet")} == {"public"},
    "private_with_nat": lambda doc: _kinds(doc, "nat")[0].serves_subnets == ["subnet:private"],
    "sg_references_sg": lambda doc: any(isinstance(p, PolicyPeer) for p in _peers(doc)),
    "custom_nacl": lambda doc: _kinds(doc, "network_acl")[0].subnets == ["subnet:db"],
    "peering_route": lambda doc: _kinds(doc, "custom_route")[0].target_type == "peering",
    "multi_vpc": lambda doc: len(_kinds(doc, "network")) == 2,
    "ipv6": lambda doc: (
        bool(_kinds(doc, "network")[0].ipv6_cidr_blocks)
        and _kinds(doc, "subnet")[0].ipv6_cidr is not None
    ),
    "external_sg_ref": lambda doc: any(isinstance(p, ExternalPolicyPeer) for p in _peers(doc)),
}


def _normalize(case: str) -> IRDocument:
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)
    objects = NetworkPlugin().normalize(Snapshot.from_dir(FIXTURES / case / "snapshot"), ctx)
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=objects,
        findings=ctx.findings,
    )


def test_every_spec_case_has_a_fixture():
    assert set(CASES) == set(KEY_FACTS)


@pytest.mark.parametrize("case", CASES)
def test_golden(case, update_golden):
    doc = _normalize(case)
    actual = doc.model_dump_json(indent=2) + "\n"
    expected_path = FIXTURES / case / "expected_ir.json"

    if update_golden:
        expected_path.write_text(actual)

    assert expected_path.exists(), "missing golden file; run pytest --update-golden"
    assert actual == expected_path.read_text()
    assert KEY_FACTS[case](doc), f"key fact for {case} does not hold"
```

Run: `uv run pytest tests/plugins/network/test_golden.py -v`
Expected: FAIL in `test_every_spec_case_has_a_fixture`, because no fixtures exist yet (`CASES` is empty).

- [ ] **Step 4: Write the fixture generator**

`scripts/make_network_fixtures.py`:
```python
"""Regenerate network golden fixtures from moto scenarios (spec §7).

    uv run python scripts/make_network_fixtures.py

Each scenario builds resources in a mocked AWS account, runs the real collector, keeps only
the scenario's own VPCs (moto always creates a default VPC), anonymizes, and writes
tests/plugins/network/fixtures/<case>/snapshot/. Then run `uv run pytest --update-golden
tests/plugins/network/test_golden.py` and review the expected_ir.json diffs."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from anonymize_fixture import anonymize_files  # noqa: E402

from easymigrate.core.plugin import CollectContext  # noqa: E402
from easymigrate.plugins.network.collect_aws import collect  # noqa: E402
from easymigrate.providers.aws.source import connect  # noqa: E402

FIXTURES = ROOT / "tests" / "plugins" / "network" / "fixtures"
REGION = "ap-south-1"
MOTO_ACCOUNT = "123456789012"


def _tags(name: str) -> list[dict[str, Any]]:
    return [{"Key": "Name", "Value": name}]


def _vpc(ec2: Any, cidr: str, name: str, **kwargs: Any) -> str:
    return ec2.create_vpc(
        CidrBlock=cidr, TagSpecifications=[{"ResourceType": "vpc", "Tags": _tags(name)}], **kwargs
    )["Vpc"]["VpcId"]


def _subnet(
    ec2: Any, vpc: str, cidr: str, name: str, zone: str = "ap-south-1a", **kwargs: Any
) -> str:
    return ec2.create_subnet(
        VpcId=vpc,
        CidrBlock=cidr,
        AvailabilityZone=zone,
        TagSpecifications=[{"ResourceType": "subnet", "Tags": _tags(name)}],
        **kwargs,
    )["Subnet"]["SubnetId"]


def _route_table(ec2: Any, vpc: str, name: str, subnets: list[str]) -> str:
    table = ec2.create_route_table(
        VpcId=vpc, TagSpecifications=[{"ResourceType": "route-table", "Tags": _tags(name)}]
    )["RouteTable"]["RouteTableId"]
    for subnet in subnets:
        ec2.associate_route_table(RouteTableId=table, SubnetId=subnet)
    return table


def _igw(ec2: Any, vpc: str) -> str:
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    return igw


def basic_public(ec2: Any) -> list[str]:
    vpc = _vpc(ec2, "10.0.0.0/16", "migration-test-vpc")
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value": True})
    one = _subnet(ec2, vpc, "10.0.1.0/24", "migration-test-subnet-1", "ap-south-1a")
    two = _subnet(ec2, vpc, "10.0.2.0/24", "migration-test-subnet-2", "ap-south-1b")
    for subnet in (one, two):
        ec2.modify_subnet_attribute(SubnetId=subnet, MapPublicIpOnLaunch={"Value": True})
    table = _route_table(ec2, vpc, "migration-test-rt", [one, two])
    ec2.create_route(RouteTableId=table, DestinationCidrBlock="0.0.0.0/0", GatewayId=_igw(ec2, vpc))
    return [vpc]


def private_with_nat(ec2: Any) -> list[str]:
    vpc = _vpc(ec2, "10.10.0.0/16", "app-vpc")
    public = _subnet(ec2, vpc, "10.10.1.0/24", "public")
    private = _subnet(ec2, vpc, "10.10.2.0/24", "private")
    public_table = _route_table(ec2, vpc, "public-rt", [public])
    ec2.create_route(
        RouteTableId=public_table, DestinationCidrBlock="0.0.0.0/0", GatewayId=_igw(ec2, vpc)
    )
    allocation = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(
        SubnetId=public,
        AllocationId=allocation,
        TagSpecifications=[{"ResourceType": "natgateway", "Tags": _tags("egress-nat")}],
    )["NatGateway"]["NatGatewayId"]
    private_table = _route_table(ec2, vpc, "private-rt", [private])
    ec2.create_route(RouteTableId=private_table, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat)
    return [vpc]


def sg_references_sg(ec2: Any) -> list[str]:
    vpc = _vpc(ec2, "10.20.0.0/16", "web-vpc")
    web = ec2.create_security_group(GroupName="web", Description="web tier", VpcId=vpc)["GroupId"]
    db = ec2.create_security_group(GroupName="db", Description="database", VpcId=vpc)["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=web,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "https"}],
            },
            {
                "IpProtocol": "icmp",
                "FromPort": 8,
                "ToPort": -1,
                "IpRanges": [{"CidrIp": "10.20.0.0/16"}],
            },
        ],
    )
    ec2.authorize_security_group_ingress(
        GroupId=db,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 5432,
                "ToPort": 5432,
                "UserIdGroupPairs": [{"GroupId": web, "Description": "from web"}],
            },
            {"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": db}]},
        ],
    )
    ec2.authorize_security_group_egress(
        GroupId=web,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 5432,
                "ToPort": 5432,
                "UserIdGroupPairs": [{"GroupId": db}],
            }
        ],
    )
    return [vpc]


def custom_nacl(ec2: Any) -> list[str]:
    vpc = _vpc(ec2, "10.30.0.0/16", "locked-vpc")
    subnet = _subnet(ec2, vpc, "10.30.1.0/24", "db")
    acl = ec2.create_network_acl(
        VpcId=vpc, TagSpecifications=[{"ResourceType": "network-acl", "Tags": _tags("db-acl")}]
    )["NetworkAcl"]["NetworkAclId"]
    entries = [
        dict(
            RuleNumber=100,
            Protocol="6",
            RuleAction="allow",
            Egress=False,
            CidrBlock="10.30.0.0/16",
            PortRange={"From": 5432, "To": 5432},
        ),
        dict(
            RuleNumber=110,
            Protocol="1",
            RuleAction="allow",
            Egress=False,
            CidrBlock="10.30.0.0/16",
            IcmpTypeCode={"Type": 8, "Code": -1},
        ),
        dict(
            RuleNumber=100, Protocol="-1", RuleAction="allow", Egress=True, CidrBlock="10.30.0.0/16"
        ),
    ]
    for entry in entries:
        ec2.create_network_acl_entry(NetworkAclId=acl, **entry)
    association = next(
        a["NetworkAclAssociationId"]
        for n in ec2.describe_network_acls(Filters=[{"Name": "vpc-id", "Values": [vpc]}])[
            "NetworkAcls"
        ]
        for a in n["Associations"]
        if a["SubnetId"] == subnet
    )
    ec2.replace_network_acl_association(AssociationId=association, NetworkAclId=acl)
    return [vpc]


def peering_route(ec2: Any) -> list[str]:
    vpc = _vpc(ec2, "10.40.0.0/16", "requester")
    peer = _vpc(ec2, "172.16.0.0/16", "peer-not-migrated")
    subnet = _subnet(ec2, vpc, "10.40.1.0/24", "app")
    pcx = ec2.create_vpc_peering_connection(VpcId=vpc, PeerVpcId=peer)["VpcPeeringConnection"][
        "VpcPeeringConnectionId"
    ]
    table = _route_table(ec2, vpc, "app-rt", [subnet])
    ec2.create_route(
        RouteTableId=table, DestinationCidrBlock="172.16.0.0/16", VpcPeeringConnectionId=pcx
    )
    return [vpc]


def multi_vpc(ec2: Any) -> list[str]:
    prod = _vpc(ec2, "10.50.0.0/16", "prod")
    staging = _vpc(ec2, "10.50.0.0/16", "staging")
    _subnet(ec2, prod, "10.50.1.0/24", "public")
    _subnet(ec2, staging, "10.50.1.0/24", "public")
    return [prod, staging]


def ipv6(ec2: Any) -> list[str]:
    vpc = _vpc(ec2, "10.60.0.0/16", "dual-stack", AmazonProvidedIpv6CidrBlock=True)
    block = ec2.describe_vpcs(VpcIds=[vpc])["Vpcs"][0]["Ipv6CidrBlockAssociationSet"][0][
        "Ipv6CidrBlock"
    ]
    subnet_block = block.replace("::/56", "::/64")
    _subnet(ec2, vpc, "10.60.1.0/24", "dual", Ipv6CidrBlock=subnet_block)
    return [vpc]


def external_sg_ref(ec2: Any) -> list[str]:
    vpc = _vpc(ec2, "10.70.0.0/16", "consumer")
    other = _vpc(ec2, "10.71.0.0/16", "provider-not-migrated")
    remote = ec2.create_security_group(GroupName="remote", Description="remote", VpcId=other)[
        "GroupId"
    ]
    app = ec2.create_security_group(GroupName="app", Description="app", VpcId=vpc)["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=app,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 8080,
                "ToPort": 8080,
                "UserIdGroupPairs": [{"GroupId": remote, "VpcId": other}],
            }
        ],
    )
    return [vpc]


SCENARIOS: dict[str, Callable[[Any], list[str]]] = {
    "basic_public": basic_public,
    "private_with_nat": private_with_nat,
    "sg_references_sg": sg_references_sg,
    "custom_nacl": custom_nacl,
    "peering_route": peering_route,
    "multi_vpc": multi_vpc,
    "ipv6": ipv6,
    "external_sg_ref": external_sg_ref,
}


def fix_moto_quirks(files: dict[str, Any]) -> dict[str, Any]:
    """Make moto output match real AWS where moto is known to differ."""
    for subnet in files.get("network.subnets", []):
        for association in subnet.get("Ipv6CidrBlockAssociationSet") or []:
            state = association.get("Ipv6CidrBlockState")
            # moto stores {"State": "{'State': 'associated'}"}; AWS returns {"State": "associated"}
            if isinstance(state, dict) and str(state.get("State", "")).startswith("{"):
                association["Ipv6CidrBlockState"] = {"State": "associated"}
    return files


def keep_vpcs(files: dict[str, Any], vpc_ids: set[str]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for name, data in files.items():
        if name == "network.vpc_attributes":
            kept[name] = {k: v for k, v in data.items() if k in vpc_ids}
        elif name == "network.internet_gateways":
            kept[name] = [
                g
                for g in data
                if any(a.get("VpcId") in vpc_ids for a in g.get("Attachments") or [])
            ]
        else:
            kept[name] = [item for item in data if item.get("VpcId") in vpc_ids]
    return kept


def main() -> None:
    os.environ.update(
        AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_DEFAULT_REGION=REGION
    )
    for case, build in SCENARIOS.items():
        with mock_aws():
            connection = connect(region=REGION)
            vpc_ids = set(build(connection.client("ec2")))
            ctx = CollectContext(plugin="network")
            files = fix_moto_quirks(keep_vpcs(collect(connection, ctx), vpc_ids))
            if ctx.findings:
                raise SystemExit(f"{case}: unexpected scan findings {ctx.findings}")
        target = FIXTURES / case / "snapshot"
        target.mkdir(parents=True, exist_ok=True)
        for old in target.glob("*.json"):
            old.unlink()
        for name, data in anonymize_files(files, MOTO_ACCOUNT).items():
            (target / f"{name}.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"wrote {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Generate snapshots and golden files**

Run:
```bash
uv run python scripts/make_network_fixtures.py
uv run pytest --update-golden tests/plugins/network/test_golden.py
uv run pytest tests/plugins/network/test_golden.py -v
```
Expected: the generator prints `wrote tests/plugins/network/fixtures/<case>/snapshot` 8 times. Both pytest runs pass 9 tests (1 case-coverage test plus 8 goldens).

- [ ] **Step 6: Review the generated golden files**

For each `expected_ir.json`, check the objects match the scenario in `make_network_fixtures.py`. Report anything surprising as a concern. At minimum confirm:
- `basic_public`: network `network:migration-test-vpc`, two `public` subnets, and a `firewall_policy:default`. No NACL, because the default allow-all ACL is omitted.
- `private_with_nat`: subnet `public` is `public`, subnet `private` is `private_nat`, and `nat:egress-nat` serves `subnet:private`.
- `sg_references_sg`: `db` has a `PolicyPeer` to `firewall_policy:web` and a `SelfPeer`. `web` has ingress 443 with description `https`, and ICMP type 8.
- `custom_nacl`: `network_acl:db-acl` on `subnet:db`, with 3 rules and no rule 32767.
- `peering_route`: one `custom_route` with `target_type` `peering` and destination `172.16.0.0/16`.
- `multi_vpc`: `network:prod` and `network:staging`, and two subnets named `public` with native-id suffixed ids.
- `ipv6`: the network has an IPv6 `/56` and the subnet an IPv6 `/64`.
- `external_sg_ref`: `app` has an `ExternalPolicyPeer` with a `vpc_id`.

Also confirm no file under `fixtures/` contains a real-looking account id other than `123456789012`: run `grep -rhoE "[0-9]{12}" tests/plugins/network/fixtures | sort -u | grep -v -e "^123456789012$" -e "^0*$"`, which should print nothing. All-zero digit runs come from anonymized resource ids and are expected.

- [ ] **Step 7: Lint and commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`

```bash
git add scripts tests/test_anonymize_fixture.py tests/plugins/network/test_golden.py tests/plugins/network/fixtures
git commit -m "$(cat <<'EOF'
test(network): anonymized moto-based golden fixtures for the eight spec cases

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: README usage and end-to-end check

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the `scan` and `analyze` commands (Task 10)
- Produces: user-facing usage docs

- [ ] **Step 1: Add a Usage section to `README.md`** directly after the first paragraph

```markdown
## Usage

    # 1. Read a live AWS region (read-only access is enough)
    easymigrate scan --region ap-south-1 --aws-profile my-readonly-profile
    #    or: easymigrate scan --region ap-south-1 --aws-role-arn arn:aws:iam::123456789012:role/EasyMigrateScan

    # 2. Turn the scan into the intent-level IR (.easymigrate/ir.json)
    easymigrate analyze

Both commands work in the current directory and keep their output in `.easymigrate/`. That folder holds account metadata such as account IDs and ARNs; keep it private. It is added to `.gitignore` automatically inside a git repository.

Permissions `scan` needs: `ec2:DescribeVpcs`, `ec2:DescribeVpcAttribute`, `ec2:DescribeSubnets`, `ec2:DescribeRouteTables`, `ec2:DescribeInternetGateways`, `ec2:DescribeNatGateways`, `ec2:DescribeSecurityGroups`, `ec2:DescribeNetworkAcls`, `tag:GetResources`, `sts:GetCallerIdentity`. Missing permissions are reported as scan gaps rather than failing the scan.
```

- [ ] **Step 2: End-to-end check against mocked AWS**

Run:
```bash
uv run pytest && uv run ruff format --check . && uv run ruff check .
uv run pytest tests/test_cli_scan_analyze.py::test_scan_then_analyze -v
```
Expected: everything passes.

- [ ] **Step 3 (optional, only with the user's go-ahead): real read-only scan**

In an empty temporary directory, with read-only AWS credentials, run `uv run easymigrate scan --region ap-south-1` and then `uv run easymigrate analyze`. Both should exit 0 and write `.easymigrate/ir.json`. This makes only read calls, and costs nothing.

- [ ] **Step 4: Commit and push**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs: scan and analyze usage and required permissions

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
git push
```
