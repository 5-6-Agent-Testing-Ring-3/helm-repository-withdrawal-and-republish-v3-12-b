"""The on-disk transaction journal.

A publication touches three independent remote systems (a release, a tag, a
branch). If the process dies between two of them the operator needs to know
exactly how far it got, which is what this journal records. It holds only
non-secret facts: object names, identifiers, digests, and phase markers.

The journal is what makes a retry a no-op rather than a duplicate: a recorded
``asset_id`` with a matching digest means the upload already happened.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from chartpub.errors import ChartpubError

STATE_DIR_NAME = ".chartpub"
STATE_FILE_NAME = "transaction.json"

#: Ordered publication phases. ``publish`` walks these in order; ``repair`` and
#: a resumed ``publish`` use the recorded phase to decide what is already done.
PHASES = (
    "planned",
    "validated",
    "release-drafted",
    "asset-uploaded",
    "asset-verified",
    "release-published",
    "pages-updated",
    "complete",
)

#: Phases in which the attempt has not yet become publicly discoverable and can
#: therefore be rolled back without any user-visible window.
PRIVATE_PHASES = frozenset(
    {"planned", "validated", "release-drafted", "asset-uploaded", "asset-verified"}
)


@dataclass(frozen=True)
class TransactionState:
    """A resumable record of one publish or withdraw attempt."""

    command: str
    repository: str
    chart: str
    version: str
    tag: str
    phase: str = "planned"
    asset_name: str = ""
    sha256: str = ""
    size: int = 0
    release_id: int | None = None
    asset_id: int | None = None
    created_release: bool = False
    created_tag: bool = False
    pages_old_tip: str | None = None
    pages_new_tip: str | None = None
    index_created: str = ""
    history: tuple[str, ...] = field(default_factory=tuple)

    def advanced(self, phase: str, **changes: Any) -> TransactionState:
        if phase not in PHASES:  # pragma: no cover - programmer error
            raise ValueError(f"unknown phase: {phase}")
        return replace(self, phase=phase, history=(*self.history, phase), **changes)

    @property
    def publicly_visible(self) -> bool:
        return self.phase not in PRIVATE_PHASES

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["history"] = list(self.history)
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TransactionState:
        known = {
            "command",
            "repository",
            "chart",
            "version",
            "tag",
            "phase",
            "asset_name",
            "sha256",
            "size",
            "release_id",
            "asset_id",
            "created_release",
            "created_tag",
            "pages_old_tip",
            "pages_new_tip",
            "index_created",
            "history",
        }
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ChartpubError(
                f"unreadable transaction journal; unknown field(s): {', '.join(unknown)}"
            )
        data = dict(raw)
        data["history"] = tuple(data.get("history") or ())
        return cls(**data)


class Journal:
    """Reads and writes :class:`TransactionState` under ``state_dir``."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    @property
    def path(self) -> Path:
        return self.state_dir / STATE_FILE_NAME

    def load(self) -> TransactionState | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ChartpubError(f"cannot read transaction journal: {exc}") from exc
        if not isinstance(raw, dict):
            raise ChartpubError("transaction journal must be a JSON object")
        return TransactionState.from_dict(raw)

    def save(self, state: TransactionState) -> TransactionState:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
        # Write-then-rename so a crash mid-write cannot leave a truncated
        # journal that would block recovery.
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)
        return state

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def resumable(self, *, command: str, version: str, sha256: str) -> TransactionState | None:
        """A journal entry that describes this exact attempt, if any.

        A journal for a different version or a different candidate digest is not
        resumable: resuming it would publish the wrong bytes.
        """
        state = self.load()
        if state is None:
            return None
        if state.command != command or state.version != version:
            return None
        if state.sha256 and sha256 and state.sha256 != sha256:
            return None
        return state
