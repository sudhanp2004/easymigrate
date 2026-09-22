"""No silent mapping (D19): every field of every load-balancing kind is declared."""

import pytest

from easymigrate.ir.loadbalancing import LoadBalancer, TargetGroup
from easymigrate.plugins.loadbalancing.generate_gcp import FIELD_COVERAGE


@pytest.mark.parametrize("model", [LoadBalancer, TargetGroup])
def test_every_field_is_declared(model):
    assert set(model.model_fields) == set(FIELD_COVERAGE[model.model_fields["kind"].default])
