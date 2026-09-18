from datetime import date

import pytest

from snp500.normalize.text import normalize_name, normalize_ticker, parse_date, strip_footnotes


def test_parse_dates_in_all_source_formats():
    assert parse_date("August 18, 2026 [ 2 ]") == date(2026, 8, 18)
    assert parse_date("2013-06-21") == date(2013, 6, 21)
    assert parse_date("Dec 2, 2013") == date(2013, 12, 2)
    with pytest.raises(ValueError):
        parse_date("sometime in 2013")


def test_ticker_normalisation_handles_share_classes_and_markup_artifacts():
    assert normalize_ticker("BRK-B") == "BRK.B"
    assert normalize_ticker("BF.B") == "BF.B"
    assert normalize_ticker("ALLE |") == "ALLE"  # stray pipe seen in the changes table
    assert normalize_ticker("") == ""
    with pytest.raises(ValueError):
        normalize_ticker("not a ticker!")


def test_name_key_strips_legal_forms_but_keeps_distinguishing_words():
    assert normalize_name("The Walt Disney Company") == "walt disney"
    assert normalize_name("Alphabet Inc. (Class A)") == "alphabet"
    assert normalize_name("U.S. Bancorp") == "u s bancorp"
    assert normalize_name("Wells Fargo & Company") == "wells fargo"
    assert normalize_name("Dow Chemical") != normalize_name("Dow Inc.")


def test_strip_footnotes():
    assert strip_footnotes("Chubb[a] Limited [ 12 ]") == "Chubb Limited"
