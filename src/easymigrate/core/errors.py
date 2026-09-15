"""Expected, user-facing failures. The CLI prints the message and exits with code 1."""


class EasyMigrateError(Exception):
    pass


class SourceConnectionError(EasyMigrateError):
    """The source cloud could not be reached: credentials, role or region problem."""
