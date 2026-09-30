from __future__ import annotations

import json
from pathlib import Path

import pytest

from chartpub.errors import ChartpubError
from chartpub.state import Journal, TransactionState


def state(**overrides: object) -> TransactionState:
    base: dict[str, object] = {
        "command": "publish",
        "repository": "o/r",
        "chart": "ledger-api",
        "version": "0.4.1",
        "tag": "chart-v0.4.1",
        "sha256": "a" * 64,
    }
    base.update(overrides)
    return TransactionState(**base)  # type: ignore[arg-type]


def test_round_trips_through_disk(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    assert journal.load() is None
    saved = journal.save(state(release_id=7, asset_id=8))
    loaded = journal.load()
    assert loaded == saved
    assert json.loads(journal.path.read_text())["release_id"] == 7


def test_advanced_records_history() -> None:
    current = state()
    current = current.advanced("validated")
    current = current.advanced("asset-uploaded", asset_id=12)
    assert current.phase == "asset-uploaded"
    assert current.history == ("validated", "asset-uploaded")
    assert current.asset_id == 12


def test_advanced_rejects_an_unknown_phase() -> None:
    with pytest.raises(ValueError, match="unknown phase"):
        state().advanced("made-up")


@pytest.mark.parametrize(
    ("phase", "visible"),
    [
        ("planned", False),
        ("validated", False),
        ("release-drafted", False),
        ("asset-uploaded", False),
        ("asset-verified", False),
        ("release-published", True),
        ("pages-updated", True),
        ("complete", True),
    ],
)
def test_public_visibility_boundary(phase: str, visible: bool) -> None:
    assert state(phase=phase).publicly_visible is visible


def test_save_is_atomic_and_leaves_no_temporary(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    journal.save(state())
    assert [p.name for p in journal.state_dir.iterdir()] == ["transaction.json"]


def test_clear_is_tolerant(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    journal.clear()
    journal.save(state())
    journal.clear()
    assert journal.load() is None


def test_load_rejects_corrupt_json(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    journal.state_dir.mkdir(parents=True)
    journal.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ChartpubError, match="cannot read transaction journal"):
        journal.load()


def test_load_rejects_non_object(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    journal.state_dir.mkdir(parents=True)
    journal.path.write_text("[]", encoding="utf-8")
    with pytest.raises(ChartpubError, match="must be a JSON object"):
        journal.load()


def test_load_rejects_unknown_fields(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    journal.state_dir.mkdir(parents=True)
    journal.path.write_text(json.dumps({"command": "publish", "surprise": 1}), encoding="utf-8")
    with pytest.raises(ChartpubError, match="unknown field"):
        journal.load()


def test_resumable_matches_only_the_same_attempt(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    journal.save(state(phase="asset-uploaded", asset_id=5))
    assert journal.resumable(command="publish", version="0.4.1", sha256="a" * 64) is not None
    assert journal.resumable(command="withdraw", version="0.4.1", sha256="a" * 64) is None
    assert journal.resumable(command="publish", version="0.5.0", sha256="a" * 64) is None
    assert journal.resumable(command="publish", version="0.4.1", sha256="b" * 64) is None, (
        "a different candidate digest must never be resumed"
    )


def test_resumable_with_no_journal(tmp_path: Path) -> None:
    assert (
        Journal(tmp_path / "state").resumable(command="publish", version="0.4.1", sha256="") is None
    )


def test_state_serialises_history_as_a_list(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    journal.save(state().advanced("validated"))
    assert json.loads(journal.path.read_text())["history"] == ["validated"]


def test_journal_holds_no_secret_shaped_fields() -> None:
    fields = set(state().to_dict())
    assert not any("token" in name or "secret" in name or "password" in name for name in fields)
