"""The CLI prints the launch URL; the SPA parses it. Nothing else crosses that boundary,
so nothing else catches a drift in the query parameter's name.

This is not a hypothetical: the CLI shipped `?token=` while the frontend read `?t=`. Both
sides' own tests passed — the Python tests never saw the TypeScript, and vitest never saw
the CLI — and the first user to open the UI got "Not authenticated"."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TOKEN_TS = _ROOT / "frontend" / "src" / "api" / "token.ts"
_CLI = _ROOT / "src" / "easymigrate" / "cli.py"


def _frontend_token_param() -> str:
    """The parameter name the SPA reads, from its single exported constant."""
    source = _TOKEN_TS.read_text(encoding="utf-8")
    match = re.search(r"export const TOKEN_PARAM = '([^']+)'", source)
    assert match, "frontend/src/api/token.ts must export TOKEN_PARAM as a string literal"
    return match.group(1)


def _cli_launch_url_param() -> str:
    """The parameter name the CLI puts the token in, from its launch URL f-string."""
    source = _CLI.read_text(encoding="utf-8")
    match = re.search(r'f"http://127\.0\.0\.1:\{bound_port\}/\?([A-Za-z_]+)=\{token\}"', source)
    assert match, "cli.py must build the launch URL as an f-string with ?<param>={token}"
    return match.group(1)


def test_cli_and_frontend_agree_on_the_token_parameter():
    assert _cli_launch_url_param() == _frontend_token_param(), (
        "the CLI's launch URL and the SPA's token parser disagree; "
        "the browser will show 'Not authenticated'"
    )


def test_frontend_uses_the_constant_rather_than_a_literal():
    """A second literal would drift from TOKEN_PARAM without this test noticing."""
    source = _TOKEN_TS.read_text(encoding="utf-8")
    body = source.split("export function bootstrapToken", 1)[1]
    assert "searchParams.get(TOKEN_PARAM)" in body
    assert "searchParams.delete(TOKEN_PARAM)" in body
