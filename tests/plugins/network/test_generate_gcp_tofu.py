"""`tofu validate` golden test (plan 4 A5), marked `tofu` and excluded from the default
run (see pyproject.toml `addopts`). Needs network access to download OpenTofu and the
google provider the first time; run explicitly with:

    uv run pytest -m tofu
"""

from __future__ import annotations

from pathlib import Path

import pytest

from easymigrate.engine.tofu import Tofu, ensure_tofu
from easymigrate.providers.gcp.boilerplate import (
    render_providers_tf,
    render_variables_tf,
)

FIXTURES = Path(__file__).parent / "fixtures"
GOLDENS = sorted(p.parent.name for p in FIXTURES.glob("*/expected_network.tf"))

# The real boilerplate `plan()` generates is used here, so validate exercises the
# same providers.tf/variables.tf a live run gets.


@pytest.mark.tofu
@pytest.mark.parametrize("case", GOLDENS)
def test_golden_network_tf_validates(case, tmp_path):
    golden = (FIXTURES / case / "expected_network.tf").read_text()
    (tmp_path / "network.tf").write_text(golden)
    (tmp_path / "variables.tf").write_text(render_variables_tf("0.0.0-test"))
    (tmp_path / "providers.tf").write_text(render_providers_tf("0.0.0-test"))

    binary = ensure_tofu()
    tofu = Tofu(binary, tmp_path)
    tofu.init(backend=False)
    tofu.validate()
