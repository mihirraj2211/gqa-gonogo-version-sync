"""End-to-end runs of the CLI against a stubbed Confluence."""

from pathlib import Path

import pytest

from gonogo import confluence, sync

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "clients.yml"
FIXTURES = Path(__file__).parent / "fixtures"
PAGE = FIXTURES / "sample_page.xhtml"
# One file per product, the way the API serves one product per call.
BUILDS = [
    f"max={FIXTURES / 'latest_versions_max.json'}",
    f"dplus={FIXTURES / 'latest_versions_dplus.json'}",
]


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


def builds_args(*files: str) -> list[str]:
    return [arg for path in (files or BUILDS) for arg in ("--builds-file", path)]


def run_cli(*extra: str) -> int:
    return sync.main(["--config", str(CONFIG), *builds_args(), *extra])


def test_dry_run_does_not_publish():
    assert run_cli("--dry-run") == sync.EXIT_OK
    assert FakeConfluence.published == []


def test_live_run_publishes_updated_versions(tmp_path):
    output = tmp_path / "body.xhtml"
    assert run_cli("--output", str(output)) == sync.EXIT_OK

    assert len(FakeConfluence.published) == 1
    body, message = FakeConfluence.published[0]
    assert "cell(s)" in message
    # Every row of the table fills from the feed.
    assert "<p>7.12.0.133</p>" in body and "<p>7.12.0.96</p>" in body    # Web
    assert "<p>7.12.0.49</p>" in body and "<p>7.12.0.50</p>" in body     # Roku
    assert "<p>7.12.0.73</p>" in body                                    # Apple
    assert "<p>7.12.0.66</p>" in body and "<p>21.12.0.66</p>" in body    # Android
    assert "<p>7.12.0.67</p>" in body and "<p>21.12.0.67</p>" in body    # LB
    assert "<p>7.12.0.132</p>" in body and "<p>7.12.0.90</p>" in body    # CDEV
    # Apple reports no D+ build, so that cell keeps its blank rather than
    # gaining a number borrowed from MAX.
    assert "<p>21.12.0.73</p>" not in body
    # The status macro, the Notes column and the untracked Playstation row live.
    assert 'ac:name="status"' in body
    assert "<p>Playstation</p>" in body and "<p>7.12.0.10</p>" in body
    assert "PLAY-128731" in body
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


def test_the_repeated_header_row_is_not_treated_as_a_client():
    """The table repeats its header mid-way; that row must survive untouched."""
    run_cli()
    body = FakeConfluence.published[0][0]
    assert body.count("<strong>MAX Version Number</strong>") == 2


def test_page_file_runs_without_credentials_and_never_publishes(monkeypatch, tmp_path):
    monkeypatch.delenv("ATLASSIAN_USER_EMAIL", raising=False)
    monkeypatch.delenv("ATLASSIAN_API_TOKEN", raising=False)
    output = tmp_path / "body.xhtml"

    exit_code = sync.main(
        ["--config", str(CONFIG), *builds_args(), "--page-file", str(PAGE), "--output", str(output)]
    )

    assert exit_code == sync.EXIT_OK
    assert FakeConfluence.published == []
    assert "<p>7.12.0.133</p>" in output.read_text(encoding="utf-8")


def test_a_concurrent_edit_is_retried_against_the_fresh_page(monkeypatch):
    """A 409 means someone else published; re-read and reapply, don't wait 15 minutes."""
    attempts = {"count": 0}
    original_init = FakeConfluence.__init__

    def init_with_conflict(self, config, email, token, session=None):
        original_init(self, config, email, token, session)

        def update_page(page, body, message):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise confluence.ConflictError("version conflict")
            FakeConfluence.published.append((body, message))
            return page.version + 1

        self.update_page = update_page

    monkeypatch.setattr(FakeConfluence, "__init__", init_with_conflict)

    assert run_cli() == sync.EXIT_OK
    assert attempts["count"] == 2
    assert len(FakeConfluence.published) == 1
    assert "<p>7.12.0.133</p>" in FakeConfluence.published[0][0]


def test_a_feed_on_the_next_train_fails_with_an_explanation(caplog):
    """The 7.12.0 page must refuse 7.13.0 builds, and say why."""
    next_train = str(FIXTURES / "latest_versions_next_train.json")
    exit_code = sync.main(
        ["--config", str(CONFIG), *builds_args(f"max={next_train}"), "--page-file", str(PAGE)]
    )

    assert exit_code == sync.EXIT_ERROR
    assert FakeConfluence.published == []
    assert "7.13.0" in caplog.text and "7.12.0" in caplog.text
