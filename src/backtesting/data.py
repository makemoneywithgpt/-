"""Strict validation of the requested closed-hour window; never fill price gaps."""
from pathlib import Path

import numpy as np
import pandas as pd

HOUR = pd.Timedelta(hours=1)
REQUIRED = ['open_time', 'open', 'high', 'low', 'close', 'volume']


class DataError(ValueError):
    pass


def utc(value):
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise DataError('날짜가 올바르지 않습니다.')
    return timestamp.tz_localize('UTC') if timestamp.tzinfo is None else timestamp.tz_convert('UTC')


def validate(frame, start, end, *, now=None):
    start, end = utc(start), utc(end)
    cutoff = utc(now if now is not None else pd.Timestamp.now(tz='UTC')).floor('h')
    if start != start.floor('h') or end != end.floor('h') or start >= end:
        raise DataError('시작/종료는 정시 UTC이고 시작 < 종료여야 합니다.')
    if end > cutoff:
        raise DataError(f'미완성 봉 요청: 종료는 {cutoff.isoformat()} 이하여야 합니다.')
    absent = set(REQUIRED).difference(frame.columns)
    if absent:
        raise DataError(f'필수 열 없음: {sorted(absent)}')
    f = frame.copy()
    try:
        # Naive datetimes are interpreted as UTC, as in the collector.
        f['open_time'] = pd.to_datetime(f.open_time, utc=True, errors='raise')
    except (ValueError, TypeError) as exc:
        raise DataError(f'시간 열 오류: {exc}') from exc
    if f.open_time.isna().any():
        raise DataError('빈 시간 값이 있습니다.')
    f = f.loc[(f.open_time >= start) & (f.open_time < end)].sort_values('open_time').reset_index(drop=True)
    if f.empty:
        raise DataError('요청 구간 데이터 없음: 파일 경로와 기간을 확인하세요.')
    if f.open_time.duplicated().any():
        raise DataError(f'중복 봉: {int(f.open_time.duplicated().sum())}개')
    if (f.open_time != f.open_time.dt.floor('h')).any():
        raise DataError('정시가 아닌 시간 봉이 있습니다.')
    columns = [c for c in ['open', 'high', 'low', 'close', 'volume', 'quote_volume',
                           'trades', 'taker_buy_volume', 'taker_buy_quote_volume'] if c in f]
    try:
        f[columns] = f[columns].apply(pd.to_numeric, errors='raise')
    except (ValueError, TypeError) as exc:
        raise DataError(f'숫자 열 오류: {exc}') from exc
    invalid = (~np.isfinite(f[columns]).all(axis=1) | (f[columns] < 0).any(axis=1)
               | (f[['open', 'high', 'low', 'close']] <= 0).any(axis=1)
               | (f.high < f[['open', 'close', 'low']].max(axis=1))
               | (f.low > f[['open', 'close', 'high']].min(axis=1)))
    if 'trades' in f:
        invalid |= f.trades % 1 != 0
    for taker, total in [('taker_buy_volume', 'volume'), ('taker_buy_quote_volume', 'quote_volume')]:
        if taker in f and total in f:
            invalid |= f[taker] > f[total]
    if invalid.any():
        raise DataError(f'비정상 캔들 {int(invalid.sum())}개: {f.loc[invalid, "open_time"].iloc[0].isoformat()}부터')
    expected = pd.date_range(start, end, freq='h', inclusive='left')
    missing = expected.difference(pd.DatetimeIndex(f.open_time))
    if len(missing):
        ranges = []
        left = previous = missing[0]
        for t in missing[1:]:
            if t != previous + HOUR:
                ranges.append(f'[{left.isoformat()}, {(previous+HOUR).isoformat()})')
                left = t
            previous = t
        ranges.append(f'[{left.isoformat()}, {(previous+HOUR).isoformat()})')
        raise DataError(f'누락 봉 {len(missing)}개 (구간 끝 제외): ' + ', '.join(ranges))
    return f


def load(path: Path, start, end, *, now=None):
    if not path.is_file():
        raise DataError(f'데이터 파일 없음: {path}. Windows PC의 Parquet 경로를 확인하세요.')
    return validate(pd.read_parquet(path), start, end, now=now)
