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
from gonogo.sync import plan_updates

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "clients.yml"
FIXTURES = Path(__file__).parent / "fixtures"
PAYLOADS = {
    "Max": json.loads((FIXTURES / "latest_versions_max.json").read_text()),
    "D-Plus": json.loads((FIXTURES / "latest_versions_dplus.json").read_text()),
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FuseSession:
    """Stands in for the API, keyed on the product parameter."""

    def __init__(self, status_code=200, error=None):
        self.calls = []
        self.status_code = status_code
        self.error = error

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if self.error is not None:
            return FakeResponse(self.error, self.status_code)
        return FakeResponse(PAYLOADS[(params or {})["product"]])


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
    assert builds["ios"].max_version == "7.12.0.73"
    assert builds["ios"].dplus_version == "21.12.0.16"
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
    builds = fetch(FuseSession())

    # playstation-5 reports ComingSoon for MAX, so it has no build at all.
    assert "playstation5" not in builds
    # FireTV reports N/A for D+ only, so MAX still lands and D+ stays unset.
    assert builds["firetv"].max_version == "7.12.0.43"
    assert builds["firetv"].dplus_version is None

    # A platform with no build yet is expected news; a failed lookup is not.
    def messages(level: int) -> str:
        return "\n".join(r.getMessage() for r in caplog.records if r.levelno == level)

    assert "playstation5" in messages(logging.INFO)
    assert "ComingSoon (platform declared but no tree yet)" in messages(logging.INFO)
    assert "N/A (no build matched the tree)" in messages(logging.INFO)
    assert "xbox" in messages(logging.WARNING)
    assert "fetch_error: forbidden" in messages(logging.WARNING)


def test_reported_dplus_versions_are_written_verbatim():
    config = load_config(CONFIG)
    updates, _ = plan_updates(config, fetch(FuseSession()), train="7.12.0")

    # Samsung D+ is 7.12.0.130 against a MAX of 7.12.0.132: deriving from the
    # MAX build number would have written the wrong number.
    assert updates["CDEV"] == {"max": "7.12.0.132", "dplus": "7.12.0.130"}
    assert updates["Apple iOS"] == {"max": "7.12.0.73", "dplus": "21.12.0.16"}
    # Fire TV has no D+ build reported, so the +14 scheme fills it in.
    assert updates["Fire TV"] == {"max": "7.12.0.43", "dplus": "21.12.0.43"}
    # VisionOS does not ship D+ and is not a device this API builds.
    assert "Apple VisionOS" not in updates


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
