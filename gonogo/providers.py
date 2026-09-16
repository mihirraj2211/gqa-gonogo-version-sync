"""Build-number sources.

Every provider returns ``{platform_key: PlatformBuild}``. The platform keys are
whatever ``clients[].platform`` says in the config, so swapping providers never
touches the Confluence side of the sync.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
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


def _auth_headers(token: str | None, mode: str, header_name: str | None = None) -> dict[str, str]:
    if not token or mode == "none":
        return {}
    if mode == "bearer":
        return {"Authorization": f"Bearer {token}"}
    if mode == "token":
        return {"Authorization": f"Token {token}"}
    if mode == "jfrog":
        return {"X-JFrog-Art-Api": token}
    if mode == "basic":
        # A token without a colon is treated as the username, which is how
        # Atlassian-style "email:token" pairs and bare API keys both work.
        pair = token if ":" in token else f"{token}:"
        return {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}
    if mode == "header":
        return {header_name or "X-Api-Key": token}
    raise ProviderError(f"unsupported auth mode {mode!r}")


class _Moment:
    """A datetime that formats itself inside query templates: ``{today:%d/%m/%Y}``."""

    def __init__(self, moment: datetime):
        self._moment = moment

    def __format__(self, spec: str) -> str:
        return self._moment.strftime(spec or "%Y-%m-%d")

    def __str__(self) -> str:
        return self._moment.strftime("%Y-%m-%d")


def render_query(query: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Expand date tokens in query and body values, at any nesting depth.

    The build dashboard is queried per day, so the schedule needs ``today`` to
    move with the clock instead of being pinned in config.
    """
    moment = now or datetime.now()
    tokens = {
        "today": _Moment(moment),
        "yesterday": _Moment(moment - timedelta(days=1)),
        "tomorrow": _Moment(moment + timedelta(days=1)),
        "now": _Moment(moment),
        "epoch_ms": int(moment.timestamp() * 1000),
    }

    def render(key: str, value: Any) -> Any:
        if isinstance(value, dict):
            return {inner: render(inner, item) for inner, item in value.items()}
        if isinstance(value, list):
            return [render(key, item) for item in value]
        if isinstance(value, str) and "{" in value:
            try:
                return value.format(**tokens)
            except (KeyError, IndexError, ValueError) as exc:
                raise ProviderError(f"cannot render query parameter {key}={value!r}: {exc}") from exc
        return value

    return {key: render(key, value) for key, value in query.items()}


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


def _brand_aliases(response_cfg: dict[str, Any], key: str, default: str) -> tuple[str, ...]:
    """Read a brand value that may be a single name or a list of aliases."""
    raw = response_cfg.get(key, default)
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    return tuple(normalise_label(str(value)) for value in values if str(value).strip())


def _read_version(record: dict[str, Any], field: str | None) -> str | None:
    if not field:
        return None
    raw = record.get(field)
    if raw is None:
        return None
    text = str(raw).strip()
    return text if is_version(text) else extract_version(text)


def _nested_brand_versions(
    nested: Any,
    response_cfg: dict[str, Any],
    max_brands: tuple[str, ...],
    dplus_brands: tuple[str, ...],
) -> tuple[str | None, str | None]:
    """Read a per-brand block carried on one platform record.

    Covers both ``{"hbomax": "7.13.0.68", "dplus": "21.13.0.68"}`` and
    ``[{"brand": "HBOMAX", "version": "7.13.0.68"}, ...]``.
    """
    version_field = response_cfg.get("version_field", "version")
    brand_field = response_cfg.get("brand_field") or "brand"

    if isinstance(nested, dict):
        pairs = [(normalise_label(str(key)), value) for key, value in nested.items()]
    elif isinstance(nested, list):
        pairs = [
            (normalise_label(str(item.get(brand_field, ""))), item)
            for item in nested
            if isinstance(item, dict)
        ]
    else:
        return None, None

    max_version: str | None = None
    dplus_version: str | None = None
    for brand, value in pairs:
        record = value if isinstance(value, dict) else {version_field: value}
        version = _read_version(record, version_field)
        if not version:
            continue
        if brand in dplus_brands:
            dplus_version = version
        elif brand in max_brands:
            max_version = version
    return max_version, dplus_version


def collect_builds(payload: Any, response_cfg: dict[str, Any], brand: str | None = None) -> list[PlatformBuild]:
    """Parse one payload into per-record builds, without merging or validating.

    ``brand`` tags every record in the payload and is used when the API serves
    one brand per call instead of labelling each record.
    """
    records = _dig(payload, response_cfg.get("records_path", "") or "")
    platform_field = response_cfg.get("platform_field", "name")
    version_field = response_cfg.get("version_field", "version")
    brand_field = response_cfg.get("brand_field")
    brand_versions_field = response_cfg.get("brand_versions_field")
    max_brands = _brand_aliases(response_cfg, "max_brand_value", "max")
    dplus_brands = _brand_aliases(response_cfg, "dplus_brand_value", "dplus")
    built_at_field = response_cfg.get("built_at_field")
    request_brand = normalise_label(brand or "")

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

    collected: list[PlatformBuild] = []
    for platform_name, record in rows:
        platform = normalise_label(platform_name).replace(" ", "")
        if not platform:
            log.warning("skipping build record with no platform: %s", record)
            continue

        built_at = str(record.get(built_at_field)) if built_at_field and record.get(built_at_field) else None
        nested = record.get(brand_versions_field) if brand_versions_field else None

        if nested:
            max_version, dplus_version = _nested_brand_versions(nested, response_cfg, max_brands, dplus_brands)
            if not max_version and not dplus_version:
                log.warning("skipping %s: no parsable version in %s", platform, record)
                continue
            collected.append(
                PlatformBuild(
                    platform=platform,
                    max_version=max_version or "",
                    dplus_version=dplus_version,
                    built_at=built_at,
                )
            )
            continue

        version = _read_version(record, version_field)
        if not version:
            log.warning("skipping %s: no parsable version in %s", platform, record)
            continue

        record_brand = request_brand
        if brand_field and record.get(brand_field) is not None:
            record_brand = normalise_label(str(record.get(brand_field)))

        if record_brand and record_brand in dplus_brands:
            collected.append(
                PlatformBuild(platform=platform, max_version="", dplus_version=version, built_at=built_at)
            )
            continue

        # Explicit per-brand D+ fields win over a brand-tagged record.
        dplus = _read_version(record, "dplus_version") or _read_version(record, "dplusVersion")
        collected.append(
            PlatformBuild(platform=platform, max_version=version, dplus_version=dplus, built_at=built_at)
        )

    return collected


def merge_builds(items: Iterable[PlatformBuild]) -> dict[str, PlatformBuild]:
    """Fold per-record builds into one entry per platform.

    Later records win, so a MAX and a D+ call for the same platform combine
    instead of overwriting each other.
    """
    merged: dict[str, PlatformBuild] = {}
    for item in items:
        existing = merged.get(item.platform)
        if existing is None:
            merged[item.platform] = item
            continue
        merged[item.platform] = PlatformBuild(
            platform=item.platform,
            max_version=item.max_version or existing.max_version,
            dplus_version=item.dplus_version or existing.dplus_version,
            built_at=item.built_at or existing.built_at,
        )
    return merged


def require_builds(builds: dict[str, PlatformBuild]) -> dict[str, PlatformBuild]:
    """Drop platforms with no MAX build and fail if nothing usable is left."""
    complete = {}
    for key, value in builds.items():
        if not value.max_version:
            log.warning("dropping %s: D+ build present but no MAX build", key)
            continue
        complete[key] = value
    if not complete:
        raise ProviderError("build source returned no usable versions")
    return complete


def normalise_payload(
    payload: Any, response_cfg: dict[str, Any], brand: str | None = None
) -> dict[str, PlatformBuild]:
    """Turn a single API payload into ``{platform: PlatformBuild}``."""
    return require_builds(merge_builds(collect_builds(payload, response_cfg, brand)))


class HttpJsonProvider:
    """Reads builds from a JSON HTTP endpoint (Fuse build API or Grafana).

    ``source.brand_requests`` makes one call per entry and merges the results,
    for APIs that serve a single brand per request the way the dashboard's
    MAX / HBOMAX selector does.
    """

    def __init__(self, source: SourceConfig, session: requests.Session | None = None):
        self.source = source
        self.session = session or build_session()

    def _base_url(self) -> str:
        url = os.environ.get(self.source.url_env)
        if not url:
            raise ProviderError(
                f"{self.source.url_env} is not set; point it at the build API "
                "documented on the Build Fetch API curl reference page"
            )
        return url

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        headers.update(
            _auth_headers(
                os.environ.get(self.source.token_env),
                self.source.auth,
                self.source.options.get("auth_header"),
            )
        )
        extra = self.source.options.get("headers") or {}
        headers.update({str(key): str(value) for key, value in extra.items()})
        return headers

    def fetch_raw(self) -> list[tuple[dict[str, Any], Any]]:
        """Return ``[(request_description, payload), ...]`` for every call made."""
        base_url = self._base_url()
        headers = self._headers()
        variants = self.source.brand_requests or ({},)
        method = (self.source.method or "get").lower()
        if method not in {"get", "post"}:
            raise ProviderError(f"unsupported source.method {self.source.method!r}, expected get or post")
        results: list[tuple[dict[str, Any], Any]] = []

        for variant in variants:
            url = variant.get("url") or base_url
            if variant.get("path"):
                url = f"{url.rstrip('/')}/{str(variant['path']).lstrip('/')}"
            query = {**render_query(self.source.query), **render_query(variant.get("query", {}) or {})}
            body = {**render_query(self.source.body), **render_query(variant.get("body", {}) or {})}

            log.info("fetching builds from %s %s %s", method.upper(), url, query or "")
            if method == "post":
                response = self.session.post(
                    url, headers=headers, params=query, json=body, timeout=self.source.timeout_seconds
                )
            else:
                response = self.session.get(url, headers=headers, params=query, timeout=self.source.timeout_seconds)
            if response.status_code >= 400:
                raise ProviderError(f"build API returned {response.status_code}: {response.text[:400]}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderError(f"build API did not return JSON: {response.text[:200]}") from exc

            description = {"url": url, "query": query, "brand": variant.get("brand", ""), "method": method.upper()}
            results.append((description, payload))

        return results

    def fetch(self) -> dict[str, PlatformBuild]:
        items: list[PlatformBuild] = []
        for description, payload in self.fetch_raw():
            items.extend(collect_builds(payload, self.source.response, description.get("brand")))
        return require_builds(merge_builds(items))


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

        headers = _auth_headers(token, self.source.auth or "bearer", self.source.options.get("auth_header"))
        headers["Content-Type"] = "text/plain"
        dplus_brands = _brand_aliases(self.source.response, "dplus_brand_value", "dplus")
        items: list[PlatformBuild] = []

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
            # A search may target the D+ artifact of a platform MAX already covered.
            if normalise_label(str(search.get("brand", ""))) in dplus_brands:
                items.append(
                    PlatformBuild(platform=platform, max_version="", dplus_version=version, built_at=created)
                )
            else:
                items.append(PlatformBuild(platform=platform, max_version=version, built_at=created))

        return require_builds(merge_builds(items))


class FileProvider:
    """Reads builds from a local JSON file. Used for dry runs and tests."""

    def __init__(self, source: SourceConfig, path: str | Path):
        self.source = source
        self.path = Path(path)

    def fetch_raw(self) -> list[tuple[dict[str, Any], Any]]:
        if not self.path.exists():
            raise ProviderError(f"builds file not found: {self.path}")
        description = {"url": str(self.path), "query": {}, "brand": "", "method": "FILE"}
        return [(description, json.loads(self.path.read_text(encoding="utf-8")))]

    def fetch(self) -> dict[str, PlatformBuild]:
        items: list[PlatformBuild] = []
        for description, payload in self.fetch_raw():
            items.extend(collect_builds(payload, self.source.response, description.get("brand")))
        return require_builds(merge_builds(items))


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
