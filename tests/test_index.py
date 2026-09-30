from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chartpub.errors import ValidationError
from chartpub.index import (
    add_artifact,
    advertised_versions,
    chart_names,
    digests,
    dump_index,
    empty_index,
    find_entry,
    format_timestamp,
    latest_version,
    load_index,
    parse_index,
    referenced_files,
    remove_version,
    write_index,
)
from chartpub.models import Artifact, ChartMetadata

BASE = "https://example.test/charts"


def artifact(
    version: str = "0.4.1", *, digest: str | None = None, name: str = "ledger-api"
) -> Artifact:
    return Artifact(
        Path(f"{name}-{version}.tgz"),
        name,
        version,
        digest or ("a" * 64),
        42,
        ChartMetadata("v2", name, version, "example chart", "application", "1.8.2"),
    )


def test_add_is_idempotent() -> None:
    index: dict[str, object] = {"apiVersion": "v1", "entries": {}}
    add_artifact(index, artifact(), "https://example.test/charts")
    add_artifact(index, artifact(), "https://example.test/charts")
    assert len(index["entries"]["ledger-api"]) == 1  # type: ignore[index]


def test_remove_preserves_other_versions() -> None:
    index: dict[str, object] = {"apiVersion": "v1", "entries": {}}
    add_artifact(index, artifact("0.4.0"), "https://example.test/charts")
    add_artifact(index, artifact("0.3.0"), "https://example.test/charts")
    remove_version(index, "ledger-api", "0.4.0")
    versions = index["entries"]["ledger-api"]  # type: ignore[index]
    assert [item["version"] for item in versions] == ["0.3.0"]


def test_repeated_add_serialises_identically() -> None:
    first = empty_index()
    add_artifact(first, artifact(), BASE, created="2026-01-01T00:00:00Z")
    once = dump_index(first)
    add_artifact(first, artifact(), BASE, created="2027-09-09T09:09:09Z")
    assert dump_index(first) == once, "re-adding must not change the published bytes"


def test_generated_is_derived_from_entries_not_now() -> None:
    index = empty_index()
    add_artifact(index, artifact("0.4.0"), BASE, created="2026-01-01T00:00:00Z")
    add_artifact(index, artifact("0.4.1"), BASE, created="2026-05-05T00:00:00Z")
    assert index["generated"] == "2026-05-05T00:00:00Z"


def test_ordering_is_newest_first_by_semver() -> None:
    index = empty_index()
    for version in ("0.4.0", "0.10.0", "0.4.1", "1.0.0-rc.1", "1.0.0", "0.4.1-alpha.2"):
        add_artifact(index, artifact(version), BASE, created="2026-01-01T00:00:00Z")
    assert advertised_versions(index, "ledger-api") == (
        "1.0.0",
        "1.0.0-rc.1",
        "0.10.0",
        "0.4.1",
        "0.4.1-alpha.2",
        "0.4.0",
    )
    assert latest_version(index, "ledger-api") == "1.0.0"


def test_charts_are_sorted_and_preserved() -> None:
    index = empty_index()
    add_artifact(index, artifact(name="zeta"), BASE, created="2026-01-01T00:00:00Z")
    add_artifact(index, artifact(name="alpha"), BASE, created="2026-01-01T00:00:00Z")
    assert chart_names(index) == ("alpha", "zeta")
    remove_version(index, "alpha", "0.4.1")
    assert chart_names(index) == ("zeta",), "unrelated charts survive a removal"


def test_replacing_a_version_keeps_its_created_timestamp() -> None:
    index = empty_index()
    add_artifact(index, artifact(digest="a" * 64), BASE, created="2026-01-01T00:00:00Z")
    add_artifact(index, artifact(digest="c" * 64), BASE, created="2026-09-09T00:00:00Z")
    entry = find_entry(index, "ledger-api", "0.4.1")
    assert entry is not None
    assert entry["digest"] == "c" * 64
    assert entry["created"] == "2026-01-01T00:00:00Z"
    assert len(index["entries"]["ledger-api"]) == 1


def test_add_accepts_datetime_and_defaults_to_now() -> None:
    index = empty_index()
    add_artifact(index, artifact(), BASE, created=datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC))
    entry = find_entry(index, "ledger-api", "0.4.1")
    assert entry is not None and entry["created"] == "2026-03-04T05:06:07Z"
    other = empty_index()
    add_artifact(other, artifact("0.4.0"), BASE)
    stamped = find_entry(other, "ledger-api", "0.4.0")
    assert stamped is not None and stamped["created"].endswith("Z")


def test_add_without_metadata_still_produces_a_valid_entry() -> None:
    index = empty_index()
    bare = Artifact(Path("ledger-api-0.4.1.tgz"), "ledger-api", "0.4.1", "d" * 64, 1)
    add_artifact(index, bare, BASE, created="2026-01-01T00:00:00Z")
    entry = find_entry(index, "ledger-api", "0.4.1")
    assert entry == {
        "apiVersion": "v2",
        "created": "2026-01-01T00:00:00Z",
        "digest": "d" * 64,
        "name": "ledger-api",
        "urls": [f"{BASE}/ledger-api-0.4.1.tgz"],
        "version": "0.4.1",
    }


def test_entry_keys_are_canonically_ordered() -> None:
    index = empty_index()
    add_artifact(index, artifact(), BASE, created="2026-01-01T00:00:00Z")
    entry = find_entry(index, "ledger-api", "0.4.1")
    assert entry is not None
    assert list(entry) == [
        "apiVersion",
        "appVersion",
        "created",
        "description",
        "digest",
        "name",
        "type",
        "urls",
        "version",
    ]


def test_unknown_entry_fields_survive_deterministically() -> None:
    text = dump_index(
        {
            "apiVersion": "v1",
            "entries": {
                "ledger-api": [
                    {
                        "version": "0.4.1",
                        "name": "ledger-api",
                        "created": "2026-01-01T00:00:00Z",
                        "zzz": 1,
                        "annotations": {"a": "b"},
                    }
                ]
            },
        }
    )
    reparsed = parse_index(text)
    entry = find_entry(reparsed, "ledger-api", "0.4.1")
    assert entry is not None and entry["zzz"] == 1
    assert dump_index(reparsed) == text


def test_remove_missing_version_is_a_noop() -> None:
    index = empty_index()
    add_artifact(index, artifact(), BASE, created="2026-01-01T00:00:00Z")
    before = dump_index(dict(index))
    remove_version(index, "ledger-api", "9.9.9")
    remove_version(index, "absent-chart", "0.4.1")
    assert dump_index(index) == before


def test_removing_the_last_version_keeps_generated() -> None:
    index = empty_index()
    add_artifact(index, artifact(), BASE, created="2026-01-01T00:00:00Z")
    remove_version(index, "ledger-api", "0.4.1")
    assert index["entries"] == {}
    assert index["generated"] == "2026-01-01T00:00:00Z"


def test_referenced_files_and_digests() -> None:
    index = empty_index()
    add_artifact(index, artifact("0.4.0", digest="e" * 64), BASE, created="2026-01-01T00:00:00Z")
    add_artifact(index, artifact("0.4.1", digest="f" * 64), BASE, created="2026-01-01T00:00:00Z")
    assert referenced_files(index) == ("ledger-api-0.4.0.tgz", "ledger-api-0.4.1.tgz")
    assert digests(index, "ledger-api") == {"0.4.0": "e" * 64, "0.4.1": "f" * 64}


def test_parse_index_rejects_non_mapping() -> None:
    with pytest.raises(ValidationError, match="must be a mapping"):
        parse_index("- a\n")


def test_parse_index_rejects_bad_yaml() -> None:
    with pytest.raises(ValidationError, match="not valid YAML"):
        parse_index("entries: [unclosed\n")


def test_parse_index_rejects_non_mapping_entries() -> None:
    with pytest.raises(ValidationError, match="mapping of chart name"):
        parse_index("entries: 5\n")


def test_parse_index_rejects_non_list_versions() -> None:
    with pytest.raises(ValidationError, match="must be a list"):
        parse_index("entries:\n  ledger-api: 5\n")


def test_parse_index_accepts_empty_and_missing_entries() -> None:
    assert parse_index("")["entries"] == {}
    assert parse_index("apiVersion: v1\n")["entries"] == {}


def test_load_and_write_index_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "index.yaml"
    assert load_index(path) == empty_index()
    index = empty_index()
    add_artifact(index, artifact(), BASE, created="2026-01-01T00:00:00Z")
    assert write_index(path, index) is True
    assert write_index(path, index) is False, "unchanged bytes must not be rewritten"
    assert load_index(path)["entries"]["ledger-api"][0]["version"] == "0.4.1"


def test_non_dict_entries_are_dropped() -> None:
    index = parse_index("entries:\n  ledger-api:\n    - not-a-mapping\n")
    assert dump_index(index) == "apiVersion: v1\nentries: {}\n"


def test_format_timestamp_is_utc_seconds() -> None:
    assert (
        format_timestamp(datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=UTC))
        == "2026-01-02T03:04:05Z"
    )
