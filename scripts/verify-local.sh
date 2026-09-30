#!/usr/bin/env bash
# The same gate CI runs, for use before pushing. Read-only: touches no remote.
set -euo pipefail

ruff format --check .
ruff check .
mypy
pytest --cov=chartpub --cov-report=term-missing --cov-fail-under=90
python -m build

helm lint --strict charts/ledger-api
helm template ledger-api charts/ledger-api >/dev/null
for values in tests/fixtures/values-*.yaml; do
  helm template ledger-api charts/ledger-api -f "${values}" >/dev/null
done

# Validate the packaged archive, not just the source tree: the archive is what
# gets published, and the structural checks below are what caught the 0.4.0
# selector/label defect that `helm lint` happily passed.
out="$(mktemp -d)"
trap 'rm -rf "${out}"' EXIT
python - "${out}" <<'PY'
import json
import sys
from pathlib import Path

from chartpub.archive import package_chart
from chartpub.validate import validate_candidate

artifact = package_chart(Path("charts/ledger-api"), Path(sys.argv[1]), "0.4.1")
report = validate_candidate(
    artifact, values_files=sorted(Path("tests/fixtures").glob("values-*.yaml"))
)
print(json.dumps(report.to_dict(), indent=2))
print("digest:", artifact.sha256)
report.raise_for_status()
PY
