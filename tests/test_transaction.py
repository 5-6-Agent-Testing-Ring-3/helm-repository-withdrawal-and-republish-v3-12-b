from __future__ import annotations

from pathlib import Path

import pytest

from chartpub.errors import RollbackError
from chartpub.models import Artifact
from chartpub.state import Journal, TransactionState
from chartpub.transaction import Publisher, Rollback, Step, run_steps


def test_release_is_verified_before_pages_change(tmp_path: Path) -> None:
    events: list[str] = []

    def fail_release(_artifact: Artifact) -> None:
        events.append("release")
        raise RuntimeError("upload rejected")

    publisher = Publisher(
        tmp_path / "state", lambda _artifact: events.append("pages"), fail_release
    )
    artifact = Artifact(tmp_path / "chart.tgz", "ledger-api", "0.4.1", "a" * 64, 1)
    with pytest.raises(RuntimeError, match="upload rejected"):
        publisher.publish(artifact)
    assert events == ["release"]


def test_completed_publication_records_digest(tmp_path: Path) -> None:
    publisher = Publisher(tmp_path / "state", lambda _artifact: None, lambda _artifact: None)
    artifact = Artifact(tmp_path / "chart.tgz", "ledger-api", "0.4.1", "b" * 64, 1)
    publisher.publish(artifact)
    assert '"phase": "complete"' in (tmp_path / "state" / "transaction.json").read_text()
    assert artifact.sha256 in (tmp_path / "state" / "transaction.json").read_text()


def test_publication_order_is_release_then_pages(tmp_path: Path) -> None:
    events: list[str] = []
    publisher = Publisher(
        tmp_path / "state",
        lambda _a: events.append("pages"),
        lambda _a: events.append("release"),
    )
    publisher.publish(Artifact(tmp_path / "c.tgz", "ledger-api", "0.4.1", "c" * 64, 1))
    assert events == ["release", "pages"]


def test_rollback_unwinds_newest_first() -> None:
    order: list[str] = []
    stack = Rollback()
    stack.push("first", lambda: order.append("first"))
    stack.push("second", lambda: order.append("second"))
    assert stack.unwind() == []
    assert order == ["second", "first"]
    assert stack.actions == []


def test_rollback_reports_every_failed_undo() -> None:
    stack = Rollback()

    def boom(name: str) -> None:
        raise RuntimeError(f"{name} undo failed")

    stack.push("delete asset", lambda: boom("asset"))
    stack.push("delete release", lambda: boom("release"))
    failures = stack.unwind()
    assert failures == [
        "delete release: release undo failed",
        "delete asset: asset undo failed",
    ]


def test_rollback_raises_the_original_cause_when_undo_succeeds() -> None:
    stack = Rollback()
    stack.push("clean", lambda: None)
    cause = RuntimeError("upload rejected")
    with pytest.raises(RuntimeError, match="upload rejected"):
        stack.unwind_or_raise(cause)


def test_rollback_error_names_what_still_needs_attention() -> None:
    stack = Rollback()

    def boom() -> None:
        raise RuntimeError("404 from GitHub")

    stack.push("delete uploaded asset id=7", boom)
    with pytest.raises(RollbackError) as excinfo:
        stack.unwind_or_raise(RuntimeError("digest mismatch"))
    message = str(excinfo.value)
    assert "digest mismatch" in message
    assert "rollback incomplete" in message
    assert "delete uploaded asset id=7" in message
    assert "404 from GitHub" in message


def base_state() -> TransactionState:
    return TransactionState(
        command="publish", repository="o/r", chart="ledger-api", version="0.4.1", tag="chart-v0.4.1"
    )


def test_run_steps_journals_each_phase(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    seen: list[str] = []
    steps = [
        Step("validated", "validate", lambda s: s),
        Step("asset-uploaded", "upload", lambda s: s, undo=lambda _s: seen.append("undo-upload")),
        Step("complete", "finish", lambda s: s),
    ]
    final = run_steps(
        steps, base_state(), journal, on_step=lambda step, _s: seen.append(step.phase)
    )
    assert final.history == ("validated", "asset-uploaded", "complete")
    assert seen == ["validated", "asset-uploaded", "complete"]
    loaded = journal.load()
    assert loaded is not None and loaded.phase == "complete"


def test_run_steps_unwinds_completed_steps_on_failure(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    undone: list[str] = []

    def explode(_s: TransactionState) -> TransactionState:
        raise RuntimeError("pages push refused")

    steps = [
        Step("asset-uploaded", "delete asset", lambda s: s, undo=lambda _s: undone.append("asset")),
        Step(
            "release-published",
            "unpublish release",
            lambda s: s,
            undo=lambda _s: undone.append("release"),
        ),
        Step("pages-updated", "revert pages", explode),
    ]
    with pytest.raises(RuntimeError, match="pages push refused"):
        run_steps(steps, base_state(), journal)
    assert undone == ["release", "asset"]
    loaded = journal.load()
    assert loaded is not None and loaded.phase == "release-published"


def test_run_steps_clears_the_stack_on_success(tmp_path: Path) -> None:
    stack = Rollback()
    run_steps(
        [Step("complete", "x", lambda s: s, undo=lambda _s: None)],
        base_state(),
        Journal(tmp_path / "state"),
        rollback=stack,
    )
    assert stack.actions == []
