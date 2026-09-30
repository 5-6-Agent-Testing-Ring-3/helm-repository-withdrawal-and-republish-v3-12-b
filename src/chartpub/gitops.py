"""Git plumbing for the Pages snapshot and the source branch.

The Pages branch is updated with ``git push --force-with-lease=<ref>:<old>``
rather than a REST ref update. The lease is a server-side compare-and-swap:
if anything else advanced ``gh-pages`` between inspection and push, the push is
rejected instead of silently discarding that work. An unguarded ``--force`` is
never used.

Credentials never touch the repository, the remote URL, or Git configuration.
The token is handed to ``git`` through a short-lived ``GIT_ASKPASS`` helper in a
0700 temporary directory, reading it from the child process environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chartpub.errors import PublicationError, RemoteConflict, UsageError
from chartpub.security import Redactor, scrubbed_environment

#: Environment variable the askpass helper reads. Not a secret name that any
#: other tool looks for, so it cannot leak through an unrelated integration.
ASKPASS_ENV = "CHARTPUB_GIT_PASSWORD"

ASKPASS_SCRIPT = f"""#!/bin/sh
# chartpub credential helper: prints the token supplied via the environment.
printf '%s' "${ASKPASS_ENV}"
"""

#: Fixed identity so a rebuilt Pages snapshot is byte-reproducible.
SNAPSHOT_AUTHOR_NAME = "chartpub"
SNAPSHOT_AUTHOR_EMAIL = "chartpub@users.noreply.github.com"


@dataclass(frozen=True)
class GitResult:
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


GitRunner = Callable[[Sequence[str], Path | None, Mapping[str, str] | None], GitResult]


def default_git_runner(
    args: Sequence[str], cwd: Path | None, env: Mapping[str, str] | None
) -> GitResult:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        env=None if env is None else dict(env),
        check=False,
    )
    return GitResult(
        args=tuple(args),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def credential_free_url(url: str) -> str:
    """Strip any ``user[:pass]@`` prefix from a remote URL."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    return f"{scheme}://{rest}"


def parse_repository(url: str) -> str | None:
    """Extract ``owner/name`` from an https or ssh GitHub remote URL."""
    cleaned = credential_free_url(url.strip())
    if cleaned.startswith("git@"):
        _, _, path = cleaned.partition(":")
    elif "://" in cleaned:
        path = cleaned.split("://", 1)[1].partition("/")[2]
    else:
        path = cleaned
    path = path.strip("/").removesuffix(".git")
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


@contextmanager
def askpass_environment(token: str) -> Iterator[dict[str, str]]:
    """Yield an environment that lets ``git`` authenticate without persistence."""
    directory = Path(tempfile.mkdtemp(prefix="chartpub-askpass-"))
    try:
        os.chmod(directory, 0o700)
        helper = directory / "askpass.sh"
        helper.write_text(ASKPASS_SCRIPT, encoding="utf-8")
        helper.chmod(0o700)
        env = scrubbed_environment()
        env.update(
            {
                ASKPASS_ENV: token,
                "GIT_ASKPASS": str(helper),
                "GIT_TERMINAL_PROMPT": "0",
                # Ignore any ambient credential helper or global config that
                # might otherwise persist the credential.
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(directory / "gitconfig"),
            }
        )
        (directory / "gitconfig").write_text("", encoding="utf-8")
        yield env
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class GitRepository:
    """Thin wrapper around ``git`` in one working directory."""

    def __init__(
        self,
        path: Path,
        *,
        runner: GitRunner | None = None,
        redactor: Redactor | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.path = path
        self.runner = runner or default_git_runner
        self.redact = redactor or Redactor()
        self.env = dict(env) if env is not None else None

    def run(self, *args: str, env: Mapping[str, str] | None = None) -> GitResult:
        return self.runner(args, self.path, env if env is not None else self.env)

    def check(self, *args: str, env: Mapping[str, str] | None = None) -> GitResult:
        result = self.run(*args, env=env)
        if not result.ok:
            raise PublicationError(
                self.redact(f"git {' '.join(args)} failed ({result.returncode}): {result.output}")
            )
        return result

    def rev_parse(self, revision: str) -> str | None:
        result = self.run("rev-parse", "--verify", "--quiet", revision)
        return result.stdout.strip() or None

    def origin_url(self) -> str:
        result = self.check("remote", "get-url", "origin")
        return credential_free_url(result.stdout.strip())

    def ls_remote(
        self, url: str, pattern: str, *, env: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        result = self.check("ls-remote", url, pattern, env=env)
        refs: dict[str, str] = {}
        for line in result.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if sha and ref:
                refs[ref.strip()] = sha.strip()
        return refs

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.run("merge-base", "--is-ancestor", ancestor, descendant).ok


def resolve_target_repository(repo: GitRepository, configured: str) -> str:
    """Refuse to operate when the origin URL disagrees with the contract.

    This is the guard that keeps a misconfigured checkout from mutating some
    other repository that the token happens to be able to write.
    """
    url = repo.origin_url()
    resolved = parse_repository(url)
    if resolved is None:
        raise UsageError(f"cannot determine owner/repository from origin URL: {url}")
    if resolved.lower() != configured.lower():
        raise UsageError(
            "refusing to mutate: git origin resolves to "
            f"{resolved!r} but the publication contract targets {configured!r}"
        )
    return resolved


def build_snapshot(work_dir: Path, files: Mapping[str, bytes]) -> None:
    """Replace the working tree with exactly ``files``.

    ``.git`` is preserved; every other tracked or untracked path is removed so
    the resulting commit is a pure function of ``files``.
    """
    for entry in sorted(work_dir.iterdir()):
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for name in sorted(files):
        if name.startswith("/") or ".." in Path(name).parts:
            raise UsageError(f"refusing to write snapshot member outside the tree: {name}")
        target = work_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[name])


def commit_snapshot(
    repo: GitRepository,
    message: str,
    *,
    timestamp: datetime | str,
) -> str | None:
    """Commit the current tree deterministically; None when nothing changed."""
    repo.check("add", "--all")
    if repo.run("diff", "--cached", "--quiet").ok:
        return None
    stamp = (
        timestamp
        if isinstance(timestamp, str)
        else timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )
    env = dict(repo.env or scrubbed_environment())
    env.update(
        {
            "GIT_AUTHOR_NAME": SNAPSHOT_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": SNAPSHOT_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": SNAPSHOT_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": SNAPSHOT_AUTHOR_EMAIL,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
    )
    repo.check("commit", "--no-gpg-sign", "-m", message, env=env)
    return repo.rev_parse("HEAD")


def push_with_lease(
    repo: GitRepository,
    url: str,
    *,
    branch: str,
    local_ref: str,
    expected_old: str,
    env: Mapping[str, str] | None = None,
) -> GitResult:
    """Push ``local_ref`` to ``branch`` with a lease on ``expected_old``.

    The lease makes the update a compare-and-swap on the server: a concurrent
    update fails the push instead of being overwritten.
    """
    remote_refs = repo.ls_remote(url, f"refs/heads/{branch}", env=env)
    actual = remote_refs.get(f"refs/heads/{branch}")
    if actual != expected_old:
        raise RemoteConflict(
            f"refs/heads/{branch} is at {actual or '<absent>'} but the lease expects "
            f"{expected_old}; refusing to push"
        )
    result = repo.run(
        "push",
        f"--force-with-lease=refs/heads/{branch}:{expected_old}",
        url,
        f"{local_ref}:refs/heads/{branch}",
        env=env,
    )
    if not result.ok:
        message = repo.redact(result.output)
        if "stale info" in message or "fetch first" in message or "non-fast-forward" in message:
            raise RemoteConflict(
                f"lease on refs/heads/{branch} was rejected by the remote: {message}"
            )
        raise PublicationError(f"pushing refs/heads/{branch} failed: {message}")
    return result


def push_fast_forward(
    repo: GitRepository,
    url: str,
    *,
    branch: str,
    sha: str,
    expected_old: str | None = None,
    env: Mapping[str, str] | None = None,
) -> GitResult:
    """Advance ``branch`` to ``sha`` without force.

    The remote rejects the push unless it is a fast-forward, so history on the
    branch can never be rewritten by this call.
    """
    remote_refs = repo.ls_remote(url, f"refs/heads/{branch}", env=env)
    actual = remote_refs.get(f"refs/heads/{branch}")
    if expected_old is not None and actual != expected_old:
        raise RemoteConflict(
            f"refs/heads/{branch} is at {actual or '<absent>'} but {expected_old} was expected; "
            "refusing to advance it"
        )
    if actual is not None and not repo.is_ancestor(actual, sha):
        raise RemoteConflict(
            f"{sha} is not a descendant of refs/heads/{branch} ({actual}); "
            "refusing a non-fast-forward update"
        )
    result = repo.run("push", url, f"{sha}:refs/heads/{branch}", env=env)
    if not result.ok:
        raise PublicationError(repo.redact(f"pushing refs/heads/{branch} failed: {result.output}"))
    return result


def push_branch(
    repo: GitRepository,
    url: str,
    *,
    local_ref: str,
    branch: str,
    env: Mapping[str, str] | None = None,
) -> GitResult:
    """Create or fast-forward a topic branch. Never forces."""
    result = repo.run("push", url, f"{local_ref}:refs/heads/{branch}", env=env)
    if not result.ok:
        raise PublicationError(repo.redact(f"pushing {branch} failed: {result.output}"))
    return result


@contextmanager
def pages_worktree(
    repo: GitRepository,
    url: str,
    branch: str,
    *,
    env: Mapping[str, str] | None = None,
    redactor: Redactor | None = None,
) -> Iterator[tuple[GitRepository, str | None]]:
    """Check out ``branch`` from ``url`` into a temp clone; yield it and its tip.

    An orphan branch is created when ``branch`` does not exist yet, which is how
    ``repair`` reconstructs a deleted Pages branch.
    """
    directory = Path(tempfile.mkdtemp(prefix="chartpub-pages-"))
    try:
        remote_refs = repo.ls_remote(url, f"refs/heads/{branch}", env=env)
        tip = remote_refs.get(f"refs/heads/{branch}")
        clone = GitRepository(
            directory, runner=repo.runner, redactor=redactor or repo.redact, env=env
        )
        clone.check("init", "--quiet", "--initial-branch", branch, ".")
        clone.check("config", "commit.gpgsign", "false")
        if tip is not None:
            clone.check("fetch", "--quiet", "--depth", "1", url, f"refs/heads/{branch}")
            clone.check("checkout", "--quiet", "-B", branch, "FETCH_HEAD")
        yield clone, tip
    finally:
        shutil.rmtree(directory, ignore_errors=True)
