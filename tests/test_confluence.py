from pathlib import Path

import pytest

from gonogo.confluence import (
    TableNotFoundError,
    apply_versions,
    describe_table,
    parse_storage,
    serialise_storage,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_page.xhtml"
COLUMNS = {
    "client": ["Client", "Platform"],
    "max": ["MAX Version Number", "MAX"],
    "dplus": ["DPlus Version Number", "D+"],
}


@pytest.fixture
def body() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_round_trip_preserves_macros_and_self_closing_tags(body):
    unchanged = serialise_storage(parse_storage(body))
    assert 'ac:name="status"' in unchanged
    assert "<br/>" in unchanged or "<br />" in unchanged
    assert "<ri:user" in unchanged
    # Namespace prefixes survive without being redeclared on every macro.
    assert "xmlns:ac" not in unchanged


def test_updates_only_the_targeted_cells(body):
    updates = {"Web": {"max": "7.12.0.133", "dplus": "7.12.0.96"}}
    new_body, changes, unmatched = apply_versions(body, updates, COLUMNS)

    assert {(c.row, c.new) for c in changes} == {("Web", "7.12.0.133"), ("Web", "7.12.0.96")}
    assert not unmatched
    assert "7.12.0.133" in new_body and "7.12.0.96" in new_body
    # Other rows and the status macro are untouched.
    assert "<p>7.12.0.66</p>" in new_body
    assert 'ac:name="status"' in new_body
    assert "<p>GO</p>" not in new_body


def test_rerunning_with_the_same_versions_reports_no_changes(body):
    updates = {"Android Mobile": {"max": "7.12.0.66", "dplus": "21.12.0.66"}}
    new_body, changes, _ = apply_versions(body, updates, COLUMNS)
    assert changes == []
    assert new_body == serialise_storage(parse_storage(body))


def test_fills_empty_cells(body):
    updates = {"Roku": {"max": "7.12.0.49", "dplus": "7.12.0.50"}}
    new_body, changes, _ = apply_versions(body, updates, COLUMNS)
    assert [(c.old, c.new) for c in changes] == [("", "7.12.0.49"), ("", "7.12.0.50")]
    assert "7.12.0.49" in new_body


def test_row_matching_ignores_case_and_punctuation(body):
    updates = {"apple ios": {"max": "7.12.0.73"}}
    _, changes, unmatched = apply_versions(body, updates, COLUMNS)
    assert [c.row for c in changes] == ["Apple iOS"]
    assert not unmatched


def test_rows_missing_from_the_table_are_reported(body):
    updates = {"Vega": {"max": "7.12.0.43", "dplus": "21.12.0.43"}}
    _, changes, unmatched = apply_versions(body, updates, COLUMNS)
    assert changes == []
    assert unmatched == ["Vega"]


def test_rows_absent_from_config_are_left_alone(body):
    # Playstation is in the table but not in the update set.
    new_body, _, _ = apply_versions(body, {"Web": {"max": "7.12.0.133"}}, COLUMNS)
    assert "<p>Playstation</p>" in new_body
    assert "<p>7.12.0.10</p>" in new_body


def test_missing_version_columns_raise(body):
    with pytest.raises(TableNotFoundError):
        apply_versions(body, {"Web": {"max": "7.12.0.133"}}, {"client": ["Client"], "max": ["Nope"], "dplus": ["Nah"]})


def test_named_entities_survive_parsing(body):
    new_body, _, _ = apply_versions(body, {"Web": {"max": "7.12.0.133"}}, COLUMNS)
    assert "&#160;" in new_body or "&nbsp;" in new_body


def test_describe_table_reports_headers_and_rows(body):
    shape = describe_table(body, COLUMNS)
    assert shape.headers == ["Client", "MAX Version Number", "DPlus Version Number", "Go / No-Go"]
    assert shape.row_labels == ["Web", "Apple iOS", "Apple VisionOS", "Android Mobile", "Roku", "Playstation"]
    assert shape.indices == {"client": 0, "max": 1, "dplus": 2}
