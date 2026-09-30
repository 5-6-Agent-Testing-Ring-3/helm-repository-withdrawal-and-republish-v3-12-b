from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from chartpub import cli, gitops, lifecycle
from chartpub.cli import build_parser, main
from chartpub.errors import (
    EXIT_CONFLICT,
    EXIT_OK,
    EXIT_REMOTE,
    EXIT_USAGE,
    EXIT_VALIDATION,
)

from .conftest import Remote, contract_mapping
from .fakes import MISMATCHED_MANIFEST, TOKEN, FakeHelm
from .test_config import valid_contract, write_contract


def test_plan_is_machine_readable(tmp_path: Path, capsys: object) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, valid_contract())
    assert main(["plan", "--contract", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["command"] == "plan"


def test_all_lifecycle_commands_exist() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "withdraw" in help_text
    assert "repair" in help_text


def test_every_documented_command_is_wired() -> None:
    help_text = build_parser().format_help()
    for name in ("plan", "publish", "withdraw", "audit", "repair"):
        assert name in help_text


def test_dry_run_is_offered_where_it_can_mutate() -> None:
    parser = build_parser()
    for name in ("plan", "publish", "withdraw", "repair"):
        assert "--dry-run" in parser.parse_args([name, "--help"]) if False else True
    # argparse has no public introspection for one subparser, so assert via parse.
    assert parser.parse_args(["publish", "--dry-run"]).dry_run is True
    assert parser.parse_args(["withdraw", "--dry-run"]).dry_run is True
    assert parser.parse_args(["repair", "--dry-run"]).dry_run is True
    assert parser.parse_args(["plan", "--dry-run"]).dry_run is True


def test_offline_plan_needs_no_credential(tmp_path: Path, capsys: object, in_clone: Path) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, contract_mapping())
    assert (
        main(["plan", "--offline", "--contract", str(path), "--chart-dir", "does/not/exist"]) == 0
    )
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["remote_inspected"] is False
    assert output["candidate"] is None
    assert output["plan"]["command"] == "publish"


def test_plan_degrades_and_says_so_without_a_credential(
    tmp_path: Path, capsys: object, in_clone: Path
) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, contract_mapping())
    assert main(["plan", "--contract", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["remote_inspected"] is False
    assert any("remote inspection skipped" in note for note in output["notes"])


@pytest.mark.parametrize("command", ["publish", "withdraw", "audit", "repair"])
def test_mutating_commands_refuse_without_a_credential(
    command: str, tmp_path: Path, capsys: object, in_clone: Path
) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, contract_mapping())
    assert main([command, "--contract", str(path)]) == EXIT_USAGE
    assert "cannot read credential file" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_bad_contract_exits_with_a_usage_code(tmp_path: Path, capsys: object) -> None:
    path = tmp_path / "contract.json"
    path.write_text("{}", encoding="utf-8")
    assert main(["plan", "--contract", str(path)]) == EXIT_USAGE
    assert "missing contract key" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_refuses_a_credential_file_for_another_repository(
    tmp_path: Path, contract_file: Path, capsys: object, in_clone: Path
) -> None:
    credentials = tmp_path / "wrong.env"
    credentials.write_text(
        f"GITHUB_TOKEN={TOKEN}\nGITHUB_REPOSITORY=someone-else/other\n", encoding="utf-8"
    )
    code = main(["audit", "--contract", str(contract_file), "--credentials", str(credentials)])
    assert code == EXIT_USAGE
    assert "different repository" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_refuses_when_origin_disagrees_with_the_contract(
    tmp_path: Path, credential_file: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .fakes import git

    work = tmp_path / "elsewhere"
    work.mkdir()
    git("init", "--quiet", ".", cwd=work)
    git("remote", "add", "origin", "https://github.com/somebody/else.git", cwd=work)
    monkeypatch.chdir(work)
    contract = tmp_path / "contract.json"
    write_contract(contract, contract_mapping())
    code = main(["audit", "--contract", str(contract), "--credentials", str(credential_file)])
    assert code == EXIT_USAGE
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "refusing to mutate" in error
    assert "somebody/else" in error


def wire(
    monkeypatch: pytest.MonkeyPatch,
    remote: Remote,
    local_clone: Path,
    tmp_path: Path,
    *,
    helm_runner: object | None = None,
) -> None:
    """Point the CLI's session builder at the temporary remote."""
    original = cli._build_session

    def build(args: object, contract: object, stack: object, *, offline: bool) -> lifecycle.Session:
        session = original(args, contract, stack, offline=offline)  # type: ignore[arg-type]
        session.remote_url = remote.url
        session.client = __import__("chartpub.github", fromlist=["GitHubClient"]).GitHubClient(
            session.contract.repository,
            TOKEN,
            transport=remote.api,
            redactor=session.redactor,
            sleep=lambda _s: None,
        )
        session.repo = gitops.GitRepository(local_clone, redactor=session.redactor)
        session.git_env = None
        session.helm_runner = helm_runner or FakeHelm()  # type: ignore[assignment]
        session.server_side_install = False
        session.work_dir = tmp_path / "cli-build"
        return session

    monkeypatch.setattr(cli, "_build_session", build)


def contract_path(tmp_path: Path, remote: Remote, **overrides: object) -> Path:
    path = tmp_path / "contract.json"
    payload = contract_mapping(expected_pages_tip=remote.pages_tip or "b" * 40, **overrides)
    write_contract(path, payload)
    return path


FIXTURES = Path(__file__).parent / "fixtures"


def common_args(
    tmp_path: Path, remote: Remote, credential_file: Path, source_chart: Path, **overrides: object
) -> list[str]:
    """The invariant CLI arguments; absolute, because the CWD is a temp checkout."""
    return [
        "--contract",
        str(contract_path(tmp_path, remote, **overrides)),
        "--credentials",
        str(credential_file),
        "--state-dir",
        str(tmp_path / "state"),
        "--chart-dir",
        str(source_chart),
        "--values-dir",
        str(FIXTURES),
    ]


def test_audit_reports_drift_with_the_validation_exit_code(
    tmp_path: Path,
    remote: Remote,
    local_clone: Path,
    credential_file: Path,
    published_pages: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    in_clone: Path,
) -> None:
    wire(monkeypatch, remote, local_clone, tmp_path)
    args = [
        "audit",
        "--contract",
        str(contract_path(tmp_path, remote)),
        "--credentials",
        str(credential_file),
        "--state-dir",
        str(tmp_path / "state"),
    ]
    assert main(args) == EXIT_VALIDATION
    assert "withdrawn-tag-present" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert main([*args, "--no-strict"]) == EXIT_OK
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["command"] == "audit"
    assert output["healthy"] is False


def test_audit_live_reports_what_a_client_sees(
    tmp_path: Path,
    remote: Remote,
    local_clone: Path,
    credential_file: Path,
    published_pages: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    in_clone: Path,
) -> None:
    wire(monkeypatch, remote, local_clone, tmp_path)
    payload = remote.pages_files()["index.yaml"]
    monkeypatch.setattr(
        lifecycle,
        "fetch_live_index",
        lambda session, fetch=None: __import__(
            "chartpub.index", fromlist=["parse_index"]
        ).parse_index(payload.decode("utf-8")),
    )
    code = main(
        [
            "audit",
            "--no-strict",
            "--live",
            "--contract",
            str(contract_path(tmp_path, remote)),
            "--credentials",
            str(credential_file),
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    assert code == EXIT_OK
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["live"]["advertised_versions"] == ["0.4.0"]


def test_publish_dry_run_via_cli_writes_nothing(
    tmp_path: Path,
    remote: Remote,
    local_clone: Path,
    credential_file: Path,
    published_pages: dict[str, bytes],
    source_chart: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    in_clone: Path,
) -> None:
    wire(monkeypatch, remote, local_clone, tmp_path)
    before = remote.refs()
    code = main(
        ["publish", "--dry-run", *common_args(tmp_path, remote, credential_file, source_chart)]
    )
    assert code == EXIT_OK
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["applied"] is False
    assert remote.refs() == before


def test_publish_validation_failure_uses_the_validation_exit_code(
    tmp_path: Path,
    remote: Remote,
    local_clone: Path,
    credential_file: Path,
    published_pages: dict[str, bytes],
    source_chart: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    in_clone: Path,
) -> None:
    wire(
        monkeypatch,
        remote,
        local_clone,
        tmp_path,
        helm_runner=FakeHelm(rendered=MISMATCHED_MANIFEST),
    )
    code = main(["publish", *common_args(tmp_path, remote, credential_file, source_chart)])
    assert code == EXIT_VALIDATION
    assert "selector does not match" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert remote.refs().get("refs/tags/chart-v0.4.1") is None


def test_publish_conflict_uses_the_conflict_exit_code(
    tmp_path: Path,
    remote: Remote,
    local_clone: Path,
    credential_file: Path,
    published_pages: dict[str, bytes],
    source_chart: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    in_clone: Path,
) -> None:
    wire(monkeypatch, remote, local_clone, tmp_path)
    code = main(
        [
            "publish",
            *common_args(tmp_path, remote, credential_file, source_chart),
            "--expect-pages-tip",
            "0" * 40,
        ]
    )
    assert code == EXIT_CONFLICT
    assert "refs/heads/gh-pages" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_full_recovery_through_the_cli(
    tmp_path: Path,
    remote: Remote,
    local_clone: Path,
    credential_file: Path,
    published_pages: dict[str, bytes],
    source_chart: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    in_clone: Path,
) -> None:
    wire(monkeypatch, remote, local_clone, tmp_path)
    common = common_args(
        tmp_path,
        remote,
        credential_file,
        source_chart,
        expected_bad_tag_target=remote.main_tip,
    )

    assert main(["withdraw", "--force", *common]) == EXIT_OK
    withdrawn = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert withdrawn["applied"] is True

    assert main(["publish", "--force", *common]) == EXIT_OK
    published = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert published["applied"] is True

    assert main(["audit", *common]) == EXIT_OK
    audited = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert audited["healthy"] is True

    assert main(["repair", *common]) == EXIT_OK
    repaired = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert repaired["changed"] is False

    from chartpub.index import advertised_versions, parse_index

    index = parse_index(remote.pages_files()["index.yaml"].decode("utf-8"))
    assert advertised_versions(index, "ledger-api") == ("0.4.1",)
    assert "refs/tags/chart-v0.4.0" not in remote.refs()


def test_plan_for_withdraw_and_repair(
    tmp_path: Path,
    remote: Remote,
    local_clone: Path,
    credential_file: Path,
    published_pages: dict[str, bytes],
    source_chart: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    in_clone: Path,
) -> None:
    wire(monkeypatch, remote, local_clone, tmp_path)
    common = common_args(
        tmp_path,
        remote,
        credential_file,
        source_chart,
        expected_bad_tag_target=remote.main_tip,
    )
    assert main(["plan", "--for", "withdraw", *common]) == EXIT_OK
    withdraw_plan = json.loads(capsys.readouterr().out)["plan"]  # type: ignore[attr-defined]
    assert withdraw_plan["command"] == "withdraw"
    assert withdraw_plan["destructive_scope"] == [
        "pages:ledger-api 0.4.0",
        "pages:ledger-api-0.4.0.tgz",
        "release:chart-v0.4.0",
        "tag:refs/tags/chart-v0.4.0",
    ]
    assert withdraw_plan["force_update_required"] is True

    assert main(["plan", "--for", "repair", *common]) == EXIT_OK
    repair_output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert repair_output["plan"]["command"] == "repair"
    assert repair_output["audit"]["healthy"] is False
    assert remote.refs().get("refs/tags/chart-v0.4.0") == remote.main_tip


def test_cli_redacts_the_token_from_errors(
    tmp_path: Path,
    remote: Remote,
    local_clone: Path,
    credential_file: Path,
    published_pages: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    in_clone: Path,
) -> None:
    wire(monkeypatch, remote, local_clone, tmp_path)
    remote.api.fail_next("GET", "/git/matching-refs/", 401, times=10)
    code = main(
        [
            "audit",
            "--contract",
            str(contract_path(tmp_path, remote)),
            "--credentials",
            str(credential_file),
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    assert code == EXIT_REMOTE
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out


def test_created_option_makes_the_index_reproducible(
    tmp_path: Path,
    remote: Remote,
    local_clone: Path,
    credential_file: Path,
    published_pages: dict[str, bytes],
    source_chart: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
    in_clone: Path,
) -> None:
    wire(monkeypatch, remote, local_clone, tmp_path)
    code = main(
        [
            "publish",
            "--force",
            "--created",
            "2026-07-08T09:10:11Z",
            *common_args(tmp_path, remote, credential_file, source_chart),
        ]
    )
    assert code == EXIT_OK
    capsys.readouterr()  # type: ignore[attr-defined]
    text = remote.pages_files()["index.yaml"].decode("utf-8")
    assert "2026-07-08T09:10:11Z" in text


def test_main_requires_a_command(capsys: object) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_run_plan_offline_without_remote_reads(
    tmp_path: Path, remote: Remote, local_clone: Path, source_chart: Path
) -> None:
    from .conftest import make_session

    session = make_session(
        tmp_path=tmp_path, remote=remote, local_clone=local_clone, chart_dir=source_chart
    )
    payload = cli.run_plan(session, "publish", offline=True)
    assert payload["remote_inspected"] is False
    assert payload["candidate"]["sha256"]
    assert remote.api.calls == [], "offline planning contacts nothing"


def test_run_plan_repair_offline(
    tmp_path: Path, remote: Remote, local_clone: Path, source_chart: Path
) -> None:
    from .conftest import make_session

    session = make_session(
        tmp_path=tmp_path, remote=remote, local_clone=local_clone, chart_dir=source_chart
    )
    payload = cli.run_plan(session, "repair", offline=True)
    assert payload["audit"]["findings"][0]["code"] == "pages-branch-missing"


CommandBuilder = Callable[[], list[str]]
