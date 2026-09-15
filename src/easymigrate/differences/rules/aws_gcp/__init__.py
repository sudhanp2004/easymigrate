"""AWS→GCP difference rule catalogue, in the fixed order rules apply (spec §4.4)."""

from __future__ import annotations

from easymigrate.differences.model import DifferenceRule

AWS_GCP_RULES: tuple[DifferenceRule, ...] = ()
