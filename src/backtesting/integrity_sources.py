"""v0.9.1 read-only official archive/REST evidence, never trading endpoints."""
import hashlib
import io
import json
from pathlib import Path
import time
import zipfile

import numpy as np
import pandas as pd
import requests

from market_data.core import COLUMNS, normalize
from .data import utc
from .intraday_data import digest, atomic_bytes


class OfficialSources:
    def __init__(self,cache,legacy_cache='data/intraday_downloads_v09',session=None):
        self.cache=Path(cache);self.legacy=Path(legacy_cache);self.session=session or requests.Session();self.evidence=[];self.revalidate_cache=False;self.checked_remote=set()

    def request(self,url,params=None):
        item=dict(url=url,params=params,checked_at=pd.Timestamp.now(tz='UTC').isoformat(),cached=False)
        self.evidence.append(item)
        for attempt in range(3):
            try:
                r=self.session.get(url,params=params,timeout=(10,60));item['http_status']=r.status_code
                headers=getattr(r,'headers',{})
                item['public_headers']={k:headers[k] for k in ['Last-Modified','ETag','Content-Length'] if k in headers}
                if r.status_code in (429,500,502,503,504) and attempt<2:time.sleep(2**attempt);continue
                if r.status_code!=200:
                    item['error_body']=r.text[:600];return None,item
                return r,item
            except requests.RequestException as exc:
                item['network_error']=type(exc).__name__
                if attempt<2:time.sleep(2**attempt)
        return None,item

    @staticmethod
    def parse(content,interval):
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names=[n for n in z.namelist() if n.endswith('.csv')]
            if len(names)!=1:raise ValueError('ZIP 内 CSV 수 오류')
            raw=pd.read_csv(z.open(names[0]),header=None,dtype=str)
        if raw.empty or len(raw.columns)!=len(COLUMNS):raise ValueError('공식 CSV schema 오류')
        if raw.iloc[0,0]=='open_time':raw=raw.iloc[1:]
        if raw.empty:raise ValueError('공식 CSV 데이터 없음')
        values=pd.to_numeric(raw.iloc[:,0],errors='raise');millis=900000 if interval=='15m' else 3600000
        if (values%millis!=0).any() or (values<1e12).any() or (values>1e14).any():raise ValueError('ms/시간 경계 오류')
        f=normalize(raw.values.tolist());quality(f,interval)
        lexical=raw.copy();lexical.columns=COLUMNS;lexical['open_time']=f.open_time.to_numpy()
        f.attrs['raw_decimal']=lexical.astype(str).to_dict('records')
        return f,names

    def archive(self,symbol,date,interval,monthly):
        period='monthly' if monthly else 'daily';stamp=date.strftime('%Y-%m' if monthly else '%Y-%m-%d')
        name=f'{symbol}-{interval}-{stamp}.zip';url=f'https://data.binance.vision/data/futures/um/{period}/klines/{symbol}/{interval}/{name}'
        new=self.cache/symbol/period/name;legacy=self.legacy/symbol/period/name
        roots=[self.cache.resolve(),self.legacy.resolve()]
        for path,root in [(new,roots[0]),(legacy,roots[1])]:
            if root not in path.resolve().parents:raise ValueError('캐시 symlink 경로 이탈')
        path=next((p for p in [new,legacy] if p.exists()),None)
        if path is not None:
            meta=path.with_suffix('.json')
            if not meta.is_file():raise ValueError(f'캐시 증거 없음: {path}')
            info=json.loads(meta.read_text());sha=digest(path)
            if info['url']!=url or info['sha256']!=sha:raise ValueError(f'캐시 hash/URL 오류: {path}')
            expected=info.get('official_checksum')
            if expected is not None and expected.lower()!=sha:raise ValueError('캐시 official checksum 오류')
            content=path.read_bytes();item=dict(url=url,cached=True,http_status=None,checked_at=pd.Timestamp.now(tz='UTC').isoformat(),
                original_fetch_time=info.get('checked_at','unknown (legacy metadata)'),sha256=sha,
                checksum_verified=expected is not None,cache_path=str(path))
            self.evidence.append(item)
            if self.revalidate_cache and url not in self.checked_remote:
                current,checked=self.request(url+'.CHECKSUM')
                self.checked_remote.add(url)
                item['current_checksum_http_status']=checked.get('http_status')
                if current is not None:
                    current_sha=current.text.split()[0].lower();item['current_official_checksum']=current_sha
                    if current_sha!=sha:raise ValueError(f'원격 공식 checksum 변경 감지: {url}; 기존 캐시 보존, 새 캐시 경로로 별도 재조사 필요')
        else:
            response,item=self.request(url)
            if response is None:return None
            content=response.content;sha=hashlib.sha256(content).hexdigest()
            check,check_item=self.request(url+'.CHECKSUM')
            if check is None:
                item['checksum_verified']=False
                raise ValueError(f'교차 검증용 공식 checksum 확인 불가: {url} (HTTP {check_item.get("http_status")})')
            expected=check.text.split()[0].lower()
            if expected!=sha:raise ValueError(f'checksum mismatch: {url}')
            item.update(sha256=sha,checksum_verified=True)
            # Structural validation may find coverage gaps; these remain evidence,
            # never a certification of a complete data set.
            self.parse(content,interval)
            atomic_bytes(new,content)
            atomic_bytes(new.with_suffix('.json'),(json.dumps(dict(url=url,sha256=sha,official_checksum=expected,
                checked_at=item['checked_at']),indent=2)+'\n').encode())
        f,names=self.parse(content,interval)
        item['cache_path']=str(path if path is not None else new)
        left=date.normalize().replace(day=1) if monthly else date.normalize()
        right=left+pd.offsets.MonthBegin(1) if monthly else left+pd.Timedelta(days=1)
        step=pd.Timedelta(minutes=15) if interval=='15m' else pd.Timedelta(hours=1)
        missing=pd.date_range(left,right,freq=step,inclusive='left').difference(pd.DatetimeIndex(f.open_time))
        item.update(rows=len(f),zip_csv_files=names,missing_count=len(missing),coverage_complete=len(missing)==0,
                    missing_times=[t.isoformat() for t in missing])
        return f

    def rest(self,symbol,start,end,interval):
        start,end=utc(start),utc(end);step=pd.Timedelta(minutes=15) if interval=='15m' else pd.Timedelta(hours=1)
        rows=[];cursor=start
        while cursor<end:
            url='https://fapi.binance.com/fapi/v1/klines'
            params=dict(symbol=symbol,interval=interval,startTime=int(cursor.timestamp()*1000),endTime=int(end.timestamp()*1000)-1,limit=1500)
            r,item=self.request(url,params)
            if r is None:return None
            payload=r.json()
            if not isinstance(payload,list) or not payload:raise ValueError('REST 누락/응답 schema 오류')
            f=normalize(payload);quality(f,interval)
            if (f.open_time<cursor).any() or (f.open_time>=end).any():raise ValueError('REST 범위 이탈')
            next_cursor=f.open_time.iloc[-1]+step
            if next_cursor<=cursor:raise ValueError('REST 페이지 전진 오류')
            response_path=self.cache/'REST'/symbol/interval/f'{params["startTime"]}-{params["endTime"]}.json'
            if self.cache.resolve() not in response_path.resolve().parents:raise ValueError('REST 증거 경로 이탈')
            atomic_bytes(response_path,r.content)
            item.update(rows=len(f),response_sha256=hashlib.sha256(r.content).hexdigest(),response_path=str(response_path))
            rows.append(f);cursor=next_cursor
        f=pd.concat(rows,ignore_index=True);quality(f,interval)
        expected=pd.date_range(start,end,freq=step,inclusive='left')
        if not pd.DatetimeIndex(f.open_time).equals(expected):raise ValueError(f'REST {interval} 누락/중복: {start} ~ {end}')
        return f


def quality(f,interval):
    step='15min' if interval=='15m' else 'h'
    if f.empty or f.open_time.isna().any() or f.open_time.duplicated().any() or not f.open_time.is_monotonic_increasing:
        raise ValueError('출처 빈 행/중복/역순/NaT')
    if f.open_time.ne(f.open_time.dt.floor(step)).any():raise ValueError('출처 시각 경계 위반')
    numbers=f.drop(columns='open_time')
    bad=(~np.isfinite(numbers).all(axis=1)|numbers.lt(0).any(axis=1)|f[['open','high','low','close']].le(0).any(axis=1)
         |f.high.lt(f[['open','close','low']].max(axis=1))|f.low.gt(f[['open','close','high']].min(axis=1)))
    if 'trades' in f:bad|=f.trades.mod(1).ne(0)
    for a,b in [('taker_buy_volume','volume'),('taker_buy_quote_volume','quote_volume')]:
        if a in f and b in f:bad|=f[a]>f[b]
    if bad.any():raise ValueError('출처 비정상 가격/거래량/숫자')
