"""GCP resource names and Terraform resource labels derived from IR names and ids (spec §5.2)."""

import re

GCP_NAME_PATTERN = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
_MAX_GCP_NAME = 63
# Cloud SQL instance names allow up to 98 characters (still lowercase letters, digits,
# hyphens, starting with a letter) — spec §6.3.
MAX_CLOUDSQL_NAME = 98


def _slugify(name: str, max_length: int) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"r-{cleaned}".rstrip("-")
    return cleaned[:max_length].rstrip("-")


def gcp_name(name: str) -> str:
    return _slugify(name, _MAX_GCP_NAME)


def cloudsql_name(name: str) -> str:
    """Slug a Cloud SQL instance name: lowercase letters/digits/hyphens, starting with
    a letter, at most 98 characters (spec §6.3). `gcp_name`'s 63-character cap is too
    tight for Cloud SQL, which allows 98."""
    return _slugify(name, MAX_CLOUDSQL_NAME)


class GcpNameAllocator:
    def __init__(self, max_length: int = _MAX_GCP_NAME) -> None:
        self._taken: dict[str, set[str]] = {}
        self._max_length = max_length

    def allocate(self, resource_type: str, name: str) -> tuple[str, bool]:
        base = _slugify(name, self._max_length)
        taken = self._taken.setdefault(resource_type, set())
        candidate = base
        n = 2
        while candidate in taken:
            suffix = f"-{n}"
            candidate = base[: self._max_length - len(suffix)].rstrip("-") + suffix
            n += 1
        taken.add(candidate)
        return candidate, candidate != name


def tf_label(ir_id: str) -> str:
    _, _, rest = ir_id.rpartition(":") if ":" in ir_id else ("", "", ir_id)
    label = re.sub(r"[^a-z0-9_]+", "_", rest.lower())
    label = re.sub(r"_{2,}", "_", label).strip("_")
    if not label or label[0].isdigit():
        label = f"r_{label}".rstrip("_")
    return label


class TfLabelAllocator:
    def __init__(self) -> None:
        self._taken: dict[str, set[str]] = {}

    def allocate(self, resource_type: str, ir_id: str) -> str:
        base = tf_label(ir_id)
        taken = self._taken.setdefault(resource_type, set())
        candidate = base
        n = 2
        while candidate in taken:
            candidate = f"{base}_{n}"
            n += 1
        taken.add(candidate)
        return candidate
