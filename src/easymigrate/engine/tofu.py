"""App-managed OpenTofu engine (spec §5.1).

Only `easymigrate.core.errors` may be imported from the rest of the package
(see `tests/test_dependency_rules.py`); this module must stay usable without
importing any other `easymigrate` code, plugins, or providers.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import subprocess
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlopen

from easymigrate.core.errors import EasyMigrateError

# Pinned to the latest stable OpenTofu 1.x release, looked up with:
#   gh api repos/opentofu/opentofu/releases/latest --jq '.tag_name'
# -> v1.12.6 (2026-09-15).
TOFU_VERSION = "1.12.6"
TOFU_MIN_VERSION = "1.6.0"

# SHA-256 checksums for tofu_1.12.6_<os>_<arch>.zip, copied from the release's
# tofu_1.12.6_SHA256SUMS asset:
#   gh release download v1.12.6 -R opentofu/opentofu -p 'tofu_1.12.6_SHA256SUMS'
TOFU_SHA256: dict[str, str] = {
    "linux_amd64": "5dc43da4f750f33873dc25e94587128709e819e544b7be9016b255316153c3a8",
    "linux_arm64": "e573979ba68a17fe7b881752051a694a7efcd970e39521f6a25775197861ed4d",
    "darwin_amd64": "166388e5feed47e107e11721b6366bf91d21e47eccbced75f3cbe0c7184ffd9b",
    "darwin_arm64": "e083ee43790ab9e19ad66d9933e24a7244a1412e1d5728f37999ae2163fdac95",
    "windows_amd64": "0d1421721cf9ec24b41b698a9620dda218d47fa7e76ac3dc15cdbc13bd79b0bb",
}

_DOWNLOAD_URL = (
    "https://github.com/opentofu/opentofu/releases/download/"
    "v{version}/tofu_{version}_{platform}.zip"
)

Downloader = Callable[[str], bytes]
Runner = Callable[..., subprocess.CompletedProcess]


class TofuError(EasyMigrateError):
    """An OpenTofu download, checksum, or run failure. The message names the fix."""


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.strip().lstrip("v").split("."))


def _platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system)
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if os_name is None or arch is None:
        raise TofuError(f"unsupported platform {system}/{machine}")
    return f"{os_name}_{arch}"


def _http_download(url: str) -> bytes:
    with urlopen(url) as response:  # noqa: S310 - fixed https URL to github releases
        return response.read()


def _default_cache_dir() -> Path:
    return Path.home() / ".cache" / "easymigrate" / "tofu"


def _check_min_version(binary: Path, runner: Runner) -> None:
    result = runner(
        [str(binary), "version", "-json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TofuError(
            f"could not run `{binary} version -json`: {result.stderr}. "
            "Set EASYMIGRATE_TOFU_PATH to a working tofu binary, or unset it to let "
            "EasyMigrate download one."
        )
    data = json.loads(result.stdout)
    version = data.get("terraform_version") or data.get("version")
    if not version or _version_tuple(version) < _version_tuple(TOFU_MIN_VERSION):
        raise TofuError(
            f"{binary} reports version {version!r}, but EasyMigrate requires "
            f">= {TOFU_MIN_VERSION}. Point EASYMIGRATE_TOFU_PATH at a newer tofu binary."
        )


def ensure_tofu(
    cache_dir: Path | None = None,
    *,
    downloader: Downloader | None = None,
    runner: Runner = subprocess.run,
) -> Path:
    """Return a path to a usable `tofu` binary, downloading and verifying it if needed."""
    env_path = os.environ.get("EASYMIGRATE_TOFU_PATH")
    if env_path:
        binary = Path(env_path)
        _check_min_version(binary, runner)
        return binary

    cache_dir = cache_dir if cache_dir is not None else _default_cache_dir()
    version_dir = cache_dir / TOFU_VERSION
    exe_name = "tofu.exe" if platform.system().lower() == "windows" else "tofu"
    binary = version_dir / exe_name
    if binary.exists():
        return binary

    key = _platform_key()
    expected = TOFU_SHA256.get(key)
    if expected is None:
        raise TofuError(
            f"no checksum recorded for platform {key!r}. "
            "Set EASYMIGRATE_TOFU_PATH to a local tofu binary to work around this."
        )

    url = _DOWNLOAD_URL.format(version=TOFU_VERSION, platform=key)
    download = downloader if downloader is not None else _http_download
    try:
        data = download(url)
    except Exception as exc:
        raise TofuError(
            f"failed to download OpenTofu from {url}: {exc}. "
            "Set EASYMIGRATE_TOFU_PATH to a local tofu binary to work offline."
        ) from exc

    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise TofuError(
            f"checksum mismatch downloading {url}: expected {expected}, got {digest}. "
            "Nothing was written to the cache; the download was discarded."
        )

    version_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extract(exe_name, path=version_dir)
    binary.chmod(0o755)
    return binary


def _plugin_cache_dir() -> Path:
    return Path.home() / ".cache" / "easymigrate" / "plugins"


def _hint(stderr: str) -> str | None:
    if "SERVICE_DISABLED" in stderr or "has not been used in project" in stderr:
        return "enable the API: `gcloud services enable compute.googleapis.com --project P`"
    if "403" in stderr or "Permission" in stderr:
        return "the service account lacks permission"
    return None


@dataclass
class Tofu:
    binary: Path
    workdir: Path
    runner: Runner = subprocess.run
    env: dict[str, str] | None = field(default=None)
    # Long-running commands (plan, apply) go through Popen so each output line reaches the
    # caller as tofu prints it. `run` with capture_output replayed everything after exit, and
    # a user watched an empty log for five minutes while the process was already dead.
    popen: Callable[..., subprocess.Popen] = subprocess.Popen

    def _env(self) -> dict[str, str]:
        env = dict(os.environ) if self.env is None else dict(self.env)
        env.setdefault("TF_PLUGIN_CACHE_DIR", str(_plugin_cache_dir()))
        return env

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        result = self.runner(
            [str(self.binary), *args],
            cwd=self.workdir,
            env=self._env(),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            hint = _hint(result.stderr)
            message = result.stderr if hint is None else f"{result.stderr}\n{hint}"
            raise TofuError(message)
        return result

    def _run_streaming(self, args: list[str], stream: Callable[[str], None]) -> str:
        proc = self.popen(
            [str(self.binary), *args],
            cwd=self.workdir,
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # Drained on its own thread: a full stderr pipe would block tofu while this thread
        # waits on stdout, and neither would ever finish.
        stderr_parts: list[str] = []
        reader = threading.Thread(target=lambda: stderr_parts.append(proc.stderr.read()))
        reader.start()
        lines: list[str] = []
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            stream(line)
        returncode = proc.wait()
        reader.join()
        stderr = "".join(stderr_parts)
        if returncode != 0:
            hint = _hint(stderr)
            raise TofuError(stderr if hint is None else f"{stderr}\n{hint}")
        return "\n".join(lines)

    def init(self, backend_config: dict[str, str] | None = None, *, backend: bool = True) -> None:
        args = ["init"]
        if backend:
            for key, value in (backend_config or {}).items():
                args.append(f"-backend-config={key}={value}")
        else:
            # Used to validate generated Terraform (e.g. in the `tofu`-marked golden
            # test) without a real GCS backend to talk to.
            args.append("-backend=false")
        args += ["-no-color", "-input=false"]
        self._run(args)

    def validate(self) -> None:
        # `validate` does not accept `-input`; it never touches remote state or variables.
        self._run(["validate", "-no-color"])

    def plan(self, out: Path, stream: Callable[[str], None] | None = None) -> str:
        args = ["plan", f"-out={out}", "-no-color", "-input=false"]
        if stream is not None:
            return self._run_streaming(args, stream)
        return self._run(args).stdout

    def show_json(self, plan: Path) -> dict:
        # `show` does not accept `-input`; it only reads a plan/state file.
        result = self._run(["show", "-json", str(plan), "-no-color"])
        return json.loads(result.stdout)

    def apply(self, plan: Path, stream: Callable[[str], None]) -> None:
        self._run_streaming(["apply", "-no-color", "-input=false", str(plan)], stream)
