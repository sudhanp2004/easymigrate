"""Lookup tables for RDS engines and instance classes (spec §4.1, §4.4)."""

# vCPUs, memory MiB for DB instance classes ec2:DescribeInstanceTypes does not expose
# (RDS-only families or ones that may be filtered out of a describe-instance-types call).
# Figures are the AWS-documented vCPU/memory for the equivalent EC2 instance type.
FALLBACK_INSTANCE_TYPES: dict[str, tuple[int, int]] = {
    "t3.micro": (2, 1024),  # 2 vCPU, 1 GiB - aws.amazon.com/ec2/instance-types/t3
    "t3.small": (2, 2048),  # 2 vCPU, 2 GiB
    "t3.medium": (2, 4096),  # 2 vCPU, 4 GiB
    "t4g.micro": (2, 1024),  # 2 vCPU, 1 GiB - aws.amazon.com/ec2/instance-types/t4
    "t4g.small": (2, 2048),  # 2 vCPU, 2 GiB
    "t4g.medium": (2, 4096),  # 2 vCPU, 4 GiB
    "m5.large": (2, 8192),  # 2 vCPU, 8 GiB - aws.amazon.com/ec2/instance-types/m5
    "m5.xlarge": (4, 16384),  # 4 vCPU, 16 GiB
    "r5.large": (2, 16384),  # 2 vCPU, 16 GiB - aws.amazon.com/ec2/instance-types/r5
    "r5.xlarge": (4, 32768),  # 4 vCPU, 32 GiB
}

DEFAULT_PORT: dict[str, int] = {"postgres": 5432, "mysql": 3306}

SUPPORTED_ENGINES = frozenset({"postgres", "mysql"})

# Keyed by the engine-family prefix as it appears in RDS's `Engine` field (matched with
# str.startswith by the normalizer), one sentence per family (spec §4.4).
UNSUPPORTED_ENGINE_REASONS: dict[str, str] = {
    "aurora-postgresql": (
        "Aurora's distributed storage, reader endpoints and serverless scaling have no "
        "Cloud SQL equivalent and need a spec of their own."
    ),
    "aurora-mysql": (
        "Aurora's distributed storage, reader endpoints and serverless scaling have no "
        "Cloud SQL equivalent and need a spec of their own."
    ),
    "mariadb": "Cloud SQL does not offer a MariaDB engine.",
    "oracle": "Cloud SQL does not offer an Oracle engine; consider Bare Metal Solution instead.",
    "sqlserver": "SQL Server support is not yet built for this migration path.",
    "db2": "Cloud SQL does not offer a Db2 engine.",
    "custom": (
        "RDS Custom instances run on customer-managed infrastructure with no Cloud SQL analog."
    ),
}
