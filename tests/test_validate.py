from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chartpub.archive import package_chart
from chartpub.errors import ValidationError
from chartpub.models import Artifact
from chartpub.validate import (
    CommandResult,
    ValidationReport,
    check_archive_contents,
    check_manifest_invariants,
    check_test_install,
    cluster_reachable,
    default_runner,
    discover_values_fixtures,
    helm_available,
    manifest_problems,
    validate_candidate,
)

from .fakes import DEFAULT_MANIFEST, MISMATCHED_MANIFEST, FakeHelm


def candidate(tmp_path: Path, chart_dir: Path) -> Artifact:
    return package_chart(chart_dir, tmp_path / "out", "0.4.1")


def test_happy_path_passes_every_check(
    tmp_path: Path, chart_dir: Path, values_files: tuple[Path, ...]
) -> None:
    report = validate_candidate(
        candidate(tmp_path, chart_dir), values_files=values_files, runner=FakeHelm()
    )
    assert report.ok, report.to_dict()
    assert [check.name for check in report.checks] == [
        "archive-contents",
        "archive-digest",
        "helm-lint",
        "helm-template[default]",
        "helm-template[values-ha.yaml]",
        "helm-template[values-minimal.yaml]",
        "manifest-invariants",
        "test-install",
    ]


def test_selector_mismatch_is_rejected(
    tmp_path: Path, chart_dir: Path, values_files: tuple[Path, ...]
) -> None:
    """The exact defect the 0.4.0 publication shipped."""
    report = validate_candidate(
        candidate(tmp_path, chart_dir),
        values_files=values_files,
        runner=FakeHelm(rendered=MISMATCHED_MANIFEST),
    )
    assert not report.ok
    failed = {check.name for check in report.failures}
    assert failed == {"manifest-invariants"}
    detail = next(c.detail for c in report.failures)
    assert "selector does not match template labels" in detail
    assert "matches no pod template" in detail


def test_real_chart_passes_manifest_invariants(chart_dir: Path) -> None:
    """Guards against the fix regressing: the shipped chart must be consistent."""
    if not helm_available():  # pragma: no cover - helm is installed in CI
        pytest.skip("helm is not installed")
    rendered = default_runner(["helm", "template", "probe", str(chart_dir)])
    assert rendered.ok, rendered.output
    assert manifest_problems(rendered.stdout) == []


def test_lint_failure_is_reported(
    tmp_path: Path, chart_dir: Path, values_files: tuple[Path, ...]
) -> None:
    report = validate_candidate(
        candidate(tmp_path, chart_dir),
        values_files=values_files,
        runner=FakeHelm(failures={"helm lint": (1, "chart is broken")}),
    )
    assert not report.ok
    assert "chart is broken" in next(c.detail for c in report.failures if c.name == "helm-lint")


def test_render_failure_for_one_fixture_is_reported(
    tmp_path: Path, chart_dir: Path, values_files: tuple[Path, ...]
) -> None:
    report = validate_candidate(
        candidate(tmp_path, chart_dir),
        values_files=values_files,
        runner=FakeHelm(failures={"helm template": (1, "render exploded")}),
    )
    assert {c.name for c in report.failures} == {
        "helm-template[default]",
        "helm-template[values-ha.yaml]",
        "helm-template[values-minimal.yaml]",
        "manifest-invariants",
    }
    assert "render exploded" in next(
        c.detail for c in report.failures if c.name == "helm-template[values-ha.yaml]"
    )


def test_missing_values_fixtures_is_a_failure(tmp_path: Path, chart_dir: Path) -> None:
    report = validate_candidate(candidate(tmp_path, chart_dir), values_files=(), runner=FakeHelm())
    assert not report.ok
    assert "no values fixtures found" in next(
        c.detail for c in report.failures if c.name == "helm-template[fixtures]"
    )


def test_test_install_failure_is_reported(
    tmp_path: Path, chart_dir: Path, values_files: tuple[Path, ...]
) -> None:
    report = validate_candidate(
        candidate(tmp_path, chart_dir),
        values_files=values_files,
        runner=FakeHelm(failures={"helm install": (1, "admission webhook denied")}),
    )
    assert {c.name for c in report.failures} == {"test-install"}
    assert "admission webhook denied" in next(c.detail for c in report.failures)


def test_test_install_uses_an_isolated_release_and_namespace(
    tmp_path: Path, chart_dir: Path
) -> None:
    helm = FakeHelm()
    report = ValidationReport()
    check_test_install(candidate(tmp_path, chart_dir), report, runner=helm, server_side=False)
    argv = next(c.args for c in helm.commands if c.args[:2] == ("helm", "install"))
    assert argv[2] == "chartpub-verify-ledger-api-0-4-1"
    assert "--namespace" in argv
    assert argv[argv.index("--namespace") + 1] == "chartpub-verify-ledger-api-0-4-1-ns"
    assert "--dry-run=client" in argv
    assert "client-side" in report.checks[0].detail


def test_test_install_prefers_server_side_when_a_cluster_answers(
    tmp_path: Path, chart_dir: Path
) -> None:
    helm = FakeHelm(cluster=True)
    report = ValidationReport()
    check_test_install(candidate(tmp_path, chart_dir), report, runner=helm)
    argv = next(c.args for c in helm.commands if c.args[:2] == ("helm", "install"))
    assert "--dry-run=server" in argv
    assert cluster_reachable(runner=helm) is True
    assert cluster_reachable(runner=FakeHelm(cluster=False)) is False


def test_digest_mismatch_short_circuits_before_helm(tmp_path: Path, chart_dir: Path) -> None:
    artifact = candidate(tmp_path, chart_dir)
    tampered = Artifact(
        artifact.path, artifact.name, artifact.version, "0" * 64, artifact.size, artifact.metadata
    )
    helm = FakeHelm()
    report = validate_candidate(tampered, values_files=(), runner=helm)
    assert not report.ok
    assert {c.name for c in report.failures} == {"archive-digest"}
    assert helm.commands == [], "no helm work is done on a corrupt candidate"


def test_archive_contents_rejects_an_unsafe_archive(tmp_path: Path) -> None:
    path = tmp_path / "ledger-api-0.4.1.tgz"
    path.write_bytes(b"definitely not a tarball")
    report = ValidationReport()
    check_archive_contents(
        Artifact(path, "ledger-api", "0.4.1", "0" * 64, 1), report, expected_files=()
    )
    assert not report.ok
    assert "not a readable gzip tar archive" in report.checks[0].detail


def test_archive_contents_rejects_a_missing_member(tmp_path: Path, source_chart: Path) -> None:
    (source_chart / "values.yaml").unlink()
    artifact = package_chart(source_chart, tmp_path / "out", "0.4.1")
    report = ValidationReport()
    check_archive_contents(artifact, report)
    assert not report.ok
    assert "ledger-api/values.yaml" in report.checks[0].detail


def test_archive_contents_rejects_a_version_disagreement(
    tmp_path: Path, source_chart: Path
) -> None:
    artifact = package_chart(source_chart, tmp_path / "out", "0.4.1")
    mislabelled = Artifact(
        artifact.path, artifact.name, "0.9.9", artifact.sha256, artifact.size, artifact.metadata
    )
    report = ValidationReport()
    check_archive_contents(mislabelled, report)
    assert "packaged Chart.yaml declares" in report.checks[0].detail


def test_archive_contents_rejects_a_root_mismatch(tmp_path: Path, source_chart: Path) -> None:
    artifact = package_chart(source_chart, tmp_path / "out", "0.4.1")
    renamed = Artifact(
        artifact.path, "other", artifact.version, artifact.sha256, artifact.size, artifact.metadata
    )
    report = ValidationReport()
    check_archive_contents(renamed, report)
    assert "expected 'other'" in report.checks[0].detail


def test_archive_contents_reports_unreadable_metadata(
    tmp_path: Path, source_chart: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = package_chart(source_chart, tmp_path / "out", "0.4.1")

    def boom(*_args: object, **_kwargs: object) -> object:
        raise ValidationError("metadata unreadable")

    monkeypatch.setattr("chartpub.validate.archive_metadata", boom)
    report = ValidationReport()
    check_archive_contents(artifact, report)
    assert report.checks[0].detail == "metadata unreadable"


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        (DEFAULT_MANIFEST, []),
        ("not: [valid yaml", ["rendered manifest is not valid YAML"]),
        ("kind: Deployment\nspec: 5\n", []),
        (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: d}\nspec: {template: {}}\n",
            ["Deployment/d has no spec.selector.matchLabels"],
        ),
        ("apiVersion: v1\nkind: Service\nmetadata: {name: s}\nspec: {selector: {}}\n", []),
        ("apiVersion: v1\nkind: Service\nmetadata: {name: s}\nspec: 5\n", []),
    ],
)
def test_manifest_problems_cases(manifest: str, expected: list[str]) -> None:
    problems = manifest_problems(manifest)
    assert len(problems) == len(expected)
    for actual, wanted in zip(problems, expected, strict=True):
        assert wanted in actual


def test_manifest_problems_deduplicates_across_fixtures() -> None:
    report = ValidationReport()
    check_manifest_invariants([MISMATCHED_MANIFEST, MISMATCHED_MANIFEST], report)
    assert report.checks[0].detail.count("selector does not match") == 1


def test_statefulset_selector_is_checked() -> None:
    manifest = """
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
spec:
  selector:
    matchLabels:
      role: primary
  template:
    metadata:
      labels:
        role: replica
"""
    assert "StatefulSet/db selector does not match" in manifest_problems(manifest)[0]


def test_report_raise_for_status() -> None:
    report = ValidationReport()
    report.record("ok-check", True, "fine")
    report.raise_for_status()
    report.record("bad-check", False, "why")
    with pytest.raises(ValidationError, match="bad-check: why"):
        report.raise_for_status()


def test_discover_values_fixtures(tmp_path: Path, values_files: tuple[Path, ...]) -> None:
    assert discover_values_fixtures(tmp_path / "absent") == ()
    target = tmp_path / "fixtures"
    target.mkdir()
    for source in values_files:
        shutil.copy(source, target / source.name)
    (target / "unrelated.yaml").write_text("{}", encoding="utf-8")
    assert [p.name for p in discover_values_fixtures(target)] == [
        "values-ha.yaml",
        "values-minimal.yaml",
    ]


def test_default_runner_strips_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret-value-should-not-leak")
    result = default_runner(["env"])
    assert "secret-value-should-not-leak" not in result.stdout


def test_command_result_helpers() -> None:
    result = CommandResult(("helm",), 1, "out", "err")
    assert result.ok is False
    assert result.output == "out\nerr"
