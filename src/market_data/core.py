from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pandas as pd

LOG = logging.getLogger(__name__)
HOUR = pd.Timedelta(hours=1)
COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
           "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
VALUES = ["open", "high", "low", "close", "volume", "quote_volume", "trades",
          "taker_buy_volume", "taker_buy_quote_volume"]


def utc(value) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def empty() -> pd.DataFrame:
    return pd.DataFrame({"open_time": pd.Series([], dtype="datetime64[ns, UTC]"),
                         **{c: pd.Series([], dtype="float64") for c in VALUES}})


def normalize(rows) -> pd.DataFrame:
    if not len(rows):
        return empty()
    frame = pd.DataFrame(rows, columns=COLUMNS)
    # Binance USDT-M archives and REST klines use milliseconds.
    frame["open_time"] = pd.to_datetime(pd.to_numeric(frame.open_time), unit="ms", utc=True)
    for col in VALUES:
        frame[col] = pd.to_numeric(frame[col], errors="raise")
    return frame[["open_time", *VALUES]]


def inspect(frame: pd.DataFrame, start, end, hours=1) -> dict:
    """Bounds are inclusive start, exclusive end; only complete candles are expected."""
    start, end = utc(start), utc(end)
    step = pd.Timedelta(hours=hours)
    expected = pd.date_range(start, end, freq=step, inclusive="left")
    times = frame.open_time
    numeric = frame[VALUES]
    import numpy as np
    invalid = (~np.isfinite(numeric).all(axis=1) | (numeric < 0).any(axis=1)
               | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
               | (frame.high < frame[["open", "close", "low"]].max(axis=1))
               | (frame.low > frame[["open", "close", "high"]].min(axis=1))
               | (frame.trades % 1 != 0)
               | (frame.taker_buy_volume > frame.volume)
               | (frame.taker_buy_quote_volume > frame.quote_volume)
               | (times != times.dt.floor(f"{hours}h")) | times.isna())
    return {"rows": len(frame), "duplicates": int(times.duplicated().sum()),
            "invalid_rows": int(invalid.sum()),
            "missing": expected.difference(pd.DatetimeIndex(times)).tolist()}


def clean(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return empty()
    frame = frame.sort_values("open_time").drop_duplicates("open_time", keep="last")
    if inspect(frame, "2023-01-01", "2023-01-01")["invalid_rows"]:
        raise ValueError("Invalid candle values or timestamps; existing file was not overwritten")
    return frame.reset_index(drop=True)


def read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return empty()
    frame = pd.read_parquet(path)
    frame["open_time"] = pd.to_datetime(frame.open_time, utc=True)
    return frame


def write(frame: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".parquet")
    os.close(fd)
    try:
        frame.to_parquet(temporary, index=False, engine="pyarrow")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def missing_ranges(frame, start, end):
    missing = inspect(frame, start, end)["missing"]
    if not missing:
        return []
    ranges, left, previous = [], missing[0], missing[0]
    for t in missing[1:]:
        if t != previous + HOUR:
            ranges.append((left, previous + HOUR))
            left = t
        previous = t
    return ranges + [(left, previous + HOUR)]


def resample_4h(frame):
    frame = clean(frame)
    if frame.empty:
        return frame
    indexed = frame.set_index("open_time")
    groups = indexed.resample("4h", origin="epoch", label="left", closed="left")
    result = groups.agg({"open": "first", "high": "max", "low": "min", "close": "last",
                         **{c: "sum" for c in VALUES if c not in ["open", "high", "low", "close"]}})
    return result.loc[groups.size() == 4].reset_index()
