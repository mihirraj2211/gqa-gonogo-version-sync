"""Entry point: fetch builds, update the Go/No-Go table, publish if changed."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import Config, load_config
from .confluence import CellChange, ConfluenceClient, ConfluenceError, apply_versions
from .providers import PlatformBuild, ProviderError, get_provider
from .versions import VersionError, derive_dplus, parse_version

log = logging.getLogger("gonogo")

EXIT_OK = 0
EXIT_ERROR = 1


def plan_updates(config: Config, builds: dict[str, PlatformBuild]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Map fetched builds onto client rows, deriving D+ versions per scheme."""
    updates: dict[str, dict[str, str]] = {}
    skipped: list[str] = []

    for client in config.clients:
        build = builds.get(client.platform)
        if build is None:
            skipped.append(f"{client.row}: no build reported for platform '{client.platform}'")
            continue
        try:
            max_version = parse_version(build.max_version)
        except VersionError as exc:
            skipped.append(f"{client.row}: {exc}")
            continue

        if config.release.enforce_train and config.release.train and max_version.train != config.release.train:
            skipped.append(
                f"{client.row}: build {max_version} is not on train {config.release.train}"
            )
            continue

        values = {"max": str(max_version)}
        dplus_build = None
        if build.dplus_version:
            try:
                dplus_build = parse_version(build.dplus_version).build
            except VersionError:
                log.warning("%s: ignoring unparsable D+ version %r", client.row, build.dplus_version)
        dplus = derive_dplus(str(max_version), client.dplus_scheme, dplus_build)
        if dplus:
            values["dplus"] = dplus
        updates[client.row] = values

    return updates, skipped


def write_step_summary(lines: list[str]) -> None:
    """Surface the run in the GitHub Actions job summary when available."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _summary_lines(changes: list[CellChange], skipped: list[str], unmatched: list[str], published: bool) -> list[str]:
    lines = ["## Go/No-Go version sync", ""]
    if changes:
        lines.append(f"{'Published' if published else 'Would publish'} {len(changes)} cell update(s):")
        lines.append("")
        lines.append("| Row | Column | Was | Now |")
        lines.append("| --- | --- | --- | --- |")
        lines.extend(f"| {c.row} | {c.column} | {c.old or '_(empty)_'} | {c.new} |" for c in changes)
    else:
        lines.append("Table already matches the latest builds; page left untouched.")
    if unmatched:
        lines += ["", f"Rows not found in the table: {', '.join(unmatched)}"]
    if skipped:
        lines += ["", "Skipped:", ""] + [f"- {item}" for item in skipped]
    return lines


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.page_id:
        config = Config(
            release=config.release,
            confluence=type(config.confluence)(
                domain=config.confluence.domain,
                page_id=args.page_id,
                version_message=config.confluence.version_message,
                columns=config.confluence.columns,
            ),
            source=config.source,
            clients=config.clients,
        )

    provider = get_provider(config.source, args.builds_file)
    builds = provider.fetch()
    log.info("fetched %d platform build(s)", len(builds))

    updates, skipped = plan_updates(config, builds)
    for item in skipped:
        log.warning("%s", item)
    if not updates:
        log.error("no client rows could be resolved from the fetched builds")
        write_step_summary(_summary_lines([], skipped, [], published=False))
        return EXIT_ERROR

    client = ConfluenceClient(
        config.confluence,
        email=os.environ.get("ATLASSIAN_USER_EMAIL", ""),
        token=os.environ.get("ATLASSIAN_API_TOKEN", ""),
    )
    page = client.get_page(config.confluence.page_id)
    log.info("loaded page %s (%r) at version %d", page.id, page.title, page.version)

    new_body, changes, unmatched = apply_versions(page.body, updates, config.confluence.columns)
    for item in unmatched:
        log.warning("row %r not found in the sign-off table", item)

    if args.output:
        Path(args.output).write_text(new_body, encoding="utf-8")
        log.info("wrote updated storage body to %s", args.output)

    if not changes:
        log.info("no version changes; leaving page at version %d", page.version)
        write_step_summary(_summary_lines(changes, skipped, unmatched, published=False))
        return EXIT_OK

    for change in changes:
        log.info("change: %s", change)

    if args.dry_run:
        log.info("dry run: not publishing %d change(s)", len(changes))
        write_step_summary(_summary_lines(changes, skipped, unmatched, published=False))
        return EXIT_OK

    message = f"{config.confluence.version_message} ({len(changes)} cell(s))"
    version = client.update_page(page, new_body, message)
    log.info("published page %s as version %d", page.id, version)
    write_step_summary(_summary_lines(changes, skipped, unmatched, published=True))
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync build versions into the GQA Go/No-Go sign-off table")
    parser.add_argument("--config", default="config/clients.yml", help="path to the client mapping config")
    parser.add_argument("--page-id", help="override the Confluence page id from config")
    parser.add_argument("--builds-file", help="read builds from a local JSON file instead of the API")
    parser.add_argument("--output", help="write the updated storage-format body to this file")
    parser.add_argument("--dry-run", action="store_true", help="report changes without publishing")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return run(args)
    except (ProviderError, ConfluenceError, VersionError, ValueError) as exc:
        log.error("%s", exc)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
