"""No silent mapping (D19): every field of every kubernetes kind is declared in FIELD_COVERAGE."""

import pytest

from easymigrate.ir.kubernetes import KubernetesCluster, NodePool
from easymigrate.plugins.kubernetes.generate_gcp import FIELD_COVERAGE


@pytest.mark.parametrize("model", [KubernetesCluster, NodePool])
def test_every_field_is_declared(model):
    kind = model.model_fields["kind"].default
    assert set(model.model_fields) == set(FIELD_COVERAGE[kind])
