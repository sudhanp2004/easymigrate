"""Cloud SQL facts referenced by the AWS→GCP database difference rules (spec §5).

All tables below were checked 2026-09-16 against Cloud SQL for PostgreSQL/MySQL
documentation. Refreshing them later is one edit to this module.
"""

from __future__ import annotations

import re

# Cloud SQL supported major versions per engine, newest first. Checked 2026-09-16.
SUPPORTED_VERSIONS: dict[str, list[str]] = {
    "postgres": ["17", "16", "15", "14", "13"],
    "mysql": ["8.0", "5.7"],
}

# Oldest Cloud SQL major version still supported per engine. Checked 2026-09-16.
OLDEST_SUPPORTED: dict[str, str] = {
    "postgres": "13",
    "mysql": "5.7",
}

# (engine, major) -> Cloud SQL `database_version` enum value. Checked 2026-09-16.
DATABASE_VERSION: dict[tuple[str, str], str] = {
    ("postgres", "17"): "POSTGRES_17",
    ("postgres", "16"): "POSTGRES_16",
    ("postgres", "15"): "POSTGRES_15",
    ("postgres", "14"): "POSTGRES_14",
    ("postgres", "13"): "POSTGRES_13",
    ("mysql", "8.0"): "MYSQL_8_0",
    ("mysql", "5.7"): "MYSQL_5_7",
}

# Shared-core tier ids, in the order Cloud SQL lists them. Checked 2026-09-16.
SHARED_CORE_TIERS = ("db-f1-micro", "db-g1-small")

# Shared-core tier id -> (vcpus, memory_mib). Checked 2026-09-16.
SHARED_CORE_SPECS: dict[str, tuple[int, int]] = {
    "db-f1-micro": (1, 614),
    "db-g1-small": (1, 1740),
}

# Custom-tier memory-per-vCPU band, in MiB (Cloud SQL's 0.9-6.5 GB/vCPU range), and the
# multiple memory must round up to. Checked 2026-09-16.
MEMORY_PER_VCPU_MIB = (922, 6656)
MEMORY_STEP_MIB = 256

# Minimum Cloud SQL disk size, in GiB. Checked 2026-09-16.
MIN_DISK_GIB = 10

# Backup/PITR limits. Checked 2026-09-16.
MAX_RETAINED_BACKUPS = 365
MAX_TRANSACTION_LOG_RETENTION_DAYS = 7

# Cloud SQL database flag allow-list per engine, seeded from the fixtures this family
# needs plus the common flags called out in plan 2. `shared_buffers` is deliberately
# excluded: it is not settable on Cloud SQL. Extend an engine by adding the flag name to
# its frozenset below.
SUPPORTED_FLAGS: dict[str, frozenset[str]] = {
    "postgres": frozenset(
        {
            "max_connections",
            "log_min_duration_statement",
            "work_mem",
            "autovacuum",
            "cloudsql_iam_authentication",
        }
    ),
    "mysql": frozenset(
        {
            "max_connections",
            "character_set_server",
            "collation_server",
            "sql_mode",
            "slow_query_log",
            "long_query_time",
            "innodb_lock_wait_timeout",
            "cloudsql_iam_authentication",
        }
    ),
}

# AWS parameter-group "formula" values (e.g. "{DBInstanceClassMemory*3/4}") are computed
# from instance shape at attach time and never translate to a Cloud SQL flag value.
FORMULA = re.compile(r"\{.*\}")
