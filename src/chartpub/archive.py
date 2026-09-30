"""Reproducible chart packaging and archive safety checks.

``helm package`` embeds the build machine's mtimes, ownership and directory
iteration order, so two builds of the same source produce different bytes and
therefore different digests. That is what made the published digest unusable as
an integrity check. Packaging here is byte-for-byte deterministic: the member
list is sorted, and every mtime, mode, uid/gid and name is normalised.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import posixpath
import tarfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from chartpub.errors import ValidationError
from chartpub.models import Artifact, ChartMetadata, parse_version

#: Fixed member timestamp. Any constant works; 0 keeps the archive identical
#: across machines and time zones.
FIXED_MTIME = 0
FILE_MODE = 0o644
DIR_MODE = 0o755

#: Never packaged: editor/OS noise, VCS metadata, and previously built
#: archives that would otherwise nest inside the new one.
EXCLUDED_NAMES = frozenset(
    {".DS_Store", ".git", ".gitignore", ".idea", ".vscode", "__pycache__", "Thumbs.db"}
)
EXCLUDED_SUFFIXES = (".tgz", ".pyc", ".swp", ".orig", ".rej")

#: Cap on uncompressed expansion, so a hostile archive cannot exhaust disk
#: during verification.
MAX_TOTAL_UNCOMPRESSED = 64 * 1024 * 1024
MAX_MEMBERS = 4096


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    is_dir: bool
    size: int
    mode: int


def _is_excluded(relative: PurePosixPath) -> bool:
    for part in relative.parts:
        if part in EXCLUDED_NAMES:
            return True
    name = relative.name
    return name.endswith(EXCLUDED_SUFFIXES)


def _collect(chart_dir: Path) -> list[tuple[PurePosixPath, Path]]:
    """Every packaged path, sorted, with symlinks and special files refused."""
    collected: list[tuple[PurePosixPath, Path]] = []
    for path in sorted(chart_dir.rglob("*"), key=lambda item: item.as_posix()):
        relative = PurePosixPath(path.relative_to(chart_dir).as_posix())
        if _is_excluded(relative):
            continue
        if path.is_symlink():
            raise ValidationError(f"refusing to package symlink: {relative}")
        if not path.is_dir() and not path.is_file():
            raise ValidationError(f"refusing to package non-regular file: {relative}")
        collected.append((relative, path))
    return collected


def read_chart_metadata(chart_dir: Path) -> ChartMetadata:
    """Load and shape-check ``Chart.yaml``."""
    chart_yaml = chart_dir / "Chart.yaml"
    try:
        raw = yaml.safe_load(chart_yaml.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"cannot read {chart_yaml}") from exc
    except yaml.YAMLError as exc:
        raise ValidationError(f"{chart_yaml} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError(f"{chart_yaml} must contain a mapping")
    for key in ("apiVersion", "name", "version"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ValidationError(f"{chart_yaml} is missing a usable {key}")
    parse_version(str(raw["version"]))
    return ChartMetadata(
        api_version=str(raw["apiVersion"]),
        name=str(raw["name"]),
        version=str(raw["version"]),
        description=str(raw.get("description", "")),
        type=str(raw.get("type", "")),
        # appVersion is conventionally quoted in Chart.yaml but YAML may still
        # hand back a float/int if it was not; normalise to text.
        app_version="" if raw.get("appVersion") is None else str(raw["appVersion"]),
    )


def _tar_info(name: str, *, is_dir: bool, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    info.mode = DIR_MODE if is_dir else FILE_MODE
    info.size = 0 if is_dir else size
    info.mtime = FIXED_MTIME
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def package_chart(chart_dir: Path, output_dir: Path, version: str) -> Artifact:
    """Package ``chart_dir`` deterministically as ``<name>-<version>.tgz``.

    The root directory inside the archive is the chart name, as Helm requires,
    and ``Chart.yaml`` must already declare ``version`` — packaging never
    rewrites chart metadata behind the operator's back.
    """
    metadata = read_chart_metadata(chart_dir)
    if metadata.version != version:
        raise ValidationError(
            f"{chart_dir / 'Chart.yaml'} declares version {metadata.version!r} but "
            f"{version!r} was requested; update Chart.yaml instead"
        )
    if metadata.name != chart_dir.name:
        raise ValidationError(
            f"chart directory {chart_dir.name!r} must match Chart.yaml name {metadata.name!r}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{metadata.name}-{version}.tgz"
    members = _collect(chart_dir)

    raw = io.BytesIO()
    # mtime=0 keeps the gzip header itself reproducible; tarfile's own gzip
    # wrapper would stamp the current time.
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        archive.addfile(_tar_info(metadata.name, is_dir=True, size=0))
        for relative, path in members:
            name = posixpath.join(metadata.name, relative.as_posix())
            if path.is_dir():
                archive.addfile(_tar_info(name, is_dir=True, size=0))
                continue
            payload = path.read_bytes()
            archive.addfile(_tar_info(name, is_dir=False, size=len(payload)), io.BytesIO(payload))
    payload = raw.getvalue()
    output.write_bytes(payload)

    return Artifact(
        path=output,
        name=metadata.name,
        version=version,
        sha256=sha256_bytes(payload),
        size=len(payload),
        metadata=metadata,
    )


def _iter_members(archive: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    total = 0
    for count, info in enumerate(archive, start=1):
        total += max(info.size, 0)
        if count > MAX_MEMBERS:
            raise ValidationError(f"archive has more than {MAX_MEMBERS} members")
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise ValidationError("archive expands beyond the supported size limit")
        yield info


def inspect_archive(path: Path, *, expected_root: str | None = None) -> tuple[ArchiveMember, ...]:
    """Return the archive's members, refusing anything unsafe.

    Rejects absolute paths, ``..`` traversal, symlinks and hard links, device
    and FIFO members, duplicate names, and members outside the single expected
    root directory.
    """
    seen: set[str] = set()
    roots: set[str] = set()
    members: list[ArchiveMember] = []
    try:
        opened = tarfile.open(path, "r:gz")  # noqa: SIM115 - closed by the `with` below
    except (OSError, tarfile.TarError) as exc:
        raise ValidationError(f"{path.name} is not a readable gzip tar archive: {exc}") from exc
    with opened as archive:
        for info in _iter_members(archive):
            name = info.name
            if name in {".", "./"}:
                continue
            normalised = name[2:] if name.startswith("./") else name
            if not normalised:
                continue
            if normalised.startswith("/") or PurePosixPath(normalised).is_absolute():
                raise ValidationError(f"{path.name} contains an absolute member path: {name}")
            if "\\" in normalised:
                raise ValidationError(f"{path.name} contains a backslash member path: {name}")
            parts = PurePosixPath(normalised).parts
            if ".." in parts:
                raise ValidationError(f"{path.name} contains a path traversal member: {name}")
            if info.issym() or info.islnk():
                raise ValidationError(f"{path.name} contains a link member: {name}")
            if not (info.isfile() or info.isdir()):
                raise ValidationError(f"{path.name} contains a special-file member: {name}")
            if normalised in seen:
                raise ValidationError(f"{path.name} contains duplicate member: {name}")
            seen.add(normalised)
            roots.add(parts[0])
            members.append(
                ArchiveMember(
                    name=normalised,
                    is_dir=info.isdir(),
                    size=max(info.size, 0),
                    mode=info.mode,
                )
            )

    if not members:
        raise ValidationError(f"{path.name} is empty")
    if len(roots) != 1:
        raise ValidationError(
            f"{path.name} must contain exactly one top-level directory, found: "
            + ", ".join(sorted(roots))
        )
    root = next(iter(roots))
    if expected_root is not None and root != expected_root:
        raise ValidationError(
            f"{path.name} top-level directory is {root!r}, expected {expected_root!r}"
        )
    if not any(member.name == f"{root}/Chart.yaml" for member in members):
        raise ValidationError(f"{path.name} does not contain {root}/Chart.yaml")
    return tuple(sorted(members, key=lambda item: item.name))


def read_member(path: Path, member: str) -> bytes:
    """Read one member's bytes after the archive has been vetted."""
    with tarfile.open(path, "r:gz") as archive:
        try:
            extracted = archive.extractfile(member)
        except KeyError as exc:
            raise ValidationError(f"{path.name} has no member {member}") from exc
        if extracted is None:
            raise ValidationError(f"{path.name} member {member} is not a regular file")
        with extracted:
            return extracted.read()


def verify_archive(path: Path, expected_sha256: str) -> bool:
    """True when the file on disk hashes to ``expected_sha256``."""
    return sha256_file(path) == expected_sha256


def require_digest(path: Path, expected_sha256: str, *, what: str = "archive") -> None:
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ValidationError(
            f"{what} digest mismatch for {path.name}: expected {expected_sha256}, got {actual}"
        )


def archive_metadata(path: Path, *, expected_root: str | None = None) -> ChartMetadata:
    """Chart metadata read back out of a packaged archive."""
    members = inspect_archive(path, expected_root=expected_root)
    root = PurePosixPath(members[0].name).parts[0]
    raw = yaml.safe_load(read_member(path, f"{root}/Chart.yaml").decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValidationError(f"{path.name}: Chart.yaml must contain a mapping")
    return ChartMetadata(
        api_version=str(raw.get("apiVersion", "")),
        name=str(raw.get("name", "")),
        version=str(raw.get("version", "")),
        description=str(raw.get("description", "")),
        type=str(raw.get("type", "")),
        app_version="" if raw.get("appVersion") is None else str(raw["appVersion"]),
    )


def member_names(members: Iterable[ArchiveMember]) -> tuple[str, ...]:
    return tuple(item.name for item in members)
