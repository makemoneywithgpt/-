import hashlib
import io
import zipfile

import pandas as pd
import pytest
import requests

from market_data.binance import BinancePublic
from market_data.collector import collect
from market_data.core import clean, inspect, missing_ranges, normalize, read, resample_4h, utc, write


def candles(n=8):
    return normalize([[int((utc('2023-01-01') + pd.Timedelta(hours=i)).timestamp()*1000),
                       100+i, 110+i, 90+i, 105+i, 10, 0, 1000, 5, 4, 400, 0] for i in range(n)])


def test_normalize_utc():
    f = candles()
    assert str(f.open_time.dtype) == 'datetime64[ns, UTC]'
    assert f.quote_volume.sum() == 8000
    assert utc('2023-01-01T09:00:00+09:00') == f.open_time.iloc[0]


def test_validation_gaps_duplicates_and_values():
    f = candles().drop(index=2)
    f = pd.concat([f, f.iloc[:1]], ignore_index=True)
    f.loc[0, 'high'] = 1
    r = inspect(f, '2023-01-01', '2023-01-01T08:00Z')
    assert r['duplicates'] == 1 and r['invalid_rows'] == 1
    assert r['missing'] == [utc('2023-01-01T02:00Z')]
    assert missing_ranges(f, '2023-01-01', '2023-01-01T08:00Z') == [(utc('2023-01-01T02:00Z'), utc('2023-01-01T03:00Z'))]


@pytest.mark.parametrize('field,value', [('volume', -1), ('close', float('nan')), ('quote_volume', float('inf')), ('trades', 1.5)])
def test_reject_invalid(field, value):
    f = candles()
    f[field] = f[field].astype(float)
    f.loc[0, field] = value
    with pytest.raises(ValueError):
        clean(f)


def test_resample_complete_only():
    f = candles(7)
    result = resample_4h(f)
    assert len(result) == 1
    assert result.iloc[0]['open'] == 100
    assert result.iloc[0]['close'] == 108
    assert result.iloc[0]['high'] == 113
    assert result.iloc[0]['low'] == 90
    assert result.iloc[0]['volume'] == 40
    assert result.iloc[0]['quote_volume'] == 4000
    assert resample_4h(f.drop(index=2)).empty


def test_parquet(tmp_path):
    path = tmp_path/'test.parquet'
    write(candles(), path)
    pd.testing.assert_frame_equal(read(path), candles())


class Source:
    def __init__(self):
        self.calls = []

    def archive(self, *args, **kwargs):
        return None

    def rest(self, symbol, start, end):
        self.calls.append((start, end))
        f = candles()
        return f[(f.open_time >= start) & (f.open_time < end)]


def test_resume_only_missing(tmp_path):
    path = tmp_path/'klines/BTCUSDT/1h.parquet'
    write(candles().drop(index=[2, 3, 6]), path)
    source = Source()
    collect(source, 'BTCUSDT', '2023-01-01', '2023-01-01T08:00Z', tmp_path)
    assert source.calls == [(utc('2023-01-01T02:00Z'), utc('2023-01-01T04:00Z')),
                            (utc('2023-01-01T06:00Z'), utc('2023-01-01T07:00Z'))]
    source.calls.clear()
    collect(source, 'BTCUSDT', '2023-01-01', '2023-01-01T08:00Z', tmp_path)
    assert source.calls == []
    assert len(read(path.with_name('4h.parquet'))) == 2


class Response:
    def __init__(self, status=200, content=b'', payload=None, headers=None):
        self.status_code, self.content, self.payload = status, content, payload
        self.headers = headers or {}
        self.text = content.decode(errors='replace')

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, *args, **kwargs):
        r = next(self.responses)
        if isinstance(r, Exception):
            raise r
        return r


def test_retry_and_terminal_error():
    delays = []
    client = BinancePublic(session=Session([requests.Timeout(), Response(429), Response()]), sleep=delays.append)
    assert client.get('https://example.test').status_code == 200
    assert delays == [1, 2]
    with pytest.raises(requests.HTTPError):
        BinancePublic(session=Session([Response(451)]), sleep=lambda _: None).get('x')


def test_archive_checksum_and_header():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('test.csv', 'open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n1672531200000,100,110,90,105,10,0,1000,5,4,400,0\n')
    content = buf.getvalue()
    digest = hashlib.sha256(content).hexdigest().encode()
    client = BinancePublic(session=Session([Response(content=content), Response(content=digest)]))
    assert len(client.archive('BTCUSDT', utc('2023-01-01'))) == 1
    client = BinancePublic(session=Session([Response(content=content), Response(content=b'bad')]))
    with pytest.raises(ValueError, match='checksum'):
        client.archive('BTCUSDT', utc('2023-01-01'))


def test_rest_pagination():
    row = [1672531200000,100,110,90,105,10,0,1000,5,4,400,0]
    row2 = [1672534800000,100,110,90,105,10,0,1000,5,4,400,0]
    client = BinancePublic(session=Session([Response(payload=[row]), Response(payload=[row2])]), sleep=lambda _:None)
    assert len(client.rest('BTCUSDT','2023-01-01','2023-01-01T02:00Z')) == 2


def test_failed_backfill_preserves_existing(tmp_path):
    path = tmp_path/'klines/BTCUSDT/1h.parquet'
    original = candles().iloc[:2].copy()
    write(original, path)
    class Broken(Source):
        def rest(self, *args):
            raise requests.Timeout('offline')
    with pytest.raises(requests.Timeout):
        collect(Broken(), 'BTCUSDT', '2023-01-01', '2023-01-01T08:00Z', tmp_path)
    pd.testing.assert_frame_equal(read(path), original)


def test_existing_invalid_not_overwritten(tmp_path):
    path = tmp_path/'klines/BTCUSDT/1h.parquet'
    original = candles()
    original.loc[0, 'low'] = -1
    write(original, path)
    with pytest.raises(ValueError):
        collect(Source(), 'BTCUSDT', '2023-01-01', '2023-01-01T08:00Z', tmp_path)
    pd.testing.assert_frame_equal(read(path), original)
