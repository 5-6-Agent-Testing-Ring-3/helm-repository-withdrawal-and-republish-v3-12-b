"""Value objects: the publication contract, artifacts, remote state, plans."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from chartpub.errors import ContractError

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)
REF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
CHART_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

SUPPORTED_SCHEMA_VERSION = 1

#: Plan scopes. ``plan`` output groups its steps by these so an operator can
#: see at a glance what is local and what is remote.
Scope = Literal["local", "release", "tag", "pages"]


def _require_str(raw: dict[str, Any], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{key} must be a non-empty string")
    return value


def parse_version(version: str) -> tuple[int, int, int, str]:
    """Parse a SemVer 2.0 string, raising :class:`ContractError` if invalid."""
    match = SEMVER_RE.match(version)
    if match is None:
        raise ContractError(f"not a valid semantic version: {version!r}")
    return (
        int(match["major"]),
        int(match["minor"]),
        int(match["patch"]),
        match["pre"] or "",
    )


def version_sort_key(version: str) -> tuple[int, int, int, int, tuple[object, ...]]:
    """Total order over version strings, newest last.

    Unparseable versions sort below every valid one rather than raising, so a
    hand-edited index never makes the tool unusable.
    """
    match = SEMVER_RE.match(version)
    if match is None:
        return (-1, 0, 0, 0, (version,))
    pre = match["pre"]
    # A release outranks any prerelease of the same core version.
    identifiers: tuple[object, ...] = ()
    if pre:
        identifiers = tuple(
            (0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split(".")
        )
    return (
        int(match["major"]),
        int(match["minor"]),
        int(match["patch"]),
        0 if pre else 1,
        identifiers,
    )


@dataclass(frozen=True)
class PublicationContract:
    """The operator-authored description of what may be touched.

    Loading is strict: every key is required, unknown keys are refused, and
    every value is shape-checked. A typo must not silently widen the blast
    radius of a destructive command.
    """

    schema_version: int
    repository: str
    source_branch: str
    pages_branch: str
    pages_url: str
    chart: str
    bad_version: str
    replacement_version: str
    bad_tag: str
    replacement_tag: str
    expected_bad_tag_target: str
    expected_pages_tip: str
    release_asset_name: str

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> PublicationContract:
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ContractError(f"unknown contract key(s): {', '.join(unknown)}")
        missing = sorted(known - set(raw))
        if missing:
            raise ContractError(f"missing contract key(s): {', '.join(missing)}")

        # bool is a subclass of int; `"schema_version": true` must not pass.
        version = raw["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ContractError("schema_version must be an integer")
        if version != SUPPORTED_SCHEMA_VERSION:
            raise ContractError(
                f"unsupported schema_version {version}; this tool understands "
                f"{SUPPORTED_SCHEMA_VERSION}"
            )

        text = {key: _require_str(raw, key) for key in known - {"schema_version"}}

        if not REPOSITORY_RE.match(text["repository"]):
            raise ContractError("repository must be in 'owner/name' form")
        for key in ("source_branch", "pages_branch", "bad_tag", "replacement_tag"):
            if not REF_NAME_RE.match(text[key]):
                raise ContractError(f"{key} is not a usable Git ref name")
        if text["source_branch"] == text["pages_branch"]:
            raise ContractError("source_branch and pages_branch must differ")
        if not text["pages_url"].startswith(("http://", "https://")):
            raise ContractError("pages_url must be an absolute http(s) URL")
        if not CHART_NAME_RE.match(text["chart"]):
            raise ContractError("chart must be a lowercase DNS-style name")
        for key in ("bad_version", "replacement_version"):
            parse_version(text[key])
        if text["bad_version"] == text["replacement_version"]:
            raise ContractError("replacement_version must differ from bad_version")
        if version_sort_key(text["replacement_version"]) < version_sort_key(text["bad_version"]):
            raise ContractError("replacement_version must be newer than bad_version")
        if text["bad_tag"] == text["replacement_tag"]:
            raise ContractError("bad_tag and replacement_tag must differ")
        for key in ("expected_bad_tag_target", "expected_pages_tip"):
            if not SHA_RE.match(text[key]):
                raise ContractError(f"{key} must be a full 40-character commit SHA")
        if "{version}" not in text["release_asset_name"]:
            raise ContractError("release_asset_name must contain the {version} placeholder")
        rendered = text["release_asset_name"].format(version=text["replacement_version"])
        if "/" in rendered or rendered.startswith(".") or not rendered.endswith(".tgz"):
            raise ContractError("release_asset_name must render to a plain *.tgz file name")

        return cls(schema_version=version, **text)

    def asset_name(self, version: str) -> str:
        return self.release_asset_name.format(version=version)

    def tag_for(self, version: str) -> str:
        """The public tag for ``version``, restricted to contract-named ones."""
        if version == self.bad_version:
            return self.bad_tag
        if version == self.replacement_version:
            return self.replacement_tag
        raise ContractError(f"version {version} is not named by the publication contract")

    def asset_url(self, version: str) -> str:
        return f"{self.pages_url.rstrip('/')}/{self.asset_name(version)}"

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repository.split("/", 1)[1]

    @property
    def chart_dir(self) -> Path:
        return Path("charts") / self.chart

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChartMetadata:
    """The subset of ``Chart.yaml`` that ends up in ``index.yaml``."""

    api_version: str
    name: str
    version: str
    description: str
    type: str
    app_version: str

    def index_fields(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "apiVersion": self.api_version,
            "name": self.name,
            "version": self.version,
        }
        if self.description:
            entry["description"] = self.description
        if self.type:
            entry["type"] = self.type
        if self.app_version:
            entry["appVersion"] = self.app_version
        return entry


@dataclass(frozen=True)
class Artifact:
    """A packaged chart on local disk."""

    path: Path
    name: str
    version: str
    sha256: str
    size: int
    metadata: ChartMetadata | None = None


@dataclass(frozen=True)
class ReleaseAsset:
    id: int
    name: str
    size: int
    state: str


@dataclass(frozen=True)
class ReleaseState:
    """A GitHub release as chartpub cares about it."""

    id: int
    tag_name: str
    name: str
    draft: bool
    prerelease: bool
    target_commitish: str
    html_url: str
    assets: tuple[ReleaseAsset, ...] = ()

    def asset(self, name: str) -> ReleaseAsset | None:
        return next((item for item in self.assets if item.name == name), None)


@dataclass(frozen=True)
class RemoteSnapshot:
    """Everything the tool inspected before deciding what to do."""

    repository: str
    main_tip: str | None
    pages_tip: str | None
    tags: dict[str, str] = field(default_factory=dict)
    releases: tuple[ReleaseState, ...] = ()
    index: dict[str, Any] | None = None
    pages_files: tuple[str, ...] = ()

    def release_for_tag(self, tag: str) -> ReleaseState | None:
        return next((item for item in self.releases if item.tag_name == tag), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "main_tip": self.main_tip,
            "pages_tip": self.pages_tip,
            "tags": dict(sorted(self.tags.items())),
            "releases": [
                {
                    "id": release.id,
                    "tag_name": release.tag_name,
                    "name": release.name,
                    "draft": release.draft,
                    "prerelease": release.prerelease,
                    "html_url": release.html_url,
                    "assets": [
                        {"id": a.id, "name": a.name, "size": a.size, "state": a.state}
                        for a in release.assets
                    ],
                }
                for release in self.releases
            ],
            "pages_files": list(self.pages_files),
        }


@dataclass(frozen=True)
class PlanStep:
    """One observable change, or one deliberate no-op."""

    scope: Scope
    action: str
    target: str
    detail: str
    destructive: bool = False
    noop: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "destructive": self.destructive,
            "noop": self.noop,
        }


@dataclass(frozen=True)
class Precondition:
    """A remote compare-and-swap check evaluated before any mutation."""

    subject: str
    expected: str | None
    actual: str | None
    satisfied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "expected": self.expected,
            "actual": self.actual,
            "satisfied": self.satisfied,
        }


@dataclass(frozen=True)
class Plan:
    """A deterministic, machine-readable description of an intended change."""

    command: str
    repository: str
    chart: str
    version: str
    steps: tuple[PlanStep, ...]
    preconditions: tuple[Precondition, ...]
    force_update_required: bool
    dry_run: bool
    notes: tuple[str, ...] = ()

    def steps_for(self, scope: Scope) -> tuple[PlanStep, ...]:
        return tuple(step for step in self.steps if step.scope == scope)

    @property
    def unsatisfied(self) -> tuple[Precondition, ...]:
        return tuple(item for item in self.preconditions if not item.satisfied)

    @property
    def is_noop(self) -> bool:
        return all(step.noop for step in self.steps)

    @property
    def remote_is_noop(self) -> bool:
        """True when nothing remote would change.

        Local packaging and validation always run, so ``is_noop`` is never true
        for ``publish``; this is the property that says "already published".
        """
        return all(step.noop for step in self.steps if step.scope != "local")

    @property
    def destructive_targets(self) -> tuple[str, ...]:
        return tuple(
            f"{step.scope}:{step.target}"
            for step in self.steps
            if step.destructive and not step.noop
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "repository": self.repository,
            "chart": self.chart,
            "version": self.version,
            "dry_run": self.dry_run,
            "force_update_required": self.force_update_required,
            "is_noop": self.is_noop,
            "remote_is_noop": self.remote_is_noop,
            "local_changes": [s.to_dict() for s in self.steps_for("local")],
            "release_changes": [s.to_dict() for s in self.steps_for("release")],
            "tag_changes": [s.to_dict() for s in self.steps_for("tag")],
            "pages_changes": [s.to_dict() for s in self.steps_for("pages")],
            "destructive_scope": list(self.destructive_targets),
            "preconditions": [p.to_dict() for p in self.preconditions],
            "notes": list(self.notes),
        }
