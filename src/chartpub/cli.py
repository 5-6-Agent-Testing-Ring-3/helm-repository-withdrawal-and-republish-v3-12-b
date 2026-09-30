"""The ``chartpub`` command line.

Commands: ``plan``, ``publish``, ``withdraw``, ``audit``, ``repair``.

Every command prints one JSON object on stdout so a run can be diffed and
archived. Human-readable failures go to stderr, always redacted. Exit codes are
defined in :mod:`chartpub.errors` and documented in ``docs/OPERATIONS.md``.

``--dry-run`` performs no remote write: it packages and validates locally, reads
the remote, and prints the plan. ``plan`` is read-only unconditionally, and
degrades to a local-only plan when no credential is available.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from chartpub import gitops, lifecycle
from chartpub.config import load_contract
from chartpub.errors import EXIT_INTERNAL, EXIT_OK, AuditDrift, ChartpubError, UsageError
from chartpub.github import GitHubClient
from chartpub.models import PublicationContract, RemoteSnapshot
from chartpub.security import Redactor, read_env_file, require_token, secret_values
from chartpub.state import STATE_DIR_NAME, Journal
from chartpub.validate import default_runner, discover_values_fixtures

COMMANDS = ("plan", "publish", "withdraw", "audit", "repair")

DEFAULT_CREDENTIAL_FILE = "~/.config/agent-eval/github-helm-publish.env"

_HELP = {
    "plan": "show the deterministic plan for a transition without changing anything",
    "publish": "publish the contract's replacement version as a recoverable transaction",
    "withdraw": "withdraw exactly the contract's bad version",
    "audit": "check the configured public repository against the contract",
    "repair": "reconcile a partially applied transition; idempotent",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chartpub",
        description="Deterministic publication, withdrawal and recovery for a "
        "GitHub Pages Helm repository.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        command = subcommands.add_parser(name, help=_HELP[name])
        command.add_argument("--contract", type=Path, default=Path("publication-contract.json"))
        command.add_argument(
            "--credentials",
            type=Path,
            default=None,
            help="operator credential file (KEY=VALUE). Read at runtime only, never stored.",
        )
        command.add_argument(
            "--chart-dir",
            type=Path,
            default=None,
            help="chart source directory (default: charts/<chart> from the contract)",
        )
        command.add_argument(
            "--values-dir",
            type=Path,
            default=Path("tests/fixtures"),
            help="directory of values-*.yaml fixtures every candidate must render with",
        )
        command.add_argument(
            "--state-dir",
            type=Path,
            default=Path(STATE_DIR_NAME),
            help="where the resumable transaction journal lives",
        )
        command.add_argument(
            "--expect-pages-tip",
            default=None,
            help="commit the gh-pages lease is taken against (default: journal, then contract)",
        )
        command.add_argument(
            "--created",
            default=None,
            help="index 'created' timestamp for new entries (ISO-8601 Z), for reproducible output",
        )
        command.add_argument(
            "--offline",
            action="store_true",
            help="never read a credential or contact the remote",
        )
        if name in {"plan", "publish", "withdraw", "repair"}:
            command.add_argument(
                "--dry-run",
                action="store_true",
                help="print the plan and perform no remote write",
            )
        if name in {"publish", "withdraw", "repair"}:
            command.add_argument(
                "--force",
                action="store_true",
                help="accept the destructive steps the plan lists",
            )
        if name in {"plan", "publish"}:
            command.add_argument(
                "--server-side-install",
                dest="server_side",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="force or forbid server-side validation of the isolated test install",
            )
        if name == "plan":
            command.add_argument(
                "--for",
                dest="target",
                choices=("publish", "withdraw", "repair"),
                default="publish",
                help="which transition to plan",
            )
        if name == "audit":
            command.add_argument(
                "--live",
                action="store_true",
                help="also fetch the published index over HTTPS and report what a client sees",
            )
            command.add_argument(
                "--no-strict",
                dest="strict",
                action="store_false",
                default=True,
                help="report findings without failing",
            )
    return parser


def _build_session(
    args: argparse.Namespace,
    contract: PublicationContract,
    stack: ExitStack,
    *,
    offline: bool,
) -> lifecycle.Session:
    repo = gitops.GitRepository(Path.cwd())
    token = ""
    redactor = Redactor()
    git_env: dict[str, str] | None = None
    remote_url = ""

    if not offline:
        credential_path = (
            args.credentials
            if args.credentials is not None
            else Path(DEFAULT_CREDENTIAL_FILE).expanduser()
        )
        values = read_env_file(credential_path)
        token = require_token(values)
        redactor = Redactor(secret_values(values))
        configured = values.get("GITHUB_REPOSITORY")
        if configured and configured.lower() != contract.repository.lower():
            raise UsageError(
                "credential file targets a different repository than the publication contract"
            )
        # Refuse before anything else: a checkout pointing elsewhere must never
        # be mutated, even when the token would allow it.
        gitops.resolve_target_repository(repo, contract.repository)
        git_env = stack.enter_context(gitops.askpass_environment(token))
        remote_url = repo.origin_url()

    repo.redact = redactor
    repo.env = dict(git_env) if git_env else None

    return lifecycle.Session(
        contract=contract,
        client=GitHubClient(contract.repository, token, redactor=redactor),
        repo=repo,
        remote_url=remote_url,
        git_env=git_env,
        redactor=redactor,
        journal=Journal(args.state_dir),
        chart_dir=args.chart_dir if args.chart_dir is not None else contract.chart_dir,
        values_files=discover_values_fixtures(args.values_dir),
        helm_runner=default_runner,
        dry_run=bool(getattr(args, "dry_run", False)),
        force=bool(getattr(args, "force", False)),
        created=args.created or "",
        expect_pages_tip=args.expect_pages_tip,
        server_side_install=getattr(args, "server_side", None),
    )


def run_plan(session: lifecycle.Session, target: str, *, offline: bool) -> dict[str, Any]:
    """Build a plan. Read-only: no remote write, ever."""
    session.dry_run = True
    contract = session.contract
    if offline:
        snapshot = RemoteSnapshot(repository=contract.repository, main_tip=None, pages_tip=None)
    else:
        snapshot = lifecycle.collect_snapshot(session)

    payload: dict[str, Any] = {"remote_inspected": not offline}
    if target == "withdraw":
        payload["plan"] = lifecycle.plan_withdraw(session, snapshot).to_dict()
        return payload
    if target == "repair":
        result = lifecycle.audit(session, snapshot=snapshot)
        payload["plan"] = lifecycle.plan_repair(
            session, result, [item.subject for item in result.findings]
        ).to_dict()
        payload["audit"] = result.to_dict()
        return payload

    artifact = None
    if session.chart_dir.is_dir():
        artifact = lifecycle.build_candidate(session, contract.replacement_version)
        payload["candidate"] = {
            "name": artifact.path.name,
            "sha256": artifact.sha256,
            "size": artifact.size,
        }
    else:
        payload["candidate"] = None
    payload["plan"] = lifecycle.plan_publish(session, snapshot, artifact).to_dict()
    return payload


def dispatch(
    args: argparse.Namespace, session: lifecycle.Session, *, offline: bool
) -> dict[str, Any]:
    if args.command == "plan":
        return run_plan(session, args.target, offline=offline)
    if args.command == "publish":
        return lifecycle.publish(session)
    if args.command == "withdraw":
        return lifecycle.withdraw(session)
    if args.command == "repair":
        return lifecycle.repair(session)

    payload = lifecycle.audit_command(session, strict=False)
    if args.live:
        payload["live"] = lifecycle.verify_live(session)
    if args.strict and not payload["healthy"]:
        raise AuditDrift(
            "public repository state does not match the publication contract: "
            + "; ".join(f"{item['code']} ({item['subject']})" for item in payload["findings"])
        )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    redactor = Redactor()
    try:
        contract = load_contract(args.contract)
        offline = bool(args.offline)
        notes: list[str] = []
        with ExitStack() as stack:
            try:
                session = _build_session(args, contract, stack, offline=offline)
            except (UsageError, ChartpubError) as exc:
                # `plan` never mutates anything, so it stays useful without a
                # credential; every other command must refuse.
                if args.command != "plan" or offline:
                    raise
                notes.append(f"remote inspection skipped: {exc}")
                offline = True
                session = _build_session(args, contract, stack, offline=True)
            redactor = session.redactor
            result = dispatch(args, session, offline=offline)
        result["command"] = args.command
        if notes:
            result["notes"] = notes
        print(json.dumps(result, indent=2, sort_keys=True))
        return EXIT_OK
    except ChartpubError as exc:
        print(f"chartpub: {redactor(str(exc))}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("chartpub: interrupted", file=sys.stderr)
        return EXIT_INTERNAL
