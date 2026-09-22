"""Intent-level intermediate representation."""

from easymigrate.ir import (  # registers kinds; import order does not matter
    compute,
    database,
    kubernetes,
    network,
)
from easymigrate.ir.base import (
    Finding,
    IntentObject,
    IRDocument,
    ResolutionRef,
    SourceRef,
    StrictModel,
    intent_class,
)

__all__ = [
    "Finding",
    "IRDocument",
    "IntentObject",
    "ResolutionRef",
    "SourceRef",
    "StrictModel",
    "compute",
    "database",
    "intent_class",
    "kubernetes",
    "network",
]
