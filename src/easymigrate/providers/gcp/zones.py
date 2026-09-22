"""Compute Engine zones per region, so a positional zone index becomes a zone that exists.

`VM-ZONE-MAPPING` assigns letters a, b, c… by position. Those are indices, not zone names:
several regions do not start at `a` (us-east1 is b, c, d; europe-west1 is b, c, d) and some have
more than three. Model knowledge as of 2026-09-22, like `gce_facts.py`: re-verify before customers.
"""

from __future__ import annotations

ZONES_AS_OF = "2026-09-22"

_ABC = ("a", "b", "c")

ZONE_SUFFIXES: dict[str, tuple[str, ...]] = {
    "us-east1": ("b", "c", "d"),
    "us-east4": _ABC,
    "us-east5": _ABC,
    "us-central1": ("a", "b", "c", "f"),
    "us-west1": _ABC,
    "us-west2": _ABC,
    "northamerica-northeast1": _ABC,
    "northamerica-northeast2": _ABC,
    "southamerica-east1": _ABC,
    "europe-west1": ("b", "c", "d"),
    "europe-west2": _ABC,
    "europe-west3": _ABC,
    "europe-west6": _ABC,
    "europe-west8": _ABC,
    "europe-west9": _ABC,
    "europe-north1": _ABC,
    "europe-southwest1": _ABC,
    "me-central1": _ABC,
    "me-west1": _ABC,
    "africa-south1": _ABC,
    "asia-south1": _ABC,
    "asia-south2": _ABC,
    "asia-southeast1": _ABC,
    "asia-southeast2": _ABC,
    "australia-southeast1": _ABC,
    "australia-southeast2": _ABC,
    "asia-northeast1": _ABC,
    "asia-northeast2": _ABC,
    "asia-northeast3": _ABC,
    "asia-east1": _ABC,
    "asia-east2": _ABC,
}


def zone_suffix(region: str, letter: str) -> tuple[str, bool]:
    """(zone suffix, wrapped) for the positional `letter` in `region`.

    `wrapped` is true when the source used more zones than the region has, so instances that
    were spread apart now share a zone; an unknown region is assumed to have zones a, b, c.
    """
    suffixes = ZONE_SUFFIXES.get(region, _ABC)
    index = ord(letter) - ord("a")
    return suffixes[index % len(suffixes)], index >= len(suffixes)
