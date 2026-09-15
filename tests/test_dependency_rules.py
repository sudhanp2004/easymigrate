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
                if (
                    _within(name, "easymigrate.plugins")
                    or _within(name, "easymigrate.providers")
                    or _within(name, "easymigrate.engine")
                ):
                    bad(
                        name,
                        "only easymigrate.core.registry may import plugins, providers or engine",
                    )
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
    if _within(module, "easymigrate.engine"):
        for name in internal:
            if not _within(name, "easymigrate.core.errors"):
                bad(name, "engine may only import easymigrate.core.errors")
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
        (
            "easymigrate.core.pipeline",
            "from easymigrate.providers.aws.source import connect",
            False,
        ),
        (
            "easymigrate.core.registry",
            "from easymigrate.plugins.network import NetworkPlugin",
            True,
        ),
        ("easymigrate.core.registry", "from easymigrate.providers.aws import source", True),
        ("easymigrate.core.registry", "import boto3", False),
        ("easymigrate.cli", "from easymigrate.core.pipeline import scan", True),
        ("easymigrate.cli", "from easymigrate import __version__", True),
        ("easymigrate.cli", "from easymigrate.ir import IRDocument", False),
        (
            "easymigrate.plugins.network.normalize",
            "from easymigrate.core.plugin import Snapshot",
            True,
        ),
        (
            "easymigrate.plugins.network.normalize",
            "from easymigrate.core.project import Project",
            False,
        ),
        (
            "easymigrate.plugins.network.normalize",
            "from easymigrate.differences.model import X",
            False,
        ),
        (
            "easymigrate.plugins.network.collect_aws",
            "from easymigrate.providers.aws.source import x",
            True,
        ),
        ("easymigrate.differences.engine", "from easymigrate.ir import IRDocument", True),
        ("easymigrate.differences.engine", "from easymigrate.core.project import Project", False),
        ("easymigrate.ir.network", "from easymigrate.core.project import Project", False),
        ("easymigrate.engine.tofu", "from easymigrate.core.errors import EasyMigrateError", True),
        ("easymigrate.engine.tofu", "from easymigrate.core.project import Project", False),
        (
            "easymigrate.core.registry",
            "from easymigrate.engine.tofu import ensure_tofu",
            True,
        ),
        (
            "easymigrate.core.pipeline",
            "from easymigrate.engine.tofu import ensure_tofu",
            False,
        ),
    ],
)
def test_rule_checker(module, source, allowed):
    assert (violations(module, imported_modules(source)) == []) is allowed
