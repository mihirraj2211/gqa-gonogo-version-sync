"""End-to-end runs of the CLI against a stubbed Confluence."""

from pathlib import Path

import pytest

from gonogo import confluence, sync

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "clients.yml"
BUILDS = Path(__file__).parent / "fixtures" / "sample_builds.json"
PAGE = Path(__file__).parent / "fixtures" / "sample_page.xhtml"


class FakeConfluence:
    """Stands in for ConfluenceClient, recording what would be published."""

    published: list[tuple[str, str]] = []

    def __init__(self, config, email, token, session=None):
        self.config = config
        self.page = confluence.Page(
            id=config.page_id, title="7.12.0 Build GQA App Sign off", version=7,
            body=PAGE.read_text(encoding="utf-8"),
        )

    def get_page(self, page_id):
        return self.page

    def update_page(self, page, body, message):
        FakeConfluence.published.append((body, message))
        return page.version + 1


@pytest.fixture(autouse=True)
def stub_confluence(monkeypatch):
    FakeConfluence.published = []
    monkeypatch.setattr(sync, "ConfluenceClient", FakeConfluence)
    monkeypatch.setenv("ATLASSIAN_USER_EMAIL", "bot@wbd.com")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "test-token")


def run_cli(*extra: str) -> int:
    return sync.main(["--config", str(CONFIG), "--builds-file", str(BUILDS), *extra])


def test_dry_run_does_not_publish():
    assert run_cli("--dry-run") == sync.EXIT_OK
    assert FakeConfluence.published == []


def test_live_run_publishes_updated_versions(tmp_path):
    output = tmp_path / "body.xhtml"
    assert run_cli("--output", str(output)) == sync.EXIT_OK

    assert len(FakeConfluence.published) == 1
    body, message = FakeConfluence.published[0]
    assert "cell(s)" in message
    # Web and iOS moved; Android Mobile already matched and stays put.
    assert "<p>7.12.0.133</p>" in body and "<p>7.12.0.96</p>" in body
    assert "<p>7.12.0.73</p>" in body and "<p>21.12.0.16</p>" in body
    assert "<p>7.12.0.66</p>" in body and "<p>21.12.0.66</p>" in body
    # The status macro and the untracked Playstation row survive.
    assert 'ac:name="status"' in body
    assert "<p>Playstation</p>" in body
    assert output.read_text(encoding="utf-8") == body


def test_second_run_against_synced_page_publishes_nothing(monkeypatch):
    run_cli()
    synced_body = FakeConfluence.published[0][0]
    FakeConfluence.published = []

    original_init = FakeConfluence.__init__

    def init_with_synced_page(self, config, email, token, session=None):
        original_init(self, config, email, token, session)
        self.page = confluence.Page(id=self.page.id, title=self.page.title, version=8, body=synced_body)

    monkeypatch.setattr(FakeConfluence, "__init__", init_with_synced_page)
    assert run_cli() == sync.EXIT_OK
    assert FakeConfluence.published == []


def test_visionos_dplus_cell_is_never_overwritten():
    run_cli()
    body = FakeConfluence.published[0][0]
    assert "<p>N/A</p>" in body
