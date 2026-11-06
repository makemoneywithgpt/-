"""Official USD-M 15m archives only. Simulation modules never use network."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

import requests
import numpy as np
import pandas as pd

from market_data.binance import BinancePublic
from market_data.core import COLUMNS, normalize
from .data import utc

STEP=pd.Timedelta(minutes=15)
DEFAULT_START='2023-01-01T00:00:00Z'
DEFAULT_END='2026-11-04T13:00:00Z'


def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def atomic_bytes(path,content):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as stream:
        stream.write(content);temporary=stream.name
    try:os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def validate15(frame,start,end,now=None):
    start,end=utc(start),utc(end)
    if start>=end or start!=start.floor('15min') or end!=end.floor('15min'):
        raise ValueError('15m 시작/종료 경계 오류')
    cutoff=utc(now if now is not None else pd.Timestamp.now(tz='UTC')).floor('15min')
    if end>cutoff:raise ValueError('미완성 15m 봉 요청')
    required=['open_time','open','high','low','close','volume']
    if not set(required)<=set(frame):raise ValueError('15m 필수 열 누락')
    f=frame.copy();f['open_time']=pd.to_datetime(f.open_time,utc=True,errors='raise')
    if f.open_time.isna().any():raise ValueError('15m 빈 시각')
    # Do not silently sort/deduplicate or discard out-of-window data here.
    if f.open_time.duplicated().any() or not f.open_time.is_monotonic_increasing:
        raise ValueError('15m 중복/역순 시각')
    if (f.open_time!=f.open_time.dt.floor('15min')).any():raise ValueError('15m 정시 경계 위반')
    expected=pd.date_range(start,end,freq='15min',inclusive='left')
    if not pd.DatetimeIndex(f.open_time).equals(expected):
        missing=expected.difference(pd.DatetimeIndex(f.open_time));extra=pd.DatetimeIndex(f.open_time).difference(expected)
        raise ValueError(f'15m 누락 {len(missing)}개/범위밖 {len(extra)}개; 최초 누락 {list(missing[:8])}')
    numeric=[c for c in f if c!='open_time']
    for c in numeric:f[c]=pd.to_numeric(f[c],errors='raise')
    bad=(~np.isfinite(f[numeric]).all(axis=1) | f[numeric].lt(0).any(axis=1)
         | f[['open','high','low','close']].le(0).any(axis=1)
         | f.high.lt(f[['open','low','close']].max(axis=1))
         | f.low.gt(f[['open','high','close']].min(axis=1)))
    if 'trades' in f:bad|=f.trades.mod(1).ne(0)
    for a,b in [('taker_buy_volume','volume'),('taker_buy_quote_volume','quote_volume')]:
        if a in f and b in f:bad|=f[a]>f[b]
    if bad.any():raise ValueError(f'15m 비정상 캔들: 행 {int(np.flatnonzero(bad)[0])+2}')
    return f


def aggregate_hourly(f):
    groups=f.groupby(f.open_time.dt.floor('h'),sort=True)
    hour=groups.agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'))
    if not groups.size().eq(4).all():raise ValueError('1h 합성에 불완전 15m 버킷')
    return hour.reset_index()


def parity(f,hourly,start,end):
    from .data import validate
    reconstructed=aggregate_hourly(f)
    old=validate(hourly,start,end)
    if not pd.DatetimeIndex(reconstructed.open_time).equals(pd.DatetimeIndex(old.open_time)):
        raise ValueError('1h parity: 시간/개수 불일치')
    for c in ['open','high','low','close','volume']:
        tolerance=1e-6 if c=='volume' else 1e-8
        bad=~np.isclose(reconstructed[c],old[c],rtol=1e-8 if c=='volume' else 1e-10,atol=tolerance)
        if bad.any():
            i=int(np.flatnonzero(bad)[0]);raise ValueError(f'1h parity: {c} 불일치 UTC {old.open_time.iloc[i]}; USD-M/현물 소스 혼용 가능성 확인')
    return dict(status='passed',hours=len(old),price_atol=1e-8,price_rtol=1e-10,volume_atol=1e-6,volume_rtol=1e-8)


class ArchiveGap(ValueError):
    pass


class Archives:
    def __init__(self,cache,client=None):
        self.cache=Path(cache);self.client=client or BinancePublic()

    def fetch(self,symbol,date,monthly):
        stamp=date.strftime('%Y-%m' if monthly else '%Y-%m-%d');period='monthly' if monthly else 'daily'
        name=f'{symbol}-15m-{stamp}.zip'
        url=f'https://data.binance.vision/data/futures/um/{period}/klines/{symbol}/15m/{name}'
        path=self.cache/symbol/period/name;meta=path.with_suffix('.json')
        if self.cache.resolve() not in path.resolve().parents:raise ValueError('캐시 심볼 경로가 cache-dir 밖으로 연결됨')
        if path.exists():
            if not meta.exists():raise ValueError(f'미검증 캐시 (metadata 없음): {path}')
            info=json.loads(meta.read_text())
            if info['url']!=url or digest(path)!=info['sha256']:raise ValueError(f'캐시 무결성 오류: {path}')
            content=path.read_bytes()
        else:
            response=self.client.get(url,optional=True)
            if response is None:return None,None
            content=response.content;sha=hashlib.sha256(content).hexdigest()
            check=self.client.get(url+'.CHECKSUM',optional=True)
            expected=check.text.split()[0] if check is not None else None
            if expected is not None and expected.lower()!=sha:raise ValueError(f'SHA256 checksum mismatch: {url}')
            info=dict(url=url,sha256=sha,official_checksum=expected,checksum_available=check is not None)
            # Validate ZIP/CSV before trusting cache. No unverified partial ZIP.
            self.parse(content)
            atomic_bytes(path,content);atomic_bytes(meta,(json.dumps(info,indent=2)+'\n').encode())
        return self.parse(content),info

    @staticmethod
    def parse(content):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names=[n for n in archive.namelist() if n.endswith('.csv')]
            if len(names)!=1:raise ValueError('ZIP CSV 스키마 오류')
            raw=pd.read_csv(archive.open(names[0]),header=None,dtype=str)
        if len(raw.columns)!=len(COLUMNS) or raw.empty:raise ValueError('15m CSV 스키마 오류')
        if raw.iloc[0,0]=='open_time':raw=raw.iloc[1:]
        if raw.empty:raise ValueError('15m CSV 데이터 없음')
        times=pd.to_numeric(raw.iloc[:,0],errors='raise')
        if (times%900000!=0).any() or (times<1e12).any() or (times>1e14).any():
            raise ValueError('15m timestamp는 UTC millisecond 경계여야 합니다.')
        result=normalize(raw.values.tolist())
        # Archive internal duplicate/order/values must be valid before slicing.
        try:return validate15(result,result.open_time.iloc[0],result.open_time.iloc[-1]+STEP)
        except ValueError as exc:
            if '15m 누락' in str(exc):raise ArchiveGap(str(exc)) from exc
            raise


def collect(symbol,start,end,data_dir,cache,archive=None):
    start,end=utc(start),utc(end);root=Path(data_dir).resolve()
    path=Path(data_dir)/'klines'/symbol/'15m.parquet';meta=path.with_name('15m.provenance.json')
    if root not in path.resolve().parents:raise ValueError('15m 저장 경로가 data-dir 밖으로 연결됨')
    hourly_path=path.with_name('1h.parquet')
    if path.exists() or meta.exists():
        if not path.exists() or not meta.exists():raise ValueError('15m 부분 게시 상태: 원본 보존, 캐시에서 복구 전 별도 확인 필요')
        info=json.loads(meta.read_text())
        if info['start']!=start.isoformat() or info['end_exclusive']!=end.isoformat() or info['symbol']!=symbol:
            raise ValueError('기존 15m 기간/종목 불일치: 덮어쓰기 금지')
        if digest(path)!=info['parquet_sha256']:raise ValueError('15m 저장 파일 SHA256 오류')
        f=validate15(pd.read_parquet(path),start,end)
        parity(f,pd.read_parquet(hourly_path),start,end)
        return info
    source=archive or Archives(cache);chunks=[];sources=[];missing=[]
    month=start.normalize().replace(day=1)
    while month<end:
        following=month+pd.offsets.MonthBegin(1);left=max(start,month);right=min(end,following)
        try:frame,record=source.fetch(symbol,month,True) if following<=end else (None,None)
        except ArchiveGap:
            print(f'{symbol}: 월 ZIP 내부 누락 검출, 공식 일별 아카이브로 재검증 {month}',flush=True)
            frame,record=None,None
        if frame is not None:
            chunks.append(frame.loc[(frame.open_time>=left)&(frame.open_time<right)])
            sources.append(record)
        else:
            for day in pd.date_range(left.normalize(),right,freq='D',inclusive='left'):
                f,rec=source.fetch(symbol,day,False)
                if f is None:missing.append(day.isoformat());continue
                chunks.append(f.loc[(f.open_time>=left)&(f.open_time<right)])
                sources.append(rec)
        print(f'{symbol}: 아카이브 체크포인트 {right.isoformat()}',flush=True)
        month=following
    if missing:raise ValueError(f'공식 15m 아카이브 미게시/없음 {missing}. REST 보충 없음, 다운로드 캐시는 보존됨.')
    if not chunks:raise ValueError('15m 다운로드 데이터 없음')
    f=validate15(pd.concat(chunks,ignore_index=True),start,end)
    checked=parity(f,pd.read_parquet(hourly_path),start,end)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,suffix='.parquet',delete=False) as tmp:temporary=Path(tmp.name)
    try:
        f.to_parquet(temporary,index=False)
        info=dict(symbol=symbol,start=start.isoformat(),end_exclusive=end.isoformat(),rows=len(f),
                  source='Binance public data futures/um 15m',sources=sources,parity=checked,parquet_sha256=digest(temporary))
        # Publish only complete validated data. Interrupted pairs are refused above.
        atomic_bytes(meta,(json.dumps(info,indent=2)+'\n').encode());os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)
    return info


def window(symbol,v08_dir,start=None,end=None):
    path=Path(v08_dir)/symbol/'timeframe.json'
    info=json.loads(path.read_text()) if path.exists() else {'start':DEFAULT_START,'end_exclusive':DEFAULT_END}
    a,b=utc(start or info['start']),utc(end or info['end_exclusive'])
    if a!=a.floor('h') or b!=b.floor('h') or a>=b:raise ValueError('1h parity를 위해 시작<종료 정시 UTC 필요')
    if b>pd.Timestamp.now(tz='UTC').floor('15min'):raise ValueError('미완성 미래 기간 요청')
    return a,b


def main(argv=None):
    parser=argparse.ArgumentParser(description='공식 USDT-M 15m 다운로드 및 1h parity (주문/API 키 없음)')
    parser.add_argument('--symbols',nargs='+',choices=['BTCUSDT','ETHUSDT'],default=['BTCUSDT','ETHUSDT'])
    for flag,default in [('data-dir','data'),('cache-dir','data/intraday_downloads_v09'),('v08-dir','data/timeframe_v08')]:
        parser.add_argument('--'+flag,type=Path,default=Path(default))
    parser.add_argument('--start');parser.add_argument('--end');args=parser.parse_args(argv);failed=False
    protected=[Path('.venv').resolve(),args.v08_dir.resolve()]+[(args.data_dir/k).resolve() for k in ['klines','backtests','diagnostics','regimes','experiments','strategy_lab_v05','root_cause_v06','exit_policy_v07']]
    cache=args.cache_dir.resolve()
    venv=Path('.venv').resolve()
    if args.data_dir.resolve()==venv or venv in args.data_dir.resolve().parents:
        print('data-dir이 .venv와 겹침',file=sys.stderr);return 1
    if any(cache==p or p in cache.parents or cache in p.parents for p in protected):
        print('캐시가 기존 데이터/결과/.venv와 겹침',file=sys.stderr);return 1
    for symbol in dict.fromkeys(args.symbols):
        try:
            start,end=window(symbol,args.v08_dir,args.start,args.end)
            info=collect(symbol,start,end,args.data_dir,args.cache_dir)
            print(f'{symbol}: 15m {info["rows"]}개 저장/parity 통과')
        except (OSError,ValueError,KeyError,zipfile.BadZipFile,requests.RequestException) as exc:
            # requests errors are also reported, never interpreted as completion.
            failed=True;print(f'{symbol}: 다운로드/검증 실패 — {exc}',file=sys.stderr)
    return int(failed)


if __name__=='__main__':raise SystemExit(main())
