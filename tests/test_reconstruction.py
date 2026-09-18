"""Scenario tests for entity resolution + membership reconstruction.

Each test builds a tiny synthetic world and checks the *semantics* the platform
promises: ticker changes are not exits, re-entries produce two intervals,
same-day swaps, acquisitions, spin-offs, duplicate identities, missing events,
conflicting sources and malformed data are all handled explicitly.
"""
from datetime import date

from snp500.reconstruct.entity_resolution import Curated, CuratedTickerChange
from snp500.reconstruct.events import ADD, REMOVE
from snp500.reconstruct.membership import TICKER_CHANGE, constituents_on


def ivs(res, name_fragment):
    reg = res.registry
    out = []
    for iv in res.intervals:
        c = reg.get(iv.company_id)
        if name_fragment.lower() in c.canonical_name.lower():
            out.append((iv.entry_date, iv.exit_date, iv.entry_ticker, iv.exit_ticker, iv.supported))
    return sorted(out, key=lambda x: (x[0] or date.min))


def test_same_row_ticker_change_is_not_an_exit(make):
    wiki = [make["cand"]("2022-06-09", ADD, "META", "Meta Platforms", reason="Ticker symbol changed from FB to META.", counterpart="FB"),
            make["cand"]("2022-06-09", REMOVE, "FB", "Meta Platforms", reason="Ticker symbol changed from FB to META.", counterpart="META"),
            make["cand"]("2013-12-23", ADD, "FB", "Meta Platforms")]
    res = make["run"](wiki)
    assert ivs(res, "meta") == [(date(2013, 12, 23), None, "FB", "FB", True)]
    tc = [e for e in res.events if e.action == TICKER_CHANGE]
    assert len(tc) == 1 and tc[0].ticker_at_event == "META"
    comp = res.registry.get(tc[0].company_id)
    assert comp.ticker_on(date(2020, 1, 1)) == "FB" and comp.ticker_on(date(2023, 1, 1)) == "META"


def test_curated_ticker_change_without_any_wikipedia_row(make):
    curated = Curated({}, [CuratedTickerChange("BBT", "TFC", date(2019, 12, 9), "Truist Financial", "https://src")], set())
    wiki = [make["cand"]("1997-12-04", ADD, "TFC", "Truist Financial", source="wikipedia_constituents", conf=0.75)]
    snaps = [make["snapshot"]("2015-01-01", [("BBT", "BB&T Corp")]), make["snapshot"]("2021-01-01", [("TFC", "Truist Financial")])]
    ref = make["ref"]({"1996-01-02": {"BBT"}, "2019-12-09": {"TFC"}})
    res = make["run"](wiki, snaps, ref, curated)
    live = res.registry.live()
    assert len(live) == 1, [c.canonical_name for c in live]
    # reference shows membership already at its 1996 start; Wikipedia's 1997 'date added' conflicts and is recorded as an issue
    assert ivs(res, "truist") == [(date(1996, 1, 2), None, "BBT", "BBT", True)]
    assert any(i.kind == "entry_after_opening" for i in res.issues)
    assert any(e.action == TICKER_CHANGE for e in res.events)


def test_company_re_entry_gives_two_intervals(make):
    wiki = [make["cand"]("2005-01-03", ADD, "AAL", "American Airlines"), make["cand"]("2008-06-02", REMOVE, "AAL", "American Airlines", reason="Market capitalization changes."),
            make["cand"]("2015-03-23", ADD, "AAL", "American Airlines Group"), make["cand"]("2024-09-23", REMOVE, "AAL", "American Airlines Group", reason="Market capitalization changes.")]
    res = make["run"](wiki)
    assert len(res.registry.live()) == 1
    assert ivs(res, "american airlines") == [(date(2005, 1, 3), date(2008, 6, 2), "AAL", "AAL", True), (date(2015, 3, 23), date(2024, 9, 23), "AAL", "AAL", True)]
    assert constituents_on(res.intervals, date(2010, 1, 1)) == []
    assert len(constituents_on(res.intervals, date(2016, 1, 1))) == 1


def test_ticker_reuse_by_a_different_company(make):
    """WM: Washington Mutual until 2008, Waste Management afterwards."""
    wiki = [make["cand"]("2008-09-26", REMOVE, "WM", "Washington Mutual", reason="Bankruptcy of Washington Mutual."), make["cand"]("1998-08-31", ADD, "WM", "Waste Management", source="wikipedia_constituents", conf=0.75)]
    snaps = [make["snapshot"]("2008-01-01", [("WM", "Washington Mutual")]), make["snapshot"]("2009-01-01", [("WM", "Waste Management")])]
    ref = make["ref"]({"1996-01-02": {"WM", "WMI"}, "2008-09-26": {"WMI"}, "2009-01-05": {"WM"}})
    res = make["run"](wiki, snaps, ref)
    names = sorted(c.canonical_name for c in res.registry.live())
    assert names == ["Washington Mutual", "Waste Management"]
    wamu = ivs(res, "washington")
    assert wamu[0][1] == date(2008, 9, 26)
    wm = ivs(res, "waste")
    assert wm[0][1] is None and len(wm) == 1
    assert len(constituents_on(res.intervals, date(2005, 1, 1))) == 2


def test_acquisition_removes_target_and_records_counterpart(make):
    wiki = [make["cand"]("2010-01-04", ADD, "ACQ", "Acquirer Corp"), make["cand"]("2010-01-04", ADD, "TGT", "Target Inc"),
            make["cand"]("2018-06-15", ADD, "NEW", "Newcomer", counterpart="TGT", reason="Acquirer Corp acquired Target Inc."),
            make["cand"]("2018-06-15", REMOVE, "TGT", "Target Inc", counterpart="NEW", reason="Acquirer Corp acquired Target Inc.")]
    res = make["run"](wiki)
    assert ivs(res, "target") == [(date(2010, 1, 4), date(2018, 6, 15), "TGT", "TGT", True)]
    rem = next(e for e in res.events if e.action == REMOVE)
    assert rem.reason_category == "acquisition"
    new_id = next(c.company_id for c in res.registry.live() if c.canonical_name == "Newcomer")
    assert rem.counterpart_company_id == new_id


def test_spinoff_creates_new_company_and_parent_stays(make):
    wiki = [make["cand"]("2000-01-03", ADD, "PAR", "Parent Co"), make["cand"]("2013-01-02", ADD, "SPN", "SpinCo", reason="Parent Co spun off SpinCo.")]
    res = make["run"](wiki)
    assert len(res.registry.live()) == 2
    assert len(constituents_on(res.intervals, date(2014, 1, 1))) == 2
    assert next(e for e in res.events if e.ticker_at_event == "SPN").reason_category == "spinoff"


def test_same_day_add_and_remove_flagged_not_dropped(make):
    wiki = [make["cand"]("2012-03-05", ADD, "ZZ", "Zero Day"), make["cand"]("2012-03-05", REMOVE, "ZZ", "Zero Day", reason="Market capitalization changes.")]
    res = make["run"](wiki)
    assert any(i.kind == "same_day_add_remove" for i in res.issues)
    assert ivs(res, "zero") == [(date(2012, 3, 5), date(2012, 3, 5), "ZZ", "ZZ", True)]
    assert constituents_on(res.intervals, date(2012, 3, 5)) == []


def test_missing_add_event_yields_left_censored_interval(make):
    wiki = [make["cand"]("2009-06-08", REMOVE, "OLD", "Old Economy Inc", reason="Market capitalization changes.")]
    res = make["run"](wiki)
    assert any(i.kind == "remove_without_add" for i in res.issues)
    (iv,) = ivs(res, "old economy")
    assert iv[0] is None and iv[1] == date(2009, 6, 8)
    assert len(constituents_on(res.intervals, date(2005, 1, 1))) == 1  # still counted as a member before exit


def test_reference_gap_fill_and_corroboration(make):
    """Wikipedia knows the exit only; the reference supplies the entry and corroborates the exit."""
    wiki = [make["cand"]("2010-05-03", REMOVE, "GAP", "Gap Filler", reason="Acquired by Someone.")]
    ref = make["ref"]({"1996-01-02": {"AAA"}, "2003-02-03": {"AAA", "GAP"}, "2010-05-03": {"AAA"}})
    res = make["run"](wiki, ref=ref)
    (iv,) = ivs(res, "gap filler")
    assert iv == (date(2003, 2, 3), date(2010, 5, 3), "GAP", "GAP", True)
    rem = next(e for e in res.events if e.action == REMOVE and e.ticker_at_event == "GAP")
    assert rem.source_type == "wikipedia_changes" and "reference_fja05680" in rem.corroborating_sources and rem.confidence > 0.9
    add = next(e for e in res.events if e.action == ADD and e.ticker_at_event == "GAP")
    assert add.source_type == "reference_fja05680" and add.confidence < 0.7


def test_conflicting_sources_keep_higher_confidence_and_record_issue(make):
    wiki = [make["cand"]("2015-06-01", ADD, "CFL", "Conflict Co", source="wikipedia_constituents", conf=0.75)]
    ref = make["ref"]({"1996-01-02": {"AAA"}, "2014-09-15": {"AAA", "CFL"}})
    res = make["run"](wiki, ref=ref)
    (iv,) = ivs(res, "conflict")
    assert iv[0] == date(2014, 9, 15)  # dates differ by > 10 days: both kept as events, first opens the interval
    assert any(i.kind == "conflicting_entry_date" for i in res.issues)


def test_unsupported_single_source_interval_is_flagged_and_excluded(make):
    wiki = [make["cand"]("1998-12-11", ADD, "FSR", "Firstar")]
    ref = make["ref"]({"1996-01-02": {"AAA"}, "2000-01-03": {"AAA", "BBB"}})
    res = make["run"](wiki, ref=ref)
    (iv,) = ivs(res, "firstar")
    assert iv[4] is False
    assert all(res.registry.get(i.company_id).canonical_name != "Firstar" for i in constituents_on(res.intervals, date(2001, 1, 1)))
    assert len(constituents_on(res.intervals, date(2001, 1, 1), include_unsupported=True)) == 3


def test_duplicate_identity_old_name_reused_by_spinoff(make):
    """Alcoa Inc renamed Arconic (same entity); a new Alcoa Corp appears later."""
    curated = Curated({"exact:alcoa corp": "Alcoa Corporation, 2016 spin-off", "exact:alcoa": "Arconic", "exact:alcoa inc.": "Arconic"}, [CuratedTickerChange("AA", "ARNC", date(2016, 11, 1), "Arconic", "https://src")], set())
    wiki = [make["cand"]("1996-06-03", ADD, "AA", "Alcoa Inc."), make["cand"]("2016-11-01", ADD, "ARNC", "Arconic", counterpart="AA", reason="Alcoa completed the spin-off of Alcoa Corp; the remaining company was renamed Arconic."),
            make["cand"]("2016-11-01", REMOVE, "AA", "Alcoa", counterpart="ARNC", reason="Alcoa completed the spin-off of Alcoa Corp; the remaining company was renamed Arconic."),
            make["cand"]("2017-05-01", ADD, "AA", "Alcoa Corp")]
    res = make["run"](wiki, curated=curated)
    names = sorted(c.canonical_name for c in res.registry.live())
    assert names == ["Alcoa Corporation, 2016 spin-off", "Arconic"]
    assert ivs(res, "arconic") == [(date(1996, 6, 3), None, "AA", "AA", True)]
    assert ivs(res, "2016 spin-off") == [(date(2017, 5, 1), None, "AA", "AA", True)]


def test_deterministic_ids_and_events(make):
    wiki = [make["cand"]("2010-01-04", ADD, "DET", "Determinism Ltd"), make["cand"]("2012-01-04", REMOVE, "DET", "Determinism Ltd", reason="Acquired.")]
    a = make["run"](wiki)
    b = make["run"](wiki)
    assert [(e.event_id, e.company_id) for e in a.events] == [(e.event_id, e.company_id) for e in b.events]
    assert a.events[0].event_id.startswith("E") and a.events[0].company_id.startswith("C")


def test_malformed_candidate_missing_company_is_skipped_not_crashing(make):
    c = make["cand"]("2010-01-04", ADD, "OK", "Fine Co")
    orphan = make["cand"]("2010-01-04", ADD, "ORPH", "", source="reference_fja05680", conf=0.6)
    res = make["run"]([c])
    assert len(res.events) == 1
    from snp500.reconstruct.membership import cluster_and_finalize

    assert cluster_and_finalize([orphan], res.registry, date(2001, 1, 1), date(2026, 12, 31)) == []
