# Helm Repository Withdrawal and Republish

This repository contains the `ledger-api` Helm chart and `chartpub`, the operator
CLI used to package the chart, maintain its GitHub Pages index, withdraw a bad
version, and recover from a partial publication.

`publication-contract.json` is the source of truth for what may be touched: the
affected version, the intended replacement, and the exact public refs in scope.
Anything not named there is out of scope by construction.

**Operators: read [`docs/OPERATIONS.md`](docs/OPERATIONS.md).** It documents the
state machine, publication and withdrawal order, the recovery procedure, dry-run
examples, the exact destructive scope, exit codes, credential handling, and
independent verification commands.

## Commands

```bash
chartpub plan     --for publish|withdraw|repair   # read-only; no remote write
chartpub publish  [--dry-run] [--force]           # publish the replacement version
chartpub withdraw [--dry-run] [--force]           # withdraw exactly the bad version
chartpub audit    [--live] [--no-strict]          # check the public repo against the contract
chartpub repair   [--dry-run] [--force]           # reconcile a partial state; idempotent
```

Each command prints one JSON object on stdout. Exit codes are documented in
`docs/OPERATIONS.md` (`0` ok, `2` usage, `3` validation, `4` remote conflict,
`5` remote failure, `6` incomplete rollback).

## Guarantees

- **Deterministic packaging.** Identical inputs produce a byte-identical archive
  and the same SHA-256, so the published digest is a real integrity check.
- **Validate before advertising.** A candidate must pass archive-safety and digest
  checks, strict `helm lint`, rendering with every values fixture, rendered-manifest
  invariants, and an isolated test install. A failure leaves the public index and
  the current stable version untouched.
- **Artifact before discoverability.** The immutable release asset is uploaded and
  verified by downloading it back *before* the Pages snapshot changes.
- **Compare-and-swap everywhere.** Every remote mutation is gated on the tip or
  object identity that was inspected while planning; `gh-pages` is updated with
  `git push --force-with-lease`. There is no unguarded force push.
- **Exact scope.** Withdrawal removes only the bad version's public tag, Pages
  archive and index entry, and converts its release to a draft quarantine record
  without changing the release's identity. Unrelated releases, tags, branches and
  chart versions are never touched.
- **Idempotent.** Re-running any command against an already-correct state is a
  no-op that neither duplicates index entries nor re-uploads assets.
- **No credential exposure.** The token is read at runtime only, never persisted,
  never placed in a URL or Git config, and redacted from all output.

## Development

Python 3.13 and Helm 3 are required.

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
./scripts/verify-local.sh    # the same gate CI runs; touches no remote
```

`scripts/verify-local.sh` runs formatting, Ruff, strict MyPy over `src` and
`tests`, the test suite at ≥90% coverage, a package build, Helm lint, rendering of
every values fixture, and validation of the packaged archive.

The test suite uses temporary Git repositories and a mocked GitHub HTTP transport.
It never reads the real credential file, contacts GitHub, or mutates the real
repository — `tests/conftest.py` fails the run if it tries.
