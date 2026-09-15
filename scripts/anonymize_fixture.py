"""Anonymize a real AWS snapshot before it is saved as a test fixture (spec §7).

    uv run python scripts/anonymize_fixture.py SRC_DIR DEST_DIR --account-id 538342414628

Replaces the account id, every AWS resource id (consistently across files), public IPs and
timestamps. Only `*.json` files other than `_scan.json` are copied."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

FIXTURE_ACCOUNT = "123456789012"
_RESOURCE_ID = re.compile(
    r"\b(vpc-cidr-assoc|subnet-cidr-assoc|rtbassoc|aclassoc|eipalloc|eipassoc|vpce|vpc|subnet"
    r"|rtb|eigw|igw|nat|sg|acl|eni|pcx|dopt|pl|tgw|vgw|i)-[0-9a-f]{8,17}\b"
)
_TIMESTAMP_KEYS = frozenset({"CreateTime", "DeleteTime", "LaunchTime"})
_PUBLIC_IP_KEYS = frozenset({"PublicIp", "CarrierIp"})


class Anonymizer:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self._ids: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self._ips: dict[str, str] = {}

    def _resource_id(self, match: re.Match[str]) -> str:
        original = match.group(0)
        if original not in self._ids:
            prefix = match.group(1)
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            self._ids[original] = f"{prefix}-{self._counters[prefix]:017x}"
        return self._ids[original]

    def _public_ip(self, value: str) -> str:
        if value not in self._ips:
            self._ips[value] = f"203.0.113.{len(self._ips) + 1}"
        return self._ips[value]

    def value(self, data: Any, key: str | None = None) -> Any:
        if isinstance(data, dict):
            return {k: self.value(data[k], k) for k in sorted(data)}
        if isinstance(data, list):
            return [self.value(item) for item in data]
        if key in _TIMESTAMP_KEYS:
            return "2026-01-01T00:00:00+00:00"
        if isinstance(data, datetime):
            return data.isoformat()
        if isinstance(data, str):
            if key in _PUBLIC_IP_KEYS:
                return self._public_ip(data)
            text = data.replace(self.account_id, FIXTURE_ACCOUNT)
            return _RESOURCE_ID.sub(self._resource_id, text)
        return data

    def mapping_key(self, key: str) -> str:
        return _RESOURCE_ID.sub(self._resource_id, key.replace(self.account_id, FIXTURE_ACCOUNT))


def anonymize_files(files: dict[str, Any], account_id: str) -> dict[str, Any]:
    """Anonymize snapshot files together so resource ids stay consistent across them."""
    anonymizer = Anonymizer(account_id)
    result: dict[str, Any] = {}
    for name in sorted(files):
        data = files[name]
        if name.endswith("vpc_attributes") and isinstance(data, dict):
            result[name] = {
                anonymizer.mapping_key(k): anonymizer.value(v) for k, v in sorted(data.items())
            }
        else:
            result[name] = anonymizer.value(data)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("dest", type=Path)
    parser.add_argument("--account-id", required=True)
    args = parser.parse_args()
    files = {
        path.name.removesuffix(".json"): json.loads(path.read_text())
        for path in sorted(args.source.glob("*.json"))
        if path.name != "_scan.json"
    }
    args.dest.mkdir(parents=True, exist_ok=True)
    for name, data in anonymize_files(files, args.account_id).items():
        (args.dest / f"{name}.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
