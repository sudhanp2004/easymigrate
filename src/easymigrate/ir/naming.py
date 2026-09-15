"""Readable, deterministic IR ids and GCP-compatible labels from AWS names and tags."""

import re
import string

_MAX_LABEL_LENGTH = 63
_MAX_LABELS = 64


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class IdAllocator:
    """Allocates unique `<kind>:<name>` ids. Allocate in a stable order for stable ids."""

    def __init__(self) -> None:
        self._taken: set[str] = set()

    def allocate(self, kind: str, name: str | None, native_id: str) -> str:
        base = slug(name or "") or slug(native_id) or "unnamed"
        candidate = f"{kind}:{base}"
        suffix = 2
        while candidate in self._taken:
            candidate = f"{kind}:{base}-{suffix}"
            suffix += 1
        self._taken.add(candidate)
        return candidate


def _label_part(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", text.lower()).strip("_")[:_MAX_LABEL_LENGTH]


def clean_labels(tags: dict[str, str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for key in sorted(tags):
        if key.startswith("aws:"):
            continue
        clean_key = _label_part(key)
        if not clean_key:
            continue
        if clean_key[0] not in string.ascii_lowercase:
            clean_key = f"tag_{clean_key}"[:_MAX_LABEL_LENGTH]
        if clean_key in labels:
            continue
        labels[clean_key] = _label_part(tags[key])
        if len(labels) == _MAX_LABELS:
            break
    return labels
