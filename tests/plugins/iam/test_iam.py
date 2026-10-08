"""IAM report-only family (spec 8): collection, role analysis, suggestions, report linking."""

from __future__ import annotations

import json

import pytest
from moto import mock_aws

from easymigrate.core.plugin import CollectContext, NormalizeContext, Snapshot
from easymigrate.core.report import Report, _iam_view
from easymigrate.differences.rules.aws_gcp.iam_facts import suggest
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.compute import ComputeInstance
from easymigrate.ir.iam import IamRole
from easymigrate.ir.kubernetes import KubernetesCluster, NodePool, PodIdentity
from easymigrate.plugins.iam.collect_aws import collect
from easymigrate.plugins.iam.generate_gcp import FIELD_COVERAGE
from easymigrate.plugins.iam.normalize import action_level, normalize
from easymigrate.providers.aws.source import connect

V = "2012-10-17"
EC2_TRUST = {
    "Version": V,
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("s3:GetObject", ("s3", "read")),
        ("s3:List*", ("s3", "read")),
        ("s3:PutObject", ("s3", "write")),
        ("sqs:*", ("sqs", "admin")),
        ("*", ("*", "admin")),
        # Live run, 2026-10-08: consuming a queue includes deleting what was received.
        ("sqs:DeleteMessage", ("sqs", "read")),
        ("sqs:SendMessage", ("sqs", "write")),
        # Parameter Store and the Systems Manager agent share a prefix and nothing else.
        ("ssm:GetParameter", ("ssm-parameters", "read")),
        ("ssm:UpdateInstanceInformation", ("ssm-agent", "write")),
        ("ssmmessages:OpenDataChannel", ("ssm-agent", "write")),
    ],
)
def test_action_levels(action, expected):
    assert action_level(action) == expected


def test_suggestions_fall_back_to_the_nearest_known_level():
    assert suggest("s3", "read") == ["roles/storage.objectViewer"]
    assert suggest("sns", "read") == ["roles/pubsub.publisher"]  # only write/admin known
    assert suggest("kms", "read") == ["roles/cloudkms.cryptoKeyEncrypterDecrypter"]
    assert suggest("unknown-service", "write") == []


@pytest.fixture
def aws(aws_credentials, monkeypatch):
    monkeypatch.setenv("MOTO_IAM_LOAD_MANAGED_POLICIES", "true")
    with mock_aws():
        yield connect(region="ap-south-1")


def test_collection_reads_roles_and_attached_aws_managed_documents_without_credentials(aws):
    iam = aws.client("iam")
    iam.create_role(RoleName="app", AssumeRolePolicyDocument=json.dumps(EC2_TRUST))
    iam.attach_role_policy(
        RoleName="app", PolicyArn="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
    )
    iam.create_instance_profile(InstanceProfileName="app")
    iam.add_role_to_instance_profile(InstanceProfileName="app", RoleName="app")
    iam.create_user(UserName="alice")
    iam.create_access_key(UserName="alice")

    files = collect(aws, CollectContext(plugin="iam"))

    role = files["iam.authorization"]["Roles"][0]
    assert role["RoleName"] == "app" and role["InstanceProfileList"]
    assert "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess" in files["iam.aws_managed_policies"]
    assert files["iam.authorization"]["Users"] == [{"UserName": "alice", "Groups": []}]
    assert "AccessKey" not in json.dumps(files, default=str)


def _snapshot(*roles):
    return Snapshot(
        {
            "iam.authorization": {
                "Roles": list(roles),
                "Users": [{"UserName": "u", "Groups": []}],
                "Groups": [],
                "Policies": [],
            },
            "iam.aws_managed_policies": {},
        }
    )


def _role(name, policy, trust=EC2_TRUST, **extra):
    role = {
        "RoleName": name,
        "Arn": f"arn:aws:iam::1:role/{name}",
        "Path": "/",
        "AssumeRolePolicyDocument": trust,
        "RolePolicyList": [{"PolicyName": "inline", "PolicyDocument": policy}],
        "AttachedManagedPolicies": [],
        "InstanceProfileList": [{"Arn": f"arn:aws:iam::1:instance-profile/{name}"}],
    }
    role.update(extra)
    return role


def test_roles_are_summarised_per_service_and_service_linked_roles_skipped():
    policy = {
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": "arn:aws:s3:::b/*",
            },
            {"Effect": "Allow", "Action": "sqs:ReceiveMessage", "Resource": "*"},
            {"Effect": "Deny", "Action": "*", "Resource": "*"},
        ]
    }
    linked = _role("AWSServiceRoleForX", policy, Path="/aws-service-role/x/")
    ctx = NormalizeContext(account_id="1", region="ap-south-1", strict=False)
    roles = normalize(_snapshot(_role("app", policy), linked), ctx)

    assert [r.name for r in roles] == ["app"]
    role = roles[0]
    assert [(a.service, a.level) for a in role.access] == [("s3", "write"), ("sqs", "read")]
    assert role.wildcard_resources and not role.admin_everything  # a Deny never grants
    assert role.trusted_services == ["ec2.amazonaws.com"]
    assert [f.code for f in ctx.findings] == ["IAM-ACCOUNT-SUMMARY"]


def test_url_encoded_documents_are_read():
    from urllib.parse import quote

    encoded = quote(
        json.dumps({"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]})
    )
    ctx = NormalizeContext(account_id="1", region="ap-south-1", strict=False)
    assert normalize(_snapshot(_role("admin", encoded)), ctx)[0].admin_everything


def _src(t, i):
    return SourceRef(cloud="aws", type=t, id=i, region="ap-south-1")


def test_report_links_roles_to_the_migrated_objects_that_use_them():
    def role(name, access, **kw):
        return IamRole(
            id=f"iam_role:{name}",
            name=name,
            source=_src("iam-role", name),
            arn=f"arn:aws:iam::1:role/{name}",
            access=access,
            **kw,
        )

    from easymigrate.ir.iam import ServiceAccess

    app = role(
        "app",
        [ServiceAccess(service="s3", level="read")],
        instance_profiles=["arn:aws:iam::1:instance-profile/app"],
        wildcard_resources=True,
    )
    nodes = role("nodes", [ServiceAccess(service="ecr", level="read")])
    pod = role("pod", [ServiceAccess(service="*", level="admin")], admin_everything=True)
    spare = role("spare", [])
    instance = ComputeInstance(
        id="compute_instance:web",
        name="web",
        source=_src("instance", "i-1"),
        instance_type="t3.micro",
        architecture="x86_64",
        state="running",
        zone="ap-south-1a",
        subnet="subnet:a",
        image="ami-1",
        platform="linux",
        iam_profile="arn:aws:iam::1:instance-profile/app",
    )
    cluster = KubernetesCluster(
        id="kubernetes_cluster:prod",
        name="prod",
        source=_src("eks", "prod"),
        version="1.31",
        pod_identities=[PodIdentity(namespace="app", service_account="api", role_arn=pod.arn)],
    )
    pool = NodePool(
        id="node_pool:workers",
        name="workers",
        source=_src("ng", "w"),
        cluster=cluster.id,
        origin="managed",
        min_size=1,
        max_size=1,
        desired_size=1,
        ami_type="AL2_x86_64",
        role_arn=nodes.arn,
    )
    doc = IRDocument.model_construct(
        source={"cloud": "aws"},
        objects=[app, nodes, pod, spare, instance, cluster, pool],
        findings=[],
    )

    view = _iam_view(doc)
    rows = {r.role: r for r in view.roles}
    assert rows["app"].used_by == ["compute_instance:web"]
    assert rows["app"].services == ["s3: read → roles/storage.objectViewer"]
    assert "service account attached to the VM" in rows["app"].attach_as
    assert rows["nodes"].used_by == ["node_pool:workers"]
    assert "Workload Identity" in rows["pod"].attach_as
    assert "do not copy this" in rows["pod"].warnings[0]
    assert view.unused_roles == ["spare"]
    markdown = Report(source="aws", target="gcp", coverage={}, iam=view).to_markdown()
    assert "## Identity and access" in markdown and "No IAM was migrated" in markdown


def test_every_iam_role_field_is_declared():
    assert set(IamRole.model_fields) == set(FIELD_COVERAGE["iam_role"])
