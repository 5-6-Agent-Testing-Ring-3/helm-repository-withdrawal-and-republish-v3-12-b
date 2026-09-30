from __future__ import annotations

import gzip
import io
import os
import tarfile
from pathlib import Path

import pytest

from chartpub.archive import (
    archive_metadata,
    inspect_archive,
    package_chart,
    read_chart_metadata,
    read_member,
    require_digest,
    sha256_bytes,
    verify_archive,
)
from chartpub.errors import ValidationError


def test_package_digest_verifies(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    assert verify_archive(artifact.path, artifact.sha256)


def test_package_is_reproducible(tmp_path: Path, chart_dir: Path) -> None:
    first = package_chart(chart_dir, tmp_path / "first", "0.4.1")
    chart_yaml = chart_dir / "Chart.yaml"
    stat = chart_yaml.stat()
    os.utime(chart_yaml, (stat.st_atime, stat.st_mtime + 5))
    try:
        second = package_chart(chart_dir, tmp_path / "second", "0.4.1")
    finally:
        os.utime(chart_yaml, (stat.st_atime, stat.st_mtime))
    assert first.sha256 == second.sha256


def test_package_normalises_metadata(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    with tarfile.open(artifact.path) as handle:
        infos = handle.getmembers()
    assert [item.name for item in infos] == sorted(item.name for item in infos)
    assert all(item.mtime == 0 for item in infos)
    assert all(item.uid == 0 and item.gid == 0 for item in infos)
    assert all(item.uname == "" and item.gname == "" for item in infos)
    assert {item.mode for item in infos if item.isfile()} == {0o644}
    assert infos[0].name == "ledger-api"


def test_package_root_is_the_chart_name(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    members = inspect_archive(artifact.path, expected_root="ledger-api")
    assert "ledger-api/Chart.yaml" in {item.name for item in members}


def test_package_excludes_noise(tmp_path: Path, source_chart: Path) -> None:
    (source_chart / ".DS_Store").write_bytes(b"junk")
    (source_chart / "stale-ledger-api-0.1.0.tgz").write_bytes(b"old archive")
    artifact = package_chart(source_chart, tmp_path / "out", "0.4.1")
    names = {item.name for item in inspect_archive(artifact.path)}
    assert not any(name.endswith(".DS_Store") or name.endswith(".tgz") for name in names)


def test_package_refuses_symlink(tmp_path: Path, source_chart: Path) -> None:
    (source_chart / "link.yaml").symlink_to(source_chart / "values.yaml")
    with pytest.raises(ValidationError, match="symlink"):
        package_chart(source_chart, tmp_path / "out", "0.4.1")


def test_package_refuses_version_disagreement(tmp_path: Path, chart_dir: Path) -> None:
    with pytest.raises(ValidationError, match="declares version"):
        package_chart(chart_dir, tmp_path, "9.9.9")


def test_package_refuses_directory_name_mismatch(tmp_path: Path, source_chart: Path) -> None:
    renamed = source_chart.parent / "other-name"
    source_chart.rename(renamed)
    with pytest.raises(ValidationError, match="must match Chart.yaml name"):
        package_chart(renamed, tmp_path / "out", "0.4.1")


def test_read_chart_metadata_rejects_bad_yaml(tmp_path: Path) -> None:
    chart = tmp_path / "broken"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("not: [valid", encoding="utf-8")
    with pytest.raises(ValidationError, match="not valid YAML"):
        read_chart_metadata(chart)


def test_read_chart_metadata_requires_keys(tmp_path: Path) -> None:
    chart = tmp_path / "bare"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: x\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="usable apiVersion"):
        read_chart_metadata(chart)


def test_read_chart_metadata_requires_mapping(tmp_path: Path) -> None:
    chart = tmp_path / "list"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("- a\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="must contain a mapping"):
        read_chart_metadata(chart)


def test_read_chart_metadata_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot read"):
        read_chart_metadata(tmp_path / "absent")


def write_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    raw = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as archive,
    ):
        for info, payload in members:
            archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
    path.write_bytes(raw.getvalue())
    return path


def entry(name: str, payload: bytes = b"x", *, kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = len(payload) if kind == tarfile.REGTYPE else 0
    return info


def test_inspect_rejects_absolute_path(tmp_path: Path) -> None:
    path = write_tar(tmp_path / "a.tgz", [(entry("/etc/passwd"), b"x")])
    with pytest.raises(ValidationError, match="absolute member path"):
        inspect_archive(path)


def test_inspect_rejects_traversal(tmp_path: Path) -> None:
    path = write_tar(tmp_path / "a.tgz", [(entry("chart/../../escape"), b"x")])
    with pytest.raises(ValidationError, match="path traversal"):
        inspect_archive(path)


def test_inspect_rejects_backslash(tmp_path: Path) -> None:
    path = write_tar(tmp_path / "a.tgz", [(entry("chart\\evil"), b"x")])
    with pytest.raises(ValidationError, match="backslash"):
        inspect_archive(path)


def test_inspect_rejects_symlink_member(tmp_path: Path) -> None:
    info = entry("chart/link", kind=tarfile.SYMTYPE)
    info.linkname = "/etc/passwd"
    path = write_tar(tmp_path / "a.tgz", [(info, None)])
    with pytest.raises(ValidationError, match="link member"):
        inspect_archive(path)


def test_inspect_rejects_hardlink_member(tmp_path: Path) -> None:
    info = entry("chart/hard", kind=tarfile.LNKTYPE)
    info.linkname = "chart/Chart.yaml"
    path = write_tar(tmp_path / "a.tgz", [(info, None)])
    with pytest.raises(ValidationError, match="link member"):
        inspect_archive(path)


def test_inspect_rejects_special_file(tmp_path: Path) -> None:
    path = write_tar(tmp_path / "a.tgz", [(entry("chart/fifo", kind=tarfile.FIFOTYPE), None)])
    with pytest.raises(ValidationError, match="special-file member"):
        inspect_archive(path)


def test_inspect_rejects_duplicate_members(tmp_path: Path) -> None:
    path = write_tar(
        tmp_path / "a.tgz",
        [(entry("chart/Chart.yaml"), b"a"), (entry("chart/Chart.yaml"), b"b")],
    )
    with pytest.raises(ValidationError, match="duplicate member"):
        inspect_archive(path)


def test_inspect_rejects_two_roots(tmp_path: Path) -> None:
    path = write_tar(
        tmp_path / "a.tgz", [(entry("one/Chart.yaml"), b"a"), (entry("two/Chart.yaml"), b"b")]
    )
    with pytest.raises(ValidationError, match="exactly one top-level directory"):
        inspect_archive(path)


def test_inspect_rejects_unexpected_root(tmp_path: Path) -> None:
    path = write_tar(tmp_path / "a.tgz", [(entry("other/Chart.yaml"), b"a")])
    with pytest.raises(ValidationError, match="expected 'ledger-api'"):
        inspect_archive(path, expected_root="ledger-api")


def test_inspect_requires_chart_yaml(tmp_path: Path) -> None:
    path = write_tar(tmp_path / "a.tgz", [(entry("chart/values.yaml"), b"a")])
    with pytest.raises(ValidationError, match="does not contain chart/Chart.yaml"):
        inspect_archive(path)


def test_inspect_rejects_empty_archive(tmp_path: Path) -> None:
    path = write_tar(tmp_path / "a.tgz", [])
    with pytest.raises(ValidationError, match="is empty"):
        inspect_archive(path)


def test_inspect_rejects_non_archive(tmp_path: Path) -> None:
    path = tmp_path / "a.tgz"
    path.write_bytes(b"not a tarball")
    with pytest.raises(ValidationError, match="not a readable gzip tar archive"):
        inspect_archive(path)


def test_inspect_enforces_member_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chartpub.archive.MAX_MEMBERS", 2)
    path = write_tar(
        tmp_path / "a.tgz",
        [(entry(f"chart/f{i}.yaml"), b"x") for i in range(4)],
    )
    with pytest.raises(ValidationError, match="more than 2 members"):
        inspect_archive(path)


def test_inspect_enforces_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chartpub.archive.MAX_TOTAL_UNCOMPRESSED", 4)
    payload = b"x" * 64
    path = write_tar(tmp_path / "a.tgz", [(entry("chart/big.yaml", payload), payload)])
    with pytest.raises(ValidationError, match="beyond the supported size limit"):
        inspect_archive(path)


def test_inspect_tolerates_dot_slash_prefix(tmp_path: Path) -> None:
    path = write_tar(
        tmp_path / "a.tgz",
        [
            (entry("./chart/Chart.yaml", b"a"), b"a"),
            (entry(".", kind=tarfile.DIRTYPE), None),
        ],
    )
    assert {item.name for item in inspect_archive(path)} == {"chart/Chart.yaml"}


def test_read_member_and_metadata(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    payload = read_member(artifact.path, "ledger-api/Chart.yaml")
    assert b"name: ledger-api" in payload
    metadata = archive_metadata(artifact.path, expected_root="ledger-api")
    assert (metadata.name, metadata.version, metadata.app_version) == (
        "ledger-api",
        "0.4.1",
        "1.8.2",
    )


def test_read_member_missing(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    with pytest.raises(ValidationError, match="has no member"):
        read_member(artifact.path, "ledger-api/absent.yaml")


def test_read_member_rejects_directory(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    with pytest.raises(ValidationError, match="not a regular file"):
        read_member(artifact.path, "ledger-api/templates")


def test_archive_metadata_requires_mapping(tmp_path: Path) -> None:
    path = write_tar(tmp_path / "a.tgz", [(entry("chart/Chart.yaml", b"- a"), b"- a")])
    with pytest.raises(ValidationError, match="must contain a mapping"):
        archive_metadata(path)


def test_require_digest_reports_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "chart.tgz"
    path.write_bytes(b"payload")
    require_digest(path, sha256_bytes(b"payload"))
    with pytest.raises(ValidationError, match="digest mismatch"):
        require_digest(path, "0" * 64)
