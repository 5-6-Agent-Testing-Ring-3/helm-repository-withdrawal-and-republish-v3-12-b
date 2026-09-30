"""Operator-facing failures and the exit codes they map to.

Every failure surfaced by the CLI is one of these. The numeric ``exit_code``
is part of the published operator contract; see ``docs/OPERATIONS.md``.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_CONFLICT = 4
EXIT_REMOTE = 5
EXIT_ROLLBACK = 6


class ChartpubError(Exception):
    """Base class for concise operator-facing failures."""

    exit_code = EXIT_INTERNAL


class ContractError(ChartpubError):
    """The publication contract is invalid."""

    exit_code = EXIT_USAGE


class UsageError(ChartpubError):
    """The operator asked for something the tool refuses to do."""

    exit_code = EXIT_USAGE


class TargetMismatch(UsageError):
    """The configured target disagrees with the checked-out Git origin."""

    exit_code = EXIT_USAGE


class ValidationError(ChartpubError):
    """A candidate chart failed pre-publication validation.

    Raised strictly before any remote mutation, so the public index and the
    current stable version are guaranteed untouched.
    """

    exit_code = EXIT_VALIDATION


class AuditDrift(ChartpubError):
    """``audit`` found the public repository inconsistent."""

    exit_code = EXIT_VALIDATION


class PublicationError(ChartpubError):
    """A publication transition could not be completed safely."""

    exit_code = EXIT_REMOTE


class RemoteHTTPError(PublicationError):
    """A GitHub API call returned an error status.

    ``status`` lets callers distinguish "absent" (404) from "refused", which is
    the difference between a clean no-op and a genuine failure.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class RemoteConflict(PublicationError):
    """Remote state changed after it was inspected.

    Raised *before* the mutation that would have overwritten concurrent work.
    """

    exit_code = EXIT_CONFLICT


class RollbackError(PublicationError):
    """A partial attempt could not be fully rolled back.

    The message names every object that still needs operator attention.
    """

    exit_code = EXIT_ROLLBACK


__all__ = [
    "EXIT_CONFLICT",
    "EXIT_INTERNAL",
    "EXIT_OK",
    "EXIT_REMOTE",
    "EXIT_ROLLBACK",
    "EXIT_USAGE",
    "EXIT_VALIDATION",
    "AuditDrift",
    "ChartpubError",
    "ContractError",
    "PublicationError",
    "RemoteConflict",
    "RemoteHTTPError",
    "RollbackError",
    "TargetMismatch",
    "UsageError",
    "ValidationError",
]
