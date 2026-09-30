"""Credential intake and redaction.

The credential file is read at runtime only. Nothing in this module writes a
secret to disk, to Git configuration, or into a URL, and every string that may
reach an operator's terminal goes through :class:`Redactor`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from chartpub.errors import UsageError

SENSITIVE_NAMES = ("GITHUB_TOKEN", "GH_TOKEN", "CHARTPUB_TOKEN")

#: Shapes of GitHub credentials that must never be echoed even if the exact
#: value was never handed to us (for example a token quoted back by an API
#: error body, or a token belonging to a different tool).
TOKEN_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bx-access-token:[^@\s/]+"),
)

REDACTION = "[REDACTED]"

#: Shortest value we are willing to blind-replace. Replacing very short
#: strings would mangle unrelated output without protecting anything.
_MIN_SECRET_LENGTH = 8


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` credential file.

    Values are never logged. ``export`` prefixes and ``#`` comments are
    tolerated so an operator can reuse a shell-sourceable file.
    """
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise UsageError(f"cannot read credential file: {path}") from exc
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        key, separator, value = stripped.partition("=")
        if not separator or not key.strip():
            raise UsageError(f"invalid environment entry on line {number}")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def require_token(values: Mapping[str, str]) -> str:
    """Return the GitHub token, or fail without revealing what was present."""
    for name in SENSITIVE_NAMES:
        token = values.get(name)
        if token:
            return token
    raise UsageError(
        "GitHub token is missing: set one of "
        + ", ".join(SENSITIVE_NAMES)
        + " in the credential file"
    )


def secret_values(values: Mapping[str, str]) -> tuple[str, ...]:
    """Every value in ``values`` that must be treated as a secret."""
    found = []
    for name in SENSITIVE_NAMES:
        value = values.get(name)
        if value and len(value) >= _MIN_SECRET_LENGTH:
            found.append(value)
    return tuple(found)


class Redactor:
    """Replaces known secrets and credential-shaped substrings."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(
            sorted(
                {s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH},
                key=len,
                reverse=True,
            )
        )

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= _MIN_SECRET_LENGTH and secret not in self._secrets:
            self._secrets = tuple(sorted({*self._secrets, secret}, key=len, reverse=True))

    def __call__(self, message: str) -> str:
        result = message
        for secret in self._secrets:
            result = result.replace(secret, REDACTION)
        for pattern in TOKEN_PATTERNS:
            result = pattern.sub(REDACTION, result)
        return result


def redact(message: str, secrets: Mapping[str, str]) -> str:
    """Convenience wrapper mirroring the original helper's signature."""
    return Redactor(secret_values(secrets))(message)


def scrubbed_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A child-process environment with credential variables stripped.

    Used for every ``helm`` and ``git`` invocation that does not need the
    token, so a subprocess can never echo it.
    """
    source = os.environ if base is None else base
    return {key: value for key, value in source.items() if key not in SENSITIVE_NAMES}
