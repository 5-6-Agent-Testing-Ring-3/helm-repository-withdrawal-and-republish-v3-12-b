"""Plan, publish, withdraw, audit and repair.

State machine (see ``docs/OPERATIONS.md`` for the full diagram)::

    absent ──publish──▶ published ──withdraw──▶ quarantined
       ▲                    │                       │
       └────────repair──────┴───────repair──────────┘

Every remote mutation is preceded by a compare-and-swap check against the tip
or object identity that was inspected while planning. A mismatch raises
:class:`~chartpub.errors.RemoteConflict` *before* the write, so concurrent work
is never overwritten.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from chartpub import archive as archive_mod
from chartpub import gitops
from chartpub import index as index_mod
from chartpub.errors import AuditDrift, RemoteConflict, UsageError, ValidationError
from chartpub.github import GitHubClient
from chartpub.models import (
    Artifact,
    Plan,
    PlanStep,
    Precondition,
    PublicationContract,
    ReleaseState,
    RemoteSnapshot,
    Scope,
)
from chartpub.security import Redactor
from chartpub.state import Journal, TransactionState
from chartpub.transaction import Rollback
from chartpub.validate import (
    CommandRunner,
    ValidationReport,
    default_runner,
    discover_values_fixtures,
    validate_candidate,
)

INDEX_FILE = "index.yaml"
QUARANTINE_PREFIX = "WITHDRAWN"

#: Files the Pages snapshot keeps even though they are not chart artifacts.
PRESERVED_PAGES_FILES = ("README.md", ".nojekyll", "CNAME")


# --------------------------------------------------------------------- session


@dataclass
class Session:
    """Everything a lifecycle command needs, wired for injection in tests."""

    contract: PublicationContract
    client: GitHubClient
    repo: gitops.GitRepository
    remote_url: str
    git_env: Mapping[str, str] | None
    redactor: Redactor
    journal: Journal
    chart_dir: Path
    values_files: tuple[Path, ...] = ()
    helm_runner: CommandRunner = default_runner
    dry_run: bool = False
    force: bool = False
    created: str = ""
    expect_pages_tip: str | None = None
    server_side_install: bool | None = None
    work_dir: Path | None = None

    def __post_init__(self) -> None:
        if not self.created:
            self.created = index_mod.format_timestamp(datetime.now(UTC))

    @property
    def pages_branch(self) -> str:
        return self.contract.pages_branch

    def scratch(self) -> Path:
        if self.work_dir is None:
            self.work_dir = Path(tempfile.mkdtemp(prefix="chartpub-build-"))
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return self.work_dir


# -------------------------------------------------------------------- snapshot


def read_pages_files(root: Path) -> dict[str, bytes]:
    """Every file in a Pages checkout, keyed by repo-relative POSIX path."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/") or relative == ".git":
            continue
        files[relative] = path.read_bytes()
    return files


def collect_snapshot(session: Session) -> RemoteSnapshot:
    """Read the remote state the plan will be checked against."""
    contract = session.contract
    client = session.client
    pages_tip = client.get_ref(f"heads/{contract.pages_branch}")
    index: dict[str, Any] | None = None
    pages_files: tuple[str, ...] = ()
    if pages_tip is not None:
        raw = client.get_file(contract.pages_branch, INDEX_FILE)
        if raw is not None:
            try:
                index = index_mod.parse_index(raw.decode("utf-8"))
            except ValidationError:
                # A corrupt published index must not stop `audit`/`repair` from
                # running: diagnosing it is exactly what they are for.
                index = None
        pages_files = tuple(
            sorted(
                str(item["path"])
                for item in client.get_tree(pages_tip)
                if item.get("type") == "blob" and item.get("path")
            )
        )
    return RemoteSnapshot(
        repository=contract.repository,
        main_tip=client.get_ref(f"heads/{contract.source_branch}"),
        pages_tip=pages_tip,
        tags=client.list_tags(),
        releases=tuple(client.list_releases()),
        index=index,
        pages_files=pages_files,
    )


def resolve_expected_pages_tip(session: Session, snapshot: RemoteSnapshot) -> str | None:
    """The tip the Pages lease will be taken against.

    Precedence: an explicit operator value, then the tip chartpub itself last
    pushed (recorded in the journal), then the contract. Anything else is a
    conflict the operator must acknowledge by passing ``--expect-pages-tip``.
    """
    if session.expect_pages_tip is not None:
        return session.expect_pages_tip
    recorded = session.journal.load()
    if recorded is not None and recorded.pages_new_tip:
        return recorded.pages_new_tip
    return session.contract.expected_pages_tip


# ------------------------------------------------------------------ packaging


def build_candidate(session: Session, version: str) -> Artifact:
    output = session.scratch() / "package"
    return archive_mod.package_chart(session.chart_dir, output, version)


def validate(session: Session, artifact: Artifact) -> ValidationReport:
    values = session.values_files or discover_values_fixtures(Path("tests/fixtures"))
    return validate_candidate(
        artifact,
        values_files=values,
        runner=session.helm_runner,
        server_side=session.server_side_install,
    )


# ----------------------------------------------------------------- plan build


def _step(
    scope: Scope,
    action: str,
    target: str,
    detail: str,
    *,
    destructive: bool = False,
    noop: bool = False,
) -> PlanStep:
    return PlanStep(
        scope=scope, action=action, target=target, detail=detail, destructive=destructive, noop=noop
    )


def _precondition(subject: str, expected: str | None, actual: str | None) -> Precondition:
    return Precondition(
        subject=subject, expected=expected, actual=actual, satisfied=expected == actual
    )


def plan_publish(
    session: Session,
    snapshot: RemoteSnapshot,
    artifact: Artifact | None,
) -> Plan:
    contract = session.contract
    version = contract.replacement_version
    tag = contract.replacement_tag
    asset = contract.asset_name(version)
    steps: list[PlanStep] = []
    notes: list[str] = []

    digest = artifact.sha256 if artifact is not None else ""
    steps.append(
        _step(
            "local",
            "package",
            f"{asset}",
            f"deterministic package of {session.chart_dir} at {version}"
            + (f" (sha256={digest})" if digest else ""),
        )
    )
    steps.append(
        _step(
            "local",
            "validate",
            f"{contract.chart} {version}",
            "lint, render fixtures, test install",
        )
    )

    release = snapshot.release_for_tag(tag)
    existing_asset = release.asset(asset) if release is not None else None
    if release is None:
        steps.append(_step("release", "create", tag, f"draft release '{contract.chart} {version}'"))
        steps.append(_step("release", "upload-asset", asset, "immutable chart archive"))
        steps.append(_step("release", "verify-asset", asset, "download back and compare digest"))
        steps.append(_step("release", "publish", tag, "flip draft to published"))
    else:
        steps.append(
            _step(
                "release",
                "reuse",
                tag,
                f"release id={release.id} already exists",
                noop=True,
            )
        )
        if existing_asset is None:
            steps.append(_step("release", "upload-asset", asset, "asset missing from release"))
            steps.append(
                _step("release", "verify-asset", asset, "download back and compare digest")
            )
        else:
            steps.append(
                _step(
                    "release",
                    "verify-asset",
                    asset,
                    f"asset id={existing_asset.id} already uploaded; bytes re-verified",
                    noop=True,
                )
            )
        if release.draft:
            steps.append(_step("release", "publish", tag, "flip draft to published"))
        else:
            steps.append(_step("release", "publish", tag, "already published", noop=True))

    tag_target = snapshot.tags.get(tag)
    if tag_target is None:
        steps.append(
            _step("tag", "create", f"refs/tags/{tag}", "created by publishing the release")
        )
    else:
        steps.append(
            _step("tag", "keep", f"refs/tags/{tag}", f"already at {tag_target}", noop=True)
        )

    current_index = snapshot.index or index_mod.empty_index()
    entry = index_mod.find_entry(current_index, contract.chart, version)
    pages_noop = False
    if entry is None:
        steps.append(
            _step("pages", "add-index-entry", f"{contract.chart} {version}", "new index entry")
        )
    elif digest and entry.get("digest") != digest:
        steps.append(
            _step(
                "pages",
                "replace-index-entry",
                f"{contract.chart} {version}",
                f"digest {entry.get('digest')} -> {digest}",
                destructive=True,
            )
        )
        notes.append(
            "the index already advertises this version with different bytes; "
            "--force is required to replace it"
        )
    else:
        pages_noop = True
        steps.append(
            _step(
                "pages",
                "keep-index-entry",
                f"{contract.chart} {version}",
                "identical entry already published",
                noop=True,
            )
        )
    if asset in snapshot.pages_files and pages_noop:
        steps.append(
            _step("pages", "keep-archive", asset, "identical archive already published", noop=True)
        )
    else:
        steps.append(_step("pages", "write-archive", asset, "publish archive next to the index"))

    expected_tip = resolve_expected_pages_tip(session, snapshot)
    preserved = sorted(
        set(index_mod.advertised_versions(current_index, contract.chart)) - {version}
    )
    if preserved:
        notes.append(f"preserved existing index version(s): {', '.join(preserved)}")

    force_required = any(step.destructive and not step.noop for step in steps)
    if existing_asset is not None and artifact is not None and existing_asset.size != artifact.size:
        notes.append(
            f"release asset {asset} already exists with {existing_asset.size} bytes but the "
            f"candidate is {artifact.size}; release assets are immutable, so publish will stop"
        )
    preconditions = [
        _precondition(f"refs/heads/{contract.pages_branch}", expected_tip, snapshot.pages_tip),
    ]
    return Plan(
        command="publish",
        repository=contract.repository,
        chart=contract.chart,
        version=version,
        steps=tuple(steps),
        preconditions=tuple(preconditions),
        force_update_required=force_required,
        dry_run=session.dry_run,
        notes=tuple(notes),
    )


def plan_withdraw(session: Session, snapshot: RemoteSnapshot) -> Plan:
    contract = session.contract
    version = contract.bad_version
    tag = contract.bad_tag
    asset = contract.asset_name(version)
    steps: list[PlanStep] = []
    notes: list[str] = []

    current_index = snapshot.index or index_mod.empty_index()
    entry = index_mod.find_entry(current_index, contract.chart, version)
    if entry is not None:
        steps.append(
            _step(
                "pages",
                "remove-index-entry",
                f"{contract.chart} {version}",
                "stop advertising the withdrawn version",
                destructive=True,
            )
        )
    else:
        steps.append(
            _step(
                "pages",
                "remove-index-entry",
                f"{contract.chart} {version}",
                "already absent",
                noop=True,
            )
        )
    if asset in snapshot.pages_files:
        steps.append(
            _step(
                "pages", "delete-archive", asset, "remove the published archive", destructive=True
            )
        )
    else:
        steps.append(_step("pages", "delete-archive", asset, "already absent", noop=True))

    release = snapshot.release_for_tag(tag)
    if release is None:
        steps.append(
            _step("release", "quarantine", tag, "no release found for this tag", noop=True)
        )
        notes.append(f"no GitHub release exists for {tag}; nothing to quarantine")
    elif _is_quarantined(release):
        steps.append(
            _step(
                "release",
                "quarantine",
                tag,
                f"release id={release.id} already a draft quarantine record",
                noop=True,
            )
        )
    else:
        steps.append(
            _step(
                "release",
                "quarantine",
                tag,
                f"convert release id={release.id} to a draft; identity (id, tag_name) preserved, "
                "asset kept as recovery evidence",
                destructive=True,
            )
        )

    tag_target = snapshot.tags.get(tag)
    if tag_target is None:
        steps.append(_step("tag", "delete", f"refs/tags/{tag}", "already absent", noop=True))
    else:
        steps.append(
            _step(
                "tag",
                "delete",
                f"refs/tags/{tag}",
                f"delete exactly this ref at {tag_target}",
                destructive=True,
            )
        )

    preserved = sorted(
        set(index_mod.advertised_versions(current_index, contract.chart)) - {version}
    )
    if preserved:
        notes.append(f"preserved index version(s): {', '.join(preserved)}")

    expected_tip = resolve_expected_pages_tip(session, snapshot)
    preconditions = [
        _precondition(f"refs/heads/{contract.pages_branch}", expected_tip, snapshot.pages_tip),
        _precondition(
            f"refs/tags/{tag}",
            contract.expected_bad_tag_target if tag_target is not None else None,
            tag_target,
        ),
    ]
    return Plan(
        command="withdraw",
        repository=contract.repository,
        chart=contract.chart,
        version=version,
        steps=tuple(steps),
        preconditions=tuple(preconditions),
        force_update_required=any(step.destructive and not step.noop for step in steps),
        dry_run=session.dry_run,
        notes=tuple(notes),
    )


# ----------------------------------------------------------------------- audit


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    subject: str
    detail: str
    remedy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "subject": self.subject,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass
class AuditResult:
    snapshot: RemoteSnapshot
    findings: list[Finding] = field(default_factory=list)
    live_index: dict[str, Any] | None = None
    archive_digests: dict[str, str] = field(default_factory=dict)

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity == "error")

    @property
    def healthy(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "findings": [item.to_dict() for item in self.findings],
            "remote": self.snapshot.to_dict(),
            "archive_digests": dict(sorted(self.archive_digests.items())),
        }


def _pages_payload(session: Session) -> tuple[dict[str, bytes], str | None]:
    """Read the whole Pages branch, so digests are checked against real bytes."""
    with gitops.pages_worktree(
        session.repo,
        session.remote_url,
        session.pages_branch,
        env=session.git_env,
        redactor=session.redactor,
    ) as (clone, tip):
        if tip is None:
            return {}, None
        return read_pages_files(clone.path), tip


def audit(session: Session, *, snapshot: RemoteSnapshot | None = None) -> AuditResult:
    """Inspect the configured public repository and report every inconsistency."""
    contract = session.contract
    snap = snapshot or collect_snapshot(session)
    result = AuditResult(snapshot=snap)
    add = result.findings.append

    if snap.pages_tip is None:
        add(
            Finding(
                "pages-branch-missing",
                "error",
                f"refs/heads/{contract.pages_branch}",
                "the Pages branch does not exist, so nothing is discoverable",
                "repair rebuilds the branch from the release assets",
            )
        )
        return result

    files, _ = _pages_payload(session)
    digests = {
        name: archive_mod.sha256_bytes(payload)
        for name, payload in files.items()
        if name.endswith(".tgz")
    }
    result.archive_digests = digests

    if INDEX_FILE not in files:
        add(
            Finding(
                "index-missing",
                "error",
                INDEX_FILE,
                "the Pages branch has no index.yaml",
                "repair regenerates it from the archives and release assets",
            )
        )
        live_index = index_mod.empty_index()
    else:
        try:
            live_index = index_mod.parse_index(files[INDEX_FILE].decode("utf-8"))
        except ValidationError as exc:
            add(
                Finding(
                    "index-unparseable",
                    "error",
                    INDEX_FILE,
                    str(exc),
                    "repair regenerates a canonical index",
                )
            )
            live_index = index_mod.empty_index()
        else:
            canonical = index_mod.dump_index(dict(live_index))
            if files[INDEX_FILE].decode("utf-8") != canonical:
                add(
                    Finding(
                        "index-not-canonical",
                        "warning",
                        INDEX_FILE,
                        "index.yaml is not in the deterministic generated form",
                        "repair rewrites it so future publications diff cleanly",
                    )
                )
    result.live_index = live_index

    referenced = set(index_mod.referenced_files(live_index))
    for name in sorted(referenced - set(files)):
        add(
            Finding(
                "index-entry-without-archive",
                "error",
                name,
                "the index advertises an archive that is not published",
                "repair restores it from the release asset or drops the entry",
            )
        )
    for name in sorted(set(digests) - referenced):
        add(
            Finding(
                "orphan-archive",
                "warning",
                name,
                "an archive is published but not advertised by the index",
                "repair removes the orphan or re-adds its entry",
            )
        )

    for chart in index_mod.chart_names(live_index):
        for version, digest in sorted(index_mod.digests(live_index, chart).items()):
            asset = f"{chart}-{version}.tgz"
            actual = digests.get(asset)
            if actual is not None and actual != digest:
                add(
                    Finding(
                        "digest-mismatch",
                        "error",
                        f"{chart} {version}",
                        f"index digest {digest} but the published archive hashes to {actual}",
                        "repair rewrites the index digest to the published bytes",
                    )
                )

    # Contract-specific expectations for the withdrawn and replacement versions.
    bad_tag_target = snap.tags.get(contract.bad_tag)
    if bad_tag_target is not None:
        add(
            Finding(
                "withdrawn-tag-present",
                "error",
                f"refs/tags/{contract.bad_tag}",
                f"the withdrawn version's tag still exists at {bad_tag_target}",
                "repair deletes exactly this tag",
            )
        )
    bad_release = snap.release_for_tag(contract.bad_tag)
    if bad_release is not None and not bad_release.draft:
        add(
            Finding(
                "withdrawn-release-published",
                "error",
                f"release {contract.bad_tag}",
                f"release id={bad_release.id} for the withdrawn version is still published",
                "repair converts it to a draft quarantine record",
            )
        )
    if index_mod.find_entry(live_index, contract.chart, contract.bad_version) is not None:
        add(
            Finding(
                "withdrawn-version-advertised",
                "error",
                f"{contract.chart} {contract.bad_version}",
                "the public index still advertises the withdrawn version",
                "repair removes the entry and its archive",
            )
        )
    if contract.asset_name(contract.bad_version) in files:
        add(
            Finding(
                "withdrawn-archive-published",
                "error",
                contract.asset_name(contract.bad_version),
                "the withdrawn version's archive is still downloadable from Pages",
                "repair removes it from the Pages snapshot",
            )
        )

    replacement_entry = index_mod.find_entry(
        live_index, contract.chart, contract.replacement_version
    )
    replacement_release = snap.release_for_tag(contract.replacement_tag)
    if replacement_entry is not None:
        if replacement_release is None:
            add(
                Finding(
                    "replacement-release-missing",
                    "error",
                    f"release {contract.replacement_tag}",
                    "the replacement version is advertised but has no GitHub release",
                    "re-run `chartpub publish` to recreate the release and its asset",
                )
            )
        elif replacement_release.draft:
            add(
                Finding(
                    "replacement-release-draft",
                    "error",
                    f"release {contract.replacement_tag}",
                    f"release id={replacement_release.id} is advertised but still a draft",
                    "repair publishes the existing draft release",
                )
            )
        if contract.replacement_tag not in snap.tags:
            add(
                Finding(
                    "replacement-tag-missing",
                    "error",
                    f"refs/tags/{contract.replacement_tag}",
                    "the replacement version is advertised but its tag does not exist",
                    "publish the existing draft release, which recreates the tag",
                )
            )
    elif contract.replacement_tag in snap.tags:
        # A tag with nothing advertising it is what a rolled-back publication
        # would leave behind. It is public, so it must not go unreported.
        add(
            Finding(
                "orphan-replacement-tag",
                "error",
                f"refs/tags/{contract.replacement_tag}",
                f"refs/tags/{contract.replacement_tag} exists at "
                f"{snap.tags[contract.replacement_tag]} but the index does not advertise "
                f"{contract.chart} {contract.replacement_version}",
                "re-run `chartpub publish` to finish the publication, or delete the tag by hand "
                "after confirming no release references it",
            )
        )
    return result


def fetch_live_index(
    session: Session, *, fetch: Callable[[str], bytes] | None = None
) -> dict[str, Any]:
    """Fetch ``index.yaml`` from the public Pages URL."""
    url = f"{session.contract.pages_url.rstrip('/')}/{INDEX_FILE}"
    if fetch is None:
        import urllib.request

        def fetch(target: str) -> bytes:
            with urllib.request.urlopen(target, timeout=30) as response:  # noqa: S310 - https
                raw: bytes = response.read()
                return raw

    return index_mod.parse_index(fetch(url).decode("utf-8"))


# ------------------------------------------------------------------- mutation


def _require_preconditions(plan: Plan) -> None:
    unmet = plan.unsatisfied
    if unmet:
        detail = "; ".join(
            f"{item.subject}: expected {item.expected or '<absent>'}, "
            f"found {item.actual or '<absent>'}"
            for item in unmet
        )
        raise RemoteConflict(f"refusing to proceed, remote precondition not met ({detail})")


def _mutate_pages(
    session: Session,
    *,
    expected_tip: str | None,
    message: str,
    mutate: Callable[[dict[str, bytes]], dict[str, bytes]],
    allow_create: bool = False,
) -> tuple[str | None, bool]:
    """Rebuild and push the Pages snapshot under a lease. Returns (tip, changed)."""
    with gitops.pages_worktree(
        session.repo,
        session.remote_url,
        session.pages_branch,
        env=session.git_env,
        redactor=session.redactor,
    ) as (clone, tip):
        if tip != expected_tip:
            raise RemoteConflict(
                f"refs/heads/{session.pages_branch} is at {tip or '<absent>'} but "
                f"{expected_tip or '<absent>'} was expected; refusing to update it"
            )
        if tip is None and not allow_create:
            raise RemoteConflict(
                f"refs/heads/{session.pages_branch} does not exist; "
                "run `chartpub repair` to rebuild it"
            )
        current = read_pages_files(clone.path)
        desired = mutate(dict(current))
        if desired == current:
            return tip, False
        gitops.build_snapshot(clone.path, desired)
        index_text = desired.get(INDEX_FILE, b"").decode("utf-8", "replace")
        stamp = _index_generated(index_text) or session.created
        new_sha = gitops.commit_snapshot(clone, message, timestamp=_git_stamp(stamp))
        if new_sha is None:  # pragma: no cover - guarded by the equality check above
            return tip, False
        if tip is None:
            gitops.push_branch(
                clone,
                session.remote_url,
                local_ref="HEAD",
                branch=session.pages_branch,
                env=session.git_env,
            )
        else:
            gitops.push_with_lease(
                clone,
                session.remote_url,
                branch=session.pages_branch,
                local_ref="HEAD",
                expected_old=tip,
                env=session.git_env,
            )
        return new_sha, True


def _index_generated(index_text: str) -> str | None:
    try:
        parsed = index_mod.parse_index(index_text)
    except ValidationError:  # pragma: no cover - defensive
        return None
    value = parsed.get("generated")
    return str(value) if isinstance(value, str) else None


def _git_stamp(iso_z: str) -> str:
    """Convert ``...Z`` to the ``+00:00`` form Git accepts, deterministically."""
    return iso_z[:-1] + "+00:00" if iso_z.endswith("Z") else iso_z


def _upsert_index(
    session: Session, files: dict[str, bytes], artifact: Artifact, payload: bytes
) -> dict[str, bytes]:
    index = index_mod.parse_index(files.get(INDEX_FILE, b"").decode("utf-8") or "")
    index_mod.add_artifact(index, artifact, session.contract.pages_url, created=session.created)
    files[artifact.path.name] = payload
    files[INDEX_FILE] = index_mod.dump_index(index).encode("utf-8")
    return files


def publish(session: Session) -> dict[str, Any]:
    """Publish the replacement version as a recoverable transaction."""
    contract = session.contract
    version = contract.replacement_version
    tag = contract.replacement_tag
    asset_name = contract.asset_name(version)

    artifact = build_candidate(session, version)
    snapshot = collect_snapshot(session)
    # Pin the Pages lease target now: writing this command's journal entry would
    # otherwise hide the tip a preceding `withdraw` pushed.
    session.expect_pages_tip = resolve_expected_pages_tip(session, snapshot)
    plan = plan_publish(session, snapshot, artifact)

    report = validate(session, artifact)
    outcome: dict[str, Any] = {
        "command": "publish",
        "plan": plan.to_dict(),
        "validation": report.to_dict(),
        "artifact": {
            "name": asset_name,
            "sha256": artifact.sha256,
            "size": artifact.size,
        },
    }
    if session.dry_run:
        # Nothing above touched the remote: packaging and validation are local,
        # and the snapshot was read-only.
        outcome["applied"] = False
        report.raise_for_status()
        return outcome
    report.raise_for_status()

    if plan.force_update_required and not session.force:
        raise UsageError(
            "this publication would replace already-published bytes; "
            f"re-run with --force to accept ({', '.join(plan.destructive_targets)})"
        )
    _require_preconditions(plan)

    state = session.journal.resumable(
        command="publish", version=version, sha256=artifact.sha256
    ) or TransactionState(
        command="publish",
        repository=contract.repository,
        chart=contract.chart,
        version=version,
        tag=tag,
        asset_name=asset_name,
        sha256=artifact.sha256,
        size=artifact.size,
        index_created=session.created,
    )
    if state.index_created:
        session.created = state.index_created
    state = state.advanced("validated", sha256=artifact.sha256, size=artifact.size)
    session.journal.save(state)

    rollback = Rollback()
    client = session.client
    payload = artifact.path.read_bytes()

    try:
        release = snapshot.release_for_tag(tag)
        if release is None:
            release = client.create_release(
                tag=tag,
                name=f"{contract.chart} {version}",
                body=(
                    f"Deterministic publication of {contract.chart} {version}.\n\n"
                    f"sha256: {artifact.sha256}\n"
                ),
                target_commitish=contract.source_branch,
                draft=True,
            )
            created_release_id = release.id
            rollback.push(
                f"delete draft release id={created_release_id}",
                lambda: client.delete_release(created_release_id),
            )
            state = state.advanced("release-drafted", release_id=release.id, created_release=True)
        else:
            state = state.advanced("release-drafted", release_id=release.id)
        session.journal.save(state)

        # Step 1: the immutable asset must exist before anything advertises it.
        existing = release.asset(asset_name)
        if existing is not None and existing.size != artifact.size:
            raise RemoteConflict(
                f"release {tag} already has {asset_name} with {existing.size} bytes but the "
                f"candidate is {artifact.size} bytes; release assets are immutable, so this "
                "needs operator review"
            )
        if existing is None:
            uploaded = client.upload_asset(release.id, asset_name, artifact.path)
            asset_id = uploaded.id
            rollback.push(
                f"delete uploaded asset id={asset_id}", lambda: client.delete_asset(asset_id)
            )
        else:
            asset_id = existing.id
        state = state.advanced("asset-uploaded", asset_id=asset_id)
        session.journal.save(state)

        # Step 2: verify by downloading the bytes back, not by trusting the upload.
        downloaded = client.download_asset(asset_id, session.scratch() / "verify" / asset_name)
        actual = downloaded.read_bytes()
        if actual != payload:
            raise ValidationError(
                f"uploaded asset {asset_name} does not match the candidate bytes "
                f"({len(actual)} bytes downloaded, {len(payload)} expected)"
            )
        archive_mod.require_digest(downloaded, artifact.sha256, what="downloaded asset")
        state = state.advanced("asset-verified")
        session.journal.save(state)

        # Publishing the release is what creates the public tag, so the undo has
        # to put both back: re-draft the release and delete the tag we created.
        # Without this, a later Pages failure would leave an orphan tag behind.
        tag_existed_before = tag in snapshot.tags
        if release.draft:
            release = client.update_release(release.id, draft=False)
            published_tag_target = client.get_ref(f"tags/{tag}")
            created_tag = not tag_existed_before and published_tag_target is not None
            if created_tag and published_tag_target is not None:
                rollback.push(
                    f"re-draft release {tag} and delete the refs/tags/{tag} it created",
                    partial(
                        _undo_release_publication,
                        client,
                        release.id,
                        tag,
                        published_tag_target,
                    ),
                )
        else:
            published_tag_target = client.get_ref(f"tags/{tag}")
            created_tag = False
        state = state.advanced("release-published", created_tag=created_tag)
        session.journal.save(state)

        # Step 3: only now does the version become discoverable.
        expected_tip = resolve_expected_pages_tip(session, snapshot)
        new_tip, changed = _mutate_pages(
            session,
            expected_tip=expected_tip,
            message=f"publish: {contract.chart} {version}",
            mutate=lambda files: _upsert_index(session, files, artifact, payload),
        )
        state = state.advanced("pages-updated", pages_old_tip=expected_tip, pages_new_tip=new_tip)
        session.journal.save(state)
    except BaseException as exc:
        rollback.unwind_or_raise(exc)
        raise  # pragma: no cover - unwind_or_raise always raises

    rollback.clear()
    state = state.advanced("complete")
    session.journal.save(state)
    outcome["applied"] = True
    outcome["release"] = {
        "id": release.id,
        "tag": release.tag_name,
        "draft": release.draft,
        "html_url": release.html_url,
        "asset_id": state.asset_id,
    }
    outcome["pages"] = {
        "branch": session.pages_branch,
        "old_tip": state.pages_old_tip,
        "new_tip": state.pages_new_tip,
        "changed": changed,
    }
    outcome["idempotent_noop"] = not changed and plan.remote_is_noop
    return outcome


def _undo_release_publication(
    client: GitHubClient, release_id: int, tag: str, tag_target: str
) -> None:
    """Reverse "publish the release": re-draft it, then delete the tag it made.

    Ordered so the tag stops resolving to a listed release first, and the tag is
    only deleted while it still points where publishing put it.
    """
    client.update_release(release_id, draft=True)
    client.delete_tag(tag, expected=tag_target)


def _withdraw_pages(session: Session, files: dict[str, bytes]) -> dict[str, bytes]:
    contract = session.contract
    index = index_mod.parse_index(files.get(INDEX_FILE, b"").decode("utf-8") or "")
    index_mod.remove_version(index, contract.chart, contract.bad_version)
    files.pop(contract.asset_name(contract.bad_version), None)
    files[INDEX_FILE] = index_mod.dump_index(index).encode("utf-8")
    return files


def withdraw(session: Session) -> dict[str, Any]:
    """Withdraw exactly the contract's bad version.

    Order mirrors publication in reverse: discoverability is removed first, then
    the release is quarantined, then the public tag is deleted. Nothing outside
    that one version is touched.
    """
    contract = session.contract
    version = contract.bad_version
    tag = contract.bad_tag
    snapshot = collect_snapshot(session)
    session.expect_pages_tip = resolve_expected_pages_tip(session, snapshot)
    plan = plan_withdraw(session, snapshot)
    outcome: dict[str, Any] = {"command": "withdraw", "plan": plan.to_dict()}
    if session.dry_run:
        outcome["applied"] = False
        return outcome

    _require_preconditions(plan)
    if plan.force_update_required and not session.force:
        raise UsageError(
            "withdrawal removes published objects; re-run with --force to accept "
            f"({', '.join(plan.destructive_targets)})"
        )

    state = TransactionState(
        command="withdraw",
        repository=contract.repository,
        chart=contract.chart,
        version=version,
        tag=tag,
        asset_name=contract.asset_name(version),
        index_created=session.created,
    )
    session.journal.save(state)

    expected_tip = resolve_expected_pages_tip(session, snapshot)
    new_tip, changed = _mutate_pages(
        session,
        expected_tip=expected_tip,
        message=f"withdraw: {contract.chart} {version}",
        mutate=lambda files: _withdraw_pages(session, files),
    )
    state = state.advanced("pages-updated", pages_old_tip=expected_tip, pages_new_tip=new_tip)
    session.journal.save(state)

    release = snapshot.release_for_tag(tag)
    quarantined: dict[str, Any] | None = None
    quarantine_changed = False
    if release is not None:
        quarantine_changed = not _is_quarantined(release)
        quarantined_release = quarantine_release(session, release)
        quarantined = {
            "id": quarantined_release.id,
            "tag": quarantined_release.tag_name,
            "draft": quarantined_release.draft,
            "name": quarantined_release.name,
            "html_url": quarantined_release.html_url,
            "assets_retained": [asset.name for asset in quarantined_release.assets],
            "changed": quarantine_changed,
        }

    deleted_tag: str | None = None
    tag_target = snapshot.tags.get(tag)
    if tag_target is not None:
        session.client.delete_tag(tag, expected=tag_target)
        deleted_tag = f"refs/tags/{tag}@{tag_target}"
    state = state.advanced("complete")
    session.journal.save(state)

    outcome["applied"] = True
    outcome["pages"] = {
        "branch": session.pages_branch,
        "old_tip": expected_tip,
        "new_tip": new_tip,
        "changed": changed,
    }
    outcome["release"] = quarantined
    outcome["deleted_tag"] = deleted_tag
    outcome["withdrawn_objects"] = [
        item
        for item in (
            deleted_tag,
            f"pages:{contract.asset_name(version)}" if changed else None,
            f"index-entry:{contract.chart} {version}" if changed else None,
            f"release:{tag}->draft" if quarantine_changed else None,
        )
        if item
    ]
    return outcome


def _is_quarantined(release: ReleaseState) -> bool:
    """True when this release is already a draft quarantine record."""
    return release.draft and release.name.startswith(QUARANTINE_PREFIX)


def quarantine_release(session: Session, release: ReleaseState) -> ReleaseState:
    """Convert a published release into a draft quarantine record.

    The release id and ``tag_name`` are untouched, so the object keeps its
    identity and its asset remains available as recovery evidence; it simply
    stops being publicly listed.
    """
    if _is_quarantined(release):
        return release
    name = release.name or release.tag_name
    marked = name if name.startswith(QUARANTINE_PREFIX) else f"{QUARANTINE_PREFIX}: {name}"
    body = (
        f"This release was withdrawn by `chartpub withdraw` because "
        f"{session.contract.chart} {session.contract.bad_version} failed installation "
        "validation.\n\n"
        "It is retained as a draft so the original asset stays available as recovery "
        "evidence. The public Git tag and the Pages index entry have been removed, so "
        "the version is no longer discoverable.\n"
    )
    return session.client.update_release(release.id, draft=True, name=marked, body=body)


# ---------------------------------------------------------------------- repair


def _desired_pages_files(
    session: Session,
    files: Mapping[str, bytes],
    result: AuditResult,
    recovered: Mapping[str, bytes],
    dropped: list[str],
) -> dict[str, bytes]:
    """The Pages snapshot the contract says should exist.

    Built declaratively from the archives that are actually retrievable, which
    is what makes ``repair`` idempotent: running it twice produces the same tree.
    """
    contract = session.contract
    # Only the generated files are rebuilt. Everything else the branch carries -
    # a landing page, a 404, docs, anything an operator added - is passed through
    # untouched, because repair must not delete unrelated content.
    desired: dict[str, bytes] = {
        name: payload
        for name, payload in files.items()
        if name != INDEX_FILE and not _is_chart_artifact(name)
    }

    available: dict[str, bytes] = {
        name: payload for name, payload in files.items() if name.endswith(".tgz")
    }
    available.update(recovered)
    bad_asset = contract.asset_name(contract.bad_version)
    available.pop(bad_asset, None)
    discarded = sorted(
        name
        for name in files
        if _is_chart_artifact(name) and (name == bad_asset or name.startswith(f"{bad_asset}."))
    )

    index = index_mod.empty_index()
    previous = result.live_index or index_mod.empty_index()
    for name, payload in sorted(available.items()):
        try:
            metadata = _metadata_from_bytes(session, name, payload)
        except ValidationError:
            # An unreadable or unsafe archive is never re-advertised, and its
            # provenance sidecar goes with it.
            discarded.append(name)
            continue
        if metadata.name == contract.chart and metadata.version == contract.bad_version:
            discarded.append(name)
            continue
        artifact = Artifact(
            path=Path(name),
            name=metadata.name,
            version=metadata.version,
            sha256=archive_mod.sha256_bytes(payload),
            size=len(payload),
            metadata=metadata,
        )
        existing = index_mod.find_entry(previous, metadata.name, metadata.version)
        created = (
            str(existing["created"])
            if existing is not None and isinstance(existing.get("created"), str)
            else session.created
        )
        index_mod.add_artifact(index, artifact, contract.pages_url, created=created)
        desired[name] = payload
        # Keep a Helm provenance sidecar alongside the archive it signs.
        sidecar = f"{name}.prov"
        if sidecar in files:
            desired[sidecar] = files[sidecar]

    desired[INDEX_FILE] = index_mod.dump_index(index).encode("utf-8")
    dropped.extend(sorted(set(discarded)))
    return desired


def _is_chart_artifact(name: str) -> bool:
    """True for a chart archive or its Helm provenance sidecar."""
    return name.endswith((".tgz", ".tgz.prov"))


def _metadata_from_bytes(session: Session, name: str, payload: bytes) -> Any:
    temporary = session.scratch() / "inspect"
    temporary.mkdir(parents=True, exist_ok=True)
    path = temporary / name
    path.write_bytes(payload)
    return archive_mod.archive_metadata(path)


def _recover_archives(
    session: Session, result: AuditResult, files: Mapping[str, bytes]
) -> dict[str, bytes]:
    """Download release assets the Pages branch is missing or disagrees with.

    Release assets are immutable, so they are the authoritative bytes: when the
    published archive differs from its asset, the asset wins rather than the
    index digest being rewritten to bless whatever Pages happens to hold.
    """
    recovered: dict[str, bytes] = {}
    contract = session.contract
    suspect = {
        f"{item.subject.replace(' ', '-')}.tgz"
        for item in result.findings
        if item.code == "digest-mismatch"
    }
    for release in result.snapshot.releases:
        if release.draft or release.tag_name == contract.bad_tag:
            continue
        for asset in release.assets:
            if not asset.name.endswith(".tgz"):
                continue
            published = files.get(asset.name)
            if published is not None and len(published) == asset.size and asset.name not in suspect:
                continue
            destination = session.scratch() / "recovered" / asset.name
            session.client.download_asset(asset.id, destination)
            recovered[asset.name] = destination.read_bytes()
    return recovered


def plan_repair(session: Session, result: AuditResult, actions: Sequence[str]) -> Plan:
    contract = session.contract
    steps = [
        _step(
            "local", "audit", contract.repository, f"{len(result.findings)} finding(s)", noop=True
        )
    ]
    if not actions:
        steps.append(_step("pages", "reconcile", INDEX_FILE, "already consistent", noop=True))
    for action in actions:
        # Actions arrive as "<scope>:<target>"; the scope lives in the step field,
        # so it must not be repeated in the target.
        scope_name, _, target = action.partition(":")
        scope: Scope = "pages"
        destructive = False
        if scope_name == "tag":
            scope, destructive = "tag", True
        elif scope_name == "release":
            scope, destructive = "release", True
        steps.append(
            _step(
                scope,
                "reconcile",
                target or action,
                "bring the object back to the contract",
                destructive=destructive,
            )
        )
    # Unlike publish/withdraw, repair reconciles whatever it finds rather than a
    # tip the contract predicted, so its lease is taken against the tip observed
    # while auditing. The precondition records that, and the lease enforces it.
    observed = result.snapshot.pages_tip
    return Plan(
        command="repair",
        repository=contract.repository,
        chart=contract.chart,
        version=contract.replacement_version,
        steps=tuple(steps),
        preconditions=(
            (_precondition(f"refs/heads/{contract.pages_branch}", observed, observed),)
            if observed is not None
            else ()
        ),
        force_update_required=any(step.destructive for step in steps),
        dry_run=session.dry_run,
        notes=tuple(sorted({item.remedy for item in result.findings})),
    )


def repair(session: Session) -> dict[str, Any]:
    """Reconcile a partially applied lifecycle transition.

    Declarative and idempotent: it computes the snapshot and object states the
    contract calls for, applies the difference, then re-audits. If drift it
    cannot fix survives, it says so rather than reporting a convergence that did
    not happen.
    """
    contract = session.contract
    result = audit(session)
    bad_release = result.snapshot.release_for_tag(contract.bad_tag)
    replacement_release = result.snapshot.release_for_tag(contract.replacement_tag)
    advertised_replacement = (
        result.live_index is not None
        and index_mod.find_entry(result.live_index, contract.chart, contract.replacement_version)
        is not None
    )

    actions: list[str] = []
    if contract.bad_tag in result.snapshot.tags:
        actions.append(f"tag:refs/tags/{contract.bad_tag}")
    if bad_release is not None and not bad_release.draft:
        actions.append(f"release:{contract.bad_tag}")
    # An advertised version whose release is still a draft has no public tag and
    # no download; publishing the existing draft restores both.
    publish_draft = (
        advertised_replacement and replacement_release is not None and replacement_release.draft
    )
    if publish_draft:
        actions.append(f"release:{contract.replacement_tag}")
    if result.findings:
        actions.append(f"pages:{INDEX_FILE}")

    plan = plan_repair(session, result, actions)
    outcome: dict[str, Any] = {
        "command": "repair",
        "plan": plan.to_dict(),
        "audit": result.to_dict(),
    }
    if session.dry_run:
        outcome["applied"] = False
        return outcome
    if not actions:
        outcome["applied"] = True
        outcome["changed"] = False
        outcome["idempotent_noop"] = True
        outcome["unresolved"] = []
        return outcome
    if plan.force_update_required and not session.force:
        raise UsageError(
            "repair would remove or rewrite published objects; re-run with --force to accept "
            f"({', '.join(plan.destructive_targets)})"
        )

    files, tip = _pages_payload(session)
    recovered = _recover_archives(session, result, files)
    dropped: list[str] = []
    desired = _desired_pages_files(session, files, result, recovered, dropped)

    new_tip = tip
    changed = False
    if desired != files or tip is None:
        new_tip, changed = _mutate_pages(
            session,
            expected_tip=tip,
            message=f"repair: rebuild {contract.chart} publication snapshot",
            mutate=lambda _files: dict(desired),
            allow_create=True,
        )

    quarantined: dict[str, Any] | None = None
    if bad_release is not None and not bad_release.draft:
        release = quarantine_release(session, bad_release)
        quarantined = {"id": release.id, "tag": release.tag_name, "draft": release.draft}

    published: dict[str, Any] | None = None
    if publish_draft and replacement_release is not None:
        promoted = session.client.update_release(replacement_release.id, draft=False)
        published = {"id": promoted.id, "tag": promoted.tag_name, "draft": promoted.draft}

    deleted_tag: str | None = None
    bad_target = result.snapshot.tags.get(contract.bad_tag)
    if bad_target is not None:
        session.client.delete_tag(contract.bad_tag, expected=bad_target)
        deleted_tag = f"refs/tags/{contract.bad_tag}@{bad_target}"

    after = audit(session)
    outcome["applied"] = True
    outcome["changed"] = bool(
        changed or quarantined is not None or published is not None or deleted_tag is not None
    )
    outcome["pages"] = {"branch": session.pages_branch, "old_tip": tip, "new_tip": new_tip}
    outcome["release"] = quarantined
    outcome["published_release"] = published
    outcome["deleted_tag"] = deleted_tag
    outcome["recovered_archives"] = sorted(recovered)
    outcome["dropped_files"] = sorted(set(dropped))
    outcome["unresolved"] = [item.to_dict() for item in after.findings]
    if after.findings:
        raise AuditDrift(
            "repair applied what it could but the public repository is still "
            "inconsistent with the publication contract: "
            + "; ".join(f"{item.code} ({item.subject})" for item in after.findings)
        )
    return outcome


def audit_command(session: Session, *, strict: bool = True) -> dict[str, Any]:
    result = audit(session)
    payload = result.to_dict()
    if strict and not result.healthy:
        raise AuditDrift(
            "public repository state does not match the publication contract: "
            + "; ".join(f"{item.code} ({item.subject})" for item in result.findings)
        )
    return payload


def verify_live(
    session: Session,
    *,
    fetch: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """Independently confirm what a fresh Helm client would see."""
    contract = session.contract
    live = fetch_live_index(session, fetch=fetch)
    advertised = list(index_mod.advertised_versions(live, contract.chart))
    return {
        "pages_url": contract.pages_url,
        "advertised_versions": advertised,
        "latest": index_mod.latest_version(live, contract.chart),
        "withdrawn_version_advertised": contract.bad_version in advertised,
        "replacement_version_advertised": contract.replacement_version in advertised,
        "digests": index_mod.digests(live, contract.chart),
    }


__all__ = [
    "AuditResult",
    "Finding",
    "Session",
    "audit",
    "audit_command",
    "build_candidate",
    "collect_snapshot",
    "plan_publish",
    "plan_repair",
    "plan_withdraw",
    "publish",
    "quarantine_release",
    "read_pages_files",
    "repair",
    "validate",
    "verify_live",
    "withdraw",
]
