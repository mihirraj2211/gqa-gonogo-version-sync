"""Configuration loading and row-label normalisation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .versions import SCHEMES, SCHEME_BASE

_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def normalise_label(text: str) -> str:
    """Fold a table label so ``Apple iOS / tvOS`` matches ``apple ios tvos``."""
    return _PUNCT_RE.sub(" ", (text or "").lower()).strip()


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean override, so Actions variables can flip config switches."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Client:
    row: str
    platform: str
    dplus_scheme: str = SCHEME_BASE
    aliases: tuple[str, ...] = ()
    source_repo: str = ""

    @property
    def labels(self) -> tuple[str, ...]:
        """Every label this client may appear under, normalised."""
        return tuple(normalise_label(name) for name in (self.row, *self.aliases))


@dataclass(frozen=True)
class ReleaseConfig:
    train: str = ""
    branch_max: str = ""
    branch_dplus: str = ""
    enforce_train: bool = True
    # A sign-off page is per train and says so in its title, so the page can
    # name the train it accepts instead of this repo being edited every train.
    train_from_page_title: bool = True


@dataclass(frozen=True)
class ConfluenceConfig:
    domain: str
    page_id: str
    version_message: str = "Automated build version sync"
    columns: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceConfig:
    provider: str = "fuse_api"
    url_env: str = "FUSE_BUILDS_API_URL"
    token_env: str = "FUSE_API_TOKEN"
    auth: str = "bearer"
    method: str = "get"
    timeout_seconds: int = 30
    query: dict[str, Any] = field(default_factory=dict)
    #: JSON body for POST-style build APIs (a Grafana query, for instance).
    body: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    #: One request per entry, merged by platform, for APIs that serve a single
    #: brand per call. Empty means a single request.
    brand_requests: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Config:
    release: ReleaseConfig
    confluence: ConfluenceConfig
    source: SourceConfig
    clients: tuple[Client, ...]

    def client_for_label(self, label: str) -> Client | None:
        """Find the client whose row name or aliases match a table cell."""
        normalised = normalise_label(label)
        if not normalised:
            return None
        for client in self.clients:
            if normalised in client.labels:
                return client
        # Fall back to containment so "Roku (Stick / TV)" still resolves.
        for client in self.clients:
            for candidate in client.labels:
                if candidate and (candidate in normalised or normalised in candidate):
                    return client
        return None


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    clients: list[Client] = []
    for entry in raw.get("clients", []):
        scheme = entry.get("dplus_scheme", SCHEME_BASE)
        if scheme not in SCHEMES:
            raise ValueError(f"client {entry.get('row')!r} has unknown dplus_scheme {scheme!r}")
        clients.append(
            Client(
                row=entry["row"],
                # Folded the same way provider payloads are, so "Fire TV" in
                # either place lines up with firetv.
                platform=normalise_label(str(entry["platform"])).replace(" ", ""),
                dplus_scheme=scheme,
                aliases=tuple(entry.get("aliases", ())),
                source_repo=entry.get("source_repo", ""),
            )
        )
    if not clients:
        raise ValueError("config defines no clients")

    confluence_raw = raw.get("confluence", {})
    confluence = ConfluenceConfig(
        domain=os.environ.get("CONFLUENCE_DOMAIN") or confluence_raw.get("domain", ""),
        page_id=str(os.environ.get("CONFLUENCE_PAGE_ID") or confluence_raw.get("page_id", "")),
        version_message=confluence_raw.get("version_message", "Automated build version sync"),
        columns=confluence_raw.get("columns", {}),
    )
    if not confluence.domain or not confluence.page_id:
        raise ValueError("confluence.domain and confluence.page_id are required")

    source_raw = raw.get("source", {})
    source = SourceConfig(
        provider=os.environ.get("BUILD_PROVIDER") or source_raw.get("provider", "fuse_api"),
        url_env=source_raw.get("url_env", "FUSE_BUILDS_API_URL"),
        token_env=source_raw.get("token_env", "FUSE_API_TOKEN"),
        auth=source_raw.get("auth", "bearer"),
        method=str(source_raw.get("method", "get")).lower(),
        timeout_seconds=int(source_raw.get("timeout_seconds", 30)),
        query=source_raw.get("query", {}) or {},
        body=source_raw.get("body", {}) or {},
        response=source_raw.get("response", {}) or {},
        options=source_raw.get("options", {}) or {},
        brand_requests=tuple(source_raw.get("brand_requests", ()) or ()),
    )

    release_raw = raw.get("release", {})
    release = ReleaseConfig(
        train=str(os.environ.get("RELEASE_TRAIN") or release_raw.get("train", "")),
        branch_max=release_raw.get("branch_max", ""),
        branch_dplus=release_raw.get("branch_dplus", ""),
        enforce_train=_env_flag("ENFORCE_TRAIN", bool(release_raw.get("enforce_train", True))),
        train_from_page_title=bool(release_raw.get("train_from_page_title", True)),
    )

    return Config(release=release, confluence=confluence, source=source, clients=tuple(clients))
