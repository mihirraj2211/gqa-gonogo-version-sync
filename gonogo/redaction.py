"""Keep credentials out of logs and reports.

The build API takes its token as a query parameter, so every line that carries a
URL carries the credential with it: urllib3's retry warnings, a connection
error's message, a probe report quoting either. Muting a logger is not enough,
because the leak rides on the text rather than on one logger's level.
"""

from __future__ import annotations

import logging
import os

SECRET_SUFFIXES = ("_TOKEN", "_API_KEY", "_PASSWORD", "_SECRET")
MIN_SECRET_LENGTH = 8


def secret_values() -> list[str]:
    """Credential-looking environment values, longest first so prefixes of one
    another cannot leave a fragment behind."""
    values = {
        value.strip()
        for name, value in os.environ.items()
        if name.endswith(SECRET_SUFFIXES) and len(value.strip()) >= MIN_SECRET_LENGTH
    }
    return sorted(values, key=len, reverse=True)


def redact(text: str) -> str:
    for value in secret_values():
        text = text.replace(value, "***")
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_log_redaction() -> None:
    """Scrub secrets from anything logged, including third-party loggers.

    The filter goes on the handlers rather than the logger: records from
    urllib3 and friends propagate to the root handlers, and a logger's own
    filters never see them.
    """
    root = logging.getLogger()
    for target in (root, *root.handlers):
        if not any(isinstance(existing, RedactingFilter) for existing in target.filters):
            target.addFilter(RedactingFilter())
