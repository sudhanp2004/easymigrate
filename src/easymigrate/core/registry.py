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
