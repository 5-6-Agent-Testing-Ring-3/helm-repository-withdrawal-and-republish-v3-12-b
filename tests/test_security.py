from __future__ import annotations

import os
from pathlib import Path

import pytest

from chartpub.errors import UsageError
from chartpub.security import (
    Redactor,
    read_env_file,
    redact,
    require_token,
    scrubbed_environment,
    secret_values,
)


def test_reads_token_without_logging(tmp_path: Path) -> None:
    path = tmp_path / "credentials.env"
    path.write_text("GITHUB_TOKEN=test-secret-value\n", encoding="utf-8")
    values = read_env_file(path)
    assert require_token(values) == "test-secret-value"
    assert redact("failed with test-secret-value", values) == "failed with [REDACTED]"


def test_redacts_gh_token_alias() -> None:
    assert redact("token alias-secret", {"GH_TOKEN": "alias-secret"}) == "token [REDACTED]"


def test_env_file_tolerates_comments_exports_and_quotes(tmp_path: Path) -> None:
    path = tmp_path / "credentials.env"
    path.write_text(
        "\n# a comment\nexport GITHUB_TOKEN='quoted-secret-value'\nGITHUB_REPOSITORY=\"o/r\"\n",
        encoding="utf-8",
    )
    values = read_env_file(path)
    assert values == {"GITHUB_TOKEN": "quoted-secret-value", "GITHUB_REPOSITORY": "o/r"}


def test_env_file_reports_line_number_not_content(tmp_path: Path) -> None:
    path = tmp_path / "credentials.env"
    path.write_text(
        "GITHUB_TOKEN=ok-secret-value\nthis-line-has-a-secret-and-no-equals\n", encoding="utf-8"
    )
    with pytest.raises(UsageError) as excinfo:
        read_env_file(path)
    assert "line 2" in str(excinfo.value)
    assert "this-line-has-a-secret" not in str(excinfo.value)


def test_env_file_missing(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="cannot read credential file"):
        read_env_file(tmp_path / "absent.env")


def test_require_token_names_the_variables_not_the_values() -> None:
    with pytest.raises(UsageError) as excinfo:
        require_token({"UNRELATED": "value"})
    assert "GITHUB_TOKEN" in str(excinfo.value)
    assert "value" not in str(excinfo.value).replace("values", "")


def test_require_token_accepts_each_alias() -> None:
    assert require_token({"GH_TOKEN": "alias-secret-value"}) == "alias-secret-value"
    assert require_token({"CHARTPUB_TOKEN": "third-secret-value"}) == "third-secret-value"


def test_redactor_handles_overlapping_secrets() -> None:
    redactor = Redactor(["short-secret", "short-secret-longer"])
    assert redactor("saw short-secret-longer here") == "saw [REDACTED] here"


def test_redactor_ignores_trivially_short_values() -> None:
    redactor = Redactor(["abc"])
    assert redactor("abc stays") == "abc stays"
    assert secret_values({"GITHUB_TOKEN": "abc"}) == ()


@pytest.mark.parametrize(
    "shaped",
    [
        "ghp_0123456789abcdefghij",
        "gho_0123456789abcdefghij",
        "ghs_0123456789abcdefghij",
        "github_pat_11ABCDEFG0123456789_abcdefghij",
        "https://x-access-token:ghp_secretvaluehere0000@github.com/o/r.git",
    ],
)
def test_redactor_blinds_credential_shaped_text(shaped: str) -> None:
    """Even a token we were never given must not be echoed."""
    assert "[REDACTED]" in Redactor()(f"error: {shaped}")


def test_redactor_add_is_effective() -> None:
    redactor = Redactor()
    redactor.add("a-brand-new-secret")
    redactor.add(None)
    redactor.add("tiny")
    assert redactor("saw a-brand-new-secret") == "saw [REDACTED]"
    assert redactor("saw tiny") == "saw tiny"


def test_scrubbed_environment_removes_credentials() -> None:
    env = scrubbed_environment({"GITHUB_TOKEN": "secret-value-x", "PATH": "/bin", "GH_TOKEN": "y"})
    assert env == {"PATH": "/bin"}


def test_scrubbed_environment_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret-value-in-process")
    env = scrubbed_environment()
    assert "GITHUB_TOKEN" not in env
    assert env.get("PATH") == os.environ.get("PATH")
