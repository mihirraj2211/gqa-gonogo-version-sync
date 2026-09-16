"""Finding each train's own sign-off page.

Every train gets its own page: 7.12.0 has one, 7.13.0 will get another. Naming
the train has to be enough to find it, or every train needs a repo change.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from gonogo import sync
from gonogo.config import load_config
from gonogo.confluence import ConfluenceClient, ConfluenceError
from gonogo.providers import PlatformBuild

CONFIG = Path(__file__).resolve().parents[1] / "config" / "clients.yml"
PAGE_BODY = (Path(__file__).parent / "fixtures" / "sample_page.xhtml").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class SearchSession:
    """Answers the CQL search, then the page read."""

    auth = None

    def __init__(self, matches):
        self.matches = matches
        self.searches: list[str] = []
        self.reads: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        if "content/search" in url:
            self.searches.append((params or {})["cql"])
            return FakeResponse({"results": self.matches})
        self.reads.append(url.rsplit("/", 1)[-1])
        return FakeResponse(
            {
                "id": "4200000000",
                "title": "7.13.0 Build GQA App Sign off",
                "version": {"number": 3},
                "body": {"storage": {"value": PAGE_BODY}},
            }
        )


def client_for(session) -> ConfluenceClient:
    config = load_config(CONFIG).confluence
    return ConfluenceClient(config, email="bot@wbd.com", token="token", session=session)


def test_the_config_ships_unpinned_so_trains_are_followed():
    """A page id in the config would freeze the sync on one train for good."""
    assert load_config(CONFIG).confluence.page_id == ""


def test_the_page_id_variable_pins_one_page(monkeypatch):
    """How the "Copy of ..." page is used for testing, with no code change.

    Deleting the variable is then the whole go-live step, so this is the switch
    worth pinning down.
    """
    monkeypatch.setenv("CONFLUENCE_PAGE_ID", "4137255661")
    monkeypatch.delenv("RELEASE_TRAIN", raising=False)
    session = SearchSession([])
    args = sync.parse_args(["--config", str(CONFIG)])

    sync.resolve_page(client_for(session), load_config(CONFIG), args, builds_on("7.13.0"))

    assert session.reads == ["4137255661"]
    assert session.searches == [], "a pinned id must not trigger a title search"


def test_the_title_template_renders_per_train():
    confluence = load_config(CONFIG).confluence
    assert confluence.title_for("7.13.0") == "7.13.0 Build GQA App Sign off"
    assert confluence.title_template.format(train="7.12.0", train_short="7.12").startswith("7.12.0")


def test_a_train_resolves_to_its_own_page():
    session = SearchSession([{"id": "4200000000", "title": "7.13.0 Build GQA App Sign off"}])
    page = client_for(session).find_page_by_title("7.13.0 Build GQA App Sign off")

    assert page.id == "4200000000"
    assert page.version == 3
    cql = session.searches[0]
    assert 'space = "GQA"' in cql and "type = page" in cql
    assert 'title ~ "7.13.0 Build GQA App Sign off"' in cql


def test_a_copy_of_prefix_still_matches():
    """The live 7.12.0 page is named "Copy of 7.12.0 Build GQA App Sign off"."""
    session = SearchSession([{"id": "4137255661", "title": "Copy of 7.12.0 Build GQA App Sign off"}])
    client_for(session).find_page_by_title("7.12.0 Build GQA App Sign off")
    assert session.reads == ["4137255661"]


def test_an_exact_title_wins_over_a_copy():
    session = SearchSession(
        [
            {"id": "1", "title": "Copy of 7.13.0 Build GQA App Sign off"},
            {"id": "2", "title": "7.13.0 Build GQA App Sign off"},
        ]
    )
    client_for(session).find_page_by_title("7.13.0 Build GQA App Sign off")
    assert session.reads == ["2"]


def test_two_loose_matches_refuse_to_guess():
    """Publishing to the wrong sign-off page is worse than not running."""
    session = SearchSession(
        [
            {"id": "1", "title": "Copy of 7.13.0 Build GQA App Sign off"},
            {"id": "2", "title": "Draft 7.13.0 Build GQA App Sign off"},
        ]
    )

    with pytest.raises(ConfluenceError) as excinfo:
        client_for(session).find_page_by_title("7.13.0 Build GQA App Sign off")

    message = str(excinfo.value)
    assert "2 pages match" in message
    assert "will not guess" in message
    assert "CONFLUENCE_PAGE_ID" in message
    assert session.reads == []


def test_a_train_with_no_page_yet_says_so():
    session = SearchSession([])
    with pytest.raises(ConfluenceError) as excinfo:
        client_for(session).find_page_by_title("7.14.0 Build GQA App Sign off")

    assert "no page in space GQA" in str(excinfo.value)
    assert "Create that train's" in str(excinfo.value)


def test_a_configured_page_id_is_used_without_searching(monkeypatch):
    monkeypatch.delenv("RELEASE_TRAIN", raising=False)
    session = SearchSession([])
    config = load_config(CONFIG)
    config = replace(config, confluence=replace(config.confluence, page_id="4137255661"))
    args = sync.parse_args(["--config", str(CONFIG)])

    sync.resolve_page(client_for(session), config, args)

    assert session.searches == []
    assert session.reads == ["4137255661"]


def test_naming_a_train_finds_that_page_when_no_id_is_pinned(monkeypatch):
    """This is the whole 7.13.0 workflow: set the train, nothing else."""
    monkeypatch.setenv("RELEASE_TRAIN", "7.13.0")
    session = SearchSession([{"id": "4200000000", "title": "7.13.0 Build GQA App Sign off"}])
    config = load_config(CONFIG)
    config = replace(config, confluence=replace(config.confluence, page_id=""))
    args = sync.parse_args(["--config", str(CONFIG)])

    page = sync.resolve_page(client_for(session), config, args)

    assert 'title ~ "7.13.0 Build GQA App Sign off"' in session.searches[0]
    assert page.title == "7.13.0 Build GQA App Sign off"


def builds_on(train: str) -> dict[str, PlatformBuild]:
    return {
        "web": PlatformBuild(platform="web", max_version=f"{train}.133"),
        "roku": PlatformBuild(platform="roku", max_version=f"{train}.49"),
    }


def test_the_newest_train_in_the_feed_is_detected():
    mixed = {**builds_on("7.12.0"), "ios": PlatformBuild(platform="ios", max_version="7.13.0.9")}
    assert sync.train_from_builds(builds_on("7.12.0")) == "7.12.0"
    assert sync.train_from_builds(mixed) == "7.13.0"
    # 7.9.0 must not beat 7.13.0 on a string comparison.
    assert sync.train_from_builds({**builds_on("7.9.0"), **builds_on("7.13.0")}) == "7.13.0"
    assert sync.train_from_builds({}) == ""


def test_a_scheduled_run_follows_the_feed_onto_the_new_page(monkeypatch):
    """Nobody has to change a variable the day 7.13.0 starts building."""
    monkeypatch.delenv("RELEASE_TRAIN", raising=False)
    session = SearchSession([{"id": "4200000000", "title": "7.13.0 Build GQA App Sign off"}])
    config = load_config(CONFIG)
    config = replace(config, confluence=replace(config.confluence, page_id=""))
    args = sync.parse_args(["--config", str(CONFIG)])

    page = sync.resolve_page(client_for(session), config, args, builds_on("7.13.0"))

    assert page.title == "7.13.0 Build GQA App Sign off"
    assert all('title ~ "7.13.0 Build GQA App Sign off"' in cql for cql in session.searches)


def test_a_new_train_without_a_page_keeps_the_current_one(monkeypatch, caplog):
    """Builds move before the page is made; that is a nudge, not a failure."""
    monkeypatch.delenv("RELEASE_TRAIN", raising=False)

    class OnlyTheOldPage(SearchSession):
        def get(self, url, params=None, headers=None, timeout=None):
            if "content/search" in url:
                cql = (params or {})["cql"]
                self.searches.append(cql)
                hit = {"id": "4137255661", "title": "Copy of 7.12.0 Build GQA App Sign off"}
                return FakeResponse({"results": [hit] if "7.12.0" in cql else []})
            return super().get(url, params, headers, timeout)

    session = OnlyTheOldPage([])
    config = load_config(CONFIG)
    config = replace(config, confluence=replace(config.confluence, page_id=""))
    args = sync.parse_args(["--config", str(CONFIG)])

    sync.resolve_page(client_for(session), config, args, builds_on("7.13.0"))

    assert session.reads == ["4137255661"]
    assert "the feed is building train 7.13.0" in caplog.text
    assert "no page titled '7.13.0 Build GQA App Sign off' exists yet" in caplog.text


def test_a_pinned_train_ignores_what_the_feed_is_building(monkeypatch):
    """A sign-off in progress must not be dragged onto the next train."""
    monkeypatch.setenv("RELEASE_TRAIN", "7.12.0")
    session = SearchSession([{"id": "4137255661", "title": "Copy of 7.12.0 Build GQA App Sign off"}])
    config = load_config(CONFIG)
    config = replace(config, confluence=replace(config.confluence, page_id=""))
    args = sync.parse_args(["--config", str(CONFIG)])

    sync.resolve_page(client_for(session), config, args, builds_on("7.13.0"))

    assert 'title ~ "7.12.0 Build GQA App Sign off"' in session.searches[0]


def test_an_explicit_title_beats_a_pinned_id():
    session = SearchSession([{"id": "4200000000", "title": "7.13.0 Build GQA App Sign off"}])
    config = load_config(CONFIG)
    args = sync.parse_args(
        ["--config", str(CONFIG), "--page-title", "7.13.0 Build GQA App Sign off"]
    )

    sync.resolve_page(client_for(session), config, args)
    assert session.searches and session.reads == ["4200000000"]


def test_without_an_id_or_a_train_it_explains_rather_than_searching(monkeypatch):
    monkeypatch.delenv("RELEASE_TRAIN", raising=False)
    session = SearchSession([])
    config = load_config(CONFIG)
    config = replace(
        config,
        confluence=replace(config.confluence, page_id=""),
        release=replace(config.release, train="", train_from_page_title=False),
    )
    args = sync.parse_args(["--config", str(CONFIG)])

    with pytest.raises(ConfluenceError) as excinfo:
        sync.resolve_page(client_for(session), config, args)

    assert "CONFLUENCE_PAGE_ID" in str(excinfo.value)
    assert "RELEASE_TRAIN" in str(excinfo.value)
    assert session.searches == []
