"""GCS state-bucket management and GCP credential check for `plan` (spec §5.4 steps 2/3,
plan 4 B2). The only module allowed to import `google.auth`/`google.cloud.storage`
besides `easymigrate.core.registry`, which wires it in."""

from __future__ import annotations

from collections.abc import Callable

import google.auth
from google.cloud import exceptions as gcs_exceptions
from google.cloud import storage


def credentials_ok() -> bool:
    """Whether Application Default Credentials are available (`google.auth.default()`)."""
    try:
        google.auth.default()
    except google.auth.exceptions.DefaultCredentialsError:
        return False
    return True


def bucket_name(gcp_project: str) -> str:
    return f"{gcp_project}-easymigrate-tfstate"


def bucket_exists(client: storage.Client, name: str) -> bool:
    try:
        return client.bucket(name).exists()
    except gcs_exceptions.GoogleCloudError:
        return False


def create_bucket(client: storage.Client, name: str, region: str) -> None:
    bucket = client.bucket(name)
    bucket.versioning_enabled = True
    bucket.iam_configuration.uniform_bucket_level_access_enabled = True
    client.create_bucket(bucket, location=region)


def ensure_bucket(
    gcp_project: str,
    name: str,
    region: str,
    confirm: Callable[[str], bool],
    *,
    client: storage.Client | None = None,
) -> bool:
    """Ensure the state bucket exists, prompting `confirm(name)` to create it if missing.
    Returns False if the user declined; True if the bucket exists or was just created."""
    client = client if client is not None else storage.Client(project=gcp_project)
    if bucket_exists(client, name):
        return True
    if not confirm(name):
        return False
    create_bucket(client, name, region)
    return True
