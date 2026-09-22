"""Cloud Storage facts referenced by the AWS→GCP storage rules (spec 6 §5).

Model knowledge dated 2026-09-22, like the other `*_facts.py` modules; re-verify before customers.
"""

from __future__ import annotations

import ipaddress
import re

FACTS_AS_OF = "2026-09-22"

# S3 storage class -> the Cloud Storage class with the closest access pattern. Intelligent-Tiering
# has no class of its own; S3-INTELLIGENT-TIERING offers Autoclass instead.
STORAGE_CLASS: dict[str, str | None] = {
    "STANDARD_IA": "NEARLINE",
    "ONEZONE_IA": "NEARLINE",
    "GLACIER_IR": "COLDLINE",
    "GLACIER": "ARCHIVE",
    "DEEP_ARCHIVE": "ARCHIVE",
    "INTELLIGENT_TIERING": None,
}

# Minimum storage duration, in days, billed for each colder class.
MIN_STORAGE_DAYS: dict[str, int] = {"NEARLINE": 30, "COLDLINE": 90, "ARCHIVE": 365}

# Warmest to coldest. A lifecycle transition only ever moves an object colder.
CLASS_ORDER = ("STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE")

_VALID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$")


def name_problem(name: str) -> str | None:
    """Why `name` cannot be a Cloud Storage bucket name without extra steps, or None."""
    if "." in name:
        return "dotted names need domain-ownership verification in Cloud Storage"
    if name.startswith("goog"):
        return "Cloud Storage forbids names starting with 'goog'"
    if "google" in name:
        return "Cloud Storage forbids names containing 'google'"
    try:
        ipaddress.ip_address(name)
        return "Cloud Storage forbids names that are IP addresses"
    except ValueError:
        pass
    if not _VALID.match(name):
        return "not a valid Cloud Storage bucket name"
    return None


def sanitized(name: str) -> str:
    """The nearest name Cloud Storage accepts: dots become hyphens, 'google' is broken up, and a
    'goog' prefix gets an 's3-' in front."""
    fixed = name.replace(".", "-").replace("google", "g-oogle")
    if fixed.startswith("goog"):
        fixed = f"s3-{fixed}"
    fixed = re.sub(r"[^a-z0-9_-]", "-", fixed.lower())[:63].strip("-_")
    return fixed if len(fixed) >= 3 else f"{fixed}-bucket"
