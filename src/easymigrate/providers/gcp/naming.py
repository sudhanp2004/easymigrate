"""GCP resource names and Terraform resource labels derived from IR names and ids (spec §5.2)."""

import re

GCP_NAME_PATTERN = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
_MAX_GCP_NAME = 63


def gcp_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"r-{cleaned}".rstrip("-")
    return cleaned[:_MAX_GCP_NAME].rstrip("-")


class GcpNameAllocator:
    def __init__(self) -> None:
        self._taken: dict[str, set[str]] = {}

    def allocate(self, resource_type: str, name: str) -> tuple[str, bool]:
        base = gcp_name(name)
        taken = self._taken.setdefault(resource_type, set())
        candidate = base
        n = 2
        while candidate in taken:
            suffix = f"-{n}"
            candidate = base[: _MAX_GCP_NAME - len(suffix)].rstrip("-") + suffix
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
