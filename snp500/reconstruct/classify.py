"""Classify the free-text "Reason" of an index change into a controlled vocabulary.

The vocabulary (``ReasonCategory``) is deliberately small. Classification is a
deterministic, ordered rule list; the first matching rule wins. Rules are
ordered from most to least specific so e.g. "spun off ... acquired" is
attributed correctly. Unknown text maps to ``other_unknown`` and is *never*
guessed.
"""
from __future__ import annotations

import re
from enum import Enum


class ReasonCategory(str, Enum):
    ACQUISITION = "acquisition"
    MERGER = "merger"
    BANKRUPTCY = "bankruptcy"
    SPINOFF = "spinoff"
    DELISTING = "delisting"
    GOING_PRIVATE = "going_private"
    MARKET_CAP = "market_cap_eligibility"
    INDEX_RECLASSIFICATION = "index_rebalancing_reclassification"
    RESTRUCTURING = "corporate_restructuring"
    TICKER_CHANGE = "ticker_or_name_change"
    IPO_OR_NEW_LISTING = "new_listing"
    OTHER_UNKNOWN = "other_unknown"


# (category, compiled regex). Order matters.
_RULES: list[tuple[ReasonCategory, re.Pattern]] = [
    (ReasonCategory.TICKER_CHANGE, re.compile(r"\b(ticker|symbol)\b.*\b(chang|switch)|\bchanged (its )?(name|ticker)|\brenam", re.I)),
    (ReasonCategory.BANKRUPTCY, re.compile(r"\bbankrupt|\bchapter (11|7)\b|\binsolven|\breceivership|\bliquidat", re.I)),
    (ReasonCategory.SPINOFF, re.compile(r"\bspin[- ]?(off|out)|\bspun[- ]?(off|out)|\bsplit[- ]off|\bseparat(ed|ion) (from|of)|\bcarve[- ]?out", re.I)),
    (ReasonCategory.GOING_PRIVATE, re.compile(r"\b(taken|went|going|take|go) private|\bprivate equity|\bbuyout\b|\bmanagement[- ]led", re.I)),
    (ReasonCategory.MERGER, re.compile(r"\bmerg(e|ed|er|ing)\b|\bcombin(ed|ation)\b|\bamalgamat", re.I)),
    (ReasonCategory.ACQUISITION, re.compile(r"\bacqui(r|s)|\bbought\b|\bpurchas|\btakeover|\btook over|\btender offer|\bcompleted (its|the) (purchase|deal)", re.I)),
    (ReasonCategory.DELISTING, re.compile(r"\bdelist|\bsuspend|\bhalt|\bceased trading|\bexchange (removed|listing)", re.I)),
    (ReasonCategory.INDEX_RECLASSIFICATION, re.compile(r"\bmidcap|\bmid[- ]cap|\bsmallcap|\bsmall[- ]cap|\bs&p 400|\bs&p 600|\bmoved to|\breclassif|\brebalanc|\bindex (change|reorgan)|\breorgani[sz]ed|\bindustr(y|ies) (representation|balance)|\bfewer", re.I)),
    (ReasonCategory.MARKET_CAP, re.compile(r"\bmarket[- ]cap|\bcapitali[sz]ation|\bno longer (representative|eligible|qualif)|\beligib|\bfloat|\bliquidity|\bsize\b", re.I)),
    (ReasonCategory.RESTRUCTURING, re.compile(r"\brestructur|\breorganiz|\brecapitali|\bholding company|\bconver(ted|sion) to|\bdomicile|\bredomicil|\bmoved (its )?(headquarters|hq|domicile)", re.I)),
    (ReasonCategory.IPO_OR_NEW_LISTING, re.compile(r"\bipo\b|\binitial public offering|\bnewly (listed|public)|\bdirect listing", re.I)),
]


def classify_reason(text: str) -> ReasonCategory:
    t = (text or "").strip()
    if not t:
        return ReasonCategory.OTHER_UNKNOWN
    for cat, rx in _RULES:
        if rx.search(t):
            return cat
    return ReasonCategory.OTHER_UNKNOWN
