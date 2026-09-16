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
    if not token or mode in {"none", "query"}:
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


def _auth_params(token: str | None, mode: str, param_name: str | None = None) -> dict[str, str]:
    """Tokens that travel in the query string rather than a header.

    The Fuse build API gates every JSON route on ``?token=<gate token>``. These
    are kept out of every log line and report, unlike the rest of the query.
    """
    if not token or mode != "query":
        return {}
    return {param_name or "token": token}


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
    """Read a brand value that may be a single name or a list of aliases.

    Order is preference order: the dashboard labels one platform's builds
    ``HBOMAX`` and ``BENELUX``, and only the first of those belongs in the MAX
    column when a platform reports both.
    """
    raw = response_cfg.get(key, default)
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    return tuple(normalise_label(str(value)) for value in values if str(value).strip())


@dataclass(frozen=True)
class _Pick:
    """One candidate version for a cell, with what makes it better than another."""

    version: str
    rank: int          # position in the brand alias list; lower is preferred
    live: bool         # the dashboard's LIVE badge, when the payload exposes it
    built_at: str | None

    def beats(self, other: "_Pick | None") -> bool:
        if other is None:
            return True
        if self.rank != other.rank:
            return self.rank < other.rank
        if self.live != other.live:
            return self.live
        return True  # same brand and liveness: the later record wins


#: What the build API puts in `version` when there is no build to report.
_STATUS_WORDS = {
    "n a": "no build matched the tree",
    "comingsoon": "platform declared but no tree yet",
    "notbuilt": "no tree for this brand on this platform",
    "error": "lookup failed upstream",
}


def _read_version(record: dict[str, Any], field: str | None) -> str | None:
    if not field:
        return None
    raw = record.get(field)
    if raw is None:
        return None
    text = str(raw).strip()
    return text if is_version(text) else extract_version(text)


def _missing_version_reason(record: dict[str, Any], field: str | None) -> tuple[int, str] | None:
    """Explain an unusable version, so a gap in the table is traceable.

    Returns ``None`` when the field is simply absent, which is normal for the
    optional per-brand fields and not worth a line in the log. A declared
    platform with no build yet is expected news; a failed lookup is not.
    """
    raw = record.get(field) if field else None
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    if record.get("fetch_error"):
        return logging.WARNING, f"{text}; fetch_error: {record['fetch_error']}"
    status = _STATUS_WORDS.get(normalise_label(text))
    if status:
        return logging.INFO, f"{text} ({status})"
    return logging.WARNING, f"unparsable version {text!r}"


def _brand_blocks(nested: Any, brand_field: str, version_field: str) -> list[tuple[str, dict[str, Any]]]:
    """Normalise a per-brand block into ``[(brand, record), ...]``.

    Covers both ``{"hbomax": "7.12.0.132", "benelux": "7.12.0.132"}`` and
    ``[{"brand": "HBOMAX", "version": "7.12.0.132", "isLive": true}, ...]``.
    """
    if isinstance(nested, dict):
        return [
            (normalise_label(str(key)), value if isinstance(value, dict) else {version_field: value})
            for key, value in nested.items()
        ]
    if isinstance(nested, list):
        return [
            (normalise_label(str(item.get(brand_field, ""))), item)
            for item in nested
            if isinstance(item, dict)
        ]
    return []


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

    live_field = response_cfg.get("live_field")
    picks: dict[str, dict[str, _Pick | None]] = {}

    def offer(platform: str, slot: str, pick: _Pick | None) -> None:
        if pick is None or not pick.version:
            return
        slots = picks.setdefault(platform, {"max": None, "dplus": None})
        if pick.beats(slots[slot]):
            slots[slot] = pick

    def is_live(record: dict[str, Any]) -> bool:
        return bool(live_field and record.get(live_field))

    def read(
        record: dict[str, Any],
        field: str | None,
        rank: int,
        fallback_built_at: str | None,
        platform: str = "",
    ) -> _Pick | None:
        version = _read_version(record, field)
        if not version:
            reason = _missing_version_reason(record, field)
            if reason:
                log.log(reason[0], "%s: no version to write - %s", platform or "record", reason[1])
            return None
        built_at = str(record.get(built_at_field)) if built_at_field and record.get(built_at_field) else None
        return _Pick(version=version, rank=rank, live=is_live(record), built_at=built_at or fallback_built_at)

    for platform_name, record in rows:
        platform = normalise_label(platform_name).replace(" ", "")
        if not platform:
            log.warning("skipping build record with no platform: %s", record)
            continue

        built_at = str(record.get(built_at_field)) if built_at_field and record.get(built_at_field) else None
        nested = record.get(brand_versions_field) if brand_versions_field else None

        if nested:
            blocks = _brand_blocks(nested, brand_field or "brand", version_field)
            if not blocks:
                log.warning("skipping %s: no parsable version in %s", platform, record)
                continue
            for brand, block in blocks:
                if brand in dplus_brands:
                    rank = dplus_brands.index(brand)
                    offer(platform, "dplus", read(block, version_field, rank, built_at, platform))
                elif brand in max_brands:
                    rank = max_brands.index(brand)
                    offer(platform, "max", read(block, version_field, rank, built_at, platform))
                else:
                    log.debug("%s: ignoring unmapped brand %r", platform, brand)
            continue

        record_brand = request_brand
        if brand_field and record.get(brand_field) is not None:
            record_brand = normalise_label(str(record.get(brand_field)))

        if record_brand and record_brand in dplus_brands:
            rank = dplus_brands.index(record_brand)
            offer(platform, "dplus", read(record, version_field, rank, built_at, platform))
            continue

        # An unlabelled record is MAX, but ranks behind every named brand.
        rank = max_brands.index(record_brand) if record_brand in max_brands else len(max_brands)
        offer(platform, "max", read(record, version_field, rank, built_at, platform))
        # Explicit per-brand D+ fields win over a brand-tagged record.
        for field in ("dplus_version", "dplusVersion"):
            offer(platform, "dplus", read(record, field, -1, built_at, platform))

    collected: list[PlatformBuild] = []
    for platform, slots in picks.items():
        chosen = slots["max"] or slots["dplus"]
        if chosen is None:
            continue
        collected.append(
            PlatformBuild(
                platform=platform,
                max_version=slots["max"].version if slots["max"] else "",
                dplus_version=slots["dplus"].version if slots["dplus"] else None,
                built_at=chosen.built_at,
            )
        )
    if rows and not collected:
        log.warning("no parsable versions in %d record(s)", len(rows))
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


def _error_text(response: requests.Response) -> str:
    """Prefer the API's own ``{"error": "..."}`` message over raw body text."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:400]
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return response.text[:400]


def _auth_hint(status_code: int, source: SourceConfig) -> str:
    if status_code != 401:
        return ""
    return (
        f" (set {source.token_env} to a gate token; a JFrog api_token only opens"
        " the build-url route, not the JSON version routes)"
    )


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

    def _auth_params(self) -> dict[str, str]:
        return _auth_params(
            os.environ.get(self.source.token_env),
            self.source.auth,
            self.source.options.get("auth_param"),
        )

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

            # Auth that rides in the query string is merged in here and nowhere
            # else, so the token never reaches a log line or a job summary.
            params = {**query, **self._auth_params()}

            log.info("fetching builds from %s %s %s", method.upper(), url, query or "")
            if method == "post":
                response = self.session.post(
                    url, headers=headers, params=params, json=body, timeout=self.source.timeout_seconds
                )
            else:
                response = self.session.get(url, headers=headers, params=params, timeout=self.source.timeout_seconds)
            if response.status_code >= 400:
                raise ProviderError(
                    f"build API returned {response.status_code}: {_error_text(response)}"
                    + _auth_hint(response.status_code, self.source)
                )
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
    """Reads builds from local JSON files. Used for dry runs and tests.

    Accepts one file per brand, written ``brand=path``, so an offline rehearsal
    mirrors an API that serves one product per call.
    """

    def __init__(self, source: SourceConfig, paths: str | Path | Iterable[str | Path]):
        self.source = source
        if isinstance(paths, (str, Path)):
            paths = [paths]
        self.files: list[tuple[str, Path]] = []
        for item in paths:
            brand, separator, path = str(item).partition("=")
            self.files.append((brand, Path(path)) if separator else ("", Path(item)))

    def fetch_raw(self) -> list[tuple[dict[str, Any], Any]]:
        results: list[tuple[dict[str, Any], Any]] = []
        for brand, path in self.files:
            if not path.exists():
                raise ProviderError(f"builds file not found: {path}")
            description = {"url": str(path), "query": {}, "brand": brand, "method": "FILE"}
            results.append((description, json.loads(path.read_text(encoding="utf-8"))))
        return results

    def fetch(self) -> dict[str, PlatformBuild]:
        items: list[PlatformBuild] = []
        for description, payload in self.fetch_raw():
            items.extend(collect_builds(payload, self.source.response, description.get("brand")))
        return require_builds(merge_builds(items))


def get_provider(source: SourceConfig, builds_files: str | Path | Iterable[str | Path] | None = None):
    if builds_files:
        return FileProvider(source, builds_files)
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
