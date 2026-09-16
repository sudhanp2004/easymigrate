"""Intent-level intermediate representation."""

from easymigrate.ir import database, network  # registers kinds; import order does not matter
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
    "database",
    "intent_class",
    "network",
]
