# chartpub operator guide

`chartpub` publishes, withdraws and recovers one Helm chart in one GitHub
repository whose `gh-pages` branch is served by GitHub Pages. Everything it may
touch is named by `publication-contract.json`; anything not named there is out of
scope by construction.

- [What went wrong, and what changed](#what-went-wrong-and-what-changed)
- [State machine](#state-machine)
- [Publication order](#publication-order)
- [Test install modes](#test-install-modes)
- [Withdrawal order](#withdrawal-order)
- [Recovery procedure](#recovery-procedure)
- [Dry-run examples](#dry-run-examples)
- [Exact destructive scope](#exact-destructive-scope)
- [Exit codes](#exit-codes)
- [Credential handling](#credential-handling)
- [Independent verification](#independent-verification)

---

## What went wrong, and what changed

Five defects combined into the staging incident.

1. **The advertised chart could not be installed.** `ledger-api` 0.4.0's
   Deployment declared `spec.selector.matchLabels` with
   `app.kubernetes.io/component: api` while its pod template carried
   `component: worker`. Kubernetes refuses such a Deployment ("selector does not
   match template labels") and the Service selector matched no pod either.
   `helm lint` and `helm template` both pass on it, so nothing stopped it.
   *Fix:* the pod template label now matches the selector, and
   `chartpub.validate.manifest_problems` inspects rendered objects for exactly
   this class of defect, in addition to lint, rendering every values fixture,
   archive inspection, digest verification and an isolated test install.
2. **Pages could move before the artifact existed.** The old publisher wrote the
   index first. A client could resolve an entry whose bytes were absent or
   different. *Fix:* the release asset is uploaded and verified by download
   **before** the Pages snapshot changes.
3. **Packaging was not reproducible.** Archive member order, mtimes, ownership
   and modes came from the build machine, so the published digest was not a
   usable integrity check and a re-run produced different bytes. *Fix:*
   `chartpub.archive.package_chart` sorts members and normalises every field;
   identical inputs give an identical digest.
4. **Re-running duplicated work.** `add_artifact` appended, so republishing added
   a second entry for the same version. *Fix:* the index upsert is idempotent,
   `generated` is derived from entry timestamps rather than "now", and an
   unchanged index is not rewritten or pushed.
5. **Nothing checked the remote before writing.** *Fix:* every mutation is gated
   on a compare-and-swap check, and the `gh-pages` update uses
   `git push --force-with-lease`.

## State machine

A contract-named version is in one of four states.

```
                  publish                     withdraw
   ┌──────────┐ ───────────▶ ┌───────────┐ ───────────▶ ┌─────────────┐
   │  absent  │              │ published │              │ quarantined │
   └──────────┘ ◀─────────── └───────────┘ ◀─────────── └─────────────┘
        ▲          withdraw        ▲          publish          ▲
        │                          │                           │
        └──────────── repair ──────┴─────────── repair ────────┘
                (reconciles any partial state)
```

| State | Tag | GitHub release | Pages archive | Index entry |
|---|---|---|---|---|
| `absent` | no | none | no | no |
| `published` | yes | published, asset attached | yes | yes |
| `quarantined` | **no** | **draft**, same id and `tag_name`, asset retained | **no** | **no** |

`partial` is not a state you choose: it is any disagreement between those four
columns. `chartpub audit` names the disagreements; `chartpub repair` resolves
them and is idempotent.

Internally a `publish` walks these journal phases, recorded in
`.chartpub/transaction.json`:

```
planned → validated → release-drafted → asset-uploaded → asset-verified
        → release-published → pages-updated → complete
```

Everything up to and including `asset-verified` is **not publicly
discoverable** — the release is still a draft, so it has no tag and is not
listed. A failure anywhere is rolled back: because publishing the release is what
creates the public tag, the undo for that step re-drafts the release **and**
deletes the tag it created, so a failure at `pages-updated` cannot leave an
orphan tag behind. A tag or release that already existed before the run is never
deleted. If an undo itself fails, the command exits `6` and names what is left.

## Publication order

`chartpub publish` does exactly this, and never reorders it:

1. **Package** the chart deterministically (local).
2. **Validate** the packaged archive (local): archive safety and digest, strict
   `helm lint`, render with default values and every `tests/fixtures/values-*.yaml`,
   rendered-manifest invariants, and an isolated `helm install` test release
   (see [Test install modes](#test-install-modes)). A failure stops here, so the
   public index and the current stable version are untouched.
3. **Compare-and-swap** the `gh-pages` tip against the expected value. Mismatch
   stops before any write.
4. **Create the release as a draft** (no tag yet, not listed).
5. **Upload the immutable asset.**
6. **Download the asset back** and compare both the bytes and the SHA-256.
7. **Publish the release**, which is what creates the public tag.
8. **Update the Pages snapshot** — index entry plus archive — with
   `git push --force-with-lease` against the leased tip. Only now is the version
   discoverable.

Steps 4–7 register undo actions, including the tag that step 7 creates. If a
later step fails, they are undone newest-first; if an undo itself fails, the
command exits `6` and names every object that still needs attention rather than
claiming a clean rollback.

## Test install modes

`--install-mode` controls how the isolated test release is exercised. The
release name is always `chartpub-verify-<chart>-<version>` and the namespace
`<release>-ns`; nothing outside those is touched.

| Mode | What it does | Catches the 0.4.0 defect? |
|---|---|---|
| `cluster` | a real `helm install` into the throwaway namespace, then `helm uninstall` and namespace delete | **yes** — the API server rejects the Deployment |
| `server` | `helm install --dry-run=server`; needs a cluster, writes nothing | no — a server-side dry run accepts it |
| `skip` | does not install; reports "not exercised" and why | no |
| `auto` (default) | `server` if a cluster answers, otherwise `skip` | see above |

Two things worth knowing:

- **There is no useful offline install.** `helm install --dry-run=client` still
  initialises Helm's release storage against the API server, so it fails with
  "cluster unreachable" even in client mode. `auto` therefore reports an honest
  skip rather than pretending a client-side rehearsal happened.
- **A server-side dry run is not sufficient.** The 0.4.0 selector/label mismatch
  passes `--dry-run=server` and is only rejected by a real create. That is why
  `manifest_problems` checks the invariant structurally — it catches the defect
  deterministically, with or without a cluster — and why CI provisions a `kind`
  cluster and runs `install_mode="cluster"`. CI also reconstructs the 0.4.0
  defect and asserts that validation rejects it, so the gate cannot silently
  regress.

## Withdrawal order

`chartpub withdraw` is the reverse: discoverability goes first, so the bad bytes
stop being resolvable as early as possible.

1. **Compare-and-swap** on the `gh-pages` tip *and* on
   `refs/tags/<bad_tag>` → `expected_bad_tag_target`.
2. **Remove the index entry and the archive** from the Pages snapshot
   (`--force-with-lease`).
3. **Quarantine the release**: `draft: true`, name prefixed `WITHDRAWN:`,
   explanatory body. The release **id** and **`tag_name`** are unchanged, and its
   asset is kept, so the object stays as recovery evidence.
4. **Delete exactly `refs/tags/<bad_tag>`**, refusing if it has moved.

## Recovery procedure

```bash
# 0. Establish the current state. Read-only.
chartpub audit --live --no-strict | tee audit-before.json

# 1. Review both transitions before touching anything.
chartpub plan --for withdraw
chartpub plan --for publish

# 2. Withdraw the bad version (exact scope; see below).
chartpub withdraw --dry-run
chartpub withdraw --force

# 3. Publish the replacement.
chartpub publish --dry-run
chartpub publish --force

# 4. Confirm, then re-audit.
chartpub audit --live
```

If any step stops with exit `4`, the remote moved. Re-read `audit`, decide
whether the new tip is legitimate, and re-run with an explicit
`--expect-pages-tip <sha>`. Never work around a conflict with a plain force push.

If a run died mid-flight:

```bash
cat .chartpub/transaction.json     # how far it got; contains no secrets
chartpub repair --dry-run          # what reconciliation would do
chartpub repair --force
```

`repair` is declarative and idempotent. It:

- rebuilds `index.yaml` from the chart archives that are actually retrievable;
- restores an archive that is missing, that disagrees in size with its release
  asset, or whose digest `audit` flagged, by downloading the **release asset** —
  release assets are immutable, so they are authoritative, and the index digest
  is never rewritten to bless tampered Pages bytes;
- drops entries whose archive cannot be retrieved, plus orphan and unreadable
  archives, and reports them as `dropped_files`;
- publishes an advertised replacement release that is still a draft, which also
  recreates its tag;
- quarantines a still-published withdrawn release and deletes a leftover
  withdrawn tag;
- recreates a deleted `gh-pages` branch from the release assets.

It **passes every non-generated file through untouched** — `index.html`,
`404.html`, `README.md`, `.nojekyll`, `CNAME`, docs directories, anything you
added — and keeps a `*.tgz.prov` provenance sidecar alongside the archive it
signs. Only `index.yaml` and chart archives are regenerated.

`repair` needs `--force` when its plan contains a destructive step, exactly like
`withdraw`. After applying changes it **re-audits** and, if drift it cannot fix
survives, exits `3` naming the remaining findings rather than reporting a
convergence that did not happen. Some states are deliberately out of its scope:
an advertised version with no release at all needs `chartpub publish`, because
recreating a release means re-deriving and re-validating an artifact.

Running `repair` twice changes nothing the second time.

`publish` deliberately refuses to create `gh-pages`; use `repair` for that, so a
misconfigured `pages_branch` cannot quietly create a new branch.

## Dry-run examples

`--dry-run` performs **no remote write**. It packages and validates locally,
reads the remote, and prints the plan. `plan` is read-only unconditionally.

```bash
$ chartpub plan --for withdraw
{
  "command": "plan",
  "plan": {
    "command": "withdraw",
    "force_update_required": true,
    "destructive_scope": [
      "pages:ledger-api 0.4.0",
      "pages:ledger-api-0.4.0.tgz",
      "release:chart-v0.4.0",
      "tag:refs/tags/chart-v0.4.0"
    ],
    "pages_changes": [ {"action": "remove-index-entry", "destructive": true, ...} ],
    "release_changes": [ {"action": "quarantine", "destructive": true, ...} ],
    "tag_changes": [ {"action": "delete", "destructive": true, ...} ],
    "preconditions": [
      {"subject": "refs/heads/gh-pages", "expected": "c94d20b…", "actual": "c94d20b…", "satisfied": true},
      {"subject": "refs/tags/chart-v0.4.0", "expected": "39f52ee…", "actual": "39f52ee…", "satisfied": true}
    ]
  },
  "remote_inspected": true
}
```

```bash
$ chartpub publish --dry-run
{
  "applied": false,
  "artifact": {"name": "ledger-api-0.4.1.tgz", "sha256": "e506c3ab…", "size": 1276},
  "validation": {"ok": true, "checks": [ … "manifest-invariants", "test-install" ]},
  "plan": {"local_changes": [...], "release_changes": [...], "pages_changes": [...]}
}
```

Plans always separate **`local_changes`**, **`release_changes`**,
**`tag_changes`** and **`pages_changes`**, and set
**`force_update_required`** when any step would remove or replace something
already public. A step marked `"noop": true` is something already in the desired
state; `remote_is_noop` is true when nothing remote would change at all.

Other useful read-only invocations:

```bash
chartpub plan --offline              # no credential, no network: local plan only
chartpub publish --dry-run --install-mode cluster   # strongest validation, still no remote write
chartpub audit --no-strict           # report drift without failing
chartpub plan --for repair           # what repair would reconcile
```

`audit` findings each carry a `remedy` naming what resolves them. `audit` reports
`orphan-replacement-tag` when `refs/tags/<replacement_tag>` exists but the index
does not advertise that version — the shape a rolled-back publication would leave
if the rollback had failed partway.

`--created <ISO-8601 Z>` pins the `created` timestamp of new index entries, which
makes the published `index.yaml` byte-reproducible.

## Exact destructive scope

`chartpub withdraw` touches **only** these four objects, all derived from
`publication-contract.json`:

| Object | Action |
|---|---|
| `refs/tags/<bad_tag>` | deleted, only if it still points at `expected_bad_tag_target` |
| GitHub release for `<bad_tag>` | **converted to a draft**; id and `tag_name` preserved, asset retained |
| `<chart>-<bad_version>.tgz` on `<pages_branch>` | removed from the snapshot |
| the `<bad_version>` entry in `index.yaml` | removed |

`chartpub publish` creates `refs/tags/<replacement_tag>` (as a side effect of
publishing the release), one release with one asset, and one index entry plus one
archive on `<pages_branch>`.

Guarantees that hold for every command:

- No release is ever deleted except a **draft this same run created** during
  rollback. A pre-existing release is never deleted. A release this run published
  is re-drafted, not deleted, if a later step fails.
- No release asset is ever deleted except one **this same run uploaded** during
  rollback. Release assets are treated as immutable: if the asset already exists
  with different bytes, the command stops (exit `4`) instead of replacing it.
- Only the two contract-named tags are ever created or deleted, always with an
  expected-SHA check, and `<replacement_tag>` is only deleted when this same run
  created it. No other ref, branch or tag is touched.
- `<source_branch>` is only ever **fast-forwarded**; a non-fast-forward is
  refused locally and by the remote.
- Repository visibility, secrets, credential scopes, collaborators, branch
  protections, rulesets, the default branch and Pages settings are never read for
  modification and never written. There is no code path that calls those APIs.
- Unrelated charts and unrelated versions in `index.yaml` are always preserved,
  and every non-generated file on the Pages branch survives a rebuild.
- Every `gh-pages` update is `git push --force-with-lease=<ref>:<expected-old>`.
  There is no unguarded `--force` anywhere in the codebase.

## Exit codes

| Code | Name | Meaning | What to do |
|---|---|---|---|
| `0` | ok | the command succeeded; a no-op run also exits `0` | nothing |
| `1` | internal | an unexpected error | file a bug with the redacted message |
| `2` | usage | bad contract, missing/unreadable credential, origin/contract mismatch, or a destructive plan without `--force` | fix the input and re-run |
| `3` | validation | the candidate failed validation, or `audit`/`repair` found drift it did not resolve | fix the chart, or run the command the finding's remedy names |
| `4` | conflict | a remote precondition no longer matches — **nothing was written** | re-read `audit`, then re-run with an explicit `--expect-pages-tip` |
| `5` | remote | a GitHub or Git operation failed | retry; then `repair` |
| `6` | rollback | a partial attempt could **not** be fully rolled back | read the named objects in the message, then `repair` |

Exit `4` is always raised *before* the write it guards, so a conflict never
leaves partial state.

## Credential handling

- The credential file is read **at runtime only**, from
  `~/.config/agent-eval/github-helm-publish.env` by default or from
  `--credentials <path>`. Format is `KEY=VALUE`; `export` prefixes, quotes and
  `#` comments are accepted. Recognised token variables: `GITHUB_TOKEN`,
  `GH_TOKEN`, `CHARTPUB_TOKEN`.
- The token is **never** written to the repository, to Git configuration, to a
  remote URL, or to the transaction journal. `git` receives it through a
  short-lived `GIT_ASKPASS` helper in a `0700` temporary directory that reads it
  from the child process environment; the helper file contains no secret and the
  directory is removed when the command ends. The remote URL stays
  credential-free.
- `helm` and `kubectl` are invoked with credential variables stripped from their
  environment.
- Every message that can reach a terminal passes through a redactor that replaces
  known secrets **and** anything credential-shaped (`ghp_…`, `gho_…`, `ghs_…`,
  `github_pat_…`, `x-access-token:…`), so a token echoed by an API error body
  cannot leak. URLs in error messages are reduced to scheme, host and path.
- Before any mutation, the owner/repository resolved from the **credential-free
  `origin` URL** must equal `contract.repository`, and a `GITHUB_REPOSITORY` in
  the credential file must agree too. A mismatch exits `2` and writes nothing.
- The journal records only non-secret facts: object names, numeric ids, digests,
  commit SHAs and phase markers.
- Tests never read the real credential file, never open a socket, and never touch
  the real repository; `tests/conftest.py` enforces all three.

## Independent verification

Confirm the outcome without trusting `chartpub`. Read-only; needs no token for
the public parts.

```bash
REPO=5-6-Agent-Testing-Ring-3/helm-repository-withdrawal-and-republish-v3-12-b
PAGES=https://5-6-agent-testing-ring-3.github.io/helm-repository-withdrawal-and-republish-v3-12-b

# Refs: the withdrawn tag is gone, the replacement tag exists.
git ls-remote "https://github.com/$REPO.git"

# The published index: 0.4.1 advertised, 0.4.0 absent.
curl -fsS "$PAGES/index.yaml"

# The withdrawn archive is no longer served.
curl -o /dev/null -sS -w '%{http_code}\n' "$PAGES/ledger-api-0.4.0.tgz"   # expect 404

# The advertised digest matches the bytes actually served.
curl -fsSLo /tmp/ledger-api-0.4.1.tgz "$PAGES/ledger-api-0.4.1.tgz"
shasum -a 256 /tmp/ledger-api-0.4.1.tgz

# A fresh Helm client can discover and render the replacement.
helm repo add ledger-verify "$PAGES" && helm repo update ledger-verify
helm search repo ledger-verify --versions
helm template check ledger-verify/ledger-api --version 0.4.1 | head -40
helm repo remove ledger-verify

# The quarantined release is not publicly listed but still exists for evidence
# (the second command needs a token).
curl -fsS "https://api.github.com/repos/$REPO/releases" | jq '[.[]|{tag_name,draft}]'
gh release view chart-v0.4.0 --repo "$REPO" --json name,isDraft,assets
```

`chartpub audit --live` performs the same checks and reports them as structured
findings; `--no-strict` reports without failing.
