"""Minimal GitHub REST client scoped to what publication recovery needs.

Design notes:

* Every request goes through :meth:`GitHubClient.request`, which accepts an
  injectable transport. Tests supply a fake transport, so no test can reach
  ``api.github.com``.
* Error text is passed through a :class:`~chartpub.security.Redactor` before it
  is ever raised, so a token echoed by an API response cannot leak.
* Listing endpoints follow ``Link`` headers. The original code asked for
  ``per_page=2`` and read only the first page, which silently hid releases.
* Ref updates take an ``expected`` SHA and are refused when the remote moved.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chartpub.errors import PublicationError, RemoteConflict, RemoteHTTPError
from chartpub.models import ReleaseAsset, ReleaseState
from chartpub.security import Redactor

API_URL = "https://api.github.com"
UPLOAD_URL = "https://uploads.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "chartpub/0.1"

PER_PAGE = 100
MAX_PAGES = 100
RETRY_STATUSES = frozenset({500, 502, 503, 504})
MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class Response:
    status: int
    body: Any
    headers: dict[str, str]
    raw: bytes = b""


Transport = Callable[[urllib.request.Request], Response]


def urllib_transport(request: urllib.request.Request) -> Response:
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https only
        raw = response.read()
        return Response(
            status=response.status,
            body=None,
            headers={key.lower(): value for key, value in response.headers.items()},
            raw=raw,
        )


def next_page_url(link_header: str | None) -> str | None:
    """Extract ``rel="next"`` from an RFC 5988 ``Link`` header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip()
        if not url.startswith("<") or not url.endswith(">"):
            continue
        if any(item.strip().replace(" ", "") in {'rel="next"', "rel=next"} for item in section[1:]):
            return url[1:-1]
    return None


class GitHubClient:
    """Authenticated client for one repository."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_url: str = API_URL,
        upload_url: str = UPLOAD_URL,
        transport: Transport | None = None,
        redactor: Redactor | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repository = repository
        self._token = token
        self.api_url = api_url.rstrip("/")
        self.upload_url = upload_url.rstrip("/")
        self._transport = transport or urllib_transport
        self._redact = redactor or Redactor((token,))
        self._redact.add(token)
        self._sleep = sleep

    # ---------------------------------------------------------------- plumbing

    def _headers(self, *, accept: str, content_type: str | None) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        data: bytes | None = None,
        accept: str = "application/vnd.github+json",
        content_type: str | None = None,
        base: str | None = None,
        parse_json: bool = True,
    ) -> Response:
        if payload is not None and data is not None:  # pragma: no cover - programmer error
            raise ValueError("pass either payload or data, not both")
        body = data
        if payload is not None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            content_type = content_type or "application/json"
        url = path if path.startswith("http") else f"{base or self.api_url}{path}"
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=self._headers(accept=accept, content_type=content_type),
        )

        last_error: PublicationError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._transport(request)
            except urllib.error.HTTPError as exc:
                detail = self._error_detail(exc)
                status = exc.code
                if status in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                    last_error = PublicationError(
                        self._redact(
                            f"GitHub {method} {self._safe(url)} failed ({status}): {detail}"
                        )
                    )
                    self._sleep(min(2.0**attempt, 8.0))
                    continue
                raise self._http_error(method, url, status, detail) from exc
            except urllib.error.URLError as exc:  # pragma: no cover - network dependent
                message = self._redact(
                    f"GitHub {method} {self._safe(url)} unreachable: {exc.reason}"
                )
                if attempt < MAX_ATTEMPTS:
                    last_error = PublicationError(message)
                    self._sleep(min(2.0**attempt, 8.0))
                    continue
                raise PublicationError(message) from exc

            if response.status in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                last_error = PublicationError(
                    self._redact(f"GitHub {method} {self._safe(url)} failed ({response.status})")
                )
                self._sleep(min(2.0**attempt, 8.0))
                continue
            if response.status >= 400:
                raise self._http_error(
                    method, url, response.status, response.raw.decode("utf-8", "replace")
                )
            decoded = response.body
            if decoded is None and parse_json and response.raw:
                try:
                    decoded = json.loads(response.raw)
                except json.JSONDecodeError:
                    decoded = None
            return Response(response.status, decoded, response.headers, response.raw)

        raise last_error or PublicationError("GitHub request failed")  # pragma: no cover

    def _safe(self, url: str) -> str:
        """Strip any query string and userinfo before a URL reaches a message."""
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))

    def _error_detail(self, exc: urllib.error.HTTPError) -> str:
        try:
            return exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:  # pragma: no cover - defensive
            return exc.reason if isinstance(exc.reason, str) else "unknown error"

    def _http_error(self, method: str, url: str, status: int, detail: str) -> RemoteHTTPError:
        message = self._redact(f"GitHub {method} {self._safe(url)} failed ({status}): {detail}")
        return RemoteHTTPError(message, status)

    def paginate(self, path: str) -> Iterator[dict[str, Any]]:
        separator = "&" if "?" in path else "?"
        url: str | None = f"{path}{separator}per_page={PER_PAGE}"
        pages = 0
        while url is not None:
            pages += 1
            if pages > MAX_PAGES:  # pragma: no cover - runaway guard
                raise PublicationError(f"GitHub pagination exceeded {MAX_PAGES} pages for {path}")
            response = self.request("GET", url)
            items = response.body
            if not isinstance(items, list):
                raise PublicationError(f"expected a list from {path}")
            for item in items:
                if isinstance(item, dict):
                    yield item
            url = next_page_url(response.headers.get("link"))

    # ---------------------------------------------------------------- releases

    def list_releases(self) -> list[ReleaseState]:
        """All releases, including drafts, across every page."""
        return [
            release_from_api(item) for item in self.paginate(f"/repos/{self.repository}/releases")
        ]

    def get_release_by_tag(self, tag: str) -> ReleaseState | None:
        """Find a release by tag, including drafts.

        ``GET /releases/tags/{tag}`` cannot see drafts, and a quarantined
        release is a draft, so the listing is authoritative here.
        """
        return next((item for item in self.list_releases() if item.tag_name == tag), None)

    def create_release(
        self,
        *,
        tag: str,
        name: str,
        body: str,
        target_commitish: str,
        draft: bool = True,
        prerelease: bool = False,
    ) -> ReleaseState:
        response = self.request(
            "POST",
            f"/repos/{self.repository}/releases",
            payload={
                "tag_name": tag,
                "name": name,
                "body": body,
                "target_commitish": target_commitish,
                "draft": draft,
                "prerelease": prerelease,
            },
        )
        return release_from_api(_expect_object(response.body, "release"))

    def update_release(self, release_id: int, **changes: Any) -> ReleaseState:
        response = self.request(
            "PATCH", f"/repos/{self.repository}/releases/{release_id}", payload=dict(changes)
        )
        return release_from_api(_expect_object(response.body, "release"))

    def get_release(self, release_id: int) -> ReleaseState:
        response = self.request("GET", f"/repos/{self.repository}/releases/{release_id}")
        return release_from_api(_expect_object(response.body, "release"))

    def delete_release(self, release_id: int) -> None:
        self.request("DELETE", f"/repos/{self.repository}/releases/{release_id}")

    def list_assets(self, release_id: int) -> list[ReleaseAsset]:
        return [
            asset_from_api(item)
            for item in self.paginate(f"/repos/{self.repository}/releases/{release_id}/assets")
        ]

    def upload_asset(self, release_id: int, name: str, path: Path) -> ReleaseAsset:
        query = urllib.parse.urlencode({"name": name})
        response = self.request(
            "POST",
            f"/repos/{self.repository}/releases/{release_id}/assets?{query}",
            data=path.read_bytes(),
            content_type="application/gzip",
            base=self.upload_url,
        )
        return asset_from_api(_expect_object(response.body, "asset"))

    def delete_asset(self, asset_id: int) -> None:
        self.request("DELETE", f"/repos/{self.repository}/releases/assets/{asset_id}")

    def download_asset(self, asset_id: int, destination: Path) -> Path:
        """Fetch an asset's bytes back from GitHub for independent verification."""
        response = self.request(
            "GET",
            f"/repos/{self.repository}/releases/assets/{asset_id}",
            accept="application/octet-stream",
            parse_json=False,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.raw)
        return destination

    # -------------------------------------------------------------- git refs

    def get_ref(self, ref: str) -> str | None:
        """Resolve ``heads/x`` or ``tags/x`` to a commit SHA, or None."""
        encoded = urllib.parse.quote(ref.lstrip("/"), safe="/")
        try:
            response = self.request("GET", f"/repos/{self.repository}/git/ref/{encoded}")
        except RemoteHTTPError as exc:
            if exc.status == 404:
                return None
            raise
        return _ref_sha(response.body)

    def list_matching_refs(self, prefix: str) -> dict[str, str]:
        encoded = urllib.parse.quote(prefix.lstrip("/"), safe="/")
        refs: dict[str, str] = {}
        for item in self.paginate(f"/repos/{self.repository}/git/matching-refs/{encoded}"):
            ref = str(item.get("ref", ""))
            sha = _ref_sha(item)
            if ref and sha:
                refs[ref] = sha
        return refs

    def list_tags(self) -> dict[str, str]:
        """Tag name to target SHA for every tag in the repository."""
        return {
            ref.removeprefix("refs/tags/"): sha
            for ref, sha in self.list_matching_refs("tags/").items()
        }

    def create_ref(self, ref: str, sha: str) -> None:
        qualified = ref if ref.startswith("refs/") else f"refs/{ref.lstrip('/')}"
        self.request(
            "POST",
            f"/repos/{self.repository}/git/refs",
            payload={"ref": qualified, "sha": sha},
        )

    def delete_ref(self, ref: str, *, expected: str | None = None) -> None:
        """Delete exactly one ref, refusing if it no longer points at ``expected``."""
        if expected is not None:
            actual = self.get_ref(ref)
            if actual is None:
                raise RemoteConflict(f"refs/{ref} does not exist; nothing to delete")
            if actual != expected:
                raise RemoteConflict(
                    f"refs/{ref} points at {actual}, expected {expected}; refusing to delete"
                )
        encoded = urllib.parse.quote(ref.lstrip("/"), safe="/")
        self.request("DELETE", f"/repos/{self.repository}/git/refs/{encoded}")

    def delete_tag(self, tag: str, *, expected: str | None = None) -> None:
        self.delete_ref(f"tags/{tag}", expected=expected)

    # ------------------------------------------------------------- git data

    def create_blob(self, payload: bytes) -> str:
        response = self.request(
            "POST",
            f"/repos/{self.repository}/git/blobs",
            payload={"content": base64.b64encode(payload).decode("ascii"), "encoding": "base64"},
        )
        return str(_expect_object(response.body, "blob")["sha"])

    def get_commit(self, sha: str) -> dict[str, Any]:
        response = self.request("GET", f"/repos/{self.repository}/commits/{sha}")
        return _expect_object(response.body, "commit")

    def compare(self, base: str, head: str) -> dict[str, Any]:
        response = self.request("GET", f"/repos/{self.repository}/compare/{base}...{head}")
        return _expect_object(response.body, "comparison")

    def get_tree(self, sha: str, *, recursive: bool = True) -> list[dict[str, Any]]:
        suffix = "?recursive=1" if recursive else ""
        response = self.request("GET", f"/repos/{self.repository}/git/trees/{sha}{suffix}")
        tree = _expect_object(response.body, "tree").get("tree")
        return [item for item in tree if isinstance(item, dict)] if isinstance(tree, list) else []

    def get_file(self, ref: str, path: str) -> bytes | None:
        """Raw bytes of one file at ``ref``, or None when absent."""
        query = urllib.parse.urlencode({"ref": ref})
        encoded = urllib.parse.quote(path, safe="/")
        try:
            response = self.request(
                "GET",
                f"/repos/{self.repository}/contents/{encoded}?{query}",
                accept="application/vnd.github.raw",
                parse_json=False,
            )
        except RemoteHTTPError as exc:
            if exc.status == 404:
                return None
            raise
        return response.raw


def _expect_object(body: Any, what: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise PublicationError(f"expected a {what} object from GitHub")
    return body


def _ref_sha(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    obj = body.get("object")
    if not isinstance(obj, dict):
        return None
    sha = obj.get("sha")
    return str(sha) if sha else None


def asset_from_api(raw: Mapping[str, Any]) -> ReleaseAsset:
    return ReleaseAsset(
        id=int(raw.get("id", 0)),
        name=str(raw.get("name", "")),
        size=int(raw.get("size", 0)),
        state=str(raw.get("state", "")),
    )


def release_from_api(raw: Mapping[str, Any]) -> ReleaseState:
    assets_raw = raw.get("assets")
    assets: Sequence[Mapping[str, Any]] = (
        [item for item in assets_raw if isinstance(item, dict)]
        if isinstance(assets_raw, list)
        else []
    )
    return ReleaseState(
        id=int(raw.get("id", 0)),
        tag_name=str(raw.get("tag_name", "")),
        name=str(raw.get("name") or ""),
        draft=bool(raw.get("draft", False)),
        prerelease=bool(raw.get("prerelease", False)),
        target_commitish=str(raw.get("target_commitish") or ""),
        html_url=str(raw.get("html_url") or ""),
        assets=tuple(asset_from_api(item) for item in assets),
    )
