from dataclasses import replace
from pathlib import Path

from gonogo.config import load_config
from gonogo.providers import PlatformBuild
from gonogo.sync import plan_updates, resolve_train, train_mismatch_hint
from gonogo.versions import SCHEME_NONE

CONFIG = Path(__file__).resolve().parents[1] / "config" / "clients.yml"


def build(platform: str, max_version: str, dplus_version: str | None = None) -> PlatformBuild:
    return PlatformBuild(platform=platform, max_version=max_version, dplus_version=dplus_version)


def test_offset_and_base_rows_get_the_right_dplus_scheme():
    config = load_config(CONFIG)
    builds = {
        "ios": build("ios", "7.12.0.73", "21.12.0.16"),
        "roku": build("roku", "7.12.0.49", "7.12.0.50"),
        "web": build("web", "7.12.0.133", "7.12.0.96"),
    }
    updates, _ = plan_updates(config, builds)

    assert updates["Apple IOS"] == {"max": "7.12.0.73", "dplus": "21.12.0.16"}
    assert updates["Roku"] == {"max": "7.12.0.49", "dplus": "7.12.0.50"}
    assert updates["Web"] == {"max": "7.12.0.133", "dplus": "7.12.0.96"}


def test_a_missing_dplus_leaves_the_cell_alone(caplog):
    """MAX lands, D+ is left as it was.

    This is the live iOS and tvOS case: the D-Plus product reports no build for
    them, and their real D+ build number is nothing like MAX's, so writing
    anything here would be a guess published on a sign-off page.

    A row covering several devices also disagrees loudly rather than silently:
    see test_a_row_covering_two_devices_warns_when_they_disagree.
    """
    config = load_config(CONFIG)
    updates, _ = plan_updates(config, {"androidtv": build("androidtv", "7.12.0.43")})

    assert updates["LB"] == {"max": "7.12.0.43"}
    assert "leaving that cell as it is" in caplog.text


def test_borrowing_the_max_build_can_be_turned_on():
    config = load_config(CONFIG)
    config = replace(config, release=replace(config.release, derive_missing_dplus=True))
    updates, _ = plan_updates(config, {"androidtv": build("androidtv", "7.12.0.43")})
    assert updates["LB"] == {"max": "7.12.0.43", "dplus": "21.12.0.43"}


def test_a_dplus_version_off_its_scheme_is_flagged(caplog):
    """A base-scheme number on an offset row means the row is mapped wrong."""
    config = load_config(CONFIG)
    androidtv = replace(build("androidtv", "7.12.0.43"), dplus_version="7.12.0.43")
    updates, _ = plan_updates(config, {"androidtv": androidtv})

    # Still written as reported: the source is authoritative, but say so loudly.
    assert updates["LB"] == {"max": "7.12.0.43", "dplus": "7.12.0.43"}
    assert "not a offset-scheme version" in caplog.text
    assert "expected major 21" in caplog.text


def test_a_row_that_ships_no_dplus_keeps_its_cell():
    """No table row is scheme `none` today, so build one to hold the line."""
    config = load_config(CONFIG)
    apple = config.clients[2]
    config = replace(config, clients=(replace(apple, dplus_scheme=SCHEME_NONE),))
    builds = {"ios": build("ios", "7.12.0.73", "21.12.0.16")}

    updates, _ = plan_updates(config, builds)
    assert updates["Apple IOS"] == {"max": "7.12.0.73"}


def test_a_row_covering_two_devices_uses_the_first_that_reported():
    """Android is the phone then the Fire tablet; LB is Android TV then Fire TV."""
    config = load_config(CONFIG)
    builds = {
        "firetablet": build("firetablet", "7.12.0.66", "21.12.0.66"),
        "firetv": build("firetv", "7.12.0.67", "21.12.0.67"),
    }

    updates, _ = plan_updates(config, builds)

    assert updates["Android"] == {"max": "7.12.0.66", "dplus": "21.12.0.66"}
    assert updates["LB"] == {"max": "7.12.0.67", "dplus": "21.12.0.67"}


def test_a_row_covering_two_devices_warns_when_they_disagree(caplog):
    """One row cannot sign off two numbers, so the difference must be audible."""
    config = load_config(CONFIG)
    builds = {
        "androidtv": build("androidtv", "7.12.0.67", "21.12.0.67"),
        "firetv": build("firetv", "7.12.0.43", "21.12.0.43"),
    }

    updates, _ = plan_updates(config, builds)

    assert updates["LB"] == {"max": "7.12.0.67", "dplus": "21.12.0.67"}
    assert "LB covers androidtv and firetv, which disagree" in caplog.text
    assert "writing androidtv" in caplog.text


def test_builds_from_another_train_are_skipped():
    config = load_config(CONFIG)  # train 7.12.0
    updates, skipped = plan_updates(config, {"web": build("web", "7.13.0.68")})
    assert "Web" not in updates
    assert any("not on train 7.12.0" in item for item in skipped)


def test_platforms_missing_from_the_feed_are_reported_not_blanked():
    config = load_config(CONFIG)
    updates, skipped = plan_updates(config, {"web": build("web", "7.12.0.133")})
    assert set(updates) == {"Web"}
    assert any("no build reported for roku" in item for item in skipped)
    assert any("no build reported for ios" in item for item in skipped)
    assert any("no build reported for tvos" in item for item in skipped)


def test_unparsable_versions_are_skipped():
    config = load_config(CONFIG)
    updates, skipped = plan_updates(config, {"web": build("web", "unknown")})
    assert updates == {}
    assert any("not a 4-segment version" in item for item in skipped)


def test_the_page_title_decides_the_train(monkeypatch):
    monkeypatch.delenv("RELEASE_TRAIN", raising=False)
    config = load_config(CONFIG)  # config file says 7.12.0
    assert resolve_train(config, "Copy of 7.13.0 Build GQA App Sign off") == ("7.13.0", "page title")

    # A 7.13.0 page accepts the 7.13.0 builds the dashboard is now serving.
    updates, _ = plan_updates(config, {"web": build("web", "7.13.0.68")}, train="7.13.0")
    assert updates["Web"]["max"] == "7.13.0.68"


def test_an_explicit_train_overrides_the_page_title(monkeypatch):
    monkeypatch.setenv("RELEASE_TRAIN", "7.14.0")
    config = load_config(CONFIG)
    assert resolve_train(config, "Copy of 7.12.0 Build GQA App Sign off") == ("7.14.0", "override")
    assert resolve_train(config, "7.12.0 page", override="7.15.0") == ("7.15.0", "override")


def test_train_falls_back_to_config_when_the_title_has_none(monkeypatch):
    monkeypatch.delenv("RELEASE_TRAIN", raising=False)
    config = load_config(CONFIG)
    assert resolve_train(config, "Build GQA App Sign off") == ("7.12.0", "config")

    pinned = replace(config, release=replace(config.release, train_from_page_title=False))
    assert resolve_train(pinned, "Copy of 7.13.0 Build GQA App Sign off") == ("7.12.0", "config")


def test_a_whole_train_behind_is_explained_not_just_skipped():
    builds = {"web": build("web", "7.13.0.68"), "roku": build("roku", "7.13.0.49")}
    hint = train_mismatch_hint(builds, "7.12.0")
    assert "7.13.0" in hint and "7.12.0" in hint
    assert train_mismatch_hint(builds, "7.13.0") == ""
