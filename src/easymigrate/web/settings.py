"""Hosted app configuration, read from environment variables (see render.yaml)."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ORIGIN = "https://easymigrate.sudhan.tech"


@dataclass(frozen=True)
class WebSettings:
    database_url: str | None
    data_key: str
    origin: str
    cfn_template_url: str
    dev: bool
    data_dir: Path
    max_concurrent_scans: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebSettings:
        env = os.environ if env is None else env
        dev = env.get("EASYMIGRATE_WEB_DEV") == "1"
        data_key = env.get("EASYMIGRATE_DATA_KEY", "")
        if not data_key:
            if not dev:
                raise RuntimeError("EASYMIGRATE_DATA_KEY is not set")
            from cryptography.fernet import Fernet

            data_key = Fernet.generate_key().decode()
        return cls(
            database_url=env.get("DATABASE_URL") or None,
            data_key=data_key,
            origin=env.get("EASYMIGRATE_WEB_ORIGIN", DEFAULT_ORIGIN),
            cfn_template_url=env.get("EASYMIGRATE_CFN_TEMPLATE_URL", ""),
            dev=dev,
            data_dir=Path(
                env.get("EASYMIGRATE_DATA_DIR") or Path(tempfile.gettempdir()) / "easymigrate-web"
            ),
        )
