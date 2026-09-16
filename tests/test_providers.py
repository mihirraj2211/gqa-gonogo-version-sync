import json
from pathlib import Path

import pytest

from gonogo.config import SourceConfig
from gonogo.providers import FileProvider, ProviderError, normalise_payload

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
