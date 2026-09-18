from datetime import datetime, timezone

import pytest

from snp500.scraping.raw_store import RawStore


def test_raw_store_is_append_only_and_verifies_hashes(tmp_path):
    store = RawStore(tmp_path)
    a = store.put("src", "http://x", b"hello", 200, "text/html", fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    b = store.put("src", "http://x", b"hello", 200, "text/html", fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    c = store.put("src", "http://x", b"hello!", 200, "text/html", fetched_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    assert a.path == b.path  # identical content re-used, but both fetches recorded
    assert c.path != a.path
    assert len(list(store.iter_snapshots("src"))) == 3
    assert store.latest("src").sha256 == c.sha256
    assert store.read_text(a) == "hello"
    (tmp_path / c.path).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        store.read_bytes(c)
