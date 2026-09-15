"""Pure helpers that turn raw AWS field values into IR values (plan 2 Global Constraints)."""

import ipaddress
from typing import Any

_PROTOCOLS = {
    "-1": "all",
    "6": "tcp",
    "tcp": "tcp",
    "17": "udp",
    "udp": "udp",
    "1": "icmp",
    "icmp": "icmp",
    "58": "icmpv6",
    "icmpv6": "icmpv6",
}
_PORTED = ("tcp", "udp")
_ICMP = ("icmp", "icmpv6")


def protocol(value: str | int) -> str:
    text = str(value).strip().lower()
    return _PROTOCOLS.get(text, text)


def ports(protocol_name: str, from_port: int | None, to_port: int | None) -> list[str]:
    if protocol_name not in _PORTED or from_port is None or from_port < 0:
        return []
    end = from_port if to_port is None or to_port < 0 else to_port
    if from_port == 0 and end == 65535:
        return []
    return [str(from_port)] if from_port == end else [f"{from_port}-{end}"]


def icmp(
    protocol_name: str, type_value: int | None, code_value: int | None
) -> tuple[int | None, int | None]:
    if protocol_name not in _ICMP or type_value is None or type_value < 0:
        return None, None
    if code_value is None or code_value < 0:
        return type_value, None
    return type_value, code_value


def cidr(value: str) -> str:
    return str(ipaddress.ip_network(value, strict=False))


def tags(resource: dict[str, Any]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in resource.get("Tags") or []}


def name_tag(resource: dict[str, Any]) -> str | None:
    return tags(resource).get("Name", "").strip() or None


def associated_cidrs(
    associations: list[dict[str, Any]] | None, cidr_key: str, state_key: str
) -> list[str]:
    return [
        cidr(association[cidr_key])
        for association in associations or []
        if (association.get(state_key) or {}).get("State") == "associated"
    ]
