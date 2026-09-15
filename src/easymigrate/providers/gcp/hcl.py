"""Minimal deterministic HCL writer (spec §5.2). Generated Terraform never uses string templates."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Union

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
INDENT = "  "


@dataclass(frozen=True)
class Ref:
    """A raw HCL expression, e.g. `var.region` or `google_compute_network.main.id`."""

    expr: str


Value = Union[str, bool, int, float, None, Ref, list["Value"], dict[str, "Value"]]  # noqa: UP007


def escape(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return escaped.replace("${", "$${").replace("%{", "%%{")


def _check_identifier(name: str, what: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"invalid HCL {what}: {name!r}")
    return name


@dataclass
class Block:
    type: str
    labels: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    body: list[tuple[str, Value] | Block] = field(default_factory=list)

    def __post_init__(self) -> None:
        _check_identifier(self.type, "block type")

    def attr(self, name: str, value: Value) -> Block:
        _check_identifier(name, "attribute name")
        if any(isinstance(item, tuple) and item[0] == name for item in self.body):
            raise ValueError(f"duplicate attribute {name!r} in {self.type} block")
        self.body.append((name, value))
        return self

    def block(self, type_: str, *labels: str) -> Block:
        child = Block(type_, list(labels))
        self.body.append(child)
        return child

    def comment(self, text: str) -> Block:
        self.comments.extend(text.splitlines() or [""])
        return self


@dataclass
class HclFile:
    blocks: list[Block] = field(default_factory=list)
    header: list[str] = field(default_factory=list)

    def block(self, type_: str, *labels: str) -> Block:
        child = Block(type_, list(labels))
        self.blocks.append(child)
        return child

    def render(self) -> str:
        parts: list[str] = []
        if self.header:
            parts.append("\n".join(_comment_lines(self.header, 0)))
        parts.extend("\n".join(_render_block(block, 0)) for block in self.blocks)
        return "\n\n".join(parts) + "\n" if parts else ""


def _comment_lines(comments: list[str], depth: int) -> list[str]:
    pad = INDENT * depth
    return [f"{pad}# {line}".rstrip() for line in comments]


def _render_block(block: Block, depth: int) -> list[str]:
    pad = INDENT * depth
    head = " ".join([block.type, *(f'"{escape(label)}"' for label in block.labels)])
    lines = _comment_lines(block.comments, depth)
    lines.append(f"{pad}{head} {{")
    lines.extend(_render_body(block.body, depth + 1))
    lines.append(f"{pad}}}")
    return lines


def _render_body(body: list[tuple[str, Value] | Block], depth: int) -> list[str]:
    lines: list[str] = []
    group: list[tuple[str, Value]] = []
    previous: str | None = None

    def flush() -> None:
        if not group:
            return
        width = max(len(name) for name, _ in group)
        for name, value in group:
            first, *rest = _render_value(value, depth).split("\n")
            lines.append(f"{INDENT * depth}{name.ljust(width)} = {first}")
            lines.extend(rest)
        group.clear()

    for item in body:
        if isinstance(item, Block):
            flush()
            if previous == "attr":
                lines.append("")
            lines.extend(_render_block(item, depth))
            previous = "block"
        else:
            if previous == "block":
                lines.append("")
            group.append(item)
            previous = "attr"
    flush()
    return lines


def _map_key(key: str) -> str:
    return key if _IDENT.match(key) else f'"{escape(key)}"'


def _render_value(value: Value, depth: int) -> str:
    if isinstance(value, Ref):
        return value.expr
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot render non-finite number {value!r} as HCL")
        return repr(value)
    if isinstance(value, str):
        return f'"{escape(value)}"'
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(item, depth) for item in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        entries = [(_map_key(k), v) for k, v in sorted(value.items())]
        width = max(len(key) for key, _ in entries)
        pad = INDENT * (depth + 1)
        inner = [f"{pad}{key.ljust(width)} = {_render_value(v, depth + 1)}" for key, v in entries]
        return "{\n" + "\n".join(inner) + "\n" + INDENT * depth + "}"
    raise TypeError(f"cannot render {type(value).__name__} as HCL")
