from __future__ import annotations

import json
from pathlib import Path

import pytest

from chartpub.config import load_contract
from chartpub.errors import ContractError
from chartpub.models import PublicationContract, parse_version, version_sort_key


def valid_contract() -> dict[str, object]:
    return {
        "schema_version": 1,
        "repository": "owner/repo",
        "source_branch": "main",
        "pages_branch": "gh-pages",
        "pages_url": "https://owner.example/repo",
        "chart": "ledger-api",
        "bad_version": "0.4.0",
        "replacement_version": "0.4.1",
        "bad_tag": "chart-v0.4.0",
        "replacement_tag": "chart-v0.4.1",
        "expected_bad_tag_target": "a" * 40,
        "expected_pages_tip": "b" * 40,
        "release_asset_name": "ledger-api-{version}.tgz",
    }


def write_contract(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def load(tmp_path: Path, **overrides: object) -> PublicationContract:
    value = valid_contract()
    value.update(overrides)
    path = tmp_path / "contract.json"
    write_contract(path, value)
    return load_contract(path)


def test_loads_complete_contract(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, valid_contract())
    assert load_contract(path).replacement_version == "0.4.1"


def test_rejects_non_object(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, [])
    with pytest.raises(ContractError, match="JSON object"):
        load_contract(path)


def test_rejects_unknown_key(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    value = valid_contract()
    value["typo_branch"] = "main"
    write_contract(path, value)
    with pytest.raises(ContractError, match="unknown"):
        load_contract(path)


def test_rejects_boolean_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    value = valid_contract()
    value["schema_version"] = True
    write_contract(path, value)
    with pytest.raises(ContractError, match="schema_version"):
        load_contract(path)


def test_rejects_missing_key(tmp_path: Path) -> None:
    value = valid_contract()
    del value["pages_url"]
    path = tmp_path / "contract.json"
    write_contract(path, value)
    with pytest.raises(ContractError, match="missing contract key"):
        load_contract(path)


def test_rejects_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="cannot load"):
        load_contract(tmp_path / "absent.json")


def test_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError, match="cannot load"):
        load_contract(path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema_version": 2}, "unsupported schema_version"),
        ({"schema_version": "1"}, "must be an integer"),
        ({"repository": "no-slash"}, "owner/name"),
        ({"repository": ""}, "non-empty string"),
        ({"repository": 5}, "non-empty string"),
        ({"pages_branch": "main"}, "must differ"),
        ({"bad_tag": "-bad"}, "not a usable Git ref name"),
        ({"pages_url": "ftp://x/y"}, "absolute http"),
        ({"chart": "Ledger_API"}, "lowercase DNS-style"),
        ({"bad_version": "0.4"}, "not a valid semantic version"),
        ({"replacement_version": "0.4.0"}, "must differ from bad_version"),
        ({"bad_version": "0.5.0"}, "must be newer"),
        ({"expected_pages_tip": "abc"}, "40-character commit SHA"),
        ({"expected_bad_tag_target": "A" * 40}, "40-character commit SHA"),
        ({"release_asset_name": "chart.tgz"}, "{version} placeholder"),
        ({"release_asset_name": "sub/dir-{version}.tgz"}, "plain \\*.tgz file name"),
        ({"release_asset_name": "{version}.zip"}, "plain \\*.tgz file name"),
        ({"release_asset_name": ".{version}.tgz"}, "plain \\*.tgz file name"),
    ],
)
def test_rejects_malformed_values(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ContractError, match=message):
        load(tmp_path, **overrides)


def test_rejects_identical_tags(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="bad_tag and replacement_tag must differ"):
        load(tmp_path, replacement_tag="chart-v0.4.0")


def test_derived_helpers(tmp_path: Path) -> None:
    contract = load(tmp_path)
    assert contract.owner == "owner"
    assert contract.name == "repo"
    assert contract.chart_dir == Path("charts/ledger-api")
    assert contract.asset_name("0.4.1") == "ledger-api-0.4.1.tgz"
    assert contract.asset_url("0.4.1") == "https://owner.example/repo/ledger-api-0.4.1.tgz"
    assert contract.tag_for("0.4.0") == "chart-v0.4.0"
    assert contract.tag_for("0.4.1") == "chart-v0.4.1"
    assert contract.to_dict()["chart"] == "ledger-api"


def test_tag_for_refuses_unnamed_version(tmp_path: Path) -> None:
    contract = load(tmp_path)
    with pytest.raises(ContractError, match="not named by the publication contract"):
        contract.tag_for("9.9.9")


def test_parse_version_and_sort_key() -> None:
    assert parse_version("1.2.3-rc.1") == (1, 2, 3, "rc.1")
    with pytest.raises(ContractError, match="not a valid semantic version"):
        parse_version("1.2")
    assert version_sort_key("not-a-version")[0] == -1
    assert version_sort_key("1.0.0") > version_sort_key("1.0.0-rc.1")
    assert version_sort_key("1.0.0-rc.2") > version_sort_key("1.0.0-rc.1")
    assert version_sort_key("1.0.0-alpha") < version_sort_key("1.0.0-beta")
    assert sorted(["0.10.0", "0.9.0"], key=version_sort_key) == ["0.9.0", "0.10.0"]
