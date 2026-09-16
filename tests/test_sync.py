from pathlib import Path

from gonogo.config import load_config
from gonogo.providers import PlatformBuild
from gonogo.sync import plan_updates

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

    assert updates["Apple iOS"] == {"max": "7.12.0.73", "dplus": "21.12.0.16"}
    assert updates["Roku"] == {"max": "7.12.0.49", "dplus": "7.12.0.50"}
    assert updates["Web"] == {"max": "7.12.0.133", "dplus": "7.12.0.96"}


def test_dplus_is_derived_when_the_api_reports_only_max():
    config = load_config(CONFIG)
    updates, _ = plan_updates(config, {"androidtv": build("androidtv", "7.12.0.43")})
    assert updates["Android TV"] == {"max": "7.12.0.43", "dplus": "21.12.0.43"}


def test_visionos_gets_no_dplus_value():
    config = load_config(CONFIG)
    updates, _ = plan_updates(config, {"visionos": build("visionos", "7.12.0.73")})
    assert updates["Apple VisionOS"] == {"max": "7.12.0.73"}


def test_builds_from_another_train_are_skipped():
    config = load_config(CONFIG)  # train 7.12.0
    updates, skipped = plan_updates(config, {"web": build("web", "7.13.0.68")})
    assert "Web" not in updates
    assert any("not on train 7.12.0" in item for item in skipped)


def test_platforms_missing_from_the_feed_are_reported_not_blanked():
    config = load_config(CONFIG)
    updates, skipped = plan_updates(config, {"web": build("web", "7.12.0.133")})
    assert set(updates) == {"Web"}
    assert any("no build reported for platform 'roku'" in item for item in skipped)


def test_unparsable_versions_are_skipped():
    config = load_config(CONFIG)
    updates, skipped = plan_updates(config, {"web": build("web", "unknown")})
    assert updates == {}
    assert any("not a 4-segment version" in item for item in skipped)
