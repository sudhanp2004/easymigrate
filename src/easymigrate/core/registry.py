"""Composition root: the only core module allowed to import plugins, providers and the
OpenTofu engine."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from easymigrate.core.errors import SourceConnectionError
from easymigrate.core.plugin import Plugin
from easymigrate.engine import tofu as tofu_engine
from easymigrate.plugins.network import NetworkPlugin
from easymigrate.providers.aws import source as aws_source
from easymigrate.providers.gcp import boilerplate, state_bucket

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


class GcpTarget(Protocol):
    """What `core.pipeline.plan()` needs from the GCP target: credentials and the state
    bucket (spec §5.4 steps 2-3). Core never imports `google` directly; it goes through
    this seam, which tests fake by monkeypatching `registry.gcp_target`."""

    def credentials_ok(self) -> bool: ...

    def ensure_bucket(
        self, gcp_project: str, name: str, region: str, confirm: Callable[[str], bool]
    ) -> bool: ...


class _GcpTarget:
    def credentials_ok(self) -> bool:
        return state_bucket.credentials_ok()

    def ensure_bucket(
        self, gcp_project: str, name: str, region: str, confirm: Callable[[str], bool]
    ) -> bool:
        return state_bucket.ensure_bucket(gcp_project, name, region, confirm)


def gcp_target() -> GcpTarget:
    return _GcpTarget()


def ensure_tofu(cache_dir: Path | None = None) -> Path:
    """Resolve the `tofu` binary through `easymigrate.engine.tofu`, so tests can fake
    downloads by monkeypatching `easymigrate.engine.tofu.ensure_tofu`."""
    return tofu_engine.ensure_tofu(cache_dir)


def new_tofu(binary: Path, workdir: Path) -> tofu_engine.Tofu:
    return tofu_engine.Tofu(binary, workdir)


def generate_boilerplate_tf(gcp_project: str, gcp_region: str, version: str) -> dict[str, str]:
    """The static terraform/*.tf files `plan` writes alongside plugin output (spec §5.2)."""
    return {
        "providers.tf": boilerplate.render_providers_tf(version),
        "backend.tf": boilerplate.render_backend_tf(version),
        "variables.tf": boilerplate.render_variables_tf(version),
        "terraform.tfvars": boilerplate.render_tfvars(gcp_project, gcp_region, version),
    }
