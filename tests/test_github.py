from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from chartpub.errors import PublicationError, RemoteConflict, RemoteHTTPError
from chartpub.github import GitHubClient, Response, next_page_url, release_from_api
from chartpub.security import Redactor

from .conftest import REPOSITORY, Remote
from .fakes import TOKEN


def client(remote: Remote) -> GitHubClient:
    return GitHubClient(
        REPOSITORY,
        TOKEN,
        transport=remote.api,
        redactor=Redactor([TOKEN]),
        sleep=lambda _s: None,
    )


def test_lists_every_release_across_pages(remote: Remote) -> None:
    for index in range(7):
        remote.api.add_release(
            tag_name=f"chart-v0.{index}.0", name=f"r{index}", draft=index % 2 == 0
        )
    remote.api.per_page = 100
    api = client(remote)
    # Force small pages so the Link header path is exercised.
    remote.api.per_page = 2
    seen = [item.tag_name for item in api.list_releases()]
    assert seen == [f"chart-v0.{index}.0" for index in range(7)]


def test_pagination_includes_drafts(remote: Remote) -> None:
    remote.api.add_release(tag_name="chart-v0.4.0", name="published", draft=False)
    remote.api.add_release(tag_name="chart-v0.4.1", name="WITHDRAWN: draft", draft=True)
    found = client(remote).get_release_by_tag("chart-v0.4.1")
    assert found is not None and found.draft is True


def test_get_release_by_tag_returns_none_when_absent(remote: Remote) -> None:
    assert client(remote).get_release_by_tag("chart-v9.9.9") is None


def test_next_page_url_parses_link_header() -> None:
    header = '<https://api/x?page=2>; rel="next", <https://api/x?page=9>; rel="last"'
    assert next_page_url(header) == "https://api/x?page=2"
    assert next_page_url(None) is None
    assert next_page_url("<https://api/x>") is None
    assert next_page_url('<https://api/x>; rel="last"') is None
    assert next_page_url("malformed") is None


def test_get_ref_returns_none_for_404(remote: Remote) -> None:
    api = client(remote)
    assert api.get_ref("heads/main") == remote.main_tip
    assert api.get_ref("heads/does-not-exist") is None


def test_get_ref_propagates_other_errors(remote: Remote) -> None:
    remote.api.fail_next("GET", "/git/ref/", 403)
    with pytest.raises(PublicationError, match=r"\(403\)"):
        client(remote).get_ref("heads/main")


def test_list_tags_and_matching_refs(remote: Remote) -> None:
    remote.api.set_ref("refs/tags/chart-v0.4.0", remote.main_tip)
    api = client(remote)
    assert api.list_tags() == {"chart-v0.4.0": remote.main_tip}
    assert api.list_matching_refs("heads/") == {"refs/heads/main": remote.main_tip}


def test_create_and_delete_ref(remote: Remote) -> None:
    api = client(remote)
    api.create_ref("tags/chart-v0.4.1", remote.main_tip)
    assert api.get_ref("tags/chart-v0.4.1") == remote.main_tip
    api.delete_ref("tags/chart-v0.4.1", expected=remote.main_tip)
    assert api.get_ref("tags/chart-v0.4.1") is None


def test_delete_ref_refuses_when_target_moved(remote: Remote) -> None:
    api = client(remote)
    api.create_ref("refs/tags/chart-v0.4.1", remote.main_tip)
    with pytest.raises(RemoteConflict, match="refusing to delete"):
        api.delete_tag("chart-v0.4.1", expected="0" * 40)
    assert api.get_ref("tags/chart-v0.4.1") == remote.main_tip, "the ref must survive"


def test_delete_ref_refuses_when_absent(remote: Remote) -> None:
    with pytest.raises(RemoteConflict, match="does not exist"):
        client(remote).delete_tag("chart-v9.9.9", expected=remote.main_tip)


def test_release_lifecycle_and_assets(remote: Remote, tmp_path: Path) -> None:
    api = client(remote)
    release = api.create_release(
        tag="chart-v0.4.1", name="ledger-api 0.4.1", body="x", target_commitish="main", draft=True
    )
    assert release.draft is True
    payload = b"chart bytes"
    archive = tmp_path / "ledger-api-0.4.1.tgz"
    archive.write_bytes(payload)
    asset = api.upload_asset(release.id, archive.name, archive)
    assert api.list_assets(release.id) == [asset]
    downloaded = api.download_asset(asset.id, tmp_path / "back" / archive.name)
    assert downloaded.read_bytes() == payload
    published = api.update_release(release.id, draft=False)
    assert published.draft is False
    assert api.get_ref("tags/chart-v0.4.1") == remote.main_tip, "publishing creates the tag"
    assert api.get_release(release.id).id == release.id
    api.delete_asset(asset.id)
    assert api.list_assets(release.id) == []
    api.delete_release(release.id)
    assert api.get_release_by_tag("chart-v0.4.1") is None


def test_get_file_returns_none_for_missing_path(remote: Remote) -> None:
    remote.seed_pages({"index.yaml": b"apiVersion: v1\n"})
    api = client(remote)
    assert api.get_file("gh-pages", "index.yaml") == b"apiVersion: v1\n"
    assert api.get_file("gh-pages", "absent.yaml") is None


def test_get_tree_lists_blobs(remote: Remote) -> None:
    tip = remote.seed_pages({"index.yaml": b"x", "nested/a.tgz": b"y"})
    paths = {item["path"] for item in client(remote).get_tree(tip)}
    assert paths == {"index.yaml", "nested/a.tgz"}


def test_create_blob(remote: Remote) -> None:
    class BlobFake:
        def __call__(self, request: urllib.request.Request) -> Response:
            assert request.data is not None
            return Response(201, {"sha": "c" * 40}, {}, b"")

    api = GitHubClient(REPOSITORY, TOKEN, transport=BlobFake())
    assert api.create_blob(b"payload") == "c" * 40


def test_retries_transient_server_errors(remote: Remote) -> None:
    slept: list[float] = []
    api = GitHubClient(
        REPOSITORY, TOKEN, transport=remote.api, redactor=Redactor([TOKEN]), sleep=slept.append
    )
    remote.api.fail_next("GET", "/git/ref/", 503, times=2)
    assert api.get_ref("heads/main") == remote.main_tip
    assert len(slept) == 2


def test_gives_up_after_max_attempts(remote: Remote) -> None:
    api = GitHubClient(
        REPOSITORY, TOKEN, transport=remote.api, redactor=Redactor([TOKEN]), sleep=lambda _s: None
    )
    remote.api.fail_next("GET", "/git/ref/", 500, times=10)
    with pytest.raises(PublicationError, match=r"\(500\)"):
        api.get_ref("heads/main")


def test_retries_a_non_raising_transient_status() -> None:
    attempts: list[int] = []

    def transport(request: urllib.request.Request) -> Response:
        attempts.append(1)
        if len(attempts) < 2:
            return Response(503, None, {}, b"")
        return Response(200, None, {}, b'{"object": {"sha": "' + b"d" * 40 + b'"}}')

    api = GitHubClient(REPOSITORY, TOKEN, transport=transport, sleep=lambda _s: None)
    assert api.get_ref("heads/main") == "d" * 40
    assert len(attempts) == 2


def test_error_messages_redact_the_token() -> None:
    def transport(request: urllib.request.Request) -> Response:
        import io

        body = f'{{"message": "bad credentials {TOKEN}"}}'.encode()
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(body))  # type: ignore[arg-type]

    api = GitHubClient(REPOSITORY, TOKEN, transport=transport, redactor=Redactor([TOKEN]))
    with pytest.raises(PublicationError) as excinfo:
        api.list_releases()
    message = str(excinfo.value)
    assert TOKEN not in message
    assert "[REDACTED]" in message


def test_error_messages_drop_the_query_string() -> None:
    def transport(request: urllib.request.Request) -> Response:
        return Response(422, None, {}, b"rejected")

    api = GitHubClient(REPOSITORY, TOKEN, transport=transport)
    with pytest.raises(PublicationError) as excinfo:
        api.upload_asset(1, "name.tgz", Path(__file__))
    assert "?" not in str(excinfo.value)


def test_status_is_available_on_http_errors() -> None:
    def transport(request: urllib.request.Request) -> Response:
        return Response(404, None, {}, b"missing")

    api = GitHubClient(REPOSITORY, TOKEN, transport=transport)
    with pytest.raises(RemoteHTTPError) as excinfo:
        api.get_release(7)
    assert excinfo.value.status == 404


def test_rejects_non_list_from_a_listing_endpoint() -> None:
    def transport(request: urllib.request.Request) -> Response:
        return Response(200, None, {}, b'{"not": "a list"}')

    api = GitHubClient(REPOSITORY, TOKEN, transport=transport)
    with pytest.raises(PublicationError, match="expected a list"):
        api.list_releases()


def test_rejects_non_object_where_one_is_required() -> None:
    def transport(request: urllib.request.Request) -> Response:
        return Response(200, None, {}, b"[]")

    api = GitHubClient(REPOSITORY, TOKEN, transport=transport)
    with pytest.raises(PublicationError, match="expected a release object"):
        api.get_release(7)


def test_release_from_api_tolerates_sparse_payloads() -> None:
    release = release_from_api({"id": 1, "tag_name": "t", "assets": "not a list"})
    assert release.assets == ()
    assert release.name == ""
    assert release.asset("anything") is None


def test_payload_and_data_are_mutually_exclusive() -> None:
    api = GitHubClient(REPOSITORY, TOKEN, transport=lambda _r: Response(200, None, {}, b""))
    with pytest.raises(ValueError, match="payload or data"):
        api.request("POST", "/x", payload={"a": 1}, data=b"b")


def test_unparseable_json_body_is_tolerated() -> None:
    def transport(request: urllib.request.Request) -> Response:
        return Response(200, None, {}, b"<html>not json</html>")

    api = GitHubClient(REPOSITORY, TOKEN, transport=transport)
    assert api.request("GET", "/x").body is None


def test_compare_endpoint(remote: Remote) -> None:
    def transport(request: urllib.request.Request) -> Response:
        assert "/compare/a...b" in request.full_url
        return Response(200, None, {}, b'{"status": "ahead"}')

    api = GitHubClient(REPOSITORY, TOKEN, transport=transport)
    assert api.compare("a", "b")["status"] == "ahead"


def test_get_commit(remote: Remote) -> None:
    def transport(request: urllib.request.Request) -> Response:
        return Response(200, None, {}, b'{"sha": "abc"}')

    api = GitHubClient(REPOSITORY, TOKEN, transport=transport)
    assert api.get_commit("abc")["sha"] == "abc"


def test_get_tree_tolerates_missing_tree_key() -> None:
    def transport(request: urllib.request.Request) -> Response:
        return Response(200, None, {}, b"{}")

    api = GitHubClient(REPOSITORY, TOKEN, transport=transport)
    assert api.get_tree("abc") == []


def test_get_file_propagates_non_404(remote: Remote) -> None:
    remote.seed_pages({"index.yaml": b"x"})
    remote.api.fail_next("GET", "/contents/", 403)
    with pytest.raises(PublicationError, match=r"\(403\)"):
        client(remote).get_file("gh-pages", "index.yaml")
