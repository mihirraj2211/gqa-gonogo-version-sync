"""Entry point: fetch builds, update the Go/No-Go table, publish if changed."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from .config import Config, load_config
from .confluence import (
    CellChange,
    ConfluenceClient,
    ConfluenceError,
    ConflictError,
    Page,
    apply_versions,
    cell_text,
    parse_storage,
)
from .providers import PlatformBuild, ProviderError, get_provider, summarise
from .versions import (
    VersionError,
    derive_dplus,
    expected_dplus_major,
    extract_train,
    is_version,
    parse_version,
)

log = logging.getLogger("gonogo")

EXIT_OK = 0
EXIT_ERROR = 1


def resolve_train(config: Config, page_title: str, override: str = "") -> tuple[str, str]:
    """Decide which release train this run is allowed to write.

    An explicit override wins, then the page title, because a sign-off page is
    per train and names it: that keeps a 7.13.0 build off a 7.12.0 page without
    this repo being edited every train.
    """
    explicit = (override or os.environ.get("RELEASE_TRAIN", "")).strip()
    if explicit:
        return explicit, "override"
    if config.release.train_from_page_title:
        detected = extract_train(page_title)
        if detected:
            return detected, "page title"
    return config.release.train, "config"


def plan_updates(
    config: Config, builds: dict[str, PlatformBuild], train: str | None = None
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Map fetched builds onto client rows, deriving D+ versions per scheme."""
    updates: dict[str, dict[str, str]] = {}
    skipped: list[str] = []
    train = config.release.train if train is None else train

    for client in config.clients:
        # A row may cover several devices, so take the first that reported and
        # say so when the others disagree: one row cannot sign off two numbers.
        build = None
        for key in client.platforms:
            candidate = builds.get(key)
            if candidate is None:
                continue
            if build is None:
                build = candidate
            elif (candidate.max_version, candidate.dplus_version) != (
                build.max_version,
                build.dplus_version,
            ):
                log.warning(
                    "%s covers %s and %s, which disagree (%s / %s against %s / %s); writing %s",
                    client.row,
                    build.platform,
                    key,
                    build.max_version,
                    build.dplus_version or "no D+",
                    candidate.max_version,
                    candidate.dplus_version or "no D+",
                    build.platform,
                )
        if build is None:
            covered = "/".join(client.platforms)
            skipped.append(f"{client.row}: no build reported for {covered}")
            continue
        try:
            max_version = parse_version(build.max_version)
        except VersionError as exc:
            skipped.append(f"{client.row}: {exc}")
            continue

        if config.release.enforce_train and train and max_version.train != train:
            skipped.append(f"{client.row}: build {max_version} is not on train {train}")
            continue

        values = {"max": str(max_version)}
        reported = None
        if build.dplus_version:
            try:
                reported = parse_version(build.dplus_version)
            except VersionError:
                log.warning("%s: ignoring unparsable D+ version %r", client.row, build.dplus_version)

        expected_major = expected_dplus_major(str(max_version), client.dplus_scheme)
        if reported and expected_major is not None:
            # The source is authoritative for D+, so write it as reported. A
            # major that disagrees with the row's scheme means the row is mapped
            # to the wrong platform or the wrong brand, which is worth saying.
            if reported.major != expected_major:
                log.warning(
                    "%s: D+ %s is not a %s-scheme version (expected major %d); check dplus_scheme and the platform key",
                    client.row,
                    reported,
                    client.dplus_scheme,
                    expected_major,
                )
            values["dplus"] = str(reported)
        elif expected_major is not None:
            guess = derive_dplus(
                str(max_version),
                client.dplus_scheme,
                assume_max_build=config.release.derive_missing_dplus,
            )
            if guess:
                values["dplus"] = guess
                log.warning("%s: no D+ build reported, writing %s borrowed from the MAX build", client.row, guess)
            else:
                log.warning("%s: no D+ version reported, leaving that cell as it is", client.row)
        updates[client.row] = values

    return updates, skipped


def write_step_summary(lines: list[str]) -> None:
    """Surface the run in the GitHub Actions job summary when available."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _summary_lines(
    changes: list[CellChange],
    skipped: list[str],
    unmatched: list[str],
    published: bool,
    context: dict[str, str] | None = None,
) -> list[str]:
    lines = ["## Go/No-Go version sync", ""]
    for key, value in (context or {}).items():
        lines.append(f"- **{key}:** {value}")
    if context:
        lines.append("")
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


def train_mismatch_hint(builds: dict[str, PlatformBuild], train: str) -> str:
    """Explain the common dead end: the feed has moved on to the next train.

    Without this, a 7.12.0 sign-off page against a 7.13.0 feed fails every 15
    minutes with nothing but "no client rows could be resolved".
    """
    if not train:
        return ""
    trains = sorted({parse_version(b.max_version).train for b in builds.values() if is_version(b.max_version)})
    if not trains or train in trains:
        return ""
    return (
        f"; every fetched build is on train {', '.join(trains)} while this page signs off {train}. "
        f"Set RELEASE_TRAIN={trains[-1]} to write that train's page instead, "
        "or CONFLUENCE_PAGE_ID to target one directly"
    )


def page_from_file(path: str | Path, page_id: str) -> Page:
    """Load a storage-format body from disk for a credential-free rehearsal.

    The title is taken from the first heading so train detection behaves the
    same as it does against the live page.
    """
    body = Path(path).read_text(encoding="utf-8")
    title = Path(path).stem
    root = parse_storage(body)
    for tag in ("h1", "h2", "h3"):
        heading = root.find(f".//{tag}")
        if heading is not None and cell_text(heading):
            title = cell_text(heading)
            break
    return Page(id=page_id, title=title, version=0, body=body)


def _publish(
    client: ConfluenceClient,
    page: Page,
    new_body: str,
    changes: list[CellChange],
    unmatched: list[str],
    updates: dict[str, dict[str, str]],
    columns: dict[str, list[str]],
    prefix: str,
    retries: int,
) -> tuple[int | None, list[CellChange], list[str]]:
    """Publish, re-reading the page if someone else edited it in the meantime.

    Returns ``(None, ...)`` when a concurrent edit already carries the versions
    this run wanted to write.
    """
    for attempt in range(retries + 1):
        try:
            message = f"{prefix} ({len(changes)} cell(s))"
            return client.update_page(page, new_body, message), changes, unmatched
        except ConflictError as exc:
            if attempt == retries:
                raise
            log.warning("%s; re-reading page and retrying", exc)
            page = client.get_page(page.id)
            new_body, changes, unmatched = apply_versions(page.body, updates, columns)
            if not changes:
                return None, changes, unmatched

    raise ConfluenceError("exhausted publish retries")  # pragma: no cover - loop always returns


def resolve_page(client: ConfluenceClient, config: Config, args: argparse.Namespace) -> Page:
    """Fetch the page this run should write.

    An explicit id wins. Failing that a title is searched for, which is how a
    new train works with no change here: 7.13.0 gets its own sign-off page, and
    naming the train is enough to find it.
    """
    if args.page_id:
        return client.get_page(args.page_id)

    title = args.page_title or ""
    if not title and not config.confluence.page_id:
        train, source = resolve_train(config, page_title="", override=args.release_train)
        if not train:
            raise ConfluenceError(
                "no page id and no train to search with: set CONFLUENCE_PAGE_ID, "
                "or RELEASE_TRAIN so this train's page can be found by title"
            )
        title = config.confluence.title_for(train)
        log.info("looking for the %s sign-off page (train from %s)", train, source)

    if title:
        return client.find_page_by_title(title)
    return client.get_page(config.confluence.page_id)


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.page_id:
        config = replace(config, confluence=replace(config.confluence, page_id=args.page_id))

    client: ConfluenceClient | None = None
    if args.page_file:
        page = page_from_file(args.page_file, config.confluence.page_id)
        log.info("loaded page body from %s (%r)", args.page_file, page.title)
    else:
        client = ConfluenceClient(
            config.confluence,
            email=os.environ.get("ATLASSIAN_USER_EMAIL", ""),
            token=os.environ.get("ATLASSIAN_API_TOKEN", ""),
        )
        page = resolve_page(client, config, args)
        log.info("loaded page %s (%r) at version %d", page.id, page.title, page.version)

    train, train_source = resolve_train(config, page.title, args.release_train)
    if config.release.enforce_train and not train:
        log.warning("no release train resolved; builds from any train will be accepted")
    else:
        log.info("release train %s (from %s)", train or "(none)", train_source)

    provider = get_provider(config.source, args.builds_file)
    builds = provider.fetch()
    log.info("fetched %d platform build(s): %s", len(builds), summarise(builds.values()))

    updates, skipped = plan_updates(config, builds, train)
    for item in skipped:
        log.warning("%s", item)

    context = {
        "Page": f"{page.title} (`{page.id}`)",
        "Train": f"{train or 'any'} (from {train_source})",
        "Builds fetched": str(len(builds)),
    }

    if not updates:
        hint = train_mismatch_hint(builds, train)
        log.error("no client rows could be resolved from the fetched builds%s", hint)
        if hint:
            context["Problem"] = hint.lstrip("; ")
        write_step_summary(_summary_lines([], skipped, [], published=False, context=context))
        return EXIT_ERROR

    new_body, changes, unmatched = apply_versions(page.body, updates, config.confluence.columns)
    for item in unmatched:
        log.warning("row %r not found in the sign-off table", item)

    if args.output:
        Path(args.output).write_text(new_body, encoding="utf-8")
        log.info("wrote updated storage body to %s", args.output)

    if not changes:
        log.info("no version changes; leaving page at version %d", page.version)
        write_step_summary(_summary_lines(changes, skipped, unmatched, published=False, context=context))
        return EXIT_OK

    for change in changes:
        log.info("change: %s", change)

    if client is None or args.dry_run:
        reason = "no live page" if client is None else "dry run"
        log.info("%s: not publishing %d change(s)", reason, len(changes))
        write_step_summary(_summary_lines(changes, skipped, unmatched, published=False, context=context))
        return EXIT_OK

    version, changes, unmatched = _publish(
        client,
        page,
        new_body,
        changes,
        unmatched,
        updates,
        config.confluence.columns,
        config.confluence.version_message,
        args.retries,
    )
    if version is None:
        log.info("a concurrent edit already carries these versions; nothing published")
        write_step_summary(_summary_lines(changes, skipped, unmatched, published=False, context=context))
        return EXIT_OK

    log.info("published page %s as version %d", page.id, version)
    write_step_summary(_summary_lines(changes, skipped, unmatched, published=True, context=context))
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync build versions into the GQA Go/No-Go sign-off table")
    parser.add_argument("--config", default="config/clients.yml", help="path to the client mapping config")
    parser.add_argument("--page-id", help="override the Confluence page id from config")
    parser.add_argument("--page-title", default="", help="find the page by title instead of by id")
    parser.add_argument("--page-file", help="read the page body from a file instead of Confluence (never publishes)")
    parser.add_argument(
        "--builds-file",
        action="append",
        metavar="[BRAND=]PATH",
        help="read builds from a local JSON file instead of the API; repeatable as brand=path",
    )
    parser.add_argument("--release-train", default="", help="train to enforce, e.g. 7.13.0 (overrides the page title)")
    parser.add_argument("--output", help="write the updated storage-format body to this file")
    parser.add_argument("--retries", type=int, default=2, help="publish attempts after a concurrent-edit conflict")
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
    except (ProviderError, ConfluenceError, VersionError, ValueError, OSError) as exc:
        log.error("%s", exc)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
