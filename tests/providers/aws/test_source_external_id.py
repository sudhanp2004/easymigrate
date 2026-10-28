"""Role assumption with an external ID (hosted report, spec "Connecting AWS")."""

import boto3
from moto import mock_aws

from easymigrate.providers.aws.source import connect

ROLE = "arn:aws:iam::123456789012:role/EasyMigrateReadOnly-abcd1234"


def _spy_assume_role(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    real_client = boto3.Session.client

    def client(self, service, *args, **kwargs):
        real = real_client(self, service, *args, **kwargs)
        if service == "sts":
            original = real.assume_role

            def assume_role(**params):
                calls.append(params)
                return original(**params)

            real.assume_role = assume_role
        return real

    monkeypatch.setattr(boto3.Session, "client", client)
    return calls


def test_connect_passes_external_id_and_session_name(aws_credentials, monkeypatch):
    calls = _spy_assume_role(monkeypatch)
    with mock_aws():
        connect(
            region="ap-south-1",
            role_arn=ROLE,
            external_id="abc0123456789def",
            session_name="easymigrate-ws12345678",
        )

    assert calls == [
        {
            "RoleArn": ROLE,
            "RoleSessionName": "easymigrate-ws12345678",
            "ExternalId": "abc0123456789def",
        }
    ]


def test_connect_without_external_id_omits_it(aws_credentials, monkeypatch):
    calls = _spy_assume_role(monkeypatch)
    with mock_aws():
        connect(region="ap-south-1", role_arn=ROLE)

    assert calls == [{"RoleArn": ROLE, "RoleSessionName": "easymigrate-scan"}]
