"""End-to-end: build a tiny world -> write artefacts -> load into SQLite -> query via API."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from snp500.pipeline import write_artifacts
from snp500.reconstruct.events import ADD, REMOVE
from tests.conftest import cand, ref_from_sets, run, snapshot


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    wiki = [cand("2005-01-03", ADD, "AAA", "Alpha Inc"), cand("2010-06-01", REMOVE, "AAA", "Alpha Inc", reason="Beta acquired Alpha."), cand("2010-06-01", ADD, "BBB", "Beta Corp", counterpart="AAA", reason="Beta acquired Alpha."),
            cand("2001-01-02", ADD, "CCC", "Gamma", source="wikipedia_constituents", conf=0.75)]
    snaps = [snapshot("2008-01-01", [("AAA", "Alpha Inc"), ("CCC", "Gamma")], "Industrials"), snapshot("2012-01-01", [("BBB", "Beta Corp"), ("CCC", "Gamma")], "Financials")]
    ref = ref_from_sets({"1996-01-02": {"CCC"}, "2005-01-03": {"CCC", "AAA"}, "2010-06-01": {"CCC", "BBB"}})
    res = run(wiki, snaps, ref)
    build_dir = tmp_path / "build"
    sectors = [dict(company_id=res.registry.get(iv.company_id).company_id, ticker=iv.entry_ticker, as_of_date="2008-01-01", gics_sector="Industrials", gics_sub_industry="", source_url="u", source_type="wikipedia_revision", scraped_at="") for iv in res.intervals]
    meta = {"built_at": "2026-01-01T00:00:00Z", "input_sha256": {}, "n_companies": 3}
    write_artifacts(build_dir, res, sectors, [], [], {}, [], meta)
    raw = tmp_path / "raw"
    raw.mkdir()
    url = f"sqlite:///{tmp_path / 'test.sqlite3'}"
    from snp500.db.load import load_build

    counts = load_build(build_dir, url=url, raw_dir=raw)
    assert counts["companies"] == 3 and counts["constituent_events"] >= 3
    from snp500.api import main as api
    from snp500.db.session import get_sessionmaker

    monkeypatch.setattr(api, "get_sessionmaker", lambda: get_sessionmaker(url))
    return TestClient(api.app)


def test_api_point_in_time(loaded):
    r = loaded.get("/constituents", params={"date": "2007-01-01"}).json()
    assert r["count"] == 2 and sorted(t for c in r["constituents"] for t in c["tickers"]) == ["AAA", "CCC"]
    assert next(c for c in r["constituents"] if c["tickers"] == ["AAA"])["gics_sector"] == "Industrials"
    r = loaded.get("/constituents", params={"date": "2011-01-01"}).json()
    assert sorted(t for c in r["constituents"] for t in c["tickers"]) == ["BBB", "CCC"]
    r = loaded.get("/constituents", params={"date": "2010-06-01"}).json()
    assert "AAA" not in [t for c in r["constituents"] for t in c["tickers"]]  # exit date is the first day out


def test_api_changes_history_exits_longest(loaded):
    ch = loaded.get("/changes", params={"start_date": "2010-01-01", "end_date": "2010-12-31"}).json()["changes"]
    assert {(c["action"], c["ticker_at_event"]) for c in ch} == {("REMOVE", "AAA"), ("ADD", "BBB")}
    h = loaded.get("/companies/AAA").json()["companies"][0]
    assert h["canonical_name"] == "Alpha Inc" and h["memberships"][0]["exit_reason_category"] == "acquisition"
    assert loaded.get("/companies/nope-nothing").status_code == 404
    ex = loaded.get("/exits", params={"start_date": "2010-01-01", "end_date": "2010-12-31"}).json()["exits"]
    assert ex[0]["exit_ticker"] == "AAA" and ex[0]["reason"] == "Beta acquired Alpha."
    lo = loaded.get("/memberships/longest", params={"limit": 1}).json()["memberships"]
    assert lo[0]["company_name"] == "Gamma"
    assert loaded.get("/constituents", params={"date": "yesterday"}).status_code == 400
    assert loaded.get("/analytics/turnover", params={"start_year": 2009, "end_year": 2011}).json()["turnover"][1]["removals"] == 1
    assert loaded.get("/").status_code == 200 and loaded.get("/quality").status_code == 200
