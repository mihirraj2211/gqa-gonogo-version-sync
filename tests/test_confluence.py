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
    # Other rows, the Notes column and the status macro are untouched.
    assert "<p>7.12.0.10</p>" in new_body           # the Playstation row
    assert "PLAY-128731" in new_body                # the Notes cell
    assert 'ac:name="status"' in new_body
    assert "<p>No Go</p>" in new_body


def test_rerunning_with_the_same_versions_reports_no_changes(body):
    updates = {"Web": {"max": "7.12.0.120", "dplus": "7.12.0.90"}}
    new_body, changes, _ = apply_versions(body, updates, COLUMNS)
    assert changes == []
    assert new_body == serialise_storage(parse_storage(body))


def test_fills_empty_cells(body):
    updates = {"Roku": {"max": "7.12.0.49", "dplus": "7.12.0.50"}}
    new_body, changes, _ = apply_versions(body, updates, COLUMNS)
    assert [(c.old, c.new) for c in changes] == [("", "7.12.0.49"), ("", "7.12.0.50")]
    assert "7.12.0.49" in new_body


def test_row_matching_ignores_case_and_punctuation(body):
    updates = {"cdev": {"max": "7.12.0.132"}}
    _, changes, unmatched = apply_versions(body, updates, COLUMNS)
    assert [c.row for c in changes] == ["CDEV"]
    assert not unmatched


def test_rows_missing_from_the_table_are_reported(body):
    updates = {"Chromecast": {"max": "7.12.0.43", "dplus": "21.12.0.43"}}
    _, changes, unmatched = apply_versions(body, updates, COLUMNS)
    assert changes == []
    assert unmatched == ["Chromecast"]


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
    """The real table repeats its header row, which must not become a client."""
    shape = describe_table(body, COLUMNS)
    assert shape.headers == [
        "Client",
        "GQA Player",
        "GQA AdTech",
        "GQA End-to-End",
        "MAX Version Number",
        "DPlus Version Number",
        "Notes",
    ]
    assert shape.row_labels == [
        "Web", "Roku", "Apple IOS", "Apple TV", "Android", "LB", "CDEV", "Playstation",
    ]
    assert shape.indices == {"client": 0, "max": 4, "dplus": 5}


def test_versions_are_written_as_inline_code(body):
    """The table shows versions as inline code; bare text reads as the odd one
    out next to the hand-typed rows."""
    updates = {"Roku": {"max": "7.12.0.49", "dplus": "7.12.0.50"}}
    new_body, _, _ = apply_versions(body, updates, COLUMNS)

    assert "<p><code>7.12.0.49</code></p>" in new_body
    assert "<p><code>7.12.0.50</code></p>" in new_body


def test_a_cell_left_plain_by_an_older_run_is_reformatted(body):
    """Matching text is not enough to skip a cell: an earlier version of this
    tool wrote bare text, and those rows have to be brought into line."""
    updates = {"Playstation": {"max": "7.12.0.10", "dplus": "7.12.0.10"}}
    new_body, changes, _ = apply_versions(body, updates, COLUMNS)

    assert [(c.new, c.reformat) for c in changes] == [("7.12.0.10", True), ("7.12.0.10", True)]
    assert "<p>7.12.0.10</p>" not in new_body
    assert new_body.count("<p><code>7.12.0.10</code></p>") == 2
    assert "reformatted as inline code" in str(changes[0])


def test_a_cell_already_formatted_is_left_alone(body):
    """Otherwise every run would publish a new page version for nothing."""
    updates = {"Web": {"max": "7.12.0.120", "dplus": "7.12.0.90"}}
    new_body, changes, _ = apply_versions(body, updates, COLUMNS)

    assert changes == []
    assert new_body == serialise_storage(parse_storage(body))


def test_the_code_wrapper_is_not_nested_on_rewrite(body):
    updates = {"Web": {"max": "7.12.0.140"}}
    new_body, _, _ = apply_versions(body, updates, COLUMNS)

    assert "<p><code>7.12.0.140</code></p>" in new_body
    assert "<code><code>" not in new_body


def test_plain_is_still_available(body):
    updates = {"Roku": {"max": "7.12.0.49"}}
    new_body, _, _ = apply_versions(body, updates, COLUMNS, style="plain")

    assert "<p>7.12.0.49</p>" in new_body
    assert "<code>7.12.0.49</code>" not in new_body


def _table(*row_labels: str) -> str:
    """A minimal sign-off table with the given client rows, cells empty."""
    header = (
        "<tr><th><p>Client</p></th><th><p>MAX Version Number</p></th>"
        "<th><p>DPlus Version Number</p></th></tr>"
    )
    rows = "".join(f"<tr><td><p>{label}</p></td><td></td><td></td></tr>" for label in row_labels)
    return f"<table><tbody>{header}{rows}</tbody></table>"


def test_a_row_label_that_carries_its_platforms_still_matches():
    """The live table spells the row "Apple iOS / tvOS / VisionOS" while the
    config calls it "Apple", which left those cells blank."""
    body = _table("Web", "Apple iOS / tvOS / VisionOS")
    updates = {"Apple": {"max": "7.12.0.73"}}

    new_body, changes, unmatched = apply_versions(body, updates, COLUMNS)

    assert [(c.row, c.new) for c in changes] == [("Apple iOS / tvOS / VisionOS", "7.12.0.73")]
    assert not unmatched
    assert "<code>7.12.0.73</code>" in new_body


def test_an_exact_row_wins_over_one_that_merely_extends_it():
    body = _table("Android TV", "Android")
    updates = {"Android": {"max": "7.12.0.66"}}

    _, changes, unmatched = apply_versions(body, updates, COLUMNS)

    assert [c.row for c in changes] == ["Android"]
    assert not unmatched


def test_an_ambiguous_row_is_left_alone_rather_than_guessed(caplog):
    """Writing the mobile build into the TV row would be worse than writing
    nothing, so two candidates means neither."""
    body = _table("Android Mobile", "Android TV")
    updates = {"Android": {"max": "7.12.0.66"}}

    with caplog.at_level("WARNING"):
        _, changes, unmatched = apply_versions(body, updates, COLUMNS)

    assert changes == []
    assert unmatched == ["Android"]
    assert "could be any of" in caplog.text


def test_a_longer_label_is_not_matched_by_a_shorter_word():
    body = _table("Applesauce")
    updates = {"Apple": {"max": "7.12.0.73"}}

    _, changes, unmatched = apply_versions(body, updates, COLUMNS)

    assert changes == []
    assert unmatched == ["Apple"]


def test_two_config_rows_cannot_land_on_the_same_table_row():
    body = _table("Apple iOS")
    updates = {"Apple": {"max": "7.12.0.73"}, "Apple TV": {"max": "7.12.0.99"}}

    _, changes, unmatched = apply_versions(body, updates, COLUMNS)

    assert len(changes) == 1
    assert len(unmatched) == 1


def test_an_alias_matches_a_row_the_name_does_not():
    """`aliases` was config the writer never read, so a table calling the row
    "Leanback" went unwritten while the config looked correct."""
    body = _table("Web", "Leanback")
    updates = {"LB": {"max": "7.12.0.67"}}

    _, changes, unmatched = apply_versions(body, updates, COLUMNS, aliases={"LB": ("Leanback",)})

    assert [c.row for c in changes] == ["Leanback"]
    assert not unmatched


def test_the_two_apple_rows_take_their_own_platform():
    """The live table signs off iOS and tvOS separately; one row apiece."""
    body = _table("Apple IOS", "Apple TV")
    updates = {"Apple IOS": {"max": "7.12.0.73"}, "Apple TV": {"max": "7.12.0.99"}}

    new_body, changes, unmatched = apply_versions(body, updates, COLUMNS)

    assert [(c.row, c.new) for c in changes] == [
        ("Apple IOS", "7.12.0.73"),
        ("Apple TV", "7.12.0.99"),
    ]
    assert not unmatched
    assert "<code>7.12.0.73</code>" in new_body and "<code>7.12.0.99</code>" in new_body
