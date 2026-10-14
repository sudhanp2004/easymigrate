"""Expected, user-facing failures. The CLI prints the message and exits with code 1."""

from easymigrate.ir.versioning import SchemaError as SchemaError  # re-exported for cli/ui


class EasyMigrateError(Exception):
    pass


class SourceConnectionError(EasyMigrateError):
    """The source cloud could not be reached: credentials, role or region problem."""


class ExitTwoError(EasyMigrateError):
    """Base for failures that exit 2 rather than 1: the user declined, or the run was
    refused because of unresolved decisions or a stale plan (spec §6.2)."""


class NotReady(ExitTwoError):
    """`plan` was run with unresolved decisions (spec §5.4 step 2)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


class Declined(ExitTwoError):
    """The user declined a [y/N] prompt (state bucket creation or `apply`)."""


class PlanStale(ExitTwoError):
    """`apply` was run without a matching, current `plan` (spec §5.5 step 1)."""
