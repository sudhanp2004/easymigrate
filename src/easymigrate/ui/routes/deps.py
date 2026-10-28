"""How a route module finds its project: per request, through FastAPI dependencies.

The local UI serves one fixed project; the hosted app serves one project per visitor
workspace. Both register the same review and job routes with a `RouteDeps` that says how
to get the request's `Project` and `JobStore`, and which guard dependencies reads and
mutating requests need.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from easymigrate.core.project import Project
from easymigrate.ui.jobs import JobStore


@dataclass(frozen=True)
class RouteDeps:
    project: Callable[..., Project]
    jobs: Callable[..., JobStore]
    reads: list[Any] = field(default_factory=list)
    mutating: list[Any] = field(default_factory=list)
