"""The probe is what turns a new API endpoint into a working config."""

import json
from pathlib import Path

from gonogo import probe

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "clients.yml"
FIXTURES = Path(__file__).parent / "fixtures"
PAGE = FIXTURES / "sample_page.xhtml"
FLAT_BUILDS = FIXTURES / "sample_builds.json"
BUILDS = [
    f"max={FIXTURES / 'latest_versions_max.json'}",
    f"dplus={FIXTURES / 'latest_versions_dplus.json'}",
]


def run_probe(*extra: str) -> int:
    args = ["--config", str(CONFIG)]
    for path in BUILDS:
        args += ["--builds-file", path]
    return probe.main([*args, "--page-file", str(PAGE), *extra])


def test_mapping_is_suggested_from_a_flat_record_list():
    suggestion = probe.suggest_response_mapping(json.loads(FLAT_BUILDS.read_text()))
    assert suggestion["records_path"] == ""
    assert suggestion["platform_field"] == "name"
    assert suggestion["version_field"] == "version"
    assert suggestion["brand_field"] == "brand"
    assert suggestion["built_at_field"] == "buildDate"


def test_mapping_finds_records_nested_under_a_dot_path():
    payload = {
        "status": "ok",
        "data": {"systems": [{"system": "Roku", "appVersion": "7.13.0.133", "createdAt": "2026-09-16"}]},
    }
    suggestion = probe.suggest_response_mapping(payload)
    assert suggestion["records_path"] == "data.systems"
    assert suggestion["version_field"] == "appVersion"
    assert suggestion["platform_field"] == "system"
    assert suggestion["built_at_field"] == "createdAt"


def test_mapping_handles_a_payload_keyed_by_platform():
    payload = {"Roku": {"version": "7.13.0.133"}, "Web": {"version": "7.13.0.44"}}
    suggestion = probe.suggest_response_mapping(payload)
    assert suggestion["version_field"] == "version"
    assert "keyed by platform" in suggestion["platform_field"]


def test_mapping_spots_a_per_brand_version_block():
    payload = [{"name": "FireTablet", "versions": {"hbomax": "7.13.0.68", "dplus": "21.13.0.68"}}]
    assert probe.suggest_response_mapping(payload)["brand_versions_field"] == "versions"


def test_probe_passes_offline_and_reports_both_ends(capsys):
    assert run_probe() == probe.EXIT_OK
    report = capsys.readouterr().out
    assert "7.12.0 Build GQA App Sign off" in report
    assert "Train to enforce: **7.12.0** (from page title)" in report
    assert "`web` | 7.12.0.133 | 7.12.0.96" in report
    assert "Both ends look healthy" in report


def test_probe_fails_when_coverage_drops_below_the_threshold(capsys):
    assert run_probe("--min-rows", "99") == probe.EXIT_ERROR
    assert "expected at least 99" in capsys.readouterr().out


def test_the_real_response_shape_is_recognised():
    payload = json.loads((FIXTURES / "latest_versions_max.json").read_text())
    suggestion = probe.suggest_response_mapping(payload)
    assert suggestion["records_path"] == "devices"
    assert suggestion["platform_field"] == "name"
    assert suggestion["version_field"] == "version"
    assert suggestion["built_at_field"] == "date"


def test_probe_reports_an_unreachable_build_source(capsys):
    exit_code = probe.main(
        ["--config", str(CONFIG), "--builds-file", "no/such/file.json", "--page-file", str(PAGE)]
    )
    assert exit_code == probe.EXIT_ERROR
    assert "Build source failed" in capsys.readouterr().out


def test_probe_writes_a_job_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert run_probe() == probe.EXIT_OK
    assert "Go/No-Go sync probe" in summary.read_text(encoding="utf-8")
