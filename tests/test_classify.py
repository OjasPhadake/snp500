from snp500.reconstruct.classify import ReasonCategory, classify_reason


def test_reason_classification_rules():
    assert classify_reason("Facebook acquired WhatsApp") == ReasonCategory.ACQUISITION
    assert classify_reason("Dow and DuPont merged to form DowDuPont") == ReasonCategory.MERGER
    assert classify_reason("Lehman Brothers filed for bankruptcy.") == ReasonCategory.BANKRUPTCY
    assert classify_reason("Abbott spun off AbbVie") == ReasonCategory.SPINOFF
    assert classify_reason("Market capitalization changes.") == ReasonCategory.MARKET_CAP
    assert classify_reason("Moved to S&P MidCap 400.") == ReasonCategory.INDEX_RECLASSIFICATION
    assert classify_reason("Dell was taken private.") == ReasonCategory.GOING_PRIVATE
    assert classify_reason("Ticker symbol changed from FB to META") == ReasonCategory.TICKER_CHANGE
    assert classify_reason("") == ReasonCategory.OTHER_UNKNOWN
    assert classify_reason("Something happened") == ReasonCategory.OTHER_UNKNOWN
