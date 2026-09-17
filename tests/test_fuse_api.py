"""The real Fuse build API: two products, a gate token in the query string.

Shapes and parameter names come from the "Build Fetch API - curl Reference"
page: /api/latest-versions returns one card per device for one product, gated on
?token=<gate token>, and a card's `version` may be a status word instead of a
number.
"""

import json
import logging
from pathlib import Path

import pytest

from gonogo.config import load_config
from gonogo.providers import HttpJsonProvider, ProviderError
from gonogo.sync import configure_logging, plan_updates

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "clients.yml"
FIXTURES = Path(__file__).parent / "fixtures"
PAYLOADS = {
    "Max": json.loads((FIXTURES / "latest_versions_max.json").read_text()),
    "D-Plus": json.loads((FIXTURES / "latest_versions_dplus.json").read_text()),
}
STATUSES = json.loads((FIXTURES / "latest_versions_statuses.json").read_text())


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FuseSession:
    """Stands in for the API, keyed on the product parameter."""

    def __init__(self, status_code=200, error=None, payloads=None):
        self.calls = []
        self.status_code = status_code
        self.error = error
        self.payloads = payloads or PAYLOADS

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if self.error is not None:
            return FakeResponse(self.error, self.status_code)
        product = (params or {})["product"]
        return FakeResponse(self.payloads.get(product, self.payloads))


@pytest.fixture(autouse=True)
def api_env(monkeypatch):
    monkeypatch.setenv(
        "FUSE_BUILDS_API_URL",
        "https://sprintedge.gqa.discomax.com/sprintedge/fuse/api/latest-versions",
    )
    monkeypatch.setenv("FUSE_API_TOKEN", "gate-token-value")
    monkeypatch.delenv("RELEASE_TRAIN", raising=False)


def fetch(session):
    return HttpJsonProvider(load_config(CONFIG).source, session=session).fetch()


def test_max_and_dplus_are_two_products_merged_per_device():
    builds = fetch(FuseSession())

    assert builds["web"].max_version == "7.12.0.133"
    assert builds["web"].dplus_version == "7.12.0.96"
    assert builds["androidtv"].max_version == "7.12.0.67"
    assert builds["androidtv"].dplus_version == "21.12.0.67"
    # iOS builds MAX but reports no D+ at all on this train.
    assert builds["ios"].max_version == "7.12.0.73"
    assert builds["ios"].dplus_version is None
    assert builds["playstation4"].max_version == "7.12.0.132"


def test_the_gate_token_is_sent_as_a_query_parameter():
    session = FuseSession()
    fetch(session)

    products = [call["params"]["product"] for call in session.calls]
    assert products == ["Max", "D-Plus"]
    for call in session.calls:
        assert call["params"]["token"] == "gate-token-value"
        assert call["params"]["environment"] == "Blue"
        # Nothing rides in a header for this API.
        assert "Authorization" not in call["headers"]


def test_status_words_are_skipped_rather_than_written(caplog):
    caplog.set_level(logging.INFO, logger="gonogo.providers")
    builds = fetch(FuseSession(payloads=STATUSES))

    # Only the device with a real version survives.
    assert sorted(builds) == ["web"]

    # A platform with no build yet is expected news; a failed lookup is not.
    def messages(level: int) -> str:
        return "\n".join(r.getMessage() for r in caplog.records if r.levelno == level)

    assert "ComingSoon (platform declared but no tree yet)" in messages(logging.INFO)
    assert "N/A (no build matched the tree)" in messages(logging.INFO)
    assert "NotBuilt (no tree for this brand on this platform)" in messages(logging.INFO)
    assert "xbox" in messages(logging.WARNING)
    assert "fetch_error: forbidden" in messages(logging.WARNING)


def test_ios_keeps_its_dplus_cell_when_the_feed_reports_none(caplog):
    """The live iOS case, and the reason D+ is never derived from MAX."""
    config = load_config(CONFIG)
    updates, _ = plan_updates(config, fetch(FuseSession()), train="7.12.0")

    assert updates["Apple IOS"] == {"max": "7.12.0.73"}
    assert "Apple IOS: no D+ version reported, leaving that cell as it is" in caplog.text


def test_reported_dplus_versions_are_written_verbatim():
    config = load_config(CONFIG)
    updates, _ = plan_updates(config, fetch(FuseSession()), train="7.12.0")

    # Samsung is MAX 7.12.0.132 against D+ 7.12.0.90: borrowing the MAX build
    # octet, as the old fallback did, would have written 7.12.0.132.
    assert updates["CDEV"] == {"max": "7.12.0.132", "dplus": "7.12.0.90"}
    assert updates["Web"] == {"max": "7.12.0.133", "dplus": "7.12.0.96"}
    assert updates["Roku"] == {"max": "7.12.0.49", "dplus": "7.12.0.50"}
    assert updates["LB"] == {"max": "7.12.0.67", "dplus": "21.12.0.67"}
    assert updates["Android"] == {"max": "7.12.0.66", "dplus": "21.12.0.66"}
    # Every row of the table is accounted for, and nothing else is touched.
    assert sorted(updates) == ["Android", "Apple IOS", "Apple TV", "CDEV", "LB", "Roku", "Web"]


def test_an_unset_token_fails_before_the_request(monkeypatch):
    """A missing credential must not look like a rejected one.

    Without this the request goes out with no token, the API answers its generic
    "Invalid or missing access token.", and an empty variable reads as a bad one.
    """
    monkeypatch.setenv("FUSE_API_TOKEN", "   ")
    session = FuseSession()

    with pytest.raises(ProviderError) as excinfo:
        fetch(session)

    assert session.calls == []
    assert "FUSE_API_TOKEN is not set" in str(excinfo.value)
    assert "source .env" in str(excinfo.value)


def test_a_401_explains_that_a_jfrog_token_is_not_a_gate_token():
    session = FuseSession(status_code=401, error={"error": "Invalid or missing access token."})
    with pytest.raises(ProviderError) as excinfo:
        fetch(session)

    message = str(excinfo.value)
    assert "Invalid or missing access token." in message
    assert "gate token" in message


def test_the_token_never_appears_in_a_log_line(caplog):
    fetch(FuseSession())
    assert "gate-token-value" not in caplog.text


def test_verbose_logging_does_not_print_the_token(caplog):
    """The gate token rides in the query string, so urllib3 must stay quiet.

    Its DEBUG log prints whole request lines, which put the live token in a
    terminal once already.
    """
    configure_logging(verbose=True)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("urllib3").level == logging.INFO

    # Nothing this repo logs itself carries the credential either.
    with caplog.at_level(logging.DEBUG):
        fetch(FuseSession())
    assert "gate-token-value" not in caplog.text
