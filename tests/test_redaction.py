"""The gate token must not reach a log line or a report.

It rides in the query string, so a URL in any message carries it. Both leaks
pinned here happened for real: urllib3's DEBUG request line, then its WARNING
retry line and the connection error behind it.
"""

import io
import logging

import pytest

from gonogo.redaction import install_log_redaction, redact, secret_values
from gonogo.sync import configure_logging

TOKEN = "BBNsecretGateTokenValue123456789"
URL = f"https://sprintedge.gqa.discomax.com/api/latest-versions?product=Max&token={TOKEN}"


@pytest.fixture
def captured(monkeypatch):
    """A root handler whose output the test can read."""
    monkeypatch.setenv("FUSE_API_TOKEN", TOKEN)
    stream = io.StringIO()
    root = logging.getLogger()
    handler = logging.StreamHandler(stream)
    root.addHandler(handler)
    configure_logging(verbose=True)
    yield stream
    root.removeHandler(handler)


def test_a_retry_warning_does_not_carry_the_token(captured):
    logging.getLogger("urllib3.connectionpool").warning(
        "Retrying after connection broken by ProxyError: %s", URL
    )

    output = captured.getvalue()
    assert TOKEN not in output
    assert "***" in output
    assert "latest-versions" in output, "the URL is still useful once scrubbed"


def test_a_connection_error_does_not_carry_the_token(captured):
    error = OSError(f"Max retries exceeded with url: {URL}")
    logging.getLogger("gonogo").error("%s", error)

    assert TOKEN not in captured.getvalue()


def test_the_request_line_does_not_carry_the_token(captured):
    logging.getLogger("urllib3.connectionpool").debug('"GET %s HTTP/1.1" 200 1367', URL)

    assert TOKEN not in captured.getvalue()


def test_redact_leaves_ordinary_text_alone(monkeypatch):
    monkeypatch.setenv("FUSE_API_TOKEN", TOKEN)
    assert redact("web=7.12.0.134, roku=7.12.0.49") == "web=7.12.0.134, roku=7.12.0.49"


def test_short_and_unset_values_are_not_treated_as_secrets(monkeypatch):
    for name in ("FUSE_API_TOKEN", "ATLASSIAN_API_TOKEN", "JFROG_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOME_TOKEN", "short")

    assert "short" not in secret_values(), "redacting a short value would mangle prose"
    assert redact("nothing to hide") == "nothing to hide"


def test_installing_twice_does_not_stack_filters():
    root = logging.getLogger()
    install_log_redaction()
    before = len(root.filters)
    install_log_redaction()

    assert len(root.filters) == before
