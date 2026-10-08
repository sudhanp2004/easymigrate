"""AWS service access → Google Cloud predefined roles, for the IAM report (spec 8 §5).

Suggestions, never generated (spec 8 D2). Model knowledge dated 2026-10-08, like the other
`*_facts.py` modules; re-verify before customers.
"""

from __future__ import annotations

FACTS_AS_OF = "2026-10-08"

# service -> level -> suggested predefined roles. A level missing from a service falls back to
# the next stronger one that is present.
SUGGESTED_ROLES: dict[str, dict[str, list[str]]] = {
    "s3": {
        "read": ["roles/storage.objectViewer"],
        "write": ["roles/storage.objectAdmin"],
        "admin": ["roles/storage.admin"],
    },
    "sqs": {
        "read": ["roles/pubsub.subscriber"],
        "write": ["roles/pubsub.publisher", "roles/pubsub.subscriber"],
        "admin": ["roles/pubsub.admin"],
    },
    "sns": {"write": ["roles/pubsub.publisher"], "admin": ["roles/pubsub.admin"]},
    "dynamodb": {
        "read": ["roles/datastore.viewer"],
        "write": ["roles/datastore.user"],
        "admin": ["roles/datastore.owner"],
    },
    "secretsmanager": {
        "read": ["roles/secretmanager.secretAccessor"],
        "write": ["roles/secretmanager.secretVersionManager"],
        "admin": ["roles/secretmanager.admin"],
    },
    "ssm": {
        "read": ["roles/secretmanager.secretAccessor"],
        "write": ["roles/secretmanager.secretVersionManager"],
    },
    "kms": {
        "write": ["roles/cloudkms.cryptoKeyEncrypterDecrypter"],
        "admin": ["roles/cloudkms.admin"],
    },
    "logs": {"write": ["roles/logging.logWriter"], "admin": ["roles/logging.admin"]},
    "cloudwatch": {
        "read": ["roles/monitoring.viewer"],
        "write": ["roles/monitoring.metricWriter"],
    },
    "ecr": {
        "read": ["roles/artifactregistry.reader"],
        "write": ["roles/artifactregistry.writer"],
        "admin": ["roles/artifactregistry.admin"],
    },
    "rds-db": {"write": ["roles/cloudsql.client"]},
    "rds": {"read": ["roles/cloudsql.viewer"], "write": ["roles/cloudsql.editor"]},
    "lambda": {"write": ["roles/run.invoker"]},
    "sts": {"write": ["roles/iam.serviceAccountTokenCreator"]},
    "ec2": {
        "read": ["roles/compute.viewer"],
        "write": ["roles/compute.instanceAdmin.v1"],
        "admin": ["roles/compute.admin"],
    },
    "eks": {"read": ["roles/container.viewer"], "write": ["roles/container.developer"]},
    "elasticloadbalancing": {
        "read": ["roles/compute.viewer"],
        "write": ["roles/compute.loadBalancerAdmin"],
    },
    "autoscaling": {"read": ["roles/compute.viewer"], "write": ["roles/compute.instanceAdmin.v1"]},
}

_ORDER = ("read", "write", "admin")


def suggest(service: str, level: str) -> list[str]:
    """The predefined roles nearest to `level` access on `service`, or [] when none is known."""
    levels = SUGGESTED_ROLES.get(service)
    if not levels:
        return []
    for candidate in _ORDER[_ORDER.index(level) :]:
        if candidate in levels:
            return levels[candidate]
    # Only weaker levels are known (e.g. sns read): the strongest known still covers the need.
    return levels[max(levels, key=_ORDER.index)]
