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


def connect(
    *, region: str, profile: str | None = None, role_arn: str | None = None
) -> AwsConnection:
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
        identity = session.client(
            "sts", region_name=region, config=CLIENT_CONFIG
        ).get_caller_identity()
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
