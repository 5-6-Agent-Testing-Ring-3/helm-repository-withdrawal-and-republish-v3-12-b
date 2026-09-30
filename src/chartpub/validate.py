"""Pre-publication validation of a candidate chart archive.

Everything here runs against the *packaged archive* — not the source tree — so
what is validated is exactly what will be published, and it all runs before any
remote mutation. A failure therefore leaves the public index and the current
stable version untouched by construction.

The staging incident shipped a chart that passed ``helm lint`` and
``helm template`` but that Kubernetes refuses: the Deployment's
``spec.selector`` did not match its pod template labels. Linting and rendering
alone cannot catch that, so :func:`check_manifest_invariants` inspects the
rendered objects, and :func:`check_test_install` installs the archive into an
isolated throwaway release.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from chartpub.archive import archive_metadata, inspect_archive, member_names, require_digest
from chartpub.errors import ValidationError
from chartpub.models import Artifact
from chartpub.security import scrubbed_environment

#: Workload kinds whose pod template labels must satisfy their own selector.
_SELECTOR_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}

CommandRunner = Callable[[Sequence[str]], "CommandResult"]


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ValidationReport:
    checks: list[Check] = field(default_factory=list)

    def record(self, name: str, ok: bool, detail: str = "") -> Check:
        check = Check(name=name, ok=ok, detail=detail)
        self.checks.append(check)
        return check

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.ok)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [check.to_dict() for check in self.checks]}

    def raise_for_status(self) -> None:
        if self.ok:
            return
        detail = "; ".join(f"{check.name}: {check.detail}" for check in self.failures)
        raise ValidationError(f"candidate rejected by validation ({detail})")


def default_runner(args: Sequence[str]) -> CommandResult:
    """Run a command with credentials stripped from its environment."""
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(args),
        capture_output=True,
        text=True,
        env=scrubbed_environment(),
        check=False,
    )
    return CommandResult(
        args=tuple(args),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def helm_available() -> bool:
    return shutil.which("helm") is not None


def _release_name(chart: str, version: str) -> str:
    """A deterministic, DNS-safe release name for the throwaway install."""
    slug = version.replace(".", "-").replace("+", "-").replace("_", "-").lower()
    return f"chartpub-verify-{chart}-{slug}"[:53].rstrip("-")


def check_archive_contents(
    artifact: Artifact, report: ValidationReport, *, expected_files: Iterable[str] = ()
) -> None:
    """Archive members are safe, unique, and describe the expected chart."""
    try:
        members = inspect_archive(artifact.path, expected_root=artifact.name)
    except ValidationError as exc:
        report.record("archive-contents", False, str(exc))
        return
    names = set(member_names(members))
    required = {f"{artifact.name}/Chart.yaml", f"{artifact.name}/values.yaml", *expected_files}
    missing = sorted(required - names)
    if missing:
        report.record("archive-contents", False, f"missing member(s): {', '.join(missing)}")
        return
    try:
        metadata = archive_metadata(artifact.path, expected_root=artifact.name)
    except ValidationError as exc:
        report.record("archive-contents", False, str(exc))
        return
    if metadata.version != artifact.version:
        report.record(
            "archive-contents",
            False,
            f"packaged Chart.yaml declares {metadata.version!r}, expected {artifact.version!r}",
        )
        return
    report.record("archive-contents", True, f"{len(members)} safe member(s)")


def check_digest(artifact: Artifact, report: ValidationReport) -> None:
    try:
        require_digest(artifact.path, artifact.sha256, what="candidate")
    except ValidationError as exc:
        report.record("archive-digest", False, str(exc))
        return
    report.record("archive-digest", True, f"sha256={artifact.sha256}")


def check_lint(artifact: Artifact, report: ValidationReport, *, runner: CommandRunner) -> None:
    result = runner(["helm", "lint", "--strict", str(artifact.path)])
    report.record(
        "helm-lint",
        result.ok,
        "clean" if result.ok else result.output[-2000:],
    )


def _render(artifact: Artifact, values: Path | None, *, runner: CommandRunner) -> tuple[bool, str]:
    args = [
        "helm",
        "template",
        _release_name(artifact.name, artifact.version),
        str(artifact.path),
    ]
    if values is not None:
        args += ["-f", str(values)]
    result = runner(args)
    return result.ok, result.stdout if result.ok else result.output[-2000:]


def discover_values_fixtures(values_dir: Path) -> tuple[Path, ...]:
    if not values_dir.is_dir():
        return ()
    return tuple(sorted(values_dir.glob("values-*.yaml")))


def check_renders(
    artifact: Artifact,
    report: ValidationReport,
    *,
    values_files: Sequence[Path],
    runner: CommandRunner,
) -> list[str]:
    """Render default values plus every supplied fixture; return manifests."""
    rendered: list[str] = []
    ok, output = _render(artifact, None, runner=runner)
    report.record("helm-template[default]", ok, "rendered" if ok else output)
    if ok:
        rendered.append(output)
    for values in values_files:
        ok, output = _render(artifact, values, runner=runner)
        report.record(f"helm-template[{values.name}]", ok, "rendered" if ok else output)
        if ok:
            rendered.append(output)
    if not values_files:
        report.record(
            "helm-template[fixtures]",
            False,
            "no values fixtures found; refusing to publish an unexercised chart",
        )
    return rendered


def _labels(node: Any) -> dict[str, str]:
    if not isinstance(node, dict):
        return {}
    labels = node.get("labels")
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


def manifest_problems(manifest: str) -> list[str]:
    """Structural problems Kubernetes would reject but Helm happily renders."""
    problems: list[str] = []
    try:
        documents = [doc for doc in yaml.safe_load_all(manifest) if isinstance(doc, dict)]
    except yaml.YAMLError as exc:
        return [f"rendered manifest is not valid YAML: {exc}"]

    pod_label_sets: list[tuple[str, dict[str, str]]] = []
    for doc in documents:
        kind = str(doc.get("kind", ""))
        name = str((doc.get("metadata") or {}).get("name", "<unnamed>"))
        spec = doc.get("spec")
        if not isinstance(spec, dict):
            continue
        if kind in _SELECTOR_KINDS:
            raw_template = spec.get("template")
            template: dict[str, Any] = raw_template if isinstance(raw_template, dict) else {}
            template_labels = _labels(template.get("metadata"))
            pod_label_sets.append((f"{kind}/{name}", template_labels))
            selector = spec.get("selector")
            match_labels = (
                {str(k): str(v) for k, v in selector["matchLabels"].items()}
                if isinstance(selector, dict) and isinstance(selector.get("matchLabels"), dict)
                else {}
            )
            if not match_labels:
                problems.append(f"{kind}/{name} has no spec.selector.matchLabels")
                continue
            mismatched = {
                key: value
                for key, value in match_labels.items()
                if template_labels.get(key) != value
            }
            if mismatched:
                problems.append(
                    f"{kind}/{name} selector does not match template labels: "
                    + ", ".join(
                        f"{key}={value!r} but template has {template_labels.get(key, '<absent>')!r}"
                        for key, value in sorted(mismatched.items())
                    )
                )

    for doc in documents:
        if str(doc.get("kind", "")) != "Service":
            continue
        spec = doc.get("spec")
        if not isinstance(spec, dict):
            continue
        selector = spec.get("selector")
        if not isinstance(selector, dict) or not selector:
            continue
        wanted = {str(key): str(value) for key, value in selector.items()}
        name = str((doc.get("metadata") or {}).get("name", "<unnamed>"))
        if pod_label_sets and not any(
            all(labels.get(key) == value for key, value in wanted.items())
            for _, labels in pod_label_sets
        ):
            problems.append(
                f"Service/{name} selector {wanted} matches no pod template in the chart"
            )
    return problems


def check_manifest_invariants(manifests: Sequence[str], report: ValidationReport) -> None:
    if not manifests:
        # Nothing rendered, so this check cannot vouch for anything. Passing it
        # vacuously would let a render failure look like a partial success.
        report.record("manifest-invariants", False, "no rendered manifests to inspect")
        return
    problems: list[str] = []
    for manifest in manifests:
        for problem in manifest_problems(manifest):
            if problem not in problems:
                problems.append(problem)
    report.record(
        "manifest-invariants",
        not problems,
        "consistent" if not problems else "; ".join(problems),
    )


def cluster_reachable(*, runner: CommandRunner) -> bool:
    result = runner(["kubectl", "version", "--request-timeout=10s", "-o", "json"])
    return result.ok


def check_test_install(
    artifact: Artifact,
    report: ValidationReport,
    *,
    runner: CommandRunner,
    values_files: Sequence[Path] = (),
    namespace: str | None = None,
    server_side: bool | None = None,
) -> None:
    """Install the packaged archive as an isolated, throwaway test release.

    When a cluster is reachable the install is validated server-side, which is
    the strongest check available; otherwise it falls back to a client-side
    install rehearsal and says so. Either way the release name and namespace
    are chartpub-specific and nothing outside them is touched.
    """
    release = _release_name(artifact.name, artifact.version)
    mode = cluster_reachable(runner=runner) if server_side is None else server_side
    args = [
        "helm",
        "install",
        release,
        str(artifact.path),
        "--dry-run=server" if mode else "--dry-run=client",
        "--namespace",
        namespace or f"{release}-ns",
    ]
    for values in values_files:
        args += ["-f", str(values)]
    result = runner(args)
    where = "server-side" if mode else "client-side (no cluster reachable)"
    report.record(
        "test-install",
        result.ok,
        f"{where} install of {release} succeeded" if result.ok else result.output[-2000:],
    )


def validate_candidate(
    artifact: Artifact,
    *,
    values_files: Sequence[Path],
    runner: CommandRunner | None = None,
    namespace: str | None = None,
    server_side: bool | None = None,
) -> ValidationReport:
    """Run the full gate. Never mutates anything remote."""
    run = runner or default_runner
    report = ValidationReport()
    check_archive_contents(artifact, report)
    check_digest(artifact, report)
    if not report.ok:
        # Rendering or installing a malformed archive only produces noise.
        return report
    check_lint(artifact, report, runner=run)
    manifests = check_renders(artifact, report, values_files=values_files, runner=run)
    check_manifest_invariants(manifests, report)
    check_test_install(
        artifact,
        report,
        runner=run,
        values_files=values_files,
        namespace=namespace,
        server_side=server_side,
    )
    return report
