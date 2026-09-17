"""Tests for the `easymigrate ui` command (plan 1 task 4). `easymigrate.cli` must not
import `easymigrate.ui` at module scope, so the extra's absence only breaks this one
command, never the CLI as a whole (spec §7)."""

from __future__ import annotations

import socket
import sys

from typer.testing import CliRunner

from easymigrate.cli import app

runner = CliRunner()


def test_ui_reports_missing_extra_without_traceback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "easymigrate.ui", None)

    result = runner.invoke(app, ["ui"])

    assert result.exit_code == 1
    assert 'uv tool install "easymigrate[ui]"' in result.output
    assert "Traceback" not in result.output


def test_ui_reports_port_already_in_use(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]

    try:
        result = runner.invoke(app, ["ui", "--port", str(port)])
    finally:
        busy.close()

    assert result.exit_code == 1
    assert str(port) in result.output
    assert "Traceback" not in result.output


def test_ui_binds_loopback_prints_url_and_opens_browser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    served: dict = {}
    opened: list[str] = []

    import easymigrate.cli as cli_module

    def fake_serve(fastapi_app, sock) -> None:
        served["host"], served["port"] = sock.getsockname()
        sock.close()

    monkeypatch.setattr(cli_module, "_serve", fake_serve)
    monkeypatch.setattr(cli_module.webbrowser, "open", opened.append)

    result = runner.invoke(app, ["ui", "--port", "0"])

    assert result.exit_code == 0
    assert served["host"] == "127.0.0.1"
    assert served["port"] != 0
    assert len(opened) == 1
    assert opened[0].startswith(f"http://127.0.0.1:{served['port']}/")
    assert "token=" in opened[0]
    assert f"http://127.0.0.1:{served['port']}" in result.output
    assert "token=" in result.output


def test_ui_no_browser_flag_skips_opening(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opened: list[str] = []

    import easymigrate.cli as cli_module

    monkeypatch.setattr(cli_module, "_serve", lambda fastapi_app, sock: sock.close())
    monkeypatch.setattr(cli_module.webbrowser, "open", opened.append)

    result = runner.invoke(app, ["ui", "--no-browser"])

    assert result.exit_code == 0
    assert opened == []


def test_ui_has_no_host_flag():
    result = runner.invoke(app, ["ui", "--help"])
    assert "--host" not in result.output
