"""Readable, deterministic IR ids and GCP-compatible labels from AWS names and tags."""

import re
import string
from collections.abc import Iterable

_MAX_LABEL_LENGTH = 63
_MAX_LABELS = 64


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def allocate_ids(kind: str, items: Iterable[tuple[str | None, str]]) -> dict[str, str]:
    """Map each item's native id to a `<kind>:<name>` IR id, independent of `items` order.

    `items` holds `(name, native_id)` pairs. Resources sharing a slugged name (base) get
    their slugged native id appended, so the id assigned to any one resource never depends
    on which other resources happen to be scanned alongside it.
    """
    items = list(items)

    native_ids: list[str] = [native_id for _, native_id in items]
    duplicates = sorted({nid for nid in native_ids if native_ids.count(nid) > 1})
    if duplicates:
        raise ValueError(f"duplicate native ids: {', '.join(duplicates)}")

    groups: dict[str, list[str]] = {}
    for name, native_id in items:
        base = slug(name or "") or slug(native_id) or "unnamed"
        groups.setdefault(base, []).append(native_id)

    result: dict[str, str] = {}
    for base, group_native_ids in groups.items():
        if len(group_native_ids) == 1:
            result[group_native_ids[0]] = f"{kind}:{base}"
            continue
        for native_id in group_native_ids:
            suffix = slug(native_id)
            result[native_id] = f"{kind}:{base}-{suffix}" if suffix else f"{kind}:{base}"

    by_id: dict[str, list[str]] = {}
    for native_id, ir_id in result.items():
        by_id.setdefault(ir_id, []).append(native_id)
    colliding = sorted(nid for nids in by_id.values() if len(nids) > 1 for nid in nids)
    if colliding:
        raise ValueError(f"id collision among native ids: {', '.join(colliding)}")

    return result


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
