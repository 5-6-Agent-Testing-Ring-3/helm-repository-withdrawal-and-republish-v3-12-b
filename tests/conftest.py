from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chartpub import gitops
from chartpub.archive import package_chart, sha256_bytes
from chartpub.github import GitHubClient
from chartpub.index import add_artifact, dump_index, empty_index
from chartpub.lifecycle import Session
from chartpub.models import PublicationContract
from chartpub.security import Redactor
from chartpub.state import Journal
from chartpub.validate import CommandRunner

from .fakes import TOKEN, FakeGitHub, FakeHelm, git, init_bare, seed_branch

REPOSITORY = "test-owner/test-repo"
PAGES_URL = "https://test-owner.github.io/test-repo"
CREATED = "2026-01-02T03:04:05Z"


@pytest.fixture(autouse=True)
def _forbid_real_credentials_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard stop: no test may read the operator credential file or open a socket."""
    real_open = Path.open

    def guarded_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if "agent-eval" in str(self):
            raise AssertionError(f"test attempted to read the real credential file: {self}")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    # Point the CLI's default credential path somewhere that cannot exist, so a
    # command invoked without --credentials behaves as it would on a machine
    # that has no operator credential installed.
    monkeypatch.setattr(
        "chartpub.cli.DEFAULT_CREDENTIAL_FILE", "/nonexistent/chartpub-test-credentials.env"
    )

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("tests must not perform network I/O")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr("chartpub.github.urllib_transport", forbidden)


@pytest.fixture
def chart_dir() -> Path:
    return Path(__file__).parents[1] / "charts" / "ledger-api"


@pytest.fixture
def values_files() -> tuple[Path, ...]:
    return tuple(sorted((Path(__file__).parent / "fixtures").glob("values-*.yaml")))


def contract_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "source_branch": "main",
        "pages_branch": "gh-pages",
        "pages_url": PAGES_URL,
        "chart": "ledger-api",
        "bad_version": "0.4.0",
        "replacement_version": "0.4.1",
        "bad_tag": "chart-v0.4.0",
        "replacement_tag": "chart-v0.4.1",
        "expected_bad_tag_target": "a" * 40,
        "expected_pages_tip": "b" * 40,
        "release_asset_name": "ledger-api-{version}.tgz",
    }
    value.update(overrides)
    return value


def build_index(pages_url: str, *artifacts: object, created: str = CREATED) -> bytes:
    index = empty_index()
    for artifact in artifacts:
        add_artifact(index, artifact, pages_url, created=created)  # type: ignore[arg-type]
    return dump_index(index).encode("utf-8")


@pytest.fixture
def source_chart(tmp_path: Path, chart_dir: Path) -> Path:
    """A writable copy of the real chart, so tests can perturb it safely."""
    import shutil

    target = tmp_path / "charts" / "ledger-api"
    shutil.copytree(chart_dir, target)
    return target


class Remote:
    """A temporary bare repository plus the fake GitHub API over it."""

    def __init__(self, root: Path, repository: str = REPOSITORY) -> None:
        self.root = root
        self.bare = init_bare(root / "remote.git")
        self.url = f"file://{self.bare}"
        self.api = FakeGitHub(repository, self.bare)
        self.main_tip = seed_branch(
            self.bare, root / "seed-main", "main", {"README.md": b"source\n"}
        )
        self.pages_tip: str | None = None
        self._seeds = 0

    def seed_pages(self, files: dict[str, bytes], branch: str = "gh-pages") -> str:
        """(Re)write ``branch`` from scratch, as if some other actor had pushed."""
        self._seeds += 1
        work = self.root / f"seed-{branch}-{self._seeds}"
        self.pages_tip = seed_branch(self.bare, work, branch, files)
        return self.pages_tip

    def refs(self) -> dict[str, str]:
        return self.api.refs()

    def pages_files(self, branch: str = "gh-pages") -> dict[str, bytes]:
        tip = self.refs().get(f"refs/heads/{branch}")
        if tip is None:
            return {}
        result: dict[str, bytes] = {}
        for entry in self.api.tree(tip):
            payload = self.api.blob(tip, str(entry["path"]))
            if payload is not None:
                result[str(entry["path"])] = payload
        return result


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    return Remote(tmp_path / "remote-root")


@pytest.fixture
def local_clone(tmp_path: Path, remote: Remote) -> Path:
    """A working checkout whose ``origin`` is the temporary bare repository."""
    work = tmp_path / "checkout"
    work.mkdir()
    git("init", "--quiet", "--initial-branch", "main", ".", cwd=work)
    git("remote", "add", "origin", f"https://github.com/{REPOSITORY}.git", cwd=work)
    return work


def make_session(
    *,
    tmp_path: Path,
    remote: Remote,
    local_clone: Path,
    chart_dir: Path,
    values_files: tuple[Path, ...] = (),
    contract_overrides: dict[str, object] | None = None,
    helm_runner: CommandRunner | None = None,
    dry_run: bool = False,
    force: bool = True,
    created: str = CREATED,
    expect_pages_tip: str | None = None,
) -> Session:
    overrides = dict(contract_overrides or {})
    overrides.setdefault("expected_pages_tip", remote.pages_tip or "b" * 40)
    contract = PublicationContract.from_mapping(contract_mapping(**overrides))
    redactor = Redactor([TOKEN])
    repo = gitops.GitRepository(local_clone, redactor=redactor)
    client = GitHubClient(
        contract.repository, TOKEN, transport=remote.api, redactor=redactor, sleep=lambda _s: None
    )
    return Session(
        contract=contract,
        client=client,
        repo=repo,
        remote_url=remote.url,
        git_env=None,
        redactor=redactor,
        journal=Journal(tmp_path / "state"),
        chart_dir=chart_dir,
        values_files=values_files,
        helm_runner=helm_runner or FakeHelm(),
        dry_run=dry_run,
        force=force,
        created=created,
        expect_pages_tip=expect_pages_tip,
        server_side_install=False,
        work_dir=tmp_path / "build",
    )


@pytest.fixture
def session_factory(
    tmp_path: Path, remote: Remote, local_clone: Path, source_chart: Path
) -> Iterator[object]:
    def factory(**kwargs: object) -> Session:
        params: dict[str, object] = {
            "tmp_path": tmp_path,
            "remote": remote,
            "local_clone": local_clone,
            "chart_dir": source_chart,
        }
        params.update(kwargs)
        return make_session(**params)  # type: ignore[arg-type]

    yield factory


@pytest.fixture
def published_pages(remote: Remote, source_chart: Path, tmp_path: Path) -> dict[str, bytes]:
    """A Pages branch advertising the bad 0.4.0 chart, like the real incident."""
    bad = tmp_path / "bad-chart" / "ledger-api"
    bad.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copytree(source_chart, bad)
    chart_yaml = (bad / "Chart.yaml").read_text(encoding="utf-8")
    (bad / "Chart.yaml").write_text(chart_yaml.replace("0.4.1", "0.4.0"), encoding="utf-8")
    artifact = package_chart(bad, tmp_path / "bad-out", "0.4.0")
    payload = artifact.path.read_bytes()
    files = {
        "README.md": b"# charts\n",
        "index.yaml": build_index(PAGES_URL, artifact),
        artifact.path.name: payload,
    }
    remote.seed_pages(files)
    release = remote.api.add_release(tag_name="chart-v0.4.0", name="ledger-api 0.4.0", draft=False)
    remote.api.add_asset(release, "ledger-api-0.4.0.tgz", payload)
    remote.api.set_ref("refs/tags/chart-v0.4.0", remote.main_tip)
    return files


@pytest.fixture
def credential_file(tmp_path: Path) -> Path:
    path = tmp_path / "creds.env"
    path.write_text(f"GITHUB_TOKEN={TOKEN}\nGITHUB_REPOSITORY={REPOSITORY}\n", encoding="utf-8")
    return path


@pytest.fixture
def contract_file(tmp_path: Path, remote: Remote) -> Path:
    path = tmp_path / "publication-contract.json"
    path.write_text(
        json.dumps(contract_mapping(expected_pages_tip=remote.pages_tip or "b" * 40)),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def in_clone(local_clone: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(local_clone)
    return local_clone


def digest_of(payload: bytes) -> str:
    return sha256_bytes(payload)


def env_without_git_identity() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
