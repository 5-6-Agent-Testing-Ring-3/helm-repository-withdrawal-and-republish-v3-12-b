"""Deterministic Helm repository index generation.

Two properties matter for recovery:

* **Idempotency** — republishing identical inputs must produce byte-identical
  ``index.yaml``, so a retried publication is a provable no-op rather than a
  second entry for the same version.
* **Least surprise** — unrelated valid chart versions are always preserved;
  only the version named by the contract is ever added or removed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from chartpub.errors import ValidationError
from chartpub.models import Artifact, version_sort_key

API_VERSION = "v1"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Order Helm itself uses for entry keys; anything unrecognised is appended
#: alphabetically so hand-added fields survive a rewrite deterministically.
_ENTRY_KEY_ORDER = (
    "apiVersion",
    "appVersion",
    "created",
    "description",
    "digest",
    "home",
    "icon",
    "keywords",
    "kubeVersion",
    "maintainers",
    "name",
    "sources",
    "type",
    "urls",
    "version",
)


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def empty_index() -> dict[str, Any]:
    return {"apiVersion": API_VERSION, "entries": {}}


def load_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_index()
    return parse_index(path.read_text(encoding="utf-8"))


def parse_index(text: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"index is not valid YAML: {exc}") from exc
    if value is None:
        return empty_index()
    if not isinstance(value, dict):
        raise ValidationError("index must be a mapping")
    entries = value.get("entries")
    if entries is None:
        value["entries"] = {}
    elif not isinstance(entries, dict):
        raise ValidationError("index entries must be a mapping of chart name to versions")
    else:
        for chart, versions in entries.items():
            if not isinstance(versions, list):
                raise ValidationError(f"index entries for {chart} must be a list")
    return value


def entries_for(index: Mapping[str, Any], chart: str) -> list[dict[str, Any]]:
    entries = index.get("entries") or {}
    versions = entries.get(chart) or []
    return [item for item in versions if isinstance(item, dict)]


def find_entry(index: Mapping[str, Any], chart: str, version: str) -> dict[str, Any] | None:
    return next(
        (item for item in entries_for(index, chart) if item.get("version") == version), None
    )


def build_entry(
    artifact: Artifact,
    base_url: str,
    *,
    created: str,
) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if artifact.metadata is not None:
        entry.update(artifact.metadata.index_fields())
    else:
        entry.update({"apiVersion": "v2", "name": artifact.name, "version": artifact.version})
    entry["version"] = artifact.version
    entry["name"] = artifact.name
    entry["digest"] = artifact.sha256
    entry["urls"] = [f"{base_url.rstrip('/')}/{artifact.path.name}"]
    entry["created"] = created
    return entry


def add_artifact(
    index: dict[str, Any],
    artifact: Artifact,
    base_url: str,
    *,
    created: datetime | str | None = None,
) -> dict[str, Any]:
    """Insert or replace ``artifact``'s entry, then renormalise the index.

    Re-adding an identical artifact is a no-op: the original ``created``
    timestamp is preserved so the serialised index does not change.
    """
    entries = index.setdefault("entries", {})
    versions = entries.setdefault(artifact.name, [])
    if not isinstance(versions, list):  # pragma: no cover - guarded by parse_index
        raise ValidationError(f"index entries for {artifact.name} must be a list")

    existing = next(
        (
            item
            for item in versions
            if isinstance(item, dict) and item.get("version") == artifact.version
        ),
        None,
    )
    if isinstance(created, datetime):
        stamp = format_timestamp(created)
    elif isinstance(created, str):
        stamp = created
    else:
        stamp = format_timestamp(datetime.now(UTC))
    if existing is not None and isinstance(existing.get("created"), str):
        stamp = existing["created"]

    entry = build_entry(artifact, base_url, created=stamp)
    remaining = [
        item
        for item in versions
        if not (isinstance(item, dict) and item.get("version") == artifact.version)
    ]
    remaining.append(entry)
    entries[artifact.name] = remaining
    return normalise(index)


def remove_version(index: dict[str, Any], chart: str, version: str) -> dict[str, Any]:
    """Drop exactly one chart version. Other charts and versions are kept."""
    entries = index.setdefault("entries", {})
    if chart in entries:
        versions = entries[chart]
        if isinstance(versions, list):
            entries[chart] = [
                item
                for item in versions
                if not (isinstance(item, dict) and item.get("version") == version)
            ]
            if not entries[chart]:
                del entries[chart]
    return normalise(index)


def _entry_sort_key(entry: Mapping[str, Any]) -> tuple[object, ...]:
    return version_sort_key(str(entry.get("version", "")))


def _ordered_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    known = [key for key in _ENTRY_KEY_ORDER if key in entry]
    extra = sorted(key for key in entry if key not in _ENTRY_KEY_ORDER)
    return {key: entry[key] for key in [*known, *extra]}


def normalise(index: dict[str, Any]) -> dict[str, Any]:
    """Canonicalise ordering and derive ``generated`` from the entries.

    ``generated`` is the newest entry ``created`` value rather than "now", which
    is what makes an unchanged index serialise identically on every run.
    """
    previous_generated = index.get("generated")
    entries = index.get("entries") or {}
    normalised_entries: dict[str, list[dict[str, Any]]] = {}
    stamps: list[str] = []
    for chart in sorted(entries):
        versions = entries[chart]
        if not isinstance(versions, list):  # pragma: no cover - guarded by parse_index
            raise ValidationError(f"index entries for {chart} must be a list")
        kept = [item for item in versions if isinstance(item, dict)]
        if not kept:
            continue
        # Newest first, matching `helm repo index` output.
        kept.sort(key=_entry_sort_key, reverse=True)
        normalised_entries[chart] = [_ordered_entry(item) for item in kept]
        stamps.extend(str(item["created"]) for item in kept if isinstance(item.get("created"), str))

    rebuilt: dict[str, Any] = {"apiVersion": str(index.get("apiVersion") or API_VERSION)}
    rebuilt["entries"] = normalised_entries
    if stamps:
        rebuilt["generated"] = max(stamps)
    elif isinstance(previous_generated, str):
        rebuilt["generated"] = previous_generated
    for key in sorted(index):
        if key not in rebuilt:
            rebuilt[key] = index[key]

    index.clear()
    index.update(rebuilt)
    return index


class _IndexDumper(yaml.SafeDumper):
    """Block style everywhere, with list items indented like Helm's output."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow=flow, indentless=False)


def dump_index(index: Mapping[str, Any]) -> str:
    """Serialise deterministically. Identical inputs give identical bytes."""
    snapshot = normalise(dict(index))
    return yaml.dump(
        snapshot,
        Dumper=_IndexDumper,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
        allow_unicode=True,
    )


def write_index(path: Path, index: Mapping[str, Any]) -> bool:
    """Write the index, returning True only when the bytes actually changed."""
    payload = dump_index(index)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return False
    path.write_text(payload, encoding="utf-8")
    return True


def advertised_versions(index: Mapping[str, Any], chart: str) -> tuple[str, ...]:
    return tuple(str(item.get("version", "")) for item in entries_for(index, chart))


def referenced_files(index: Mapping[str, Any]) -> tuple[str, ...]:
    """Basenames of every archive the index points at, sorted and de-duplicated."""
    names: set[str] = set()
    for versions in (index.get("entries") or {}).values():
        if not isinstance(versions, list):  # pragma: no cover - guarded by parse_index
            continue
        for entry in versions:
            if not isinstance(entry, dict):
                continue
            for url in entry.get("urls") or []:
                if isinstance(url, str) and url:
                    names.add(url.rstrip("/").rsplit("/", 1)[-1])
    return tuple(sorted(names))


def digests(index: Mapping[str, Any], chart: str) -> dict[str, str]:
    return {
        str(entry["version"]): str(entry.get("digest", ""))
        for entry in entries_for(index, chart)
        if entry.get("version")
    }


def chart_names(index: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(index.get("entries") or {}))


def latest_version(index: Mapping[str, Any], chart: str) -> str | None:
    versions: Iterable[str] = advertised_versions(index, chart)
    ordered = sorted(versions, key=version_sort_key, reverse=True)
    return ordered[0] if ordered else None
