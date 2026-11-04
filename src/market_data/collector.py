from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .core import HOUR, clean, inspect, missing_ranges, read, resample_4h, utc, write

from .sources import KlineSource

LOG = logging.getLogger(__name__)


def collect(source: KlineSource, symbol, start, end, root: Path):
    """Single writer per symbol. Checkpoint each month to allow safe resumption."""
    start, end = utc(start), utc(end)
    path = root / "klines" / symbol / "1h.parquet"
    frame = read(path)
    report = inspect(frame, start, end)
    if report["duplicates"]:
        LOG.warning("%s: removing %s existing duplicates", symbol, report["duplicates"])
    frame = clean(frame)
    month = start.normalize().replace(day=1)
    while month < end:
        next_month = month + pd.offsets.MonthBegin(1)
        left, right = max(start, month), min(end, next_month)
        ranges = missing_ranges(frame, left, right)
        if ranges:
            # Archives avoid thousands of REST requests for historical backfills.
            archive = source.archive(symbol, month, monthly=True) if next_month <= end else None
            chunks = [archive] if archive is not None else []
            if archive is None:
                for day in pd.date_range(left.normalize(), right, freq="1D", inclusive="left"):
                    day_end = min(day + pd.Timedelta(days=1), right)
                    if not missing_ranges(frame, max(left, day), day_end):
                        continue
                    daily = source.archive(symbol, day)
                    if daily is not None:
                        chunks.append(daily)
            if chunks:
                new = pd.concat(chunks, ignore_index=True)
                wanted = pd.DatetimeIndex(inspect(frame, left, right)["missing"])
                new = new[new.open_time.isin(wanted)]
                frame = clean(pd.concat([frame, new], ignore_index=True))
                write(frame, path)
            for gap_start, gap_end in missing_ranges(frame, left, right):
                new = source.rest(symbol, gap_start, gap_end)
                frame = clean(pd.concat([frame, new], ignore_index=True))
                write(frame, path)
            LOG.info("%s checkpoint through %s", symbol, right)
        month = next_month
    report = inspect(frame, start, end)
    write(resample_4h(frame), root / "klines" / symbol / "4h.parquet")
    if report["missing"]:
        raise ValueError(f"{symbol}: {len(report['missing'])} missing candles; rerun to resume")
    return report
