"""No silent mapping (D19): every storage_bucket field is declared in FIELD_COVERAGE."""

from easymigrate.ir.storage import StorageBucket
from easymigrate.plugins.storage.generate_gcp import FIELD_COVERAGE


def test_every_field_is_declared():
    assert set(StorageBucket.model_fields) == set(FIELD_COVERAGE["storage_bucket"])
