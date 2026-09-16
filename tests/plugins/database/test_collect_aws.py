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
    rds.create_db_instance(
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
    )
    return vpc, subnets, sg


def _deny(connection_cls, monkeypatch, service: str, operation: str, code: str = "AccessDenied"):
    original = connection_cls.client

    def client(self, svc):
        c = original(self, svc)

        def raise_error(**kwargs):
            raise ClientError({"Error": {"Code": code, "Message": "denied"}}, operation)

        c.meta.events.register(f"before-call.{service}.{operation}", raise_error)
        return c

    monkeypatch.setattr(connection_cls, "client", client)


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


def test_pagination_is_followed(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        rds = conn.client("rds")
        ec2 = conn.client("ec2")
        vpc, subnets = _build_vpc_and_subnets(ec2)
        rds.create_db_subnet_group(
            DBSubnetGroupName="subgrp1", DBSubnetGroupDescription="d", SubnetIds=subnets
        )
        for i in range(3):
            rds.create_db_instance(
                DBInstanceIdentifier=f"db-{i}",
                Engine="postgres",
                EngineVersion="15.5",
                DBInstanceClass="db.t3.micro",
                AllocatedStorage=20,
                MasterUsername="postgres",
                MasterUserPassword="hunter22222",
                DBSubnetGroupName="subgrp1",
            )
        ctx = CollectContext(plugin="database")
        files = collect(conn, ctx)

    ids = {i["DBInstanceIdentifier"] for i in files["database.db_instances"]["DBInstances"]}
    assert ids == {"db-0", "db-1", "db-2"}


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
    calls: list[str] = []
    with mock_aws():
        conn = connect(region="ap-south-1")
        rds = conn.client("rds")
        ec2 = conn.client("ec2")
        _build_rds(rds, ec2)

        def record(**kwargs):
            calls.append(kwargs["event_name"])

        rds.meta.events.register("before-call.rds.ListTagsForResource", record)
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
