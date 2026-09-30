from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chartpub import gitops
from chartpub.errors import PublicationError, RemoteConflict, UsageError
from chartpub.security import Redactor

from .conftest import REPOSITORY, Remote
from .fakes import TOKEN, git


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "owner/repo"),
        ("https://x-access-token:secretvalue@github.com/owner/repo.git", "owner/repo"),
        ("owner/repo", "owner/repo"),
        ("https://github.com/only-one-part", None),
    ],
)
def test_parse_repository(url: str, expected: str | None) -> None:
    assert gitops.parse_repository(url) == expected


def test_credential_free_url_strips_userinfo() -> None:
    assert (
        gitops.credential_free_url("https://user:pass@github.com/o/r.git")
        == "https://github.com/o/r.git"
    )
    assert gitops.credential_free_url("git@github.com:o/r.git") == "git@github.com:o/r.git"


def test_resolve_target_repository_accepts_a_match(local_clone: Path) -> None:
    repo = gitops.GitRepository(local_clone)
    assert gitops.resolve_target_repository(repo, REPOSITORY) == REPOSITORY
    assert gitops.resolve_target_repository(repo, REPOSITORY.upper()) == REPOSITORY


def test_resolve_target_repository_refuses_a_mismatch(local_clone: Path) -> None:
    repo = gitops.GitRepository(local_clone)
    with pytest.raises(UsageError, match="refusing to mutate"):
        gitops.resolve_target_repository(repo, "someone-else/other-repo")


def test_resolve_target_repository_refuses_an_unparseable_origin(tmp_path: Path) -> None:
    work = tmp_path / "odd"
    work.mkdir()
    git("init", "--quiet", ".", cwd=work)
    git("remote", "add", "origin", "https://example.test/", cwd=work)
    repo = gitops.GitRepository(work)
    with pytest.raises(UsageError, match="cannot determine owner/repository"):
        gitops.resolve_target_repository(repo, REPOSITORY)


def test_origin_url_never_carries_a_credential(tmp_path: Path) -> None:
    work = tmp_path / "creds"
    work.mkdir()
    git("init", "--quiet", ".", cwd=work)
    git("remote", "add", "origin", f"https://x-access-token:{TOKEN}@github.com/o/r.git", cwd=work)
    url = gitops.GitRepository(work).origin_url()
    assert TOKEN not in url
    assert url == "https://github.com/o/r.git"


def test_check_raises_with_redacted_output(tmp_path: Path) -> None:
    repo = gitops.GitRepository(tmp_path, redactor=Redactor([TOKEN]))
    with pytest.raises(PublicationError, match="git rev-list"):
        repo.check("rev-list", "definitely-not-a-ref")


def test_rev_parse_returns_none_for_unknown(tmp_path: Path) -> None:
    work = tmp_path / "empty"
    work.mkdir()
    git("init", "--quiet", ".", cwd=work)
    assert gitops.GitRepository(work).rev_parse("HEAD") is None


def test_askpass_environment_is_ephemeral_and_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-secret-value")
    with gitops.askpass_environment(TOKEN) as env:
        helper = Path(env["GIT_ASKPASS"])
        assert helper.is_file()
        assert stat.S_IMODE(helper.stat().st_mode) == 0o700
        assert stat.S_IMODE(helper.parent.stat().st_mode) == 0o700
        assert env[gitops.ASKPASS_ENV] == TOKEN
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GITHUB_TOKEN" not in env, "the child must not inherit the raw variable"
        assert TOKEN not in helper.read_text(encoding="utf-8"), "the token is never written to disk"
        directory = helper.parent
    assert not directory.exists(), "the helper directory is removed"


def test_askpass_helper_prints_the_token(tmp_path: Path) -> None:
    import subprocess

    with gitops.askpass_environment(TOKEN) as env:
        completed = subprocess.run(
            [env["GIT_ASKPASS"], "Password:"], capture_output=True, text=True, env=env, check=True
        )
    assert completed.stdout == TOKEN


def test_build_snapshot_replaces_the_tree_exactly(tmp_path: Path) -> None:
    work = tmp_path / "tree"
    work.mkdir()
    (work / ".git").mkdir()
    (work / ".git" / "HEAD").write_text("ref: refs/heads/x\n", encoding="utf-8")
    (work / "stale.txt").write_text("old", encoding="utf-8")
    (work / "nested").mkdir()
    (work / "nested" / "old.tgz").write_bytes(b"old")
    gitops.build_snapshot(work, {"index.yaml": b"new", "charts/a.tgz": b"archive"})
    assert sorted(p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()) == [
        ".git/HEAD",
        "charts/a.tgz",
        "index.yaml",
    ]


def test_build_snapshot_removes_dangling_symlinks(tmp_path: Path) -> None:
    work = tmp_path / "tree"
    work.mkdir()
    (work / "link").symlink_to(work / "missing")
    gitops.build_snapshot(work, {"index.yaml": b"new"})
    assert [p.name for p in work.iterdir()] == ["index.yaml"]


def test_build_snapshot_refuses_escaping_members(tmp_path: Path) -> None:
    work = tmp_path / "tree"
    work.mkdir()
    with pytest.raises(UsageError, match="outside the tree"):
        gitops.build_snapshot(work, {"../escape": b"x"})
    with pytest.raises(UsageError, match="outside the tree"):
        gitops.build_snapshot(work, {"/abs": b"x"})


def pages_repo(remote: Remote, files: dict[str, bytes]) -> tuple[gitops.GitRepository, str]:
    tip = remote.seed_pages(files)
    return gitops.GitRepository(remote.root), tip


def test_commit_snapshot_is_deterministic(remote: Remote, tmp_path: Path) -> None:
    repo, tip = pages_repo(remote, {"index.yaml": b"one\n"})
    shas = []
    for _ in range(2):
        with gitops.pages_worktree(repo, remote.url, "gh-pages") as (clone, seen):
            assert seen == tip
            gitops.build_snapshot(clone.path, {"index.yaml": b"two\n"})
            sha = gitops.commit_snapshot(clone, "publish: x", timestamp="2026-01-02T03:04:05+00:00")
            assert sha is not None
            shas.append(sha)
    assert shas[0] == shas[1], "identical content and timestamp give the same commit"


def test_commit_snapshot_accepts_a_datetime(remote: Remote) -> None:
    repo, _ = pages_repo(remote, {"index.yaml": b"one\n"})
    with gitops.pages_worktree(repo, remote.url, "gh-pages") as (clone, _tip):
        gitops.build_snapshot(clone.path, {"index.yaml": b"two\n"})
        sha = gitops.commit_snapshot(
            clone, "m", timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        )
    assert sha is not None


def test_commit_snapshot_returns_none_when_unchanged(remote: Remote) -> None:
    repo, _ = pages_repo(remote, {"index.yaml": b"one\n"})
    with gitops.pages_worktree(repo, remote.url, "gh-pages") as (clone, _tip):
        assert gitops.commit_snapshot(clone, "m", timestamp="2026-01-02T03:04:05+00:00") is None


def test_pages_worktree_creates_an_orphan_branch_when_absent(remote: Remote) -> None:
    repo = gitops.GitRepository(remote.root)
    with gitops.pages_worktree(repo, remote.url, "gh-pages") as (clone, tip):
        assert tip is None
        gitops.build_snapshot(clone.path, {"index.yaml": b"rebuilt\n"})
        assert gitops.commit_snapshot(clone, "repair", timestamp="2026-01-02T03:04:05+00:00")
        gitops.push_branch(clone, remote.url, local_ref="HEAD", branch="gh-pages")
    assert "refs/heads/gh-pages" in remote.refs()


def test_push_with_lease_succeeds_on_the_expected_tip(remote: Remote) -> None:
    repo, tip = pages_repo(remote, {"index.yaml": b"one\n"})
    with gitops.pages_worktree(repo, remote.url, "gh-pages") as (clone, _tip):
        gitops.build_snapshot(clone.path, {"index.yaml": b"two\n"})
        gitops.commit_snapshot(clone, "publish", timestamp="2026-01-02T03:04:05+00:00")
        gitops.push_with_lease(
            clone, remote.url, branch="gh-pages", local_ref="HEAD", expected_old=tip
        )
    assert remote.pages_files()["index.yaml"] == b"two\n"


def test_push_with_lease_refuses_a_stale_expectation(remote: Remote) -> None:
    repo, _tip = pages_repo(remote, {"index.yaml": b"one\n"})
    with gitops.pages_worktree(repo, remote.url, "gh-pages") as (clone, _seen):
        gitops.build_snapshot(clone.path, {"index.yaml": b"two\n"})
        gitops.commit_snapshot(clone, "publish", timestamp="2026-01-02T03:04:05+00:00")
        with pytest.raises(RemoteConflict, match="lease expects"):
            gitops.push_with_lease(
                clone, remote.url, branch="gh-pages", local_ref="HEAD", expected_old="0" * 40
            )
    assert remote.pages_files()["index.yaml"] == b"one\n", "concurrent work survives"


def test_push_with_lease_reports_a_server_side_rejection(
    remote: Remote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease is checked twice: locally, then by the remote itself."""
    repo, tip = pages_repo(remote, {"index.yaml": b"one\n"})
    with gitops.pages_worktree(repo, remote.url, "gh-pages") as (clone, _seen):
        gitops.build_snapshot(clone.path, {"index.yaml": b"two\n"})
        gitops.commit_snapshot(clone, "publish", timestamp="2026-01-02T03:04:05+00:00")
        # Someone else lands work after our pre-check but before the push.
        original = clone.ls_remote

        def moving_target(url: str, pattern: str, **kwargs: object) -> dict[str, str]:
            result = original(url, pattern, **kwargs)  # type: ignore[arg-type]
            seed_other_commit(remote)
            return result

        monkeypatch.setattr(clone, "ls_remote", moving_target)
        with pytest.raises(RemoteConflict, match="rejected by the remote"):
            gitops.push_with_lease(
                clone, remote.url, branch="gh-pages", local_ref="HEAD", expected_old=tip
            )
    assert remote.pages_files()["index.yaml"] == b"concurrent\n"


def seed_other_commit(remote: Remote) -> None:
    work = remote.root / "other"
    work.mkdir(parents=True, exist_ok=True)
    git("init", "--quiet", "--initial-branch", "gh-pages", ".", cwd=work)
    git("fetch", "--quiet", remote.url, "refs/heads/gh-pages", cwd=work)
    git("checkout", "--quiet", "-B", "gh-pages", "FETCH_HEAD", cwd=work)
    (work / "index.yaml").write_bytes(b"concurrent\n")
    git("add", "--all", cwd=work)
    git(
        "-c",
        "user.name=other",
        "-c",
        "user.email=other@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "concurrent work",
        cwd=work,
    )
    git("push", "--quiet", remote.url, "HEAD:refs/heads/gh-pages", cwd=work)


def test_push_with_lease_reports_other_failures(remote: Remote) -> None:
    repo, tip = pages_repo(remote, {"index.yaml": b"one\n"})
    with gitops.pages_worktree(repo, remote.url, "gh-pages") as (clone, _seen):
        gitops.build_snapshot(clone.path, {"index.yaml": b"two\n"})
        gitops.commit_snapshot(clone, "publish", timestamp="2026-01-02T03:04:05+00:00")
        with pytest.raises(PublicationError, match="pushing refs/heads/gh-pages failed"):
            gitops.push_with_lease(
                clone,
                remote.url,
                branch="gh-pages",
                local_ref="refs/heads/no-such-local-ref",
                expected_old=tip,
            )


def test_push_fast_forward_advances_main(remote: Remote, tmp_path: Path) -> None:
    work = tmp_path / "topic"
    work.mkdir()
    git("init", "--quiet", "--initial-branch", "main", ".", cwd=work)
    git("fetch", "--quiet", remote.url, "refs/heads/main", cwd=work)
    git("checkout", "--quiet", "-B", "main", "FETCH_HEAD", cwd=work)
    (work / "new.txt").write_text("x", encoding="utf-8")
    git("add", "--all", cwd=work)
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "advance",
        cwd=work,
    )
    head = git("rev-parse", "HEAD", cwd=work).strip()
    repo = gitops.GitRepository(work)
    gitops.push_fast_forward(
        repo, remote.url, branch="main", sha=head, expected_old=remote.main_tip
    )
    assert remote.refs()["refs/heads/main"] == head


def test_push_fast_forward_refuses_a_rewrite(remote: Remote, tmp_path: Path) -> None:
    work = tmp_path / "divergent"
    work.mkdir()
    git("init", "--quiet", "--initial-branch", "main", ".", cwd=work)
    (work / "unrelated.txt").write_text("x", encoding="utf-8")
    git("add", "--all", cwd=work)
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "unrelated history",
        cwd=work,
    )
    head = git("rev-parse", "HEAD", cwd=work).strip()
    git("fetch", "--quiet", remote.url, "refs/heads/main", cwd=work)
    repo = gitops.GitRepository(work)
    with pytest.raises(RemoteConflict, match="not a descendant"):
        gitops.push_fast_forward(repo, remote.url, branch="main", sha=head)
    assert remote.refs()["refs/heads/main"] == remote.main_tip


def test_push_fast_forward_refuses_an_unexpected_tip(remote: Remote, tmp_path: Path) -> None:
    repo = gitops.GitRepository(remote.root)
    with pytest.raises(RemoteConflict, match="refusing to advance"):
        gitops.push_fast_forward(
            repo, remote.url, branch="main", sha=remote.main_tip, expected_old="0" * 40
        )


def test_push_branch_reports_failure(remote: Remote) -> None:
    repo = gitops.GitRepository(remote.root)
    with pytest.raises(PublicationError, match="pushing topic failed"):
        gitops.push_branch(repo, remote.url, local_ref="refs/heads/absent", branch="topic")


def test_is_ancestor(remote: Remote, tmp_path: Path) -> None:
    work = tmp_path / "anc"
    work.mkdir()
    git("init", "--quiet", "--initial-branch", "main", ".", cwd=work)
    git("fetch", "--quiet", remote.url, "refs/heads/main", cwd=work)
    repo = gitops.GitRepository(work)
    assert repo.is_ancestor(remote.main_tip, remote.main_tip)
    assert not repo.is_ancestor(remote.main_tip, "0" * 40)


def test_default_git_runner_captures_failure(tmp_path: Path) -> None:
    result = gitops.default_git_runner(["status"], tmp_path, dict(os.environ))
    assert result.ok is False
    assert "not a git repository" in result.output
