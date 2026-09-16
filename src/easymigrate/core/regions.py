"""AWS -> GCP region mapping for `plan` (spec §5.4 step 1, plan 4 B2).

Maps every AWS commercial region to its nearest GCP region by rough geography. This
list is chosen by the implementer; an AWS region absent from it requires an explicit
`--gcp-region` override.
"""

from __future__ import annotations

from easymigrate.core.errors import EasyMigrateError

REGION_MAP: dict[str, str] = {
    # US
    "us-east-1": "us-east4",
    "us-east-2": "us-east5",
    "us-west-1": "us-west2",
    "us-west-2": "us-west1",
    # Canada
    "ca-central-1": "northamerica-northeast1",
    "ca-west-1": "northamerica-northeast2",
    # South America
    "sa-east-1": "southamerica-east1",
    # Europe
    "eu-west-1": "europe-west1",
    "eu-west-2": "europe-west2",
    "eu-west-3": "europe-west9",
    "eu-central-1": "europe-west3",
    "eu-central-2": "europe-west6",
    "eu-north-1": "europe-north1",
    "eu-south-1": "europe-west8",
    "eu-south-2": "europe-southwest1",
    # Middle East
    "me-south-1": "me-central1",
    "me-central-1": "me-central1",
    # Africa
    "af-south-1": "africa-south1",
    # Asia Pacific
    "ap-south-1": "asia-south1",
    "ap-south-2": "asia-south2",
    "ap-southeast-1": "asia-southeast1",
    "ap-southeast-2": "australia-southeast1",
    "ap-southeast-3": "asia-southeast2",
    "ap-southeast-4": "australia-southeast2",
    "ap-southeast-5": "asia-southeast2",
    "ap-southeast-7": "asia-southeast1",
    "ap-northeast-1": "asia-northeast1",
    "ap-northeast-2": "asia-northeast3",
    "ap-northeast-3": "asia-northeast2",
    "ap-east-1": "asia-east2",
    "il-central-1": "me-west1",
}


def gcp_region_for(aws_region: str, *, override: str | None) -> str:
    """The GCP region to use for `aws_region`. `override` (--gcp-region) always wins;
    otherwise the region must be in REGION_MAP."""
    if override:
        return override
    try:
        return REGION_MAP[aws_region]
    except KeyError:
        raise EasyMigrateError(
            f"no GCP region mapping for AWS region {aws_region!r}; pass --gcp-region"
        ) from None
