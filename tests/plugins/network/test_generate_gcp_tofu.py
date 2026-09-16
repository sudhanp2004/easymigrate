"""`tofu validate` golden test (plan 4 A5), marked `tofu` and excluded from the default
run (see pyproject.toml `addopts`). Needs network access to download OpenTofu and the
google provider the first time; run explicitly with:

    uv run pytest -m tofu
"""

from __future__ import annotations

from pathlib import Path

import pytest

from easymigrate.engine.tofu import Tofu, ensure_tofu

FIXTURES = Path(__file__).parent / "fixtures"
GOLDENS = sorted(p.parent.name for p in FIXTURES.glob("*/expected_network.tf"))

# Minimal scaffolding around each golden network.tf so `tofu validate` can resolve
# `var.region` and the implied `google` provider. Part B's `plan()` step is
# responsible for generating the full terraform/*.tf boilerplate (providers.tf,
# backend.tf, variables.tf, terraform.tfvars) for a real run; this is the smallest
# valid stand-in so Part A can validate the network.tf golden output in isolation.
_VARIABLES_TF = """\
variable "project" {
  type = string
}

variable "region" {
  type = string
}
"""

_PROVIDERS_TF = """\
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }
}
"""


@pytest.mark.tofu
@pytest.mark.parametrize("case", GOLDENS)
def test_golden_network_tf_validates(case, tmp_path):
    golden = (FIXTURES / case / "expected_network.tf").read_text()
    (tmp_path / "network.tf").write_text(golden)
    (tmp_path / "variables.tf").write_text(_VARIABLES_TF)
    (tmp_path / "providers.tf").write_text(_PROVIDERS_TF)

    binary = ensure_tofu()
    tofu = Tofu(binary, tmp_path)
    tofu.init(backend=False)
    tofu.validate()
