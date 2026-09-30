"""The recoverable publication transaction.

Ordering is the whole point. The incident happened because discoverability
changed first: ``index.yaml`` advertised a chart before the immutable release
asset had been accepted and verified, so a client could resolve an entry whose
bytes did not exist or did not match. Here the order is inverted and enforced:

1. upload the immutable release asset,
2. download it back and compare bytes and digest,
3. only then update the generated Pages snapshot.

Each completed step registers an undo action. If a later step fails, the
completed steps are undone newest-first and any undo that itself fails is
reported by name, so an operator is never told "rolled back" when it was not.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from chartpub.errors import RollbackError
from chartpub.models import Artifact
from chartpub.state import Journal, TransactionState

PageWriter = Callable[[Artifact], None]
ReleaseWriter = Callable[[Artifact], None]

Undo = Callable[[], None]


@dataclass
class Rollback:
    """A newest-first stack of undo actions with honest failure reporting."""

    actions: list[tuple[str, Undo]] = field(default_factory=list)

    def push(self, description: str, undo: Undo) -> None:
        self.actions.append((description, undo))

    def clear(self) -> None:
        self.actions.clear()

    def unwind(self) -> list[str]:
        """Run every undo newest-first; return the ones that failed."""
        failures: list[str] = []
        while self.actions:
            description, undo = self.actions.pop()
            try:
                undo()
            except Exception as exc:  # noqa: BLE001 - every failure must be reported
                failures.append(f"{description}: {exc}")
        return failures

    def unwind_or_raise(self, cause: BaseException) -> None:
        """Undo everything, then re-raise ``cause`` or a :class:`RollbackError`."""
        failures = self.unwind()
        if failures:
            raise RollbackError(
                f"{cause} (rollback incomplete, needs manual attention: "
                + "; ".join(failures)
                + ")"
            ) from cause
        raise cause


class Publisher:
    """Orders the two remote writes and journals progress between them.

    ``write_release`` must make the immutable asset exist and verify it;
    ``write_pages`` makes it discoverable. The release write always runs first.
    """

    def __init__(
        self,
        state_dir: Path,
        write_pages: PageWriter,
        write_release: ReleaseWriter,
        *,
        rollback: Rollback | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.write_pages = write_pages
        self.write_release = write_release
        self.rollback = rollback or Rollback()

    def publish(self, artifact: Artifact) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Release first: the asset must exist and be verified before anything
        # can discover it through the index.
        self.write_release(artifact)
        self._write_state("release-published", artifact)
        self.write_pages(artifact)
        self._write_state("pages-updated", artifact)
        self._write_state("complete", artifact)

    def _write_state(self, phase: str, artifact: Artifact) -> None:
        payload = {
            "phase": phase,
            "artifact": artifact.path.name,
            "sha256": artifact.sha256,
        }
        (self.state_dir / "transaction.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


@dataclass
class Step:
    """One named, journaled unit of remote work."""

    phase: str
    describe: str
    run: Callable[[TransactionState], TransactionState]
    undo: Callable[[TransactionState], None] | None = None


def run_steps(
    steps: Sequence[Step],
    state: TransactionState,
    journal: Journal,
    *,
    rollback: Rollback | None = None,
    on_step: Callable[[Step, TransactionState], None] | None = None,
) -> TransactionState:
    """Execute ``steps`` in order, journaling after each and unwinding on failure."""
    stack = rollback or Rollback()
    current = state
    for step in steps:
        try:
            current = step.run(current)
        except BaseException as exc:
            stack.unwind_or_raise(exc)
            raise  # pragma: no cover - unwind_or_raise always raises
        current = current.advanced(step.phase)
        journal.save(current)
        if step.undo is not None:
            stack.push(step.describe, partial(step.undo, current))
        if on_step is not None:
            on_step(step, current)
    stack.clear()
    return current
