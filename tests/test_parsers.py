"""Parser tests on synthetic and malformed HTML fragments."""
from datetime import date

import pytest

from snp500.scraping.wikipedia import parse_changes, parse_constituents

CHANGES = """
<html><body><table id="changes" class="wikitable">
<tr><th rowspan="2">Effective Date</th><th colspan="2">Added</th><th colspan="2">Removed</th><th rowspan="2">Reason</th><th rowspan="2">Refs</th></tr>
<tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
<tr><td>June 9, 2022</td><td>META</td><td>Meta Platforms</td><td>FB</td><td>Meta Platforms</td><td>Ticker symbol changed from FB to META.</td><td><a href="#cite_note-1">[1]</a></td></tr>
<tr><td>March 23, 2015</td><td>SLG</td><td>SL Green Realty</td><td>NBR</td><td>Nabors Industries</td><td>Market capitalization changes.</td></tr>
<tr><td>December 2, 2013</td><td>ALLE |</td><td>Allegion</td><td>JCP</td><td>J. C. Penney</td><td>Spin-off.</td><td></td></tr>
<tr><td>Not a date</td><td>XXX</td><td>Broken</td><td></td><td></td><td>bad row</td><td></td></tr>
<tr><td>June 30, 2026</td><td></td><td></td><td>CAG</td><td>Conagra Brands</td><td>Market capitalization changes.</td><td></td></tr>
<tr><td>too</td><td>few</td><td>cells</td></tr>
</table>
<ol><li id="cite_note-1"><a href="https://press.spglobal.com/x">S&amp;P</a></li></ol></body></html>
"""

CONSTITUENTS = """
<table id="constituents" class="wikitable">
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th><th>Headquarters Location</th><th>Date added</th><th>CIK</th><th>Founded</th></tr>
<tr><td>MMM</td><td><a href="/wiki/3M">3M</a></td><td>Industrials</td><td>Industrial Conglomerates</td><td>Saint Paul, Minnesota</td><td>1957-03-04</td><td>0000066740</td><td>1902</td></tr>
<tr><td>BRK-B</td><td>Berkshire Hathaway</td><td>Financials</td><td>Multi-Sector Holdings</td><td>Omaha</td><td>2010-02-16</td><td>0001067983</td><td>1839</td></tr>
<tr><td></td><td>Ghost row</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>
"""

OLD_REVISION = """
<table class="wikitable sortable">
<tr><th>Company</th><th>Ticker symbol</th><th>SEC filings</th><th>Industry</th></tr>
""" + "".join(f"<tr><td>Company {i}</td><td>T{i}</td><td>reports</td><td>Industrials</td></tr>" for i in range(120)) + "</table>"

TYPO_HEADER = CONSTITUENTS.replace("<th>Security</th>", "<th>Securit</th>")


def test_parse_changes_records_rows_and_problems_without_dropping_silently():
    parsed = parse_changes(CHANGES)
    dates = [r.effective_date for r in parsed.rows]
    assert date(2022, 6, 9) in dates and date(2026, 6, 30) in dates
    meta = next(r for r in parsed.rows if r.added_ticker == "META")
    assert meta.removed_ticker == "FB" and meta.refs == ["https://press.spglobal.com/x"]
    alle = next(r for r in parsed.rows if r.effective_date == date(2013, 12, 2))
    assert alle.added_ticker == "ALLE"  # pipe artefact repaired
    only_remove = next(r for r in parsed.rows if r.effective_date == date(2026, 6, 30))
    assert only_remove.added_ticker == "" and only_remove.removed_ticker == "CAG"
    errors = [p.error for p in parsed.problems]
    assert any("unparseable date" in e for e in errors)
    assert any("cells" in e for e in errors)
    assert len(parsed.rows) == 4 and len(parsed.problems) == 2


def test_parse_constituents_current_layout():
    parsed = parse_constituents(CONSTITUENTS)
    assert [r.ticker for r in parsed.rows] == ["MMM", "BRK.B"]
    assert parsed.rows[0].date_added == date(1957, 3, 4) and parsed.rows[0].cik == "0000066740"
    assert parsed.rows[1].gics_sector == "Financials"
    assert len(parsed.problems) == 1  # ghost row with empty ticker


def test_parse_constituents_old_revision_layout_by_header_meaning():
    parsed = parse_constituents(OLD_REVISION)
    assert len(parsed.rows) == 120
    assert parsed.rows[0].ticker == "T0" and parsed.rows[0].name == "Company 0" and parsed.rows[0].gics_sector == "Industrials"


def test_parse_constituents_tolerates_typo_in_header():
    parsed = parse_constituents(TYPO_HEADER)
    assert parsed.rows[0].name == "3M"


def test_missing_table_raises():
    with pytest.raises(ValueError):
        parse_constituents("<html><body>nothing here</body></html>")
    with pytest.raises(ValueError):
        parse_changes("<html><body>nothing here</body></html>")
