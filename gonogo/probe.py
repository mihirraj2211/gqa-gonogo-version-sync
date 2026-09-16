"""Read-only check of both ends of the sync: the build API and the page.

Run this first against a new endpoint or page. It reports the payload, how it
maps onto ``source.response`` and which client rows line up with the table, so
a mapping mistake shows up before a schedule starts skipping rows.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from typing import Any

from .config import Config, load_config, normalise_label
from .confluence import ConfluenceClient, ConfluenceError, describe_table
from .providers import (
    ProviderError,
    collect_builds,
    get_provider,
    merge_builds,
)
from .sync import (
    page_from_file,
    resolve_page,
    resolve_train,
    train_from_builds,
    write_step_summary,
)
from .versions import extract_version, is_version

log = logging.getLogger("gonogo.probe")

EXIT_OK = 0
EXIT_ERROR = 1

_PLATFORM_FIELD_NAMES = ("name", "platform", "system", "client", "app", "target", "device")
_DATE_FIELD_HINTS = ("date", "time", "created", "updated", "timestamp", "built")
_BRAND_HINTS = ("max", "hbomax", "hbo max", "dplus", "d+", "discovery", "discoveryplus", "beam")


def _looks_like_version(value: Any) -> bool:
    text = str(value)
    return is_version(text) or bool(extract_version(text))


def _find_records(payload: Any, depth: int = 0, path: str = "") -> tuple[str, Any]:
    """Locate the container holding the build records and its dot path."""
    if isinstance(payload, list):
        return path, payload
    if not isinstance(payload, dict) or depth > 3:
        return path, payload

    preferred = ("data", "results", "builds", "systems", "items", "records", "rows", "versions")
    keys = sorted(payload, key=lambda key: (key.lower() not in preferred, list(payload).index(key)))
    for key in keys:
        value = payload[key]
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return f"{path}.{key}".lstrip("."), value
        if isinstance(value, dict) and value:
            nested_path, nested = _find_records(value, depth + 1, f"{path}.{key}".lstrip("."))
            if isinstance(nested, list) or (isinstance(nested, dict) and nested is not value):
                return nested_path, nested
    # A map keyed by platform is itself the record container.
    return path, payload


def suggest_response_mapping(payload: Any) -> dict[str, Any]:
    """Guess a ``source.response`` block from a real payload."""
    records_path, records = _find_records(payload)
    suggestion: dict[str, Any] = {"records_path": records_path}

    if isinstance(records, dict):
        samples = [value for value in records.values() if isinstance(value, dict)][:8]
        suggestion["platform_field"] = "(payload is keyed by platform; platform_field is unused)"
    elif isinstance(records, list):
        samples = [item for item in records if isinstance(item, dict)][:8]
    else:
        return {"records_path": records_path, "note": "could not find object records in this payload"}

    if not samples:
        return {"records_path": records_path, "note": "records container held no objects"}

    fields = list(dict.fromkeys(key for sample in samples for key in sample))

    version_field = next(
        (f for f in fields if "version" in f.lower() and any(_looks_like_version(s.get(f, "")) for s in samples)),
        None,
    ) or next((f for f in fields if any(_looks_like_version(s.get(f, "")) for s in samples)), None)
    if version_field:
        suggestion["version_field"] = version_field

    if "platform_field" not in suggestion:
        platform_field = next((f for f in fields if f.lower() in _PLATFORM_FIELD_NAMES), None) or next(
            (
                f
                for f in fields
                if f != version_field and all(isinstance(s.get(f), str) for s in samples if f in s)
            ),
            None,
        )
        if platform_field:
            suggestion["platform_field"] = platform_field

    brand_field = next((f for f in fields if "brand" in f.lower()), None) or next(
        (
            f
            for f in fields
            if f not in (version_field,)
            and {normalise_label(str(s.get(f, ""))) for s in samples if f in s}
            and {normalise_label(str(s.get(f, ""))) for s in samples if f in s} <= set(_BRAND_HINTS)
        ),
        None,
    )
    if brand_field:
        suggestion["brand_field"] = brand_field
        seen = {normalise_label(str(s.get(brand_field, ""))) for s in samples}
        suggestion["max_brand_value"] = sorted(v for v in seen if "max" in v) or ["max"]
        suggestion["dplus_brand_value"] = sorted(v for v in seen if "d" in v and "max" not in v) or ["dplus"]

    nested = next(
        (
            f
            for f in fields
            if isinstance(samples[0].get(f), dict)
            and any(normalise_label(k) in _BRAND_HINTS for k in samples[0][f])
        ),
        None,
    )
    if nested:
        suggestion["brand_versions_field"] = nested

    built_at = next((f for f in fields if any(hint in f.lower() for hint in _DATE_FIELD_HINTS)), None)
    if built_at:
        suggestion["built_at_field"] = built_at

    return suggestion


def _as_yaml_block(mapping: dict[str, Any], indent: str = "    ") -> list[str]:
    lines = []
    for key, value in mapping.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(str(item) for item in value) + "]"
        else:
            rendered = '""' if value == "" else str(value)
        lines.append(f"{indent}{key}: {rendered}")
    return lines


def probe_builds(config: Config, args: argparse.Namespace, report: list[str]) -> dict[str, Any]:
    """Fetch from the build source and report what came back."""
    provider = get_provider(config.source, args.builds_file)
    report += ["## Build source", ""]

    raw = provider.fetch_raw() if hasattr(provider, "fetch_raw") else []
    if raw:
        for description, _ in raw:
            query = description.get("query") or {}
            brand = description.get("brand") or "(all brands)"
            method = description.get("method", "GET")
            report.append(f"- `{method} {description.get('url')}` query={query or '{}'} brand={brand}")
        report.append("")

    if raw:
        items = []
        for description, payload in raw:
            items.extend(collect_builds(payload, config.source.response, description.get("brand")))
        builds = merge_builds(items)
        first_payload = raw[0][1]
    else:  # jfrog and any provider without raw payload access
        builds = provider.fetch()
        first_payload = None

    if args.show_payload and first_payload is not None:
        text = json.dumps(first_payload, indent=2, default=str)
        if not args.full and len(text) > 2000:
            text = text[:2000] + "\n... truncated, use --full for everything"
        report += ["<details><summary>Raw payload</summary>", "", "```json", text, "```", "", "</details>", ""]

    if first_payload is not None:
        suggestion = suggest_response_mapping(first_payload)
        report += ["Mapping suggested from this payload:", "", "```yaml", "source:", "  response:"]
        report += _as_yaml_block(suggestion)
        report += ["```", ""]

    if builds:
        report += ["| Platform key | MAX | D+ | Built at |", "| --- | --- | --- | --- |"]
        for key in sorted(builds):
            item = builds[key]
            report.append(
                f"| `{key}` | {item.max_version or '_(none)_'} "
                f"| {item.dplus_version or '_(none)_'} | {item.built_at or ''} |"
            )
        report.append("")
    else:
        report += ["No builds parsed from the payload.", ""]

    wanted = {key for client in config.clients for key in client.platforms}
    missing = sorted(wanted - set(builds))
    extra = sorted(set(builds) - wanted)
    if missing:
        report.append(f"Configured platforms with no build in the payload: {', '.join(missing)}")
    if extra:
        report.append(f"Platform keys returned but not in the config: {', '.join(extra)}")
    if missing or extra:
        report.append("")

    return {"builds": builds, "covered": len(wanted) - len(missing), "wanted": len(wanted)}


def probe_page(
    config: Config,
    args: argparse.Namespace,
    report: list[str],
    builds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the sign-off page and report how the config lines up with it."""
    if args.page_file:
        page = page_from_file(args.page_file, config.confluence.page_id)
    else:
        client = ConfluenceClient(
            config.confluence,
            email=os.environ.get("ATLASSIAN_USER_EMAIL", ""),
            token=os.environ.get("ATLASSIAN_API_TOKEN", ""),
        )
        # Resolved the same way the sync resolves it, so a new train's page can
        # be checked here before anything is written to it.
        page = resolve_page(client, config, args, builds)

    shape = describe_table(page.body, config.confluence.columns)
    train, train_source = resolve_train(config, page.title, args.release_train)

    # A feed that has moved to the next train is the one thing the sync cannot
    # resolve on its own: it needs that train's page to exist.
    feed_train = train_from_builds(builds or {})
    mismatch = ""
    if feed_train and train and feed_train != train:
        mismatch = (
            f"the build feed is on train {feed_train} but this page signs off {train}: "
            f"create the {feed_train} sign-off page, or set RELEASE_TRAIN to the train to write"
        )

    report += [
        "## Sign-off page",
        "",
        f"- Page: **{page.title}** (`{page.id}`), version {page.version}",
        f"- Train to enforce: **{train or 'any'}** (from {train_source})",
        f"- Train in the build feed: **{feed_train or 'unknown'}**",
        f"- Headers found: {', '.join(f'`{h}`' for h in shape.headers if h)}",
        f"- Columns resolved: "
        + ", ".join(f"{key} -> #{index}" for key, index in sorted(shape.indices.items(), key=lambda kv: kv[1])),
        "",
    ]

    matched: list[str] = []
    unmatched: list[str] = []
    for client_cfg in config.clients:
        hit = next((label for label in shape.row_labels if normalise_label(label) in client_cfg.labels), None)
        if hit:
            matched.append(f"{client_cfg.row} -> {hit}")
        else:
            unmatched.append(client_cfg.row)

    report.append(f"Rows matched ({len(matched)}/{len(config.clients)}): " + ", ".join(matched))
    if unmatched:
        report += ["", f"**Config rows not found on the page:** {', '.join(unmatched)}"]
    untracked = [label for label in shape.row_labels if not config.client_for_label(label)]
    if untracked:
        report += ["", f"Rows on the page this repo leaves alone: {', '.join(untracked)}"]
    report.append("")

    if mismatch:
        report += [f"**Train mismatch:** {mismatch}", ""]

    return {
        "matched": len(matched),
        "rows": len(config.clients),
        "page": page,
        "train_mismatch": mismatch,
    }


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.page_id:
        config = replace(config, confluence=replace(config.confluence, page_id=args.page_id))

    report: list[str] = ["# Go/No-Go sync probe", ""]
    failures: list[str] = []
    builds: dict[str, Any] = {}

    try:
        build_result = probe_builds(config, args, report)
        builds = build_result["builds"]
        covered = build_result["covered"]
        if covered < max(1, args.min_platforms):
            failures.append(
                f"only {covered} configured platform(s) found in the build payload, "
                f"expected at least {max(1, args.min_platforms)}"
            )
    except ProviderError as exc:
        report += [f"**Build source failed:** {exc}", ""]
        failures.append(f"build source: {exc}")

    if args.no_page:
        report += ["## Sign-off page", "", "Skipped (`--no-page`).", ""]
    else:
        try:
            page_result = probe_page(config, args, report, builds)
            matched = page_result["matched"]
            if matched < max(1, args.min_rows):
                failures.append(
                    f"only {matched} client row(s) matched the sign-off table, "
                    f"expected at least {max(1, args.min_rows)}"
                )
            if page_result["train_mismatch"]:
                failures.append(page_result["train_mismatch"])
        except (ConfluenceError, OSError) as exc:
            report += [f"**Page check failed:** {exc}", ""]
            failures.append(f"page: {exc}")

    if failures:
        report += ["## Result", "", "Probe failed:", ""] + [f"- {item}" for item in failures]
    else:
        report += ["## Result", "", "Both ends look healthy."]

    print("\n".join(report))
    write_step_summary(report)
    return EXIT_ERROR if failures else EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the build API and sign-off page without writing anything")
    parser.add_argument("--config", default="config/clients.yml", help="path to the client mapping config")
    parser.add_argument("--page-id", help="override the Confluence page id from config")
    parser.add_argument("--page-title", default="", help="find the page by title instead of by id")
    parser.add_argument("--page-file", help="read the page body from a file instead of Confluence")
    parser.add_argument(
        "--builds-file",
        action="append",
        metavar="[BRAND=]PATH",
        help="read builds from a local JSON file instead of the API; repeatable as brand=path",
    )
    parser.add_argument("--release-train", default="", help="train that would be enforced")
    parser.add_argument("--show-payload", action="store_true", help="include the raw API payload in the report")
    parser.add_argument("--full", action="store_true", help="do not truncate the raw payload")
    parser.add_argument("--no-page", action="store_true", help="check only the build source")
    # Thresholds turn silent degradation (a renamed row, a platform that stopped
    # reporting) into a failed run instead of a warning nobody reads.
    parser.add_argument("--min-platforms", type=int, default=1, help="fail if fewer platforms are covered")
    parser.add_argument("--min-rows", type=int, default=1, help="fail if fewer client rows match the table")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.INFO)
    try:
        return run(args)
    except (ValueError, OSError) as exc:
        log.error("%s", exc)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
