"""Confluence storage-format reading, targeted table editing and publishing.

Storage format is XHTML with namespaced macro elements. It is parsed as XML, not
HTML: an HTML round-trip rewrites self-closing macro tags into something
Confluence rejects, breaking every macro on the page.
"""

from __future__ import annotations

import html.entities
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import requests
from lxml import etree

from .config import ConfluenceConfig, normalise_label
from .providers import build_session

log = logging.getLogger(__name__)

# Declared on a temporary wrapper so lxml can parse the ac:/ri:/at: prefixes.
_NAMESPACES = {
    "ac": "http://atlassian.com/content",
    "ri": "http://atlassian.com/resource/identifier",
    "at": "http://atlassian.com/schema",
}
_XML_BUILTIN_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_ENTITY_RE = re.compile(r"&([a-zA-Z][a-zA-Z0-9]*);")


@dataclass(frozen=True)
class Page:
    id: str
    title: str
    version: int
    body: str


@dataclass(frozen=True)
class CellChange:
    row: str
    column: str
    old: str
    new: str
    #: Same version, rewritten only to match the table's formatting.
    reformat: bool = False

    def __str__(self) -> str:
        if self.reformat:
            return f"{self.row} [{self.column}]: {self.new} reformatted as inline code"
        return f"{self.row} [{self.column}]: {self.old or '(empty)'} -> {self.new}"


class ConfluenceError(RuntimeError):
    pass


class TableNotFoundError(ConfluenceError):
    pass


class ConflictError(ConfluenceError):
    """The page moved on between reading and publishing; re-read and retry."""


def _numeric_entities(xhtml: str) -> str:
    """Replace named HTML entities with numeric ones so the XML parser accepts them."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in _XML_BUILTIN_ENTITIES:
            return match.group(0)
        char = html.entities.html5.get(f"{name};")
        return f"&#{ord(char[0])};" if char else match.group(0)

    return _ENTITY_RE.sub(replace, xhtml)


def parse_storage(body: str) -> etree._Element:
    declarations = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in _NAMESPACES.items())
    wrapped = f"<root {declarations}>{_numeric_entities(body)}</root>"
    parser = etree.XMLParser(recover=False, resolve_entities=False, huge_tree=True)
    try:
        return etree.fromstring(wrapped.encode("utf-8"), parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ConfluenceError(f"could not parse page storage format: {exc}") from exc


def serialise_storage(root: etree._Element) -> str:
    """Serialise the wrapper's children back to a storage-format body.

    The wrapper tags are sliced off rather than dropped element by element, so
    the namespace declarations stay on the discarded root instead of being
    repeated on every macro in the page.
    """
    xml = etree.tostring(root, encoding="unicode")
    if xml.endswith("/>"):
        return ""
    body = xml[xml.index(">") + 1 : xml.rindex("<")]
    # Confluence writes non-breaking spaces as &nbsp;. Restoring the entity
    # keeps page-history diffs limited to the cells that actually changed.
    return body.replace("\u00a0", "&nbsp;")


def cell_text(cell: etree._Element) -> str:
    return " ".join("".join(cell.itertext()).split())


def cell_style(cell: etree._Element) -> str:
    """How a cell's text is marked up: "code" for inline code, else "plain"."""
    return "code" if cell.find(".//code") is not None else "plain"


def set_cell_text(cell: etree._Element, value: str, style: str = "plain") -> None:
    """Replace a cell's contents with a single paragraph holding ``value``.

    The sign-off table shows versions as inline code, so a cell written as bare
    text reads as the odd one out next to the hand-typed rows.
    """
    for child in list(cell):
        cell.remove(child)
    cell.text = None
    paragraph = etree.SubElement(cell, "p")
    if style == "code":
        etree.SubElement(paragraph, "code").text = value
    else:
        paragraph.text = value


def _is_header_row(row: etree._Element) -> bool:
    """True for a row made only of header cells.

    The sign-off table repeats its header part-way down, which would otherwise
    be read as a client row labelled "Client".
    """
    return bool(row.findall("th")) and not row.findall("td")


def _column_index(header_cells: list[etree._Element], candidates: Iterable[str]) -> int | None:
    wanted = [normalise_label(name) for name in candidates]
    headers = [normalise_label(cell_text(cell)) for cell in header_cells]
    for name in wanted:  # exact match first, so "MAX" never steals "MAX Version Number"
        if name in headers:
            return headers.index(name)
    for name in wanted:
        for index, header in enumerate(headers):
            if name and name in header:
                return index
    return None


def find_signoff_table(root: etree._Element, columns: dict[str, list[str]]) -> tuple[etree._Element, dict[str, int]]:
    """Locate the Go/No-Go table and the indices of the columns to update."""
    client_names = columns.get("client", ["Client"])
    best: tuple[etree._Element, dict[str, int]] | None = None

    for table in root.iter("table"):
        rows = table.findall(".//tr")
        if not rows:
            continue
        header_cells = rows[0].findall("th") or rows[0].findall("td")
        if not header_cells:
            continue
        client_index = _column_index(header_cells, client_names)
        max_index = _column_index(header_cells, columns.get("max", []))
        dplus_index = _column_index(header_cells, columns.get("dplus", []))
        if max_index is None and dplus_index is None:
            continue
        indices = {"client": 0 if client_index is None else client_index}
        if max_index is not None:
            indices["max"] = max_index
        if dplus_index is not None:
            indices["dplus"] = dplus_index
        # Prefer the table that carries both version columns.
        if best is None or len(indices) > len(best[1]):
            best = (table, indices)

    if best is None:
        raise TableNotFoundError(
            "no table on the page has a MAX or D+ version column; check "
            "confluence.columns in the config against the page headers"
        )
    return best


def pair_labels(keys: Sequence[str], page_labels: Sequence[str]) -> dict[str, int]:
    """Match each configured row label to at most one label on the page.

    The two spellings drift apart: a config row "Apple" meets a table that
    calls the row "Apple iOS / tvOS / VisionOS". Exact matches are taken first,
    then a label the page only extends, and then only when exactly one row
    extends it -- two candidates is a guess, and a guess writes a version into
    the wrong client's row.

    Returns ``{config label: index into page_labels}``.
    """
    normalised = [normalise_label(label) for label in page_labels]
    pairing: dict[str, int] = {}
    taken: set[int] = set()

    for key in keys:
        wanted = normalise_label(key)
        for index, label in enumerate(normalised):
            if index not in taken and label == wanted:
                pairing[key] = index
                taken.add(index)
                break

    for key in keys:
        if key in pairing:
            continue
        wanted = normalise_label(key) + " "
        extended = [
            index
            for index, label in enumerate(normalised)
            if index not in taken and label.startswith(wanted)
        ]
        if len(extended) == 1:
            pairing[key] = extended[0]
            taken.add(extended[0])
            log.info("row %r matched the table's %r", key, page_labels[extended[0]])
        elif len(extended) > 1:
            log.warning(
                "row %r could be any of %s; name it exactly in the config rather than "
                "having a version written into the wrong row",
                key,
                ", ".join(repr(page_labels[index]) for index in extended),
            )

    return pairing


def _pair_rows(
    rows: list[etree._Element],
    updates: dict[str, dict[str, str]],
    client_index: int,
) -> dict[int, str]:
    """``{id(row): config label}`` for the rows this run may write."""
    spellings = [
        cell_text((row.findall("th") + row.findall("td"))[client_index]) for row in rows
    ]
    pairing = pair_labels(list(updates), spellings)
    return {id(rows[index]): key for key, index in pairing.items()}


def apply_versions(
    body: str,
    updates: dict[str, dict[str, str]],
    columns: dict[str, list[str]],
    style: str = "code",
) -> tuple[str, list[CellChange], list[str]]:
    """Write MAX/D+ versions into the sign-off table.

    ``updates`` maps a client row label to ``{"max": ..., "dplus": ...}``.
    Returns the new body, the changes applied, and rows that were not matched.
    """
    root = parse_storage(body)
    table, indices = find_signoff_table(root, columns)

    rows = table.findall(".//tr")
    header_cells = rows[0].findall("th") or rows[0].findall("td")
    column_names = {
        key: cell_text(header_cells[index]) if index < len(header_cells) else key
        for key, index in indices.items()
    }

    changes: list[CellChange] = []
    matched_rows: set[str] = set()

    client_rows = [
        row
        for row in rows[1:]
        if not _is_header_row(row)
        and len(row.findall("th") + row.findall("td")) > indices["client"]
    ]
    pairing = _pair_rows(client_rows, updates, indices["client"])

    for row in client_rows:
        key = pairing.get(id(row))
        values = updates.get(key) if key else None
        if not values:
            continue
        cells = row.findall("th") + row.findall("td")
        label = cell_text(cells[indices["client"]])
        matched_rows.add(key)

        for key in ("max", "dplus"):
            new_value = values.get(key)
            index = indices.get(key)
            if not new_value or index is None or index >= len(cells):
                continue
            cell = cells[index]
            old_value = cell_text(cell)
            # A cell can hold the right version in the wrong markup, left plain
            # by an older run of this tool; rewrite it so the column is uniform.
            reformat = old_value == new_value and cell_style(cell) != style
            if old_value == new_value and not reformat:
                continue
            set_cell_text(cell, new_value, style)
            changes.append(
                CellChange(
                    row=label,
                    column=column_names.get(key, key),
                    old=old_value,
                    new=new_value,
                    reformat=reformat,
                )
            )

    unmatched = [key for key in updates if key not in matched_rows]
    return serialise_storage(root), changes, unmatched


@dataclass(frozen=True)
class TableShape:
    """What the sign-off table looks like right now, for config checks."""

    headers: list[str]
    row_labels: list[str]
    indices: dict[str, int]


def describe_table(body: str, columns: dict[str, list[str]]) -> TableShape:
    """Report the headers and client rows found on the page."""
    root = parse_storage(body)
    table, indices = find_signoff_table(root, columns)
    rows = table.findall(".//tr")
    header_cells = rows[0].findall("th") or rows[0].findall("td")

    labels: list[str] = []
    for row in rows[1:]:
        if _is_header_row(row):
            continue
        cells = row.findall("th") + row.findall("td")
        if len(cells) > indices["client"]:
            label = cell_text(cells[indices["client"]])
            if label:
                labels.append(label)

    return TableShape(
        headers=[cell_text(cell) for cell in header_cells],
        row_labels=labels,
        indices=indices,
    )


class ConfluenceClient:
    def __init__(self, config: ConfluenceConfig, email: str, token: str, session: requests.Session | None = None):
        if not email or not token:
            raise ConfluenceError("ATLASSIAN_USER_EMAIL and ATLASSIAN_API_TOKEN are required")
        self.config = config
        self.session = session or build_session()
        self.session.auth = (email, token)
        self.base_url = f"https://{config.domain}/wiki/api/v2"

    def get_page(self, page_id: str) -> Page:
        response = self.session.get(
            f"{self.base_url}/pages/{page_id}",
            params={"body-format": "storage"},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        self._raise_for_status(response, f"reading page {page_id}")
        data: dict[str, Any] = response.json()
        return Page(
            id=str(data["id"]),
            title=data["title"],
            version=int(data["version"]["number"]),
            body=data["body"]["storage"]["value"],
        )

    def search_pages(self, title: str) -> list[dict[str, Any]]:
        """Candidate pages for a title: zero or one, never a guess.

        Titles are matched loosely because the live page is named "Copy of
        7.12.0 ...", and an exact title beats a copy. More than one loose match
        raises: publishing to the wrong sign-off page is worse than not running.
        """
        clauses = ["type = page", f'title ~ "{title}"']
        if self.config.space_key:
            clauses.insert(0, f'space = "{self.config.space_key}"')
        cql = " and ".join(clauses) + " order by lastmodified desc"

        response = self.session.get(
            f"https://{self.config.domain}/wiki/rest/api/content/search",
            params={"cql": cql, "limit": 25},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        self._raise_for_status(response, f"searching for a page titled {title!r}")
        results = response.json().get("results") or []

        wanted = normalise_label(title)
        exact = [item for item in results if normalise_label(item.get("title", "")) == wanted]
        candidates = exact or results

        if len(candidates) > 1:
            listing = "; ".join(f"{item['id']} ({item.get('title')!r})" for item in candidates[:10])
            raise ConfluenceError(
                f"{len(candidates)} pages match the title {title!r}, so this run will not "
                f"guess which one to publish to: {listing}. Set CONFLUENCE_PAGE_ID to the right one"
            )
        return candidates

    def find_page_by_title(self, title: str) -> Page:
        """Read the single page matching a title, or explain that there is none."""
        candidates = self.search_pages(title)
        if not candidates:
            where = f" in space {self.config.space_key}" if self.config.space_key else ""
            raise ConfluenceError(
                f"no page{where} matches the title {title!r}. Create that train's "
                "sign-off page, or set CONFLUENCE_PAGE_ID to target one directly"
            )
        chosen = candidates[0]
        log.info("resolved title %r to page %s (%r)", title, chosen["id"], chosen.get("title"))
        return self.get_page(str(chosen["id"]))

    def update_page(self, page: Page, body: str, message: str) -> int:
        next_version = page.version + 1
        payload = {
            "id": page.id,
            "status": "current",
            "title": page.title,
            "body": {"representation": "storage", "value": body},
            "version": {"number": next_version, "message": message},
        }
        response = self.session.put(
            f"{self.base_url}/pages/{page.id}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        self._raise_for_status(response, f"publishing page {page.id}")
        return next_version

    @staticmethod
    def _raise_for_status(response: requests.Response, action: str) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 409:
            raise ConflictError(f"{action} hit a version conflict: {response.text[:200]}")
        hint = ""
        if response.status_code in (401, 403):
            hint = " (check the API token and that the account can edit this page)"
        elif response.status_code == 404:
            hint = " (check the page id, and that the token owner can see the page)"
        raise ConfluenceError(f"{action} failed with {response.status_code}{hint}: {response.text[:400]}")
