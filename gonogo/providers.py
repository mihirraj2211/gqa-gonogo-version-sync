"""Build-number sources.

Every provider returns ``{platform_key: PlatformBuild}``. The platform keys are
whatever ``clients[].platform`` says in the config, so swapping providers never
touches the Confluence side of the sync.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import SourceConfig, normalise_label
from .versions import extract_version, is_version

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Raised when a build source cannot be read or understood."""


@dataclass(frozen=True)
class PlatformBuild:
    platform: str
    max_version: str
    dplus_version: str | None = None
    built_at: str | None = None


def build_session(retries: int = 3) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "PUT"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _auth_headers(token: str | None, mode: str) -> dict[str, str]:
    if not token or mode == "none":
        return {}
    if mode == "bearer":
        return {"Authorization": f"Bearer {token}"}
    if mode == "token":
        return {"Authorization": f"Token {token}"}
    if mode == "jfrog":
        return {"X-JFrog-Art-Api": token}
    raise ProviderError(f"unsupported auth mode {mode!r}")


def _dig(payload: Any, path: str) -> Any:
    """Walk a dotted path such as ``data.systems`` into a JSON payload."""
    if not path:
        return payload
    current = payload
    for key in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(key)]
                continue
            except (ValueError, IndexError) as exc:
                raise ProviderError(f"cannot read {path!r} from payload") from exc
        if not isinstance(current, dict) or key not in current:
            raise ProviderError(f"cannot read {path!r} from payload")
        current = current[key]
    return current


def normalise_payload(payload: Any, response_cfg: dict[str, Any]) -> dict[str, PlatformBuild]:
    """Turn an API payload into ``{platform: PlatformBuild}``.

    Handles the two shapes these dashboards usually serve: a flat list of build
    records, or a map keyed by platform. When a brand field is present, MAX and
    D+ arrive as separate records for the same platform and get merged.
    """
    records = _dig(payload, response_cfg.get("records_path", "") or "")
    platform_field = response_cfg.get("platform_field", "name")
    version_field = response_cfg.get("version_field", "version")
    brand_field = response_cfg.get("brand_field")
    max_brand = normalise_label(str(response_cfg.get("max_brand_value", "max")))
    dplus_brand = normalise_label(str(response_cfg.get("dplus_brand_value", "dplus")))
    built_at_field = response_cfg.get("built_at_field")

    rows: list[tuple[str, dict[str, Any]]] = []
    if isinstance(records, dict):
        for key, value in records.items():
            rows.append((str(key), value if isinstance(value, dict) else {version_field: value}))
    elif isinstance(records, list):
        for item in records:
            if not isinstance(item, dict):
                raise ProviderError(f"expected object records, got {type(item).__name__}")
            rows.append((str(item.get(platform_field, "")), item))
    else:
        raise ProviderError(f"expected a list or map of builds, got {type(records).__name__}")

    builds: dict[str, PlatformBuild] = {}
    for platform_name, record in rows:
        platform = normalise_label(platform_name).replace(" ", "")
        if not platform:
            log.warning("skipping build record with no platform: %s", record)
            continue

        version = _read_version(record, version_field)
        if not version:
            log.warning("skipping %s: no parsable version in %s", platform, record)
            continue

        brand = normalise_label(str(record.get(brand_field, ""))) if brand_field else ""
        built_at = str(record.get(built_at_field)) if built_at_field and record.get(built_at_field) else None
        existing = builds.get(platform)

        if brand == dplus_brand and brand:
            merged = PlatformBuild(
                platform=platform,
                max_version=existing.max_version if existing else "",
                dplus_version=version,
                built_at=existing.built_at if existing else built_at,
            )
        else:
            # Explicit per-brand D+ fields win over a brand-tagged record.
            dplus = _read_version(record, "dplus_version") or _read_version(record, "dplusVersion")
            merged = PlatformBuild(
                platform=platform,
                max_version=version,
                dplus_version=dplus or (existing.dplus_version if existing else None),
                built_at=built_at or (existing.built_at if existing else None),
            )
        builds[platform] = merged

    incomplete = [key for key, value in builds.items() if not value.max_version]
    for key in incomplete:
        log.warning("dropping %s: D+ build present but no MAX build", key)
        del builds[key]
    if not builds:
        raise ProviderError("build source returned no usable versions")
    return builds


def _read_version(record: dict[str, Any], field: str | None) -> str | None:
    if not field:
        return None
    raw = record.get(field)
    if raw is None:
        return None
    text = str(raw).strip()
    return text if is_version(text) else extract_version(text)


class HttpJsonProvider:
    """Reads builds from a JSON HTTP endpoint (Fuse build API or Grafana)."""

    def __init__(self, source: SourceConfig, session: requests.Session | None = None):
        self.source = source
        self.session = session or build_session()

    def fetch(self) -> dict[str, PlatformBuild]:
        url = os.environ.get(self.source.url_env)
        if not url:
            raise ProviderError(
                f"{self.source.url_env} is not set; point it at the build API "
                "documented on the Build Fetch API curl reference page"
            )
        headers = {"Accept": "application/json"}
        headers.update(_auth_headers(os.environ.get(self.source.token_env), self.source.auth))

        log.info("fetching builds from %s", url)
        response = self.session.get(
            url, headers=headers, params=self.source.query, timeout=self.source.timeout_seconds
        )
        if response.status_code >= 400:
            raise ProviderError(f"build API returned {response.status_code}: {response.text[:400]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"build API did not return JSON: {response.text[:200]}") from exc
        return normalise_payload(payload, self.source.response)


class JFrogProvider:
    """Reads the newest artifact per platform from Artifactory via AQL."""

    def __init__(self, source: SourceConfig, session: requests.Session | None = None):
        self.source = source
        self.session = session or build_session()

    def fetch(self) -> dict[str, PlatformBuild]:
        base_url = os.environ.get(self.source.url_env)
        token = os.environ.get(self.source.token_env)
        if not base_url:
            raise ProviderError(f"{self.source.url_env} is not set")
        searches = self.source.options.get("aql_searches")
        if not searches:
            raise ProviderError("source.options.aql_searches is required for the jfrog provider")

        headers = _auth_headers(token, self.source.auth or "bearer")
        headers["Content-Type"] = "text/plain"
        builds: dict[str, PlatformBuild] = {}

        for search in searches:
            platform = normalise_label(search["platform"]).replace(" ", "")
            query = (
                f'items.find({json.dumps(search["find"])})'
                '.include("name","created","path")'
                '.sort({"$desc":["created"]})'
                f'.limit({int(search.get("limit", 5))})'
            )
            response = self.session.post(
                f"{base_url.rstrip('/')}/artifactory/api/search/aql",
                data=query,
                headers=headers,
                timeout=self.source.timeout_seconds,
            )
            if response.status_code >= 400:
                raise ProviderError(f"JFrog AQL {platform} returned {response.status_code}: {response.text[:300]}")
            results = response.json().get("results", [])
            version = next(
                (v for v in (extract_version(f"{r.get('path','')}/{r.get('name','')}") for r in results) if v),
                None,
            )
            if not version:
                log.warning("no versioned artifact found for %s", platform)
                continue
            created = results[0].get("created") if results else None
            builds[platform] = PlatformBuild(platform=platform, max_version=version, built_at=created)

        if not builds:
            raise ProviderError("JFrog returned no usable versions")
        return builds


class FileProvider:
    """Reads builds from a local JSON file. Used for dry runs and tests."""

    def __init__(self, source: SourceConfig, path: str | Path):
        self.source = source
        self.path = Path(path)

    def fetch(self) -> dict[str, PlatformBuild]:
        if not self.path.exists():
            raise ProviderError(f"builds file not found: {self.path}")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return normalise_payload(payload, self.source.response)


def get_provider(source: SourceConfig, builds_file: str | Path | None = None):
    if builds_file:
        return FileProvider(source, builds_file)
    provider = (source.provider or "").lower()
    if provider in {"fuse_api", "grafana", "http"}:
        return HttpJsonProvider(source)
    if provider == "jfrog":
        return JFrogProvider(source)
    if provider == "file":
        raise ProviderError("provider 'file' requires --builds-file")
    raise ProviderError(f"unknown provider {source.provider!r}")


def summarise(builds: Iterable[PlatformBuild]) -> str:
    return ", ".join(f"{b.platform}={b.max_version}" for b in sorted(builds, key=lambda b: b.platform))
