from __future__ import annotations

import hashlib
import io
import logging
import time
import zipfile

import pandas as pd
import requests

from .core import COLUMNS, empty, normalize, utc

LOG = logging.getLogger(__name__)


class BinancePublic:
    """Only public archives and public futures klines; no credentials required."""
    archive_base = "https://data.binance.vision/data/futures/um"
    api_base = "https://fapi.binance.com"

    def __init__(self, attempts=5, session=None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.attempts = attempts
        self.sleep = sleep

    def get(self, url, *, optional=False, params=None):
        for attempt in range(self.attempts):
            try:
                response = self.session.get(url, params=params, timeout=(10, 60))
                if optional and response.status_code == 404:
                    return None
                if response.status_code in (418, 429) or response.status_code >= 500:
                    delay = min(300, max(2 ** attempt, float(response.headers.get("Retry-After", 0))))
                    LOG.warning("Retryable HTTP %s: %s (attempt %s)", response.status_code, url, attempt + 1)
                    if attempt + 1 == self.attempts:
                        response.raise_for_status()
                    self.sleep(delay)
                    continue
                response.raise_for_status()
                return response
            except (requests.ConnectionError, requests.Timeout):
                LOG.exception("Network error: %s (attempt %s)", url, attempt + 1)
                if attempt + 1 == self.attempts:
                    raise
                self.sleep(min(60, 2 ** attempt))
        raise RuntimeError("Retry budget exhausted")

    def archive(self, symbol, date, monthly=False):
        period = "monthly" if monthly else "daily"
        stamp = date.strftime("%Y-%m" if monthly else "%Y-%m-%d")
        url = f"{self.archive_base}/{period}/klines/{symbol}/1h/{symbol}-1h-{stamp}.zip"
        response = self.get(url, optional=True)
        if response is None:
            return None
        checksum = self.get(url + ".CHECKSUM")
        expected = checksum.text.split()[0]
        if hashlib.sha256(response.content).hexdigest() != expected:
            raise ValueError(f"Archive checksum mismatch: {url}")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = [n for n in archive.namelist() if n.endswith(".csv")]
            if len(names) != 1:
                raise ValueError(f"Unexpected archive contents: {url}")
            frame = pd.read_csv(archive.open(names[0]), header=None, dtype=str)
        if frame.iloc[0, 0] == "open_time":
            frame = frame.iloc[1:]
        if len(frame.columns) != len(COLUMNS):
            raise ValueError("Unexpected Binance archive schema")
        return normalize(frame.values.tolist())

    def rest(self, symbol, start, end):
        cursor, end = utc(start), utc(end)
        chunks = []
        while cursor < end:
            response = self.get(self.api_base + "/fapi/v1/klines", params={
                "symbol": symbol, "interval": "1h", "startTime": int(cursor.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000) - 1, "limit": 1500})
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Unexpected API response")
            if not rows:
                break
            frame = normalize(rows)
            next_cursor = frame.open_time.max() + pd.Timedelta(hours=1)
            if next_cursor <= cursor:
                raise ValueError("API pagination did not advance")
            chunks.append(frame[(frame.open_time >= cursor) & (frame.open_time < end)])
            cursor = next_cursor
            self.sleep(0.25)
        return pd.concat(chunks, ignore_index=True) if chunks else empty()
