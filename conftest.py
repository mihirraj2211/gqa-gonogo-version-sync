"""Keeps the repo root on sys.path so tests import `gonogo` without installing."""

import pytest

# Every one of these overrides the config file, so a developer who has sourced
# .env (or CI, which exports them) would otherwise get different results from
# the same test run.
OVERRIDES = (
    "CONFLUENCE_DOMAIN",
    "CONFLUENCE_PAGE_ID",
    "CONFLUENCE_SPACE_KEY",
    "ATLASSIAN_USER_EMAIL",
    "ATLASSIAN_API_TOKEN",
    "FUSE_BUILDS_API_URL",
    "FUSE_API_TOKEN",
    "JFROG_URL",
    "JFROG_TOKEN",
    "BUILD_PROVIDER",
    "RELEASE_TRAIN",
    "ENFORCE_TRAIN",
)


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch):
    """Run every test against the config file, not the ambient environment."""
    for name in OVERRIDES:
        monkeypatch.delenv(name, raising=False)
