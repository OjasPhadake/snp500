"""Deterministic parsers for the Wikipedia S&P 500 pages.

Two pages are used:

* ``List of S&P 500 companies`` – current constituents (with GICS sector,
  CIK and "Date added"). Historical *revisions* of this page are also parsed
  to obtain independent point-in-time snapshots (including historical GICS
  sectors), via ``oldid`` URLs.
* ``Historical components of the S&P 500`` – the "selected changes" table.

Parsers return plain dataclasses/dicts and never touch the database. Rows that
cannot be parsed are returned in ``.problems`` instead of being dropped
silently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from bs4 import BeautifulSoup, Tag

from snp500.normalize.text import (
    clean_display_name,
    normalize_ticker,
    parse_date,
    strip_footnotes,
    try_parse_date,
)

CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CHANGES_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
REVISION_URL = "https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid={oldid}"
REVISION_API = (
    "https://en.wikipedia.org/w/api.php?action=query&prop=revisions&titles=List_of_S%26P_500_companies"
    "&rvlimit=1&rvstart={ts}&rvdir=newer&rvprop=ids|timestamp&format=json"
)


@dataclass
class ParseProblem:
    where: str
    row: list[str]
    error: str


@dataclass
class ConstituentRow:
    ticker: str
    name: str
    gics_sector: Optional[str]
    gics_sub_industry: Optional[str]
    headquarters: Optional[str]
    date_added: Optional[date]
    date_added_raw: str
    cik: Optional[str]
    founded: Optional[str]
    wiki_href: Optional[str] = None


@dataclass
class ChangeRow:
    effective_date: date
    added_ticker: str
    added_name: str
    removed_ticker: str
    removed_name: str
    reason: str
    refs: list[str] = field(default_factory=list)
    row_index: int = -1


@dataclass
class ParsedConstituents:
    rows: list[ConstituentRow]
    problems: list[ParseProblem]
    header: list[str]


@dataclass
class ParsedChanges:
    rows: list[ChangeRow]
    problems: list[ParseProblem]


def _cells(tr: Tag) -> list[Tag]:
    return tr.find_all(["td", "th"], recursive=False)


def _text(c: Tag) -> str:
    return strip_footnotes(c.get_text(" ", strip=True))


def _header_index(header: list[str]) -> dict[str, int]:
    """Map semantic column names to indexes using fuzzy header matching.

    Wikipedia's column headers have changed many times since 2005; matching
    by meaning rather than position keeps the parser stable across revisions.
    """
    idx: dict[str, int] = {}
    for i, h in enumerate(header):
        hl = h.lower()
        if "symbol" in hl or "ticker" in hl:
            idx.setdefault("ticker", i)
        elif hl.startswith("securit") or hl.startswith("company") or hl in ("name", "constituent"):
            idx.setdefault("name", i)
        elif "sub-industry" in hl or "sub industry" in hl or "subindustry" in hl:
            idx.setdefault("sub_industry", i)
        elif "sector" in hl:
            idx.setdefault("sector", i)
        elif "headquarters" in hl or "address" in hl or "location" in hl:
            idx.setdefault("hq", i)
        elif "date" in hl and ("added" in hl or "first" in hl):
            idx.setdefault("date_added", i)
        elif hl == "cik":
            idx.setdefault("cik", i)
        elif "founded" in hl:
            idx.setdefault("founded", i)
    return idx


def find_constituents_table(soup: BeautifulSoup) -> Optional[Tag]:
    t = soup.find("table", id="constituents")
    if t is not None:
        return t
    # Older revisions: the first big wikitable whose header contains a ticker column
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) < 100:
            continue
        header = [_text(c) for c in _cells(rows[0])]
        if "ticker" in _header_index(header) and "name" in _header_index(header):
            return t
    return None


def parse_constituents(html: str) -> ParsedConstituents:
    soup = BeautifulSoup(html, "lxml")
    table = find_constituents_table(soup)
    if table is None:
        raise ValueError("constituents table not found")
    trs = table.find_all("tr")
    header = [_text(c) for c in _cells(trs[0])]
    idx = _header_index(header)
    if "ticker" in idx and "name" not in idx and idx["ticker"] + 1 < len(header):
        # Malformed header (seen in a 2025 revision: "Securit"): the name column
        # has always been the one right after the ticker column.
        idx["name"] = idx["ticker"] + 1
    if "ticker" not in idx or "name" not in idx:
        raise ValueError(f"cannot identify ticker/name columns in header {header}")

    rows: list[ConstituentRow] = []
    problems: list[ParseProblem] = []
    for tr in trs[1:]:
        cells = _cells(tr)
        texts = [_text(c) for c in cells]
        if not cells or all(t == "" for t in texts):
            continue
        try:
            def get(key: str) -> Optional[str]:
                i = idx.get(key)
                return texts[i] if i is not None and i < len(texts) else None

            ticker = normalize_ticker(get("ticker") or "")
            if not ticker:
                raise ValueError("empty ticker")
            name_cell = cells[idx["name"]]
            a = name_cell.find("a")
            href = a.get("href") if a is not None else None
            date_raw = get("date_added") or ""
            rows.append(
                ConstituentRow(
                    ticker=ticker,
                    name=clean_display_name(get("name") or ""),
                    gics_sector=get("sector") or None,
                    gics_sub_industry=get("sub_industry") or None,
                    headquarters=get("hq") or None,
                    date_added=try_parse_date(date_raw) if date_raw else None,
                    date_added_raw=date_raw,
                    cik=(get("cik") or None) if (get("cik") or "").isdigit() else None,
                    founded=get("founded") or None,
                    wiki_href=href,
                )
            )
        except Exception as exc:  # noqa: BLE001 - we want every failure recorded
            problems.append(ParseProblem("constituents", texts, str(exc)))
    return ParsedConstituents(rows=rows, problems=problems, header=header)


def _refs(cell: Optional[Tag]) -> list[str]:
    if cell is None:
        return []
    out = []
    for a in cell.find_all("a", href=True):
        h = a["href"]
        if h.startswith("#cite_note"):
            out.append(h)
    return out


def resolve_cite_urls(soup: BeautifulSoup, cite_ids: list[str]) -> list[str]:
    """Follow ``#cite_note-N`` anchors to the external URLs in the reference list."""
    urls = []
    for cid in cite_ids:
        li = soup.find(id=cid.lstrip("#"))
        if li is None:
            continue
        for a in li.find_all("a", href=True):
            if a["href"].startswith("http"):
                urls.append(a["href"])
    return urls


def parse_changes(html: str, resolve_refs: bool = True) -> ParsedChanges:
    """Parse the "Selected changes" table.

    Expected columns: Effective Date | Added(Ticker, Security) | Removed(Ticker, Security) | Reason | Refs.
    The header spans two rows; data rows have 7 cells (6 when the Refs cell is missing).
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="changes")
    if table is None:
        for t in soup.find_all("table"):
            hdr = [_text(c) for c in _cells(t.find("tr"))] if t.find("tr") else []
            if hdr and hdr[0].lower().startswith("effective date") or (hdr and hdr[0].lower() == "date"):
                table = t
                break
    if table is None:
        raise ValueError("changes table not found")

    rows: list[ChangeRow] = []
    problems: list[ParseProblem] = []
    for i, tr in enumerate(table.find_all("tr")):
        cells = _cells(tr)
        if not cells or cells[0].name == "th" or all(c.name == "th" for c in cells):
            continue  # header rows
        texts = [_text(c) for c in cells]
        if len(texts) < 6:
            problems.append(ParseProblem("changes", texts, f"expected >=6 cells, got {len(texts)}"))
            continue
        if any(c.get("rowspan") for c in cells):
            problems.append(ParseProblem("changes", texts, "rowspan layout not supported; verify manually"))
            continue
        try:
            eff = parse_date(texts[0])
            added_t = normalize_ticker(texts[1])
            removed_t = normalize_ticker(texts[3])
            added_n = clean_display_name(texts[2])
            removed_n = clean_display_name(texts[4])
            if not added_t and not removed_t:
                raise ValueError("neither added nor removed ticker present")
            refs = _refs(cells[6]) if len(cells) > 6 else []
            if resolve_refs:
                refs = resolve_cite_urls(soup, refs)
            rows.append(
                ChangeRow(
                    effective_date=eff,
                    added_ticker=added_t,
                    added_name=added_n,
                    removed_ticker=removed_t,
                    removed_name=removed_n,
                    reason=texts[5],
                    refs=refs,
                    row_index=i,
                )
            )
        except Exception as exc:  # noqa: BLE001
            problems.append(ParseProblem("changes", texts, str(exc)))
    return ParsedChanges(rows=rows, problems=problems)


_REVID_RE = re.compile(r'"revid":\s*(\d+)')
_TS_RE = re.compile(r'"timestamp":\s*"([^"]+)"')


def parse_revision_lookup(json_text: str) -> tuple[Optional[int], Optional[str]]:
    m = _REVID_RE.search(json_text)
    t = _TS_RE.search(json_text)
    return (int(m.group(1)) if m else None, t.group(1) if t else None)
