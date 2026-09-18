from datetime import date

from snp500.reconcile.engine import collapse, quarter_ends
from snp500.scraping.reference import intervals_from_snapshots, parse_membership_csv, snapshot_on


def test_reference_intervals_from_snapshots():
    text = "date,tickers\n1996-01-02,\"A,B\"\n1997-05-05,\"A,C\"\n1999-01-04,\"A,B,C\"\n"
    snaps = parse_membership_csv(text)
    ivs = {(i.ticker, i.start, i.end) for i in intervals_from_snapshots(snaps)}
    assert ivs == {("A", date(1996, 1, 2), None), ("B", date(1996, 1, 2), date(1997, 5, 5)), ("C", date(1997, 5, 5), None), ("B", date(1999, 1, 4), None)}
    assert snapshot_on(snaps, date(1998, 1, 1)) == frozenset({"A", "C"})
    assert snapshot_on(snaps, date(1995, 1, 1)) is None


def test_quarter_ends_and_collapse():
    qe = quarter_ends(date(2001, 1, 1), date(2001, 12, 31))
    assert qe == [date(2001, 3, 31), date(2001, 6, 30), date(2001, 9, 30), date(2001, 12, 31)]
    from snp500.reconcile.engine import Discrepancy

    ds = [Discrepancy(date(2001, 1, 2), "reference_company", "extra_in_ours", "X", "C1", "X Co"), Discrepancy(date(2001, 1, 3), "reference_company", "extra_in_ours", "X", "C1", "X Co"), Discrepancy(date(2001, 6, 1), "reference_company", "extra_in_ours", "X", "C1", "X Co")]
    rows = collapse(ds)
    assert len(rows) == 1 and rows[0]["n_dates"] == 3 and rows[0]["severity"] == "membership"
