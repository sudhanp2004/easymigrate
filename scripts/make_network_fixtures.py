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
