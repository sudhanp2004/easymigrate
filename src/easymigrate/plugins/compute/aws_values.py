"""Lookup tables and tag helpers for EC2 (spec 4 §4.1, §4.3)."""

from typing import Any

# vCPUs, memory MiB for instance types `ec2:DescribeInstanceTypes` may not return — types
# retired in the account's region, or filtered out of a describe call. Figures are the
# AWS-documented vCPU/memory for the type.
FALLBACK_INSTANCE_TYPES: dict[str, tuple[int, int]] = {
    "t2.micro": (1, 1024),  # 1 vCPU, 1 GiB - aws.amazon.com/ec2/instance-types/t2
    "t3.micro": (2, 1024),  # 2 vCPU, 1 GiB - aws.amazon.com/ec2/instance-types/t3
    "t3.small": (2, 2048),  # 2 vCPU, 2 GiB
    "t3.medium": (2, 4096),  # 2 vCPU, 4 GiB
    "t3.large": (2, 8192),  # 2 vCPU, 8 GiB
    "t4g.small": (2, 2048),  # 2 vCPU, 2 GiB - aws.amazon.com/ec2/instance-types/t4
    "t4g.medium": (2, 4096),  # 2 vCPU, 4 GiB
    "m5.large": (2, 8192),  # 2 vCPU, 8 GiB - aws.amazon.com/ec2/instance-types/m5
    "m5.xlarge": (4, 16384),  # 4 vCPU, 16 GiB
    "m6i.large": (2, 8192),  # 2 vCPU, 8 GiB - aws.amazon.com/ec2/instance-types/m6i
    "c5.large": (2, 4096),  # 2 vCPU, 4 GiB - aws.amazon.com/ec2/instance-types/c5
    "c5.xlarge": (4, 8192),  # 4 vCPU, 8 GiB
    "r5.large": (2, 16384),  # 2 vCPU, 16 GiB - aws.amazon.com/ec2/instance-types/r5
    "r5.xlarge": (4, 32768),  # 4 vCPU, 32 GiB
}

# An instance carrying this tag belongs to an EKS managed node group; spec 4 §1 keeps it in the
# inventory and leaves it for the EKS family.
EKS_CLUSTER_TAG = "eks:cluster-name"

# AWS stamps this on every instance an Auto Scaling group launches. The group migrates as a
# managed instance group, which launches its own instances, so a member is not migrated on its
# own — doing so would run every group member twice.
ASG_MEMBER_TAG = "aws:autoscaling:groupName"

# States that are neither `running` nor `stopped`: the user may be looking at a half-built or
# half-destroyed environment, so they are reported rather than translated (spec 4 §4.3).
TRANSIENT_STATES = frozenset({"pending", "shutting-down", "stopping", "terminated"})

# Resources this family deliberately does not migrate, with the reason for the report (§4.4).
UNSUPPORTED_REASONS: dict[str, str] = {
    "spot-fleet-request": (
        "a Spot Fleet request allocates capacity across instance types and pools, which has no "
        "single Compute Engine equivalent"
    ),
    "fleet": (
        "an EC2 Fleet allocates capacity across instance types and purchase options, which has no "
        "single Compute Engine equivalent"
    ),
    "dedicated-host": (
        "a Dedicated Host maps to a GCP sole-tenant node group, which this family does not generate"
    ),
    "capacity-reservation": (
        "a Capacity Reservation has no Compute Engine equivalent; GCP reservations are a separate "
        "resource with different semantics"
    ),
}


def tags(resource: dict[str, Any]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in resource.get("Tags") or []}


def name_tag(resource: dict[str, Any]) -> str | None:
    return tags(resource).get("Name")
