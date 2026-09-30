from __future__ import annotations

import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from chartpub import lifecycle
from chartpub.archive import sha256_bytes
from chartpub.errors import (
    AuditDrift,
    PublicationError,
    RemoteConflict,
    RollbackError,
    UsageError,
    ValidationError,
)
from chartpub.github import Response
from chartpub.index import (
    advertised_versions,
    digests,
    dump_index,
    find_entry,
    parse_index,
)
from chartpub.lifecycle import Session

from .conftest import PAGES_URL, Remote
from .fakes import MISMATCHED_MANIFEST, FakeHelm

SessionFactory = Callable[..., Session]

INDEX = "index.yaml"
BAD_ASSET = "ledger-api-0.4.0.tgz"
NEW_ASSET = "ledger-api-0.4.1.tgz"


def live_index(remote: Remote) -> dict[str, object]:
    return parse_index(remote.pages_files()[INDEX].decode("utf-8"))


# ------------------------------------------------------------------- publish


def test_publish_happy_path(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    session = session_factory()
    result = lifecycle.publish(session)

    assert result["applied"] is True
    assert result["validation"]["ok"] is True

    # The release exists, is published, and carries the verified asset.
    release = remote.api.releases[-1]
    assert release.tag_name == "chart-v0.4.1"
    assert release.draft is False
    assert release.asset_names() == [NEW_ASSET]
    payload = release.assets[0].payload

    # The tag was created by publishing the release, not by a separate push.
    assert remote.refs()["refs/tags/chart-v0.4.1"] == remote.main_tip

    # Pages advertises both versions, newest first, with the real digest.
    files = remote.pages_files()
    assert files[NEW_ASSET] == payload
    index = live_index(remote)
    assert advertised_versions(index, "ledger-api") == ("0.4.1", "0.4.0")
    assert digests(index, "ledger-api")["0.4.1"] == sha256_bytes(payload)
    assert files["README.md"] == published_pages["README.md"], "unrelated files survive"

    # The Pages tip moved from exactly the leased commit to the new one.
    assert result["pages"]["old_tip"] == remote.pages_tip
    assert result["pages"]["new_tip"] == remote.refs()["refs/heads/gh-pages"]
    assert result["pages"]["changed"] is True


def test_publish_orders_release_before_pages(
    session_factory: SessionFactory,
    remote: Remote,
    published_pages: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload, then verify by download, and only then advertise."""
    session = session_factory()
    pages_at: list[int] = []
    original = lifecycle._mutate_pages

    def watched(*args: Any, **kwargs: Any) -> Any:
        # Where in the API call log the Pages update happened.
        pages_at.append(len(remote.api.calls))
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_mutate_pages", watched)
    lifecycle.publish(session)

    upload = next(
        i
        for i, (method, path) in enumerate(remote.api.calls)
        if method == "POST" and path.endswith("/assets")
    )
    download = next(
        i
        for i, (method, path) in enumerate(remote.api.calls)
        if method == "GET" and "/releases/assets/" in path
    )
    assert upload < download < pages_at[0]


def test_publish_dry_run_changes_nothing(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    before_refs = remote.refs()
    before_files = remote.pages_files()
    session = session_factory(dry_run=True)
    result = lifecycle.publish(session)

    assert result["applied"] is False
    assert result["plan"]["dry_run"] is True
    assert result["validation"]["ok"] is True
    assert remote.refs() == before_refs
    assert remote.pages_files() == before_files
    assert remote.api.releases[0].tag_name == "chart-v0.4.0"
    assert len(remote.api.releases) == 1
    assert not any(method in {"POST", "PATCH", "DELETE"} for method, _ in remote.api.calls)


def test_publish_stops_at_validation_leaving_the_index_untouched(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    before_refs = remote.refs()
    before_files = remote.pages_files()
    session = session_factory(helm_runner=FakeHelm(rendered=MISMATCHED_MANIFEST))

    with pytest.raises(ValidationError, match="manifest-invariants"):
        lifecycle.publish(session)

    assert remote.refs() == before_refs
    assert remote.pages_files() == before_files
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.0",)
    assert len(remote.api.releases) == 1, "no release object was created"


def test_publish_is_idempotent(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    first = lifecycle.publish(session_factory())
    tip_after_first = remote.refs()["refs/heads/gh-pages"]
    files_after_first = remote.pages_files()

    second = lifecycle.publish(session_factory())

    assert second["applied"] is True
    assert second["pages"]["changed"] is False
    assert second["idempotent_noop"] is True
    assert remote.refs()["refs/heads/gh-pages"] == tip_after_first
    assert remote.pages_files() == files_after_first
    assert len(remote.api.releases) == 2, "no duplicate release"
    assert remote.api.releases[-1].asset_names() == [NEW_ASSET], "no duplicate asset"
    assert len(live_index(remote)["entries"]["ledger-api"]) == 2  # type: ignore[index]
    assert first["artifact"]["sha256"] == second["artifact"]["sha256"]


def test_publish_resumes_after_a_partial_upload(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    """The process dies right after the asset upload; the retry must not duplicate it."""
    session = session_factory()
    remote.api.fail_next("GET", "/releases/assets/", 500, times=10)
    with pytest.raises(PublicationError, match=r"\(500\)"):
        lifecycle.publish(session)

    # The draft release and its asset were rolled back, so nothing is orphaned.
    assert [r.tag_name for r in remote.api.releases] == ["chart-v0.4.0"]
    assert "refs/tags/chart-v0.4.1" not in remote.refs()
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.0",)

    # A clean retry succeeds and produces exactly one asset.
    remote.api.failures.clear()
    result = lifecycle.publish(session_factory())
    assert result["applied"] is True
    assert remote.api.releases[-1].asset_names() == [NEW_ASSET]


def test_publish_reuses_an_already_uploaded_asset(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    """A crash after upload but before the Pages push leaves the asset in place."""
    session = session_factory()
    artifact = lifecycle.build_candidate(session, "0.4.1")
    draft = remote.api.add_release(tag_name="chart-v0.4.1", name="ledger-api 0.4.1", draft=True)
    existing = remote.api.add_asset(draft, NEW_ASSET, artifact.path.read_bytes())

    result = lifecycle.publish(session_factory())

    assert result["applied"] is True
    assert result["release"]["id"] == draft.id, "the existing release object is reused"
    assert result["release"]["asset_id"] == existing.id, "the existing asset is reused"
    assert draft.asset_names() == [NEW_ASSET]
    assert draft.draft is False
    upload_calls = [p for m, p in remote.api.calls if m == "POST" and p.endswith("/assets")]
    assert upload_calls == [], "no second upload"


def test_publish_refuses_a_conflicting_immutable_asset(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    draft = remote.api.add_release(tag_name="chart-v0.4.1", name="ledger-api 0.4.1", draft=True)
    remote.api.add_asset(draft, NEW_ASSET, b"different bytes entirely")
    with pytest.raises(RemoteConflict, match="release assets are immutable"):
        lifecycle.publish(session_factory())
    assert draft.draft is True
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.0",)


def test_publish_detects_a_digest_mismatch_on_readback(
    session_factory: SessionFactory,
    remote: Remote,
    published_pages: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truthful upload response is not enough: the bytes must come back intact."""
    session = session_factory()
    upstream = remote.api

    def corrupting(request: urllib.request.Request) -> Response:
        response = upstream(request)
        if "/releases/assets/" in request.full_url and request.get_method() == "GET":
            return Response(200, None, {}, response.raw + b"tampered")
        return response

    monkeypatch.setattr(session.client, "_transport", corrupting)

    with pytest.raises(ValidationError, match="does not match the candidate bytes"):
        lifecycle.publish(session)

    assert [r.tag_name for r in remote.api.releases] == ["chart-v0.4.0"], "draft rolled back"
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.0",)
    assert "refs/tags/chart-v0.4.1" not in remote.refs()


def test_publish_reports_an_incomplete_rollback(
    session_factory: SessionFactory,
    remote: Remote,
    published_pages: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = session_factory()

    def refuse(release_id: int) -> None:
        raise RuntimeError("release delete forbidden")

    def ignore(asset_id: int) -> None:
        return None

    def corrupt_download(asset_id: int, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"corrupted")
        return destination

    monkeypatch.setattr(session.client, "delete_release", refuse)
    monkeypatch.setattr(session.client, "delete_asset", ignore)
    monkeypatch.setattr(session.client, "download_asset", corrupt_download)

    with pytest.raises(RollbackError) as excinfo:
        lifecycle.publish(session)
    message = str(excinfo.value)
    assert "rollback incomplete" in message
    assert "delete draft release" in message
    assert "release delete forbidden" in message
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.0",)


def test_publish_rollback_removes_the_tag_it_created(
    session_factory: SessionFactory,
    remote: Remote,
    published_pages: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publishing the release creates the tag, so a Pages failure must undo both."""
    session = session_factory()

    def refuse(*_args: Any, **_kwargs: Any) -> tuple[str | None, bool]:
        raise RemoteConflict("gh-pages moved under us")

    monkeypatch.setattr(lifecycle, "_mutate_pages", refuse)

    with pytest.raises(RemoteConflict, match="gh-pages moved under us"):
        lifecycle.publish(session)

    assert "refs/tags/chart-v0.4.1" not in remote.refs(), "no orphan tag is left behind"
    assert [r.tag_name for r in remote.api.releases] == ["chart-v0.4.0"]
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.0",)
    # And the state it left behind is clean enough that audit sees no new drift.
    codes = {item.code for item in lifecycle.audit(session_factory()).findings}
    assert "orphan-replacement-tag" not in codes


def test_publish_rollback_keeps_a_tag_it_did_not_create(
    session_factory: SessionFactory,
    remote: Remote,
    published_pages: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing tag is never deleted by a rollback."""
    remote.api.set_ref("refs/tags/chart-v0.4.1", remote.main_tip)
    existing = remote.api.add_release(tag_name="chart-v0.4.1", name="ledger-api 0.4.1", draft=False)
    session = session_factory()
    artifact = lifecycle.build_candidate(session, "0.4.1")
    remote.api.add_asset(existing, NEW_ASSET, artifact.path.read_bytes())

    def refuse(*_args: Any, **_kwargs: Any) -> tuple[str | None, bool]:
        raise RemoteConflict("gh-pages moved under us")

    monkeypatch.setattr(lifecycle, "_mutate_pages", refuse)
    with pytest.raises(RemoteConflict):
        lifecycle.publish(session)

    assert remote.refs()["refs/tags/chart-v0.4.1"] == remote.main_tip
    assert existing in remote.api.releases, "a pre-existing release is never deleted"
    assert existing.draft is False
    assert existing.asset_names() == [NEW_ASSET], "a pre-existing asset is never deleted"


def test_audit_reports_an_orphan_replacement_tag(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    """A public tag nothing advertises must not be invisible."""
    remote.api.set_ref("refs/tags/chart-v0.4.1", remote.main_tip)
    findings = {item.code for item in lifecycle.audit(session_factory()).findings}
    assert "orphan-replacement-tag" in findings


def test_publish_refuses_an_unexpected_pages_tip(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    session = session_factory(expect_pages_tip="0" * 40)
    with pytest.raises(RemoteConflict, match="refs/heads/gh-pages"):
        lifecycle.publish(session)
    assert len(remote.api.releases) == 1, "the conflict is detected before any write"
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.0",)


def test_publish_requires_force_to_replace_published_bytes(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    lifecycle.publish(session_factory())
    tip = remote.refs()["refs/heads/gh-pages"]

    # Rewrite the index digest so the candidate no longer matches what is live.
    index = live_index(remote)
    entry = find_entry(index, "ledger-api", "0.4.1")
    assert entry is not None
    entry["digest"] = "9" * 64
    files = remote.pages_files()
    files[INDEX] = dump_index(index).encode("utf-8")
    new_tip = remote.seed_pages(files, branch="gh-pages")
    assert new_tip != tip

    with pytest.raises(UsageError, match="re-run with --force"):
        lifecycle.publish(session_factory(force=False, expect_pages_tip=new_tip))

    forced = lifecycle.publish(session_factory(force=True, expect_pages_tip=new_tip))
    assert forced["applied"] is True
    assert digests(live_index(remote), "ledger-api")["0.4.1"] != "9" * 64


def test_publish_preserves_unrelated_charts(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    files = dict(published_pages)
    index = parse_index(files[INDEX].decode("utf-8"))
    entries = index["entries"]
    entries["other-chart"] = [
        {
            "apiVersion": "v2",
            "name": "other-chart",
            "version": "1.2.3",
            "digest": "c" * 64,
            "created": "2026-01-01T00:00:00Z",
            "urls": [f"{PAGES_URL}/other-chart-1.2.3.tgz"],
        }
    ]
    files[INDEX] = dump_index(index).encode("utf-8")
    tip = remote.seed_pages(files)

    lifecycle.publish(session_factory(expect_pages_tip=tip))
    assert advertised_versions(live_index(remote), "other-chart") == ("1.2.3",)


def test_publish_refuses_when_pages_branch_is_absent(
    session_factory: SessionFactory, remote: Remote
) -> None:
    """Publishing never creates the Pages branch; `repair` is the documented route."""
    session = session_factory(expect_pages_tip=None)
    with pytest.raises(RemoteConflict, match="refs/heads/gh-pages"):
        lifecycle.publish(session)
    assert "refs/heads/gh-pages" not in remote.refs()


# ------------------------------------------------------------------ withdraw


def test_withdraw_removes_exactly_the_bad_version(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    # Publish the replacement first, so withdrawal has to leave it alone.
    lifecycle.publish(session_factory())
    tip = remote.refs()["refs/heads/gh-pages"]
    replacement_release = remote.api.releases[-1]

    result = lifecycle.withdraw(
        session_factory(
            expect_pages_tip=tip, contract_overrides={"expected_bad_tag_target": remote.main_tip}
        )
    )

    assert result["applied"] is True
    assert result["deleted_tag"] == f"refs/tags/chart-v0.4.0@{remote.main_tip}"
    assert sorted(result["withdrawn_objects"]) == sorted(
        [
            f"refs/tags/chart-v0.4.0@{remote.main_tip}",
            "pages:ledger-api-0.4.0.tgz",
            "index-entry:ledger-api 0.4.0",
            "release:chart-v0.4.0->draft",
        ]
    )

    # Only the bad tag is gone.
    refs = remote.refs()
    assert "refs/tags/chart-v0.4.0" not in refs
    assert refs["refs/tags/chart-v0.4.1"] == remote.main_tip
    assert refs["refs/heads/main"] == remote.main_tip

    # The release keeps its identity and its asset, but is a draft.
    bad = next(r for r in remote.api.releases if r.tag_name == "chart-v0.4.0")
    assert bad.id == result["release"]["id"]
    assert bad.draft is True
    assert bad.name.startswith("WITHDRAWN")
    assert bad.asset_names() == [BAD_ASSET], "the asset is retained as evidence"
    assert result["release"]["assets_retained"] == [BAD_ASSET]

    # The replacement release is untouched.
    assert replacement_release.draft is False
    assert replacement_release.asset_names() == [NEW_ASSET]

    # Pages no longer advertises or serves the bad version.
    files = remote.pages_files()
    assert BAD_ASSET not in files
    assert NEW_ASSET in files
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.1",)
    assert files["README.md"] == published_pages["README.md"]


def test_withdraw_preserves_release_identity(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    before = next(r for r in remote.api.releases if r.tag_name == "chart-v0.4.0")
    original_id = before.id
    lifecycle.withdraw(
        session_factory(contract_overrides={"expected_bad_tag_target": remote.main_tip})
    )
    after = next(r for r in remote.api.releases if r.tag_name == "chart-v0.4.0")
    assert after.id == original_id
    assert after.tag_name == "chart-v0.4.0"


def test_withdraw_dry_run_changes_nothing(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    before_refs = remote.refs()
    before_files = remote.pages_files()
    result = lifecycle.withdraw(
        session_factory(
            dry_run=True, contract_overrides={"expected_bad_tag_target": remote.main_tip}
        )
    )
    assert result["applied"] is False
    assert result["plan"]["force_update_required"] is True
    assert remote.refs() == before_refs
    assert remote.pages_files() == before_files
    assert remote.api.releases[0].draft is False


def test_withdraw_refuses_an_unexpected_tag_target(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    with pytest.raises(RemoteConflict, match="refs/tags/chart-v0.4.0"):
        lifecycle.withdraw(
            session_factory(contract_overrides={"expected_bad_tag_target": "e" * 40})
        )
    assert "refs/tags/chart-v0.4.0" in remote.refs()
    assert remote.api.releases[0].draft is False
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.0",)


def test_withdraw_requires_force(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    session = session_factory(
        force=False, contract_overrides={"expected_bad_tag_target": remote.main_tip}
    )
    with pytest.raises(UsageError, match="re-run with --force"):
        lifecycle.withdraw(session)
    assert "refs/tags/chart-v0.4.0" in remote.refs()


def test_withdraw_is_idempotent(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    overrides = {"expected_bad_tag_target": remote.main_tip}
    lifecycle.withdraw(session_factory(contract_overrides=overrides))
    tip = remote.refs()["refs/heads/gh-pages"]

    second = lifecycle.withdraw(session_factory(contract_overrides=overrides, expect_pages_tip=tip))
    assert second["applied"] is True
    assert second["pages"]["changed"] is False
    assert second["deleted_tag"] is None
    assert second["plan"]["is_noop"] is True
    assert remote.refs()["refs/heads/gh-pages"] == tip


def test_withdraw_does_not_over_report_on_a_second_run(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    overrides = {"expected_bad_tag_target": remote.main_tip}
    first = lifecycle.withdraw(session_factory(contract_overrides=overrides))
    assert "release:chart-v0.4.0->draft" in first["withdrawn_objects"]
    tip = remote.refs()["refs/heads/gh-pages"]

    second = lifecycle.withdraw(session_factory(contract_overrides=overrides, expect_pages_tip=tip))
    assert second["withdrawn_objects"] == [], "an idempotent re-run withdraws nothing"
    assert second["release"]["changed"] is False


def test_withdraw_with_no_release_present(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    remote.api.releases.clear()
    remote.api.delete_ref("refs/tags/chart-v0.4.0")
    result = lifecycle.withdraw(session_factory())
    assert result["release"] is None
    assert result["deleted_tag"] is None
    assert advertised_versions(live_index(remote), "ledger-api") == ()


def test_quarantine_is_idempotent(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    session = session_factory()
    release = session.client.get_release_by_tag("chart-v0.4.0")
    assert release is not None
    first = lifecycle.quarantine_release(session, release)
    second = lifecycle.quarantine_release(session, first)
    assert second.name == first.name
    assert second.draft is True


# --------------------------------------------------------------------- audit


def test_audit_reports_the_incident_state(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    result = lifecycle.audit(session_factory())
    codes = {item.code for item in result.findings}
    assert codes == {
        "withdrawn-tag-present",
        "withdrawn-release-published",
        "withdrawn-version-advertised",
        "withdrawn-archive-published",
    }
    assert not result.healthy
    assert result.errors


def test_audit_is_clean_after_a_full_recovery(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    lifecycle.publish(session_factory())
    tip = remote.refs()["refs/heads/gh-pages"]
    overrides = {"expected_bad_tag_target": remote.main_tip}
    lifecycle.withdraw(session_factory(expect_pages_tip=tip, contract_overrides=overrides))
    result = lifecycle.audit(session_factory(contract_overrides=overrides))
    assert result.healthy, [item.to_dict() for item in result.findings]
    assert lifecycle.audit_command(session_factory(contract_overrides=overrides))["healthy"] is True


def test_audit_command_raises_on_drift(
    session_factory: SessionFactory, published_pages: dict[str, bytes]
) -> None:
    with pytest.raises(AuditDrift, match="withdrawn-tag-present"):
        lifecycle.audit_command(session_factory(), strict=True)


def test_audit_detects_a_missing_pages_branch(session_factory: SessionFactory) -> None:
    result = lifecycle.audit(session_factory())
    assert [item.code for item in result.findings] == ["pages-branch-missing"]


def test_audit_detects_a_missing_index(session_factory: SessionFactory, remote: Remote) -> None:
    remote.seed_pages({"README.md": b"x"})
    result = lifecycle.audit(session_factory())
    assert "index-missing" in {item.code for item in result.findings}


def test_audit_detects_an_unparseable_index(
    session_factory: SessionFactory, remote: Remote
) -> None:
    remote.seed_pages({INDEX: b"entries: [unclosed\n"})
    assert "index-unparseable" in {
        item.code for item in lifecycle.audit(session_factory()).findings
    }


def test_audit_detects_a_non_canonical_index(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    files = dict(published_pages)
    files[INDEX] = (
        b"apiVersion: v1\nentries:\n  ledger-api: []\ngenerated: '2026-01-01T00:00:00Z'\n"
    )
    remote.seed_pages(files)
    assert "index-not-canonical" in {
        item.code for item in lifecycle.audit(session_factory()).findings
    }


def test_audit_detects_an_entry_without_an_archive(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    files = dict(published_pages)
    del files[BAD_ASSET]
    remote.seed_pages(files)
    assert "index-entry-without-archive" in {
        item.code for item in lifecycle.audit(session_factory()).findings
    }


def test_audit_detects_an_orphan_archive(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    files = dict(published_pages)
    files["ledger-api-0.3.0.tgz"] = b"orphan bytes"
    remote.seed_pages(files)
    findings = {item.code: item for item in lifecycle.audit(session_factory()).findings}
    assert findings["orphan-archive"].severity == "warning"


def test_audit_detects_a_digest_mismatch(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    files = dict(published_pages)
    files[BAD_ASSET] = files[BAD_ASSET] + b"tampered"
    remote.seed_pages(files)
    assert "digest-mismatch" in {item.code for item in lifecycle.audit(session_factory()).findings}


def test_audit_flags_a_replacement_advertised_without_a_release(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    session = session_factory()
    artifact = lifecycle.build_candidate(session, "0.4.1")
    files = dict(published_pages)
    index = parse_index(files[INDEX].decode("utf-8"))
    from chartpub.index import add_artifact

    add_artifact(index, artifact, PAGES_URL, created="2026-01-01T00:00:00Z")
    files[INDEX] = dump_index(index).encode("utf-8")
    files[NEW_ASSET] = artifact.path.read_bytes()
    remote.seed_pages(files)

    codes = {item.code for item in lifecycle.audit(session_factory()).findings}
    assert "replacement-release-missing" in codes
    assert "replacement-tag-missing" in codes


def test_audit_flags_a_replacement_advertised_from_a_draft(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    session = session_factory()
    artifact = lifecycle.build_candidate(session, "0.4.1")
    files = dict(published_pages)
    index = parse_index(files[INDEX].decode("utf-8"))
    from chartpub.index import add_artifact

    add_artifact(index, artifact, PAGES_URL, created="2026-01-01T00:00:00Z")
    files[INDEX] = dump_index(index).encode("utf-8")
    files[NEW_ASSET] = artifact.path.read_bytes()
    remote.seed_pages(files)
    remote.api.add_release(tag_name="chart-v0.4.1", name="draft", draft=True)

    assert "replacement-release-draft" in {
        item.code for item in lifecycle.audit(session_factory()).findings
    }


# -------------------------------------------------------------------- repair


def test_repair_completes_a_half_finished_withdrawal(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    """Pages was cleaned but the tag and release were left behind."""
    lifecycle.publish(session_factory())
    tip = remote.refs()["refs/heads/gh-pages"]
    files = remote.pages_files()
    del files[BAD_ASSET]
    index = parse_index(files[INDEX].decode("utf-8"))
    from chartpub.index import remove_version

    remove_version(index, "ledger-api", "0.4.0")
    files[INDEX] = dump_index(index).encode("utf-8")
    partial_tip = remote.seed_pages(files)
    assert partial_tip != tip

    overrides = {"expected_bad_tag_target": remote.main_tip}
    result = lifecycle.repair(session_factory(contract_overrides=overrides))

    assert result["applied"] is True
    assert result["changed"] is True
    assert "refs/tags/chart-v0.4.0" not in remote.refs()
    bad = next(r for r in remote.api.releases if r.tag_name == "chart-v0.4.0")
    assert bad.draft is True and bad.name.startswith("WITHDRAWN")
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.1",)


def test_repair_rebuilds_a_deleted_pages_branch_from_release_assets(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    lifecycle.publish(session_factory())
    remote.api.delete_ref("refs/heads/gh-pages")
    assert "refs/heads/gh-pages" not in remote.refs()

    overrides = {"expected_bad_tag_target": remote.main_tip}
    result = lifecycle.repair(session_factory(contract_overrides=overrides))

    assert result["applied"] is True
    assert result["recovered_archives"] == [NEW_ASSET], "the bad version is never restored"
    files = remote.pages_files()
    assert set(files) == {INDEX, NEW_ASSET}
    index = live_index(remote)
    assert advertised_versions(index, "ledger-api") == ("0.4.1",)
    assert digests(index, "ledger-api")["0.4.1"] == sha256_bytes(files[NEW_ASSET])
    assert "refs/tags/chart-v0.4.0" not in remote.refs()


def test_repair_is_idempotent(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    lifecycle.publish(session_factory())
    overrides = {"expected_bad_tag_target": remote.main_tip}
    lifecycle.repair(session_factory(contract_overrides=overrides))
    tip = remote.refs()["refs/heads/gh-pages"]
    files = remote.pages_files()

    second = lifecycle.repair(session_factory(contract_overrides=overrides))
    assert second["changed"] is False
    assert second.get("idempotent_noop") is True
    assert remote.refs()["refs/heads/gh-pages"] == tip
    assert remote.pages_files() == files


def test_repair_dry_run_changes_nothing(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    before_refs = remote.refs()
    before_files = remote.pages_files()
    result = lifecycle.repair(session_factory(dry_run=True))
    assert result["applied"] is False
    assert result["plan"]["command"] == "repair"
    assert remote.refs() == before_refs
    assert remote.pages_files() == before_files


def test_repair_drops_an_index_entry_with_no_retrievable_archive(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    files = dict(published_pages)
    index = parse_index(files[INDEX].decode("utf-8"))
    entries = index["entries"]
    entries["ghost"] = [
        {
            "apiVersion": "v2",
            "name": "ghost",
            "version": "9.9.9",
            "digest": "0" * 64,
            "created": "2026-01-01T00:00:00Z",
            "urls": [f"{PAGES_URL}/ghost-9.9.9.tgz"],
        }
    ]
    files[INDEX] = dump_index(index).encode("utf-8")
    remote.seed_pages(files)

    lifecycle.repair(
        session_factory(contract_overrides={"expected_bad_tag_target": remote.main_tip})
    )
    assert advertised_versions(live_index(remote), "ghost") == ()


def test_repair_discards_an_unreadable_orphan_archive(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    files = dict(published_pages)
    files["ledger-api-0.3.0.tgz"] = b"not a tarball at all"
    remote.seed_pages(files)
    lifecycle.repair(
        session_factory(contract_overrides={"expected_bad_tag_target": remote.main_tip})
    )
    published = remote.pages_files()
    assert "ledger-api-0.3.0.tgz" not in published
    assert advertised_versions(live_index(remote), "ledger-api") == ()


def test_repair_preserves_an_unrelated_valid_archive_and_reports_what_it_cannot_fix(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    """An archive with no release cannot be made consistent by repair alone."""
    session = session_factory()
    other = lifecycle.build_candidate(session, "0.4.1")
    files = dict(published_pages)
    files[NEW_ASSET] = other.path.read_bytes()
    remote.seed_pages(files)

    with pytest.raises(AuditDrift, match="replacement-release-missing"):
        lifecycle.repair(
            session_factory(contract_overrides={"expected_bad_tag_target": remote.main_tip})
        )

    # The archive it could keep was kept, and the bad version is still gone.
    index = live_index(remote)
    assert advertised_versions(index, "ledger-api") == ("0.4.1",)
    assert digests(index, "ledger-api")["0.4.1"] == sha256_bytes(other.path.read_bytes())
    assert BAD_ASSET not in remote.pages_files()


def test_repair_requires_force_for_destructive_reconciliation(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    overrides = {"expected_bad_tag_target": remote.main_tip}
    with pytest.raises(UsageError, match="re-run with --force"):
        lifecycle.repair(session_factory(force=False, contract_overrides=overrides))
    assert "refs/tags/chart-v0.4.0" in remote.refs()
    assert remote.api.releases[0].draft is False
    assert advertised_versions(live_index(remote), "ledger-api") == ("0.4.0",)


def test_repair_passes_unrelated_pages_files_through(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    """A landing page, a 404, docs and a provenance sidecar must all survive."""
    lifecycle.publish(session_factory())
    files = remote.pages_files()
    files["index.html"] = b"<h1>charts</h1>"
    files["404.html"] = b"missing"
    files["docs/usage.md"] = b"# usage"
    files[f"{NEW_ASSET}.prov"] = b"-----BEGIN PGP SIGNED MESSAGE-----"
    files[f"{BAD_ASSET}.prov"] = b"provenance for the withdrawn version"
    remote.seed_pages(files)

    result = lifecycle.repair(
        session_factory(contract_overrides={"expected_bad_tag_target": remote.main_tip})
    )
    assert result["unresolved"] == []
    assert result["dropped_files"] == [BAD_ASSET, f"{BAD_ASSET}.prov"]

    published = remote.pages_files()
    assert published["index.html"] == b"<h1>charts</h1>"
    assert published["404.html"] == b"missing"
    assert published["docs/usage.md"] == b"# usage"
    assert f"{NEW_ASSET}.prov" in published, "the sidecar of a kept archive is kept"
    assert f"{BAD_ASSET}.prov" not in published, "the sidecar of the withdrawn archive goes too"
    assert BAD_ASSET not in published


def test_repair_publishes_an_advertised_draft_release(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    lifecycle.publish(session_factory())
    release = remote.api.releases[-1]
    # Simulate a publication that was rolled back to a draft after the index moved.
    release.draft = True
    remote.api.delete_ref("refs/tags/chart-v0.4.1")

    result = lifecycle.repair(
        session_factory(contract_overrides={"expected_bad_tag_target": remote.main_tip})
    )
    assert result["published_release"] == {
        "id": release.id,
        "tag": "chart-v0.4.1",
        "draft": False,
    }
    assert remote.refs()["refs/tags/chart-v0.4.1"] == remote.main_tip
    assert result["unresolved"] == []


def test_repair_restores_tampered_bytes_from_the_release_asset(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    """The immutable asset wins; the index digest is not rewritten to bless Pages."""
    lifecycle.publish(session_factory())
    authoritative = remote.api.releases[-1].assets[0].payload
    files = remote.pages_files()
    files[NEW_ASSET] = authoritative + b"tampered"
    remote.seed_pages(files)

    result = lifecycle.repair(
        session_factory(contract_overrides={"expected_bad_tag_target": remote.main_tip})
    )
    assert result["recovered_archives"] == [NEW_ASSET]
    assert remote.pages_files()[NEW_ASSET] == authoritative
    assert digests(live_index(remote), "ledger-api")["0.4.1"] == sha256_bytes(authoritative)


# ---------------------------------------------------------------- live check


def test_verify_live_summarises_what_a_client_sees(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    lifecycle.publish(session_factory())
    payload = remote.pages_files()[INDEX]
    session = session_factory()
    summary = lifecycle.verify_live(session, fetch=lambda _url: payload)
    assert summary["advertised_versions"] == ["0.4.1", "0.4.0"]
    assert summary["latest"] == "0.4.1"
    assert summary["withdrawn_version_advertised"] is True
    assert summary["replacement_version_advertised"] is True
    assert summary["pages_url"] == PAGES_URL


def test_fetch_live_index_uses_the_pages_url(session_factory: SessionFactory) -> None:
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return b"apiVersion: v1\nentries: {}\n"

    lifecycle.fetch_live_index(session_factory(), fetch=fetch)
    assert seen == [f"{PAGES_URL}/index.yaml"]


def test_read_pages_files_skips_git_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("x", encoding="utf-8")
    (root / "index.yaml").write_bytes(b"real")
    (root / "link").symlink_to(root / "index.yaml")
    assert lifecycle.read_pages_files(root) == {"index.yaml": b"real"}


def test_collect_snapshot_without_a_pages_branch(
    session_factory: SessionFactory, remote: Remote
) -> None:
    snapshot = lifecycle.collect_snapshot(session_factory())
    assert snapshot.pages_tip is None
    assert snapshot.index is None
    assert snapshot.pages_files == ()
    assert snapshot.main_tip == remote.main_tip
    assert snapshot.to_dict()["main_tip"] == remote.main_tip


def test_snapshot_to_dict_includes_releases(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    snapshot = lifecycle.collect_snapshot(session_factory())
    payload = snapshot.to_dict()
    assert payload["releases"][0]["tag_name"] == "chart-v0.4.0"
    assert payload["releases"][0]["assets"][0]["name"] == BAD_ASSET
    assert BAD_ASSET in payload["pages_files"]


def test_expected_pages_tip_precedence(
    session_factory: SessionFactory, remote: Remote, published_pages: dict[str, bytes]
) -> None:
    session = session_factory()
    snapshot = lifecycle.collect_snapshot(session)
    assert lifecycle.resolve_expected_pages_tip(session, snapshot) == remote.pages_tip

    lifecycle.publish(session_factory())
    later = session_factory()
    # A journal written by chartpub itself takes precedence over the contract.
    assert (
        lifecycle.resolve_expected_pages_tip(later, snapshot)
        == remote.refs()["refs/heads/gh-pages"]
    )
    explicit = session_factory(expect_pages_tip="7" * 40)
    assert lifecycle.resolve_expected_pages_tip(explicit, snapshot) == "7" * 40
