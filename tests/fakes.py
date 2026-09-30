"""Deterministic fakes: an in-memory GitHub API over a real temporary repo.

The fake serves the Git-data and contents endpoints out of a genuine bare
repository, so ``git push --force-with-lease`` and ``git fetch`` in the tests
exercise real Git semantics while releases and assets stay in memory. No test
opens a socket, reads the operator credential file, or touches the real
repository.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chartpub.github import Response

TOKEN = "ghp_faketokenforunittests000000000000"


def git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def init_bare(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "--bare", "--quiet", "--initial-branch", "main", str(path))
    return path


def seed_branch(bare: Path, work: Path, branch: str, files: Mapping[str, bytes]) -> str:
    """Create or replace ``branch`` in ``bare`` so it holds exactly ``files``."""
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    git("init", "--quiet", "--initial-branch", branch, ".", cwd=work)
    for name, payload in sorted(files.items()):
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    git("add", "--all", cwd=work)
    git(
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        f"seed {branch}",
        cwd=work,
    )
    git("push", "--quiet", "--force", f"file://{bare}", f"HEAD:refs/heads/{branch}", cwd=work)
    return git("rev-parse", "HEAD", cwd=work).strip()


@dataclass
class FakeAsset:
    id: int
    name: str
    payload: bytes
    state: str = "uploaded"

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": len(self.payload),
            "state": self.state,
        }


@dataclass
class FakeRelease:
    id: int
    tag_name: str
    name: str
    body: str = ""
    draft: bool = True
    prerelease: bool = False
    target_commitish: str = "main"
    assets: list[FakeAsset] = field(default_factory=list)

    def asset_names(self) -> list[str]:
        return [asset.name for asset in self.assets]

    def to_api(self, repository: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "tag_name": self.tag_name,
            "name": self.name,
            "body": self.body,
            "draft": self.draft,
            "prerelease": self.prerelease,
            "target_commitish": self.target_commitish,
            "html_url": f"https://github.com/{repository}/releases/tag/{self.tag_name}",
            "assets": [asset.to_api() for asset in self.assets],
        }


class FakeGitHub:
    """A transport for :class:`chartpub.github.GitHubClient`."""

    def __init__(self, repository: str, bare: Path, *, per_page: int = 100) -> None:
        self.repository = repository
        self.bare = bare
        self.per_page = per_page
        self.releases: list[FakeRelease] = []
        self.calls: list[tuple[str, str]] = []
        self.failures: dict[tuple[str, str], list[int]] = {}
        self.corrupt_downloads: set[int] = set()
        self._next_id = 1000

    # ------------------------------------------------------------- utilities

    def allocate(self) -> int:
        self._next_id += 1
        return self._next_id

    def add_release(self, **kwargs: Any) -> FakeRelease:
        release = FakeRelease(id=self.allocate(), **kwargs)
        self.releases.append(release)
        return release

    def add_asset(self, release: FakeRelease, name: str, payload: bytes) -> FakeAsset:
        asset = FakeAsset(id=self.allocate(), name=name, payload=payload)
        release.assets.append(asset)
        return asset

    def fail_next(self, method: str, path_fragment: str, status: int, times: int = 1) -> None:
        """Make the next ``times`` matching requests fail with ``status``."""
        self.failures.setdefault((method, path_fragment), []).extend([status] * times)

    def find_release(self, release_id: int) -> FakeRelease | None:
        return next((item for item in self.releases if item.id == release_id), None)

    def find_asset(self, asset_id: int) -> tuple[FakeRelease, FakeAsset] | None:
        for release in self.releases:
            for asset in release.assets:
                if asset.id == asset_id:
                    return release, asset
        return None

    # ------------------------------------------------------------- git state

    def refs(self, prefix: str = "") -> dict[str, str]:
        output = git(
            "--git-dir", str(self.bare), "for-each-ref", "--format=%(refname) %(objectname)"
        )
        refs: dict[str, str] = {}
        for line in output.splitlines():
            ref, _, sha = line.partition(" ")
            if ref.startswith(prefix):
                refs[ref] = sha
        return refs

    def tree(self, sha: str) -> list[dict[str, Any]]:
        output = git("--git-dir", str(self.bare), "ls-tree", "-r", sha)
        entries = []
        for line in output.splitlines():
            meta, _, path = line.partition("\t")
            mode, kind, blob = meta.split()
            entries.append(
                {
                    "path": path,
                    "type": "blob" if kind == "blob" else kind,
                    "sha": blob,
                    "mode": mode,
                }
            )
        return entries

    def blob(self, ref: str, path: str) -> bytes | None:
        try:
            completed = subprocess.run(
                ["git", "--git-dir", str(self.bare), "show", f"{ref}:{path}"],
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            return None
        return completed.stdout

    def delete_ref(self, ref: str) -> None:
        git("--git-dir", str(self.bare), "update-ref", "-d", ref)

    def set_ref(self, ref: str, sha: str) -> None:
        git("--git-dir", str(self.bare), "update-ref", ref, sha)

    # -------------------------------------------------------------- dispatch

    def __call__(self, request: urllib.request.Request) -> Response:
        method = request.get_method()
        parsed = urllib.parse.urlsplit(request.full_url)
        path = parsed.path
        url = request.full_url
        query = urllib.parse.parse_qs(parsed.query)
        self.calls.append((method, path))

        for (want_method, fragment), statuses in self.failures.items():
            if want_method == method and fragment in path and statuses:
                raise self._error(url, statuses.pop(0), "injected failure")

        raw = request.data
        body = raw if isinstance(raw, bytes) else None
        payload: dict[str, Any] = {}
        if body and request.get_header("Content-type", "").startswith("application/json"):
            payload = json.loads(body.decode("utf-8"))

        prefix = f"/repos/{self.repository}"
        if not path.startswith(prefix):
            raise self._error(url, 404, f"no fake route for {method} {path}")
        tail = path[len(prefix) :]

        if tail == "/releases":
            if method == "GET":
                return self._list_releases(query)
            if method == "POST":
                return self._create_release(payload)
        elif tail.startswith("/releases/assets/"):
            asset_id = int(tail.rsplit("/", 1)[1])
            if method == "GET":
                return self._download_asset(asset_id, url)
            if method == "DELETE":
                return self._delete_asset(asset_id)
        elif tail.startswith("/releases/") and tail.endswith("/assets"):
            release_id = int(tail.split("/")[2])
            if method == "GET":
                return self._list_assets(release_id)
            if method == "POST":
                return self._upload_asset(release_id, query, body, url)
        elif tail.startswith("/releases/"):
            release_id = int(tail.rsplit("/", 1)[1])
            if method == "GET":
                return self._get_release(release_id, url)
            if method == "PATCH":
                return self._patch_release(release_id, payload, url)
            if method == "DELETE":
                return self._delete_release(release_id, url)
        elif tail.startswith("/git/ref/") and method == "GET":
            return self._get_ref(tail[len("/git/ref/") :], url)
        elif tail.startswith("/git/matching-refs/") and method == "GET":
            return self._matching_refs(tail[len("/git/matching-refs/") :])
        elif tail == "/git/refs" and method == "POST":
            return self._create_ref(payload, url)
        elif tail.startswith("/git/refs/") and method == "DELETE":
            return self._delete_ref_route(tail[len("/git/refs/") :], url)
        elif tail.startswith("/git/trees/") and method == "GET":
            return self._get_tree(tail[len("/git/trees/") :], url)
        elif tail.startswith("/contents/") and method == "GET":
            return self._get_contents(tail[len("/contents/") :], query, url)

        raise self._error(url, 404, f"no fake route for {method} {path}")

    # -------------------------------------------------------------- handlers

    def _json(
        self, value: Any, *, status: int = 200, headers: dict[str, str] | None = None
    ) -> Response:
        raw = json.dumps(value).encode("utf-8")
        return Response(status=status, body=None, headers=headers or {}, raw=raw)

    def _error(self, url: str, status: int, message: str) -> urllib.error.HTTPError:
        detail = json.dumps({"message": message}).encode("utf-8")
        return urllib.error.HTTPError(url, status, message, {}, io.BytesIO(detail))  # type: ignore[arg-type]

    def _list_releases(self, query: dict[str, list[str]]) -> Response:
        per_page = int(query.get("per_page", [str(self.per_page)])[0])
        page = int(query.get("page", ["1"])[0])
        start = (page - 1) * per_page
        chunk = self.releases[start : start + per_page]
        headers: dict[str, str] = {}
        if start + per_page < len(self.releases):
            base = f"https://api.github.com/repos/{self.repository}/releases"
            headers["link"] = f'<{base}?per_page={per_page}&page={page + 1}>; rel="next"'
        return self._json([item.to_api(self.repository) for item in chunk], headers=headers)

    def _create_release(self, payload: dict[str, Any]) -> Response:
        release = self.add_release(
            tag_name=str(payload["tag_name"]),
            name=str(payload.get("name", "")),
            body=str(payload.get("body", "")),
            draft=bool(payload.get("draft", True)),
            prerelease=bool(payload.get("prerelease", False)),
            target_commitish=str(payload.get("target_commitish", "main")),
        )
        if not release.draft:
            self._publish_tag(release)
        return self._json(release.to_api(self.repository), status=201)

    def _publish_tag(self, release: FakeRelease) -> None:
        """Publishing a release creates its tag, as GitHub does."""
        target = self.refs().get(f"refs/heads/{release.target_commitish}")
        if target:
            self.set_ref(f"refs/tags/{release.tag_name}", target)

    def _get_release(self, release_id: int, url: str) -> Response:
        release = self.find_release(release_id)
        if release is None:
            raise self._error(url, 404, "release not found")
        return self._json(release.to_api(self.repository))

    def _patch_release(self, release_id: int, payload: dict[str, Any], url: str) -> Response:
        release = self.find_release(release_id)
        if release is None:
            raise self._error(url, 404, "release not found")
        was_draft = release.draft
        for key in ("name", "body", "target_commitish"):
            if key in payload:
                setattr(release, key, str(payload[key]))
        if "draft" in payload:
            release.draft = bool(payload["draft"])
        if was_draft and not release.draft:
            self._publish_tag(release)
        return self._json(release.to_api(self.repository))

    def _delete_release(self, release_id: int, url: str) -> Response:
        release = self.find_release(release_id)
        if release is None:
            raise self._error(url, 404, "release not found")
        self.releases.remove(release)
        return Response(status=204, body=None, headers={}, raw=b"")

    def _list_assets(self, release_id: int) -> Response:
        release = self.find_release(release_id)
        assets = release.assets if release else []
        return self._json([asset.to_api() for asset in assets])

    def _upload_asset(
        self, release_id: int, query: dict[str, list[str]], body: bytes | None, url: str
    ) -> Response:
        release = self.find_release(release_id)
        if release is None:
            raise self._error(url, 404, "release not found")
        name = query.get("name", [""])[0]
        if release.asset_names().count(name):
            raise self._error(url, 422, "asset already exists")
        asset = self.add_asset(release, name, body or b"")
        return self._json(asset.to_api(), status=201)

    def _delete_asset(self, asset_id: int) -> Response:
        found = self.find_asset(asset_id)
        if found is not None:
            release, asset = found
            release.assets.remove(asset)
        return Response(status=204, body=None, headers={}, raw=b"")

    def _download_asset(self, asset_id: int, url: str) -> Response:
        found = self.find_asset(asset_id)
        if found is None:
            raise self._error(url, 404, "asset not found")
        _, asset = found
        payload = asset.payload
        if asset_id in self.corrupt_downloads:
            payload = payload + b"corrupted"
        return Response(status=200, body=None, headers={}, raw=payload)

    def _get_ref(self, ref: str, url: str) -> Response:
        sha = self.refs().get(f"refs/{ref}")
        if sha is None:
            raise self._error(url, 404, "ref not found")
        return self._json({"ref": f"refs/{ref}", "object": {"sha": sha, "type": "commit"}})

    def _matching_refs(self, prefix: str) -> Response:
        refs = self.refs(f"refs/{prefix}")
        return self._json(
            [
                {"ref": ref, "object": {"sha": sha, "type": "commit"}}
                for ref, sha in sorted(refs.items())
            ]
        )

    def _create_ref(self, payload: dict[str, Any], url: str) -> Response:
        ref = str(payload["ref"])
        if ref in self.refs():
            raise self._error(url, 422, "reference already exists")
        self.set_ref(ref, str(payload["sha"]))
        return self._json(
            {"ref": ref, "object": {"sha": payload["sha"], "type": "commit"}}, status=201
        )

    def _delete_ref_route(self, ref: str, url: str) -> Response:
        qualified = f"refs/{ref}"
        if qualified not in self.refs():
            raise self._error(url, 422, "reference does not exist")
        self.delete_ref(qualified)
        return Response(status=204, body=None, headers={}, raw=b"")

    def _get_tree(self, sha: str, url: str) -> Response:
        clean = sha.split("?")[0]
        try:
            entries = self.tree(clean)
        except subprocess.CalledProcessError as exc:
            raise self._error(url, 404, "tree not found") from exc
        return self._json({"sha": clean, "tree": entries})

    def _get_contents(self, file_path: str, query: dict[str, list[str]], url: str) -> Response:
        ref = query.get("ref", ["HEAD"])[0]
        payload = self.blob(ref, file_path)
        if payload is None:
            raise self._error(url, 404, "file not found")
        return Response(status=200, body=None, headers={}, raw=payload)


@dataclass
class RecordedCommand:
    args: tuple[str, ...]


class FakeHelm:
    """A helm/kubectl runner that never shells out.

    ``failures`` maps an argv fragment to the ``(returncode, stderr)`` it should
    produce, so a test can make exactly one check fail.
    """

    def __init__(
        self,
        *,
        failures: Mapping[str, tuple[int, str]] | None = None,
        rendered: str | None = None,
        cluster: bool = False,
    ) -> None:
        self.failures = dict(failures or {})
        self.rendered = rendered if rendered is not None else DEFAULT_MANIFEST
        self.cluster = cluster
        self.commands: list[RecordedCommand] = []

    def __call__(self, args: Any) -> Any:
        from chartpub.validate import CommandResult

        argv = tuple(str(item) for item in args)
        self.commands.append(RecordedCommand(argv))
        joined = " ".join(argv)
        for fragment, (code, message) in self.failures.items():
            if fragment in joined:
                return CommandResult(argv, code, "", message)
        if argv[0] == "kubectl":
            return CommandResult(argv, 0 if self.cluster else 1, "", "")
        if argv[:2] == ("helm", "template"):
            return CommandResult(argv, 0, self.rendered, "")
        return CommandResult(argv, 0, "ok", "")


DEFAULT_MANIFEST = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ledger-api
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: ledger-api
      app.kubernetes.io/component: api
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ledger-api
        app.kubernetes.io/component: api
---
apiVersion: v1
kind: Service
metadata:
  name: ledger-api
spec:
  selector:
    app.kubernetes.io/name: ledger-api
    app.kubernetes.io/component: api
"""

MISMATCHED_MANIFEST = DEFAULT_MANIFEST.replace(
    """        app.kubernetes.io/name: ledger-api
        app.kubernetes.io/component: api
---""",
    """        app.kubernetes.io/name: ledger-api
        app.kubernetes.io/component: worker
---""",
)
