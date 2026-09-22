"""Provider-schema validation of the generated GKE Terraform (spec 5 §8)."""

from __future__ import annotations

import pytest

from easymigrate.engine.tofu import Tofu, ensure_tofu
from easymigrate.plugins.kubernetes.generate_gcp import generate
from easymigrate.plugins.network.generate_gcp import generate as generate_network
from easymigrate.providers.gcp.boilerplate import render_providers_tf, render_variables_tf
from tests.plugins.kubernetes.test_generate_gcp_golden import CASES, resolved_case


@pytest.mark.tofu
@pytest.mark.parametrize("case", CASES)
def test_kubernetes_tf_validates_alongside_network_tf(case, tmp_path):
    document, ctx = resolved_case(case)
    (tmp_path / "network.tf").write_text(generate_network(document, ctx).render())
    (tmp_path / "kubernetes.tf").write_text(generate(document, ctx).render())
    (tmp_path / "variables.tf").write_text(render_variables_tf("0.0.0-test"))
    (tmp_path / "providers.tf").write_text(render_providers_tf("0.0.0-test"))
    tofu = Tofu(ensure_tofu(), tmp_path)
    tofu.init(backend=False)
    tofu.validate()
