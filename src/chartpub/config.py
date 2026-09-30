from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chartpub.errors import ContractError
from chartpub.models import PublicationContract


def load_contract(path: Path) -> PublicationContract:
    """Load and strictly validate the publication contract.

    Strictness is a safety property, not pedantry: the contract is the only
    thing that names which tag, release and index entry a destructive command
    may touch, so a typo must fail loudly rather than widen the blast radius.
    """
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load publication contract: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractError("publication contract must be a JSON object")
    try:
        return PublicationContract.from_mapping(raw)
    except ContractError:
        raise
    except (TypeError, KeyError, ValueError) as exc:
        raise ContractError(f"malformed publication contract: {exc}") from exc
