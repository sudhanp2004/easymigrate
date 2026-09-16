import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from easymigrate.core.plugin import CollectContext
from easymigrate.plugins.database.collect_aws import DATABASE_SNAPSHOT_FILES, collect
from easymigrate.providers.aws import source
from easymigrate.providers.aws.source import connect


def _build_vpc_and_subnets(ec2, count=2):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = []
    for i, zone in zip(range(count), ("ap-south-1a", "ap-south-1b"), strict=False):
        subnet = ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=zone)[
            "Subnet"
        ]["SubnetId"]
        subnets.append(subnet)
    return vpc, subnets


def _build_rds(rds, ec2, *, instance_class="db.t3.micro", group_name="mygroup"):
    vpc, subnets = _build_vpc_and_subnets(ec2)
    sg = ec2.create_security_group(GroupName="db-sg", Description="db", VpcId=vpc)["GroupId"]
    rds.create_db_subnet_group(
        DBSubnetGroupName="subgrp1",
        DBSubnetGroupDescription="subgrp1",
        SubnetIds=subnets,
    )
    rds.create_db_parameter_group(
        DBParameterGroupName=group_name,
        DBParameterGroupFamily="postgres15",
        Description="pg",
    )
    rds.modify_db_parameter_group(
        DBParameterGroupName=group_name,
        Parameters=[
            {
                "ParameterName": "log_min_duration_statement",
                "ParameterValue": "1000",
                "ApplyMethod": "immediate",
            }
        ],
    )
    instance = rds.create_db_instance(
        DBInstanceIdentifier="testdb",
        Engine="postgres",
        EngineVersion="15.5",
        DBInstanceClass=instance_class,
        AllocatedStorage=20,
        MasterUsername="postgres",
        MasterUserPassword="hunter22222",
        DBSubnetGroupName="subgrp1",
        VpcSecurityGroupIds=[sg],
        DBParameterGroupName=group_name,
    )["DBInstance"]
    return vpc, subnets, sg, instance["DBInstanceArn"]


def _deny(connection_cls, monkeypatch, service: str, operation: str, code: str = "AccessDenied"):
    original = connection_cls.client

    def client(self, svc):
        c = original(self, svc)

        def raise_error(**kwargs):
            raise ClientError({"Error": {"Code": code, "Message": "denied"}}, operation)

        c.meta.events.register(f"before-call.{service}.{operation}", raise_error)
        return c

    monkeypatch.setattr(connection_cls, "client", client)


def _customize_client(connection_cls, monkeypatch, service: str, customize) -> None:
    """Patch `connection_cls.client` so every client it hands out for `service` is passed
    through `customize(client)` before use. `AwsConnection.client()` builds a fresh client on
    every call (verified: two calls for the same service are different objects with different
    event systems), so registering a handler on a client obtained separately from the one
    `collect()` uses observes nothing; patching at the class level is the only way to see what
    `collect()` itself does."""
    original = connection_cls.client

    def client(self, svc):
        c = original(self, svc)
        if svc == service:
            customize(c)
        return c

    monkeypatch.setattr(connection_cls, "client", client)


class _FakePaginator:
    """A paginator stand-in that always yields fixed, pre-baked pages, so a test can prove a
    real multi-page response is fully consumed regardless of moto's actual page size."""

    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **_kwargs):
        return iter(self._pages)


def test_collects_every_database_file(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        rds = conn.client("rds")
        ec2 = conn.client("ec2")
        _build_rds(rds, ec2)
        ctx = CollectContext(plugin="database")
        files = collect(conn, ctx)

    assert sorted(files) == sorted(DATABASE_SNAPSHOT_FILES)
    assert ctx.findings == []
    identifiers = [i["DBInstanceIdentifier"] for i in files["database.db_instances"]["DBInstances"]]
    assert "testdb" in identifiers
    assert files["database.db_subnet_groups"]["DBSubnetGroups"][0]["DBSubnetGroupName"] == "subgrp1"
    assert (
        files["database.db_parameter_groups"]["DBParameterGroups"][0]["DBParameterGroupName"]
        == "mygroup"
    )
    assert files["database.db_clusters"] == {"DBClusters": []}


def test_pagination_is_followed(aws_credentials, monkeypatch):
    # moto's default page size means 3 real instances come back as a single page (verified:
    # `len(list(paginator.paginate())) == 1`), so a test built on real instances alone cannot
    # tell "loops over every page" apart from "reads the first page and stops". A fake
    # paginator that genuinely hands back two pages closes that gap.
    pages = [
        {"DBInstances": [{"DBInstanceIdentifier": "db-0"}]},
        {"DBInstances": [{"DBInstanceIdentifier": "db-1"}, {"DBInstanceIdentifier": "db-2"}]},
    ]

    def customize(rds):
        real_get_paginator = rds.get_paginator

        def get_paginator(name):
            if name == "describe_db_instances":
                return _FakePaginator(pages)
            return real_get_paginator(name)

        rds.get_paginator = get_paginator

    with mock_aws():
        conn = connect(region="ap-south-1")
        _customize_client(source.AwsConnection, monkeypatch, "rds", customize)
        ctx = CollectContext(plugin="database")
        files = collect(conn, ctx)

    ids = {i["DBInstanceIdentifier"] for i in files["database.db_instances"]["DBInstances"]}
    assert ids == {"db-0", "db-1", "db-2"}
    # If the pagination loop were removed (only the first page read), this would be {"db-0"}.


def test_parameters_collected_per_attached_group_only_with_source_user(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        rds = conn.client("rds")
        ec2 = conn.client("ec2")
        _build_rds(rds, ec2, group_name="attached-group")
        rds.create_db_parameter_group(
            DBParameterGroupName="unattached-group",
            DBParameterGroupFamily="postgres15",
            Description="unused",
        )
        ctx = CollectContext(plugin="database")
        files = collect(conn, ctx)

    assert "attached-group" in files["database.db_parameters"]
    assert "unattached-group" not in files["database.db_parameters"]
    params = files["database.db_parameters"]["attached-group"]
    assert any(p["ParameterName"] == "log_min_duration_statement" for p in params)
    # moto's describe_db_parameters honors the Source=user filter (asserted by the
    # unattached-group check above) but does not echo a Source field back; real AWS does.


def test_parameters_request_includes_source_user_filter(aws_credentials, monkeypatch):
    # moto returns the same parameters whether or not Source=user is passed (verified: one
    # parameter either way), so nothing about the *response* can prove the filter was sent.
    # Recording the actual paginate() kwargs is the only way to catch it being dropped.
    calls: list[dict] = []

    def customize(rds):
        real_get_paginator = rds.get_paginator

        def get_paginator(name):
            paginator = real_get_paginator(name)
            if name == "describe_db_parameters":
                real_paginate = paginator.paginate

                def paginate(**kwargs):
                    calls.append(kwargs)
                    return real_paginate(**kwargs)

                paginator.paginate = paginate
            return paginator

        rds.get_paginator = get_paginator

    with mock_aws():
        conn = connect(region="ap-south-1")
        _customize_client(source.AwsConnection, monkeypatch, "rds", customize)
        rds = conn.client("rds")
        ec2 = conn.client("ec2")
        _build_rds(rds, ec2, group_name="attached-group")
        ctx = CollectContext(plugin="database")
        collect(conn, ctx)

    assert calls, "describe_db_parameters was never called"
    assert all(call.get("Source") == "user" for call in calls)
    assert any(call.get("DBParameterGroupName") == "attached-group" for call in calls)
    # If Source="user" were deleted from collect_aws.py, `call.get("Source")` would be None
    # and the second assertion would fail.


def test_instance_class_ec2_rejects_is_dropped_others_still_returned(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        rds = conn.client("rds")
        ec2 = conn.client("ec2")
        vpc, subnets = _build_vpc_and_subnets(ec2)
        rds.create_db_subnet_group(
            DBSubnetGroupName="subgrp1", DBSubnetGroupDescription="d", SubnetIds=subnets
        )
        rds.create_db_instance(
            DBInstanceIdentifier="normal-db",
            Engine="postgres",
            EngineVersion="15.5",
            DBInstanceClass="db.t3.micro",
            AllocatedStorage=20,
            MasterUsername="postgres",
            MasterUserPassword="hunter22222",
            DBSubnetGroupName="subgrp1",
        )
        rds.create_db_instance(
            DBInstanceIdentifier="serverless-db",
            Engine="postgres",
            EngineVersion="15.5",
            DBInstanceClass="db.serverless",
            AllocatedStorage=20,
            MasterUsername="postgres",
            MasterUserPassword="hunter22222",
            DBSubnetGroupName="subgrp1",
        )
        ctx = CollectContext(plugin="database")
        files = collect(conn, ctx)

    types = {t["InstanceType"] for t in files["database.instance_types"]["InstanceTypes"]}
    assert "t3.micro" in types
    assert "serverless" not in types
    assert ctx.findings == []


def test_access_denied_on_describe_db_instances_leaves_other_files(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        rds = conn.client("rds")
        ec2 = conn.client("ec2")
        _build_rds(rds, ec2)
        _deny(source.AwsConnection, monkeypatch, "rds", "DescribeDBInstances")
        ctx = CollectContext(plugin="database")
        files = collect(conn, ctx)

    assert "database.db_instances" not in files
    assert "database.db_subnet_groups" in files
    assert "database.db_clusters" in files
    assert [f.suggestion for f in ctx.findings] == ["Grant rds:DescribeDBInstances"]
    assert ctx.findings[0].code == "SCAN-ACCESS-DENIED"


def test_no_list_tags_for_resource_call_is_made(aws_credentials, monkeypatch):
    # AwsConnection.client() builds a fresh client (with its own event system) on every call
    # (verified: two calls for the same service are different objects), so registering a
    # handler on a client the test obtained separately from the one collect() uses would
    # observe nothing and pass unconditionally. Registering at the class level, the way _deny
    # does, is the only way to see what collect()'s own client does.
    calls: list[str] = []

    def customize(rds):
        rds.meta.events.register(
            "before-call.rds.ListTagsForResource",
            lambda **kwargs: calls.append(kwargs["event_name"]),
        )

    with mock_aws():
        conn = connect(region="ap-south-1")
        _customize_client(source.AwsConnection, monkeypatch, "rds", customize)
        rds = conn.client("rds")
        ec2 = conn.client("ec2")
        instance_arn = _build_rds(rds, ec2)[3]

        # Prove the handler actually observes the call before trusting its absence below.
        rds.list_tags_for_resource(ResourceName=instance_arn)
        assert calls == ["before-call.rds.ListTagsForResource"]
        calls.clear()

        ctx = CollectContext(plugin="database")
        collect(conn, ctx)

    assert calls == []


def test_other_errors_propagate(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        _deny(
            source.AwsConnection,
            monkeypatch,
            "rds",
            "DescribeDBInstances",
            code="InvalidParameterValue",
        )

        with pytest.raises(ClientError, match="InvalidParameterValue"):
            collect(conn, CollectContext(plugin="database"))
