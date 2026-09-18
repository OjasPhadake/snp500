"""Append-only raw data store.

Every HTTP response body the pipeline ever uses is written here *before* it is
parsed, so any build can be reproduced from the exact bytes that were seen.

Layout::

    data/raw/<source>/<UTC-timestamp>_<sha256[:10]>.<ext>
    data/raw/<source>/manifest.jsonl     # one JSON line per fetch

Invariants
----------
* Files are never modified or deleted by the pipeline (append-only).
* A fetch whose body is byte-identical to the latest snapshot of the same URL
  re-uses the existing file, but still gets its own manifest line (so we know
  the source was re-checked at that time).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


@dataclass(frozen=True)
class RawSnapshot:
    source: str
    url: str
    fetched_at: str  # ISO-8601 UTC
    sha256: str
    path: str  # relative to raw_dir
    status: int
    content_type: str
    n_bytes: int
    note: str = ""

    @property
    def fetched_dt(self) -> datetime:
        return datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))


class RawStore:
    def __init__(self, raw_dir: Path):
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    # -- reading -----------------------------------------------------------
    def manifest_path(self, source: str) -> Path:
        return self.raw_dir / source / "manifest.jsonl"

    def iter_snapshots(self, source: str) -> Iterator[RawSnapshot]:
        mp = self.manifest_path(source)
        if not mp.exists():
            return
        with mp.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield RawSnapshot(**json.loads(line))

    def latest(self, source: str, url: Optional[str] = None) -> Optional[RawSnapshot]:
        best = None
        for snap in self.iter_snapshots(source):
            if url is not None and snap.url != url:
                continue
            if best is None or snap.fetched_at > best.fetched_at:
                best = snap
        return best

    def read_bytes(self, snap: RawSnapshot) -> bytes:
        data = (self.raw_dir / snap.path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != snap.sha256:
            raise ValueError(f"raw snapshot {snap.path} is corrupt: sha256 mismatch")
        return data

    def read_text(self, snap: RawSnapshot, encoding: str = "utf-8") -> str:
        return self.read_bytes(snap).decode(encoding, errors="replace")

    # -- writing -----------------------------------------------------------
    def put(
        self,
        source: str,
        url: str,
        body: bytes,
        status: int,
        content_type: str,
        ext: str = "html",
        note: str = "",
        fetched_at: Optional[datetime] = None,
    ) -> RawSnapshot:
        fetched_at = fetched_at or datetime.now(timezone.utc)
        ts = fetched_at.strftime("%Y%m%dT%H%M%SZ")
        digest = hashlib.sha256(body).hexdigest()
        src_dir = self.raw_dir / source
        src_dir.mkdir(parents=True, exist_ok=True)

        # Re-use identical content already stored for this URL (never overwrite).
        prev = self.latest(source, url)
        if prev is not None and prev.sha256 == digest and (self.raw_dir / prev.path).exists():
            rel_path = prev.path
        else:
            fname = f"{ts}_{digest[:10]}.{ext}"
            target = src_dir / fname
            if target.exists():  # same second + same hash prefix; extremely unlikely, but never overwrite
                raise FileExistsError(target)
            target.write_bytes(body)
            rel_path = str(Path(source) / fname)

        snap = RawSnapshot(
            source=source,
            url=url,
            fetched_at=fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            sha256=digest,
            path=rel_path,
            status=status,
            content_type=content_type,
            n_bytes=len(body),
            note=note,
        )
        with self.manifest_path(source).open("a") as fh:
            fh.write(json.dumps(asdict(snap)) + "\n")
        return snap
