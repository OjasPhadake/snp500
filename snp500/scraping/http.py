"""Polite HTTP fetcher: rate limiting, retries with backoff, caching via the raw store.

Every successful response is persisted to :class:`RawStore` and the returned
object carries the snapshot metadata so downstream records can cite the exact
bytes (URL + fetched_at + sha256) they were derived from.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from snp500.config import settings
from snp500.logging_setup import get_logger, log_event
from snp500.scraping.raw_store import RawSnapshot, RawStore

log = get_logger(__name__)


class TransientHTTPError(Exception):
    pass


@dataclass
class Fetched:
    snapshot: RawSnapshot
    text: str
    from_cache: bool


class Fetcher:
    def __init__(
        self,
        store: Optional[RawStore] = None,
        user_agent: Optional[str] = None,
        requests_per_second: Optional[float] = None,
        timeout: Optional[float] = None,
    ):
        self.store = store or RawStore(settings.raw_dir)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent or settings.user_agent
        self.min_interval = 1.0 / (requests_per_second or settings.requests_per_second)
        self.timeout = timeout or settings.http_timeout_seconds
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    @retry(
        retry=retry_if_exception_type((TransientHTTPError, requests.ConnectionError, requests.Timeout)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(settings.http_retries),
        reraise=True,
    )
    def _get(self, url: str) -> requests.Response:
        self._throttle()
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code in (429, 500, 502, 503, 504):
            log_event(log, "transient http error", url=url, status=resp.status_code)
            raise TransientHTTPError(f"{resp.status_code} for {url}")
        resp.raise_for_status()
        return resp

    def fetch(
        self,
        source: str,
        url: str,
        max_age: Optional[timedelta] = None,
        ext: str = "html",
        note: str = "",
        force: bool = False,
    ) -> Fetched:
        """Return the body for ``url``.

        If a snapshot younger than ``max_age`` exists (or ``max_age`` is None and
        any snapshot exists) and ``force`` is False, the cached bytes are used.
        """
        if not force:
            prev = self.store.latest(source, url)
            if prev is not None:
                age = datetime.now(timezone.utc) - prev.fetched_dt
                if max_age is None or age <= max_age:
                    log_event(log, "cache hit", source=source, url=url, fetched_at=prev.fetched_at)
                    return Fetched(prev, self.store.read_text(prev), True)

        log_event(log, "fetch", source=source, url=url)
        resp = self._get(url)
        snap = self.store.put(
            source=source,
            url=url,
            body=resp.content,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", ""),
            ext=ext,
            note=note,
        )
        log_event(log, "stored", source=source, path=snap.path, sha256=snap.sha256[:10], n_bytes=snap.n_bytes)
        return Fetched(snap, resp.content.decode(resp.encoding or "utf-8", errors="replace"), False)
