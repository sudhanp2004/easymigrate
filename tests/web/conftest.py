from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from easymigrate.web.settings import WebSettings
from easymigrate.web.store import MemoryStore, PostgresStore


def make_settings(tmp_path: Path, **overrides) -> WebSettings:
    values = dict(
        database_url=None,
        data_key=Fernet.generate_key().decode(),
        origin="https://easymigrate.sudhan.tech",
        cfn_template_url="https://easymigrate-public.s3.amazonaws.com/deploy/role.json",
        dev=False,
        data_dir=tmp_path / "data",
        max_concurrent_scans=2,
    )
    values.update(overrides)
    return WebSettings(**values)


@pytest.fixture
def settings(tmp_path) -> WebSettings:
    return make_settings(tmp_path)


_PG = os.environ.get("EASYMIGRATE_TEST_DATABASE_URL")


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        return MemoryStore()
    if not _PG:
        pytest.skip("EASYMIGRATE_TEST_DATABASE_URL not set")
    pg = PostgresStore(_PG)
    pg.reset_for_tests()
    return pg
