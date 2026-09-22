"""Provider-schema validation of the generated Cloud Storage Terraform (spec 6 §8)."""

from __future__ import annotations

import pytest

from easymigrate.engine.tofu import Tofu, ensure_tofu
from easymigrate.plugins.storage.generate_gcp import generate
from easymigrate.providers.gcp.boilerplate import render_providers_tf, render_variables_tf
from tests.plugins.storage.test_generate_gcp_golden import CASES, resolved_case


@pytest.mark.tofu
@pytest.mark.parametrize("case", CASES)
def test_storage_tf_validates(case, tmp_path):
    document, ctx = resolved_case(case)
    (tmp_path / "storage.tf").write_text(generate(document, ctx).render())
    (tmp_path / "variables.tf").write_text(render_variables_tf("0.0.0-test"))
    (tmp_path / "providers.tf").write_text(render_providers_tf("0.0.0-test"))
    tofu = Tofu(ensure_tofu(), tmp_path)
    tofu.init(backend=False)
    tofu.validate()
