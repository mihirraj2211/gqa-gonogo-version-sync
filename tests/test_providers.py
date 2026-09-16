import json
from datetime import datetime
from pathlib import Path

import pytest

from gonogo.config import SourceConfig
from gonogo.providers import (
    FileProvider,
    HttpJsonProvider,
    ProviderError,
    _auth_headers,
    collect_builds,
    normalise_payload,
    render_query,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_builds.json"
RESPONSE = {
    "records_path": "",
    "platform_field": "name",
    "version_field": "version",
    "brand_field": "brand",
    "max_brand_value": "max",
    "dplus_brand_value": "dplus",
    "built_at_field": "buildDate",
}


def test_brand_tagged_records_merge_into_one_platform():
    builds = normalise_payload(json.loads(FIXTURE.read_text()), RESPONSE)
    assert builds["web"].max_version == "7.12.0.133"
    assert builds["web"].dplus_version == "7.12.0.96"
    assert builds["roku"].dplus_version == "7.12.0.50"


def test_platform_keys_are_folded():
    builds = normalise_payload(json.loads(FIXTURE.read_text()), RESPONSE)
    assert "firetablet" in builds
    assert "androidtv" in builds
    assert builds["firetablet"].max_version == "7.12.0.68"


def test_platform_without_dplus_record_has_none():
    builds = normalise_payload(json.loads(FIXTURE.read_text()), RESPONSE)
    assert builds["tvos"].dplus_version is None


def test_map_keyed_payloads_are_supported():
    payload = {"data": {"Roku": {"version": "7.12.0.49"}, "Web": {"version": "7.12.0.133"}}}
    builds = normalise_payload(payload, {**RESPONSE, "records_path": "data", "brand_field": None})
    assert builds["roku"].max_version == "7.12.0.49"


def test_versions_embedded_in_filenames_are_extracted():
    payload = [{"name": "Android", "version": "max-android-7.12.0.66-release.apk"}]
    builds = normalise_payload(payload, {**RESPONSE, "brand_field": None})
    assert builds["android"].max_version == "7.12.0.66"


def test_unusable_payload_raises():
    with pytest.raises(ProviderError):
        normalise_payload([{"name": "Web", "version": "unknown"}], RESPONSE)
    with pytest.raises(ProviderError):
        normalise_payload({"data": []}, {**RESPONSE, "records_path": "missing"})


def test_file_provider_reads_local_json():
    source = SourceConfig(provider="file", response=RESPONSE)
    builds = FileProvider(source, FIXTURE).fetch()
    assert builds["chromecast"].max_version == "7.12.0.133"


def test_brand_values_accept_aliases():
    payload = [
        {"name": "Android", "brand": "HBOMAX", "version": "7.13.0.68"},
        {"name": "Android", "brand": "Discovery+", "version": "21.13.0.68"},
    ]
    response = {**RESPONSE, "max_brand_value": ["max", "hbomax"], "dplus_brand_value": ["dplus", "discovery"]}
    builds = normalise_payload(payload, response)
    assert builds["android"].max_version == "7.13.0.68"
    assert builds["android"].dplus_version == "21.13.0.68"


def test_one_record_carrying_both_brands_is_read():
    payload = {
        "data": [
            {"system": "FireTablet", "versions": {"hbomax": "7.13.0.68", "dplus": "21.13.0.68"}},
            {"system": "Roku", "versions": [{"brand": "MAX", "version": "7.13.0.133"}]},
        ]
    }
    response = {
        "records_path": "data",
        "platform_field": "system",
        "version_field": "version",
        "brand_field": "brand",
        "brand_versions_field": "versions",
        "max_brand_value": ["max", "hbomax"],
        "dplus_brand_value": ["dplus"],
    }
    builds = normalise_payload(payload, response)
    assert builds["firetablet"].max_version == "7.13.0.68"
    assert builds["firetablet"].dplus_version == "21.13.0.68"
    assert builds["roku"].max_version == "7.13.0.133"


def test_brand_order_is_preference_order():
    """A card showing both HBOMAX and BENELUX must put HBOMAX in the MAX column."""
    payload = {
        "data": [
            {"system": "LG", "builds": {"benelux": "7.12.0.120", "hbomax": "7.12.0.132"}},
            {"system": "Samsung", "builds": {"benelux": "7.12.0.132"}},
        ]
    }
    response = {
        "records_path": "data",
        "platform_field": "system",
        "version_field": "version",
        "brand_versions_field": "builds",
        "max_brand_value": ["hbomax", "benelux"],
        "dplus_brand_value": ["dplus"],
    }
    builds = normalise_payload(payload, response)
    assert builds["lg"].max_version == "7.12.0.132"
    # Only the fallback brand reported, so the row is still filled rather than skipped.
    assert builds["samsung"].max_version == "7.12.0.132"


def test_unmapped_brands_are_ignored():
    payload = [{"name": "Xbox", "builds": {"hbomax": "7.12.0.132", "somethingelse": "9.9.9.9"}}]
    response = {
        "platform_field": "name",
        "version_field": "version",
        "brand_versions_field": "builds",
        "max_brand_value": ["hbomax"],
        "dplus_brand_value": ["dplus"],
    }
    builds = normalise_payload(payload, response)
    assert builds["xbox"].max_version == "7.12.0.132"
    assert builds["xbox"].dplus_version is None


def test_the_live_build_wins_over_a_newer_one_of_the_same_brand():
    """The sign-off page tracks the promoted build, not merely the newest."""
    payload = [
        {"name": "Web", "brand": "HBOMAX", "version": "7.12.0.133", "isLive": True},
        {"name": "Web", "brand": "HBOMAX", "version": "7.12.0.140", "isLive": False},
    ]
    response = {
        "platform_field": "name",
        "version_field": "version",
        "brand_field": "brand",
        "live_field": "isLive",
        "max_brand_value": ["hbomax"],
        "dplus_brand_value": ["dplus"],
    }
    assert normalise_payload(payload, response)["web"].max_version == "7.12.0.133"


def test_without_a_live_field_the_last_record_wins():
    payload = [
        {"name": "Web", "brand": "HBOMAX", "version": "7.12.0.133"},
        {"name": "Web", "brand": "HBOMAX", "version": "7.12.0.140"},
    ]
    response = {"platform_field": "name", "version_field": "version", "brand_field": "brand",
                "max_brand_value": ["hbomax"], "dplus_brand_value": ["dplus"]}
    assert normalise_payload(payload, response)["web"].max_version == "7.12.0.140"


def test_request_level_brand_labels_records_without_a_brand_field():
    payload = [{"name": "iOS", "version": "21.12.0.16"}]
    response = {**RESPONSE, "brand_field": None}

    items = collect_builds(payload, response, brand="dplus")
    assert [(i.platform, i.max_version, i.dplus_version) for i in items] == [("ios", "", "21.12.0.16")]
    # A D+ build alone has no MAX version to anchor the row, so it is dropped.
    with pytest.raises(ProviderError):
        normalise_payload(payload, response, brand="dplus")


def test_date_tokens_in_the_query_are_expanded():
    moment = datetime(2026, 9, 16, 15, 30)
    rendered = render_query({"date": "{today:%d/%m/%Y}", "since": "{yesterday}", "env": "orange"}, now=moment)
    assert rendered == {"date": "16/09/2026", "since": "2026-09-15", "env": "orange"}


def test_date_tokens_are_expanded_inside_nested_structures():
    moment = datetime(2026, 9, 16, 15, 30)
    rendered = render_query({"range": {"from": "{yesterday}", "to": "{today}"}, "tags": ["{today:%Y}"]}, now=moment)
    assert rendered == {"range": {"from": "2026-09-15", "to": "2026-09-16"}, "tags": ["2026"]}


def test_unknown_query_token_is_reported():
    with pytest.raises(ProviderError):
        render_query({"date": "{sprint}"})


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("bearer", {"Authorization": "Bearer secret"}),
        ("token", {"Authorization": "Token secret"}),
        ("jfrog", {"X-JFrog-Art-Api": "secret"}),
        ("basic", {"Authorization": "Basic c2VjcmV0Og=="}),
        ("none", {}),
    ],
)
def test_auth_modes(mode, expected):
    assert _auth_headers("secret", mode) == expected


def test_custom_auth_header():
    assert _auth_headers("secret", "header", "X-Build-Key") == {"X-Build-Key": "secret"}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class RecordingSession:
    """Serves a payload per brand and records the calls made."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "params": params or {}, "headers": headers or {}})
        return FakeResponse(self.payloads[(params or {}).get("brand", "")])

    def post(self, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append(
            {"method": "POST", "url": url, "params": params or {}, "headers": headers or {}, "json": json}
        )
        return FakeResponse(self.payloads[(params or {}).get("brand", "")])


def test_per_brand_requests_are_merged_into_one_build(monkeypatch):
    monkeypatch.setenv("FUSE_BUILDS_API_URL", "https://builds.example/api/versions")
    monkeypatch.setenv("FUSE_API_TOKEN", "secret")
    source = SourceConfig(
        query={"env": "orange"},
        response={**RESPONSE, "brand_field": None},
        brand_requests=(
            {"brand": "max", "query": {"brand": "MAX"}},
            {"brand": "dplus", "query": {"brand": "DPLUS"}, "path": "dplus"},
        ),
    )
    session = RecordingSession(
        {
            "MAX": [{"name": "iOS", "version": "7.12.0.73"}],
            "DPLUS": [{"name": "iOS", "version": "21.12.0.16"}],
        }
    )

    builds = HttpJsonProvider(source, session=session).fetch()

    assert builds["ios"].max_version == "7.12.0.73"
    assert builds["ios"].dplus_version == "21.12.0.16"
    assert [call["params"] for call in session.calls] == [
        {"env": "orange", "brand": "MAX"},
        {"env": "orange", "brand": "DPLUS"},
    ]
    assert session.calls[1]["url"].endswith("/versions/dplus")
    assert session.calls[0]["headers"]["Authorization"] == "Bearer secret"


def test_post_style_apis_send_a_json_body(monkeypatch):
    monkeypatch.setenv("FUSE_BUILDS_API_URL", "https://builds.example/api/query")
    source = SourceConfig(
        auth="none",
        method="post",
        body={"range": {"from": "{yesterday}", "to": "{today}"}, "env": "orange"},
        response={**RESPONSE, "brand_field": None},
    )
    session = RecordingSession({"": [{"name": "Roku", "version": "7.13.0.133"}]})

    builds = HttpJsonProvider(source, session=session).fetch()

    assert builds["roku"].max_version == "7.13.0.133"
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["json"]["env"] == "orange"
    # Date tokens are rendered inside nested body structures too.
    assert call["json"]["range"]["to"] == datetime.now().strftime("%Y-%m-%d")


def test_unsupported_method_is_rejected(monkeypatch):
    monkeypatch.setenv("FUSE_BUILDS_API_URL", "https://builds.example/api")
    source = SourceConfig(method="delete", response=RESPONSE)
    with pytest.raises(ProviderError, match="source.method"):
        HttpJsonProvider(source, session=RecordingSession({})).fetch()


def test_missing_api_url_names_the_variable(monkeypatch):
    monkeypatch.delenv("FUSE_BUILDS_API_URL", raising=False)
    with pytest.raises(ProviderError, match="FUSE_BUILDS_API_URL"):
        HttpJsonProvider(SourceConfig(response=RESPONSE), session=RecordingSession({})).fetch()
