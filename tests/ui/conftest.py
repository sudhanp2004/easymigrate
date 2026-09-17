from pathlib import Path

import pytest

from easymigrate.core.project import Project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)
