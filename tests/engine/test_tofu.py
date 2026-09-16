"""Unit tests for the app-managed OpenTofu engine (spec §5.1, plan 4 A1)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from easymigrate.engine.tofu import (
    TOFU_MIN_VERSION,
    TOFU_SHA256,
    TOFU_VERSION,
    Tofu,
    TofuError,
    ensure_tofu,
)


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self, result: FakeCompleted | None = None):
        self.result = result or FakeCompleted()
        self.calls: list[dict] = []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return self.result


GOOD_ZIP_BYTES = None


def _make_zip(exe_name: str, content: bytes = b"#!/bin/sh\necho fake-tofu\n") -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(exe_name, content)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("EASYMIGRATE_TOFU_PATH", raising=False)


@pytest.fixture(autouse=True)
def _linux_platform(monkeypatch):
    monkeypatch.setattr("easymigrate.engine.tofu.platform.system", lambda: "Linux")
    monkeypatch.setattr("easymigrate.engine.tofu.platform.machine", lambda: "x86_64")


def test_checksum_mismatch_rejected_and_nothing_cached(tmp_path):
    cache = tmp_path / "cache"
    calls = []

    def bad_downloader(url):
        calls.append(url)
        return b"not the real archive"

    with pytest.raises(TofuError, match="checksum mismatch"):
        ensure_tofu(cache, downloader=bad_downloader)

    assert calls  # download was attempted
    assert not (cache / TOFU_VERSION).exists()


def test_download_verifies_and_caches(tmp_path):
    cache = tmp_path / "cache"
    data = _make_zip("tofu")
    digest = hashlib.sha256(data).hexdigest()

    # Patch the recorded checksum to match our fake archive's digest.
    import easymigrate.engine.tofu as tofu_mod

    original = dict(tofu_mod.TOFU_SHA256)
    tofu_mod.TOFU_SHA256["linux_amd64"] = digest
    try:
        calls = []

        def downloader(url):
            calls.append(url)
            return data

        binary = ensure_tofu(cache, downloader=downloader)
        assert binary.exists()
        assert binary.read_bytes() == b"#!/bin/sh\necho fake-tofu\n"
        assert len(calls) == 1

        # Second call reuses the cache; downloader is not called again.
        binary2 = ensure_tofu(cache, downloader=downloader)
        assert binary2 == binary
        assert len(calls) == 1
    finally:
        tofu_mod.TOFU_SHA256.clear()
        tofu_mod.TOFU_SHA256.update(original)


def test_env_override_used_when_version_ok(tmp_path, monkeypatch):
    fake_binary = tmp_path / "tofu"
    fake_binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("EASYMIGRATE_TOFU_PATH", str(fake_binary))
    runner = FakeRunner(FakeCompleted(stdout=json.dumps({"terraform_version": "1.8.0"})))

    binary = ensure_tofu(runner=runner)

    assert binary == fake_binary
    assert runner.calls[0]["args"] == [str(fake_binary), "version", "-json"]


def test_env_override_rejects_too_old_version(tmp_path, monkeypatch):
    fake_binary = tmp_path / "tofu"
    monkeypatch.setenv("EASYMIGRATE_TOFU_PATH", str(fake_binary))
    runner = FakeRunner(FakeCompleted(stdout=json.dumps({"terraform_version": "1.0.0"})))

    with pytest.raises(TofuError, match=TOFU_MIN_VERSION):
        ensure_tofu(runner=runner)


def test_unsupported_platform_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("easymigrate.engine.tofu.platform.system", lambda: "AmigaOS")
    with pytest.raises(TofuError, match="unsupported platform"):
        ensure_tofu(tmp_path / "cache", downloader=lambda url: b"")


def test_all_platforms_have_checksums():
    assert set(TOFU_SHA256) == {
        "linux_amd64",
        "linux_arm64",
        "darwin_amd64",
        "darwin_arm64",
        "windows_amd64",
    }
    for value in TOFU_SHA256.values():
        assert len(value) == 64
        int(value, 16)  # valid hex


# --- Tofu runner ---------------------------------------------------------------


def test_init_argument_list(tmp_path):
    runner = FakeRunner()
    tofu = Tofu(Path("/opt/tofu"), tmp_path, runner=runner)
    tofu.init({"bucket": "b", "prefix": "easymigrate"})
    assert runner.calls[0]["args"] == [
        "/opt/tofu",
        "init",
        "-backend-config=bucket=b",
        "-backend-config=prefix=easymigrate",
        "-no-color",
        "-input=false",
    ]
    assert runner.calls[0]["cwd"] == tmp_path


def test_init_backend_false_argument_list(tmp_path):
    runner = FakeRunner()
    tofu = Tofu(Path("/opt/tofu"), tmp_path, runner=runner)
    tofu.init(backend=False)
    assert runner.calls[0]["args"] == [
        "/opt/tofu",
        "init",
        "-backend=false",
        "-no-color",
        "-input=false",
    ]


def test_validate_argument_list(tmp_path):
    runner = FakeRunner()
    tofu = Tofu(Path("/opt/tofu"), tmp_path, runner=runner)
    tofu.validate()
    assert runner.calls[0]["args"] == ["/opt/tofu", "validate", "-no-color"]


def test_plan_argument_list_and_return(tmp_path):
    runner = FakeRunner(FakeCompleted(stdout="plan text"))
    tofu = Tofu(Path("/opt/tofu"), tmp_path, runner=runner)
    out = tmp_path / "tfplan"
    text = tofu.plan(out)
    assert runner.calls[0]["args"] == [
        "/opt/tofu",
        "plan",
        f"-out={out}",
        "-no-color",
        "-input=false",
    ]
    assert text == "plan text"


def test_show_json_argument_list_and_parse(tmp_path):
    runner = FakeRunner(FakeCompleted(stdout=json.dumps({"format_version": "1.2"})))
    tofu = Tofu(Path("/opt/tofu"), tmp_path, runner=runner)
    plan = tmp_path / "tfplan"
    result = tofu.show_json(plan)
    assert runner.calls[0]["args"] == ["/opt/tofu", "show", "-json", str(plan), "-no-color"]
    assert result == {"format_version": "1.2"}


def test_apply_argument_list_and_streaming(tmp_path):
    runner = FakeRunner(FakeCompleted(stdout="line one\nline two"))
    tofu = Tofu(Path("/opt/tofu"), tmp_path, runner=runner)
    plan = tmp_path / "tfplan"
    streamed = []
    tofu.apply(plan, streamed.append)
    assert runner.calls[0]["args"] == ["/opt/tofu", "apply", "-no-color", "-input=false", str(plan)]
    assert streamed == ["line one", "line two"]


def test_plugin_cache_dir_set(tmp_path):
    runner = FakeRunner()
    tofu = Tofu(Path("/opt/tofu"), tmp_path, runner=runner)
    tofu.validate()
    env = runner.calls[0]["env"]
    assert env["TF_PLUGIN_CACHE_DIR"].endswith("/.cache/easymigrate/plugins")


@pytest.mark.parametrize(
    "stderr, expected_hint",
    [
        ("Error: SERVICE_DISABLED: compute.googleapis.com", "gcloud services enable"),
        ("Error: X has not been used in project Y", "gcloud services enable"),
        ("Error 403: Permission denied", "lacks permission"),
        ("Error: something else entirely", None),
    ],
)
def test_error_hint_mapping(tmp_path, stderr, expected_hint):
    runner = FakeRunner(FakeCompleted(returncode=1, stderr=stderr))
    tofu = Tofu(Path("/opt/tofu"), tmp_path, runner=runner)
    with pytest.raises(TofuError) as excinfo:
        tofu.validate()
    assert stderr in str(excinfo.value)
    if expected_hint:
        assert expected_hint in str(excinfo.value)
