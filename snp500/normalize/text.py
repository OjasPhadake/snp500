"""Deterministic normalisation of dates, tickers and company names.

These functions are pure and total: the same input always gives the same
output, and malformed input raises ``ValueError`` (never silently guesses).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Optional

_FOOTNOTE = re.compile(r"\[\s*[^\]]*?\s*\]")  # "[ 12 ]", "[a]", "[note 1]"
_WS = re.compile(r"\s+")

_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d %B %Y", "%B %Y")


def strip_footnotes(s: str) -> str:
    return _WS.sub(" ", _FOOTNOTE.sub("", s or "")).strip()


def parse_date(s: str) -> date:
    """Parse the date formats seen in our sources. Raises ValueError otherwise."""
    s = strip_footnotes(s).replace(" ", " ").strip()
    s = re.sub(r"\s*\(.*?\)\s*$", "", s)  # trailing parenthetical notes
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {s!r}")


def try_parse_date(s: str) -> Optional[date]:
    try:
        return parse_date(s)
    except ValueError:
        return None


_TICKER_RE = re.compile(r"^[A-Z0-9]{1,6}(\.[A-Z])?$")


def normalize_ticker(s: str) -> str:
    """Canonical ticker form: upper-case, share class separated by '.' (BRK.B, BF.B).

    Empty input returns ''. Anything that does not look like a ticker raises.
    """
    t = strip_footnotes(s).upper().replace(" ", "").strip()
    t = t.replace("-", ".").replace("/", ".").replace(" ", "")
    # Wiki-markup artefact seen in the changes table ("ALLE |"): a trailing pipe.
    t = t.rstrip("|").strip()
    if t == "":
        return ""
    if not _TICKER_RE.match(t):
        raise ValueError(f"not a ticker: {s!r}")
    return t


_SUFFIXES = [
    # Legal-form / share-class tokens only. Descriptive words such as "Financial",
    # "Industries" or "Bancorp" are deliberately kept because they distinguish companies.
    "incorporated", "inc", "corporation", "corp", "company", "co", "companies", "cos",
    "limited", "ltd", "plc", "llc", "lp", "l p", "holdings", "holding", "hldgs",
    "sa", "nv", "ag", "se", "cl a", "cl b", "class a", "class b", "class c",
    "and co", "& co",
]
_SUFFIX_RE = re.compile(r"\b(" + "|".join(re.escape(x) for x in sorted(_SUFFIXES, key=len, reverse=True)) + r")\b")


def normalize_name(s: str) -> str:
    """Aggressive company-name key used only for *matching*, never for display.

    "The Walt Disney Company" -> "walt disney"; "Alphabet Inc. (Class A)" -> "alphabet"
    """
    s = strip_footnotes(s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"\(.*?\)", " ", s)  # parenthetical
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _WS.sub(" ", s).strip()
    # repeatedly strip suffix tokens from the end and leading "the"
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"^the ", "", s)
        s = _SUFFIX_RE.sub(" ", s)
        s = _WS.sub(" ", s).strip()
        s = re.sub(r"\band$", "", s).strip()
    return s or prev or "" 


def clean_display_name(s: str) -> str:
    return strip_footnotes(s).replace(" ", " ").strip()
