"""Strict offline loader for self-contained v0.9.1 verified evidence bundles."""
import json
from pathlib import Path
import re
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from market_data.core import normalize
from .data import utc
from .intraday_data import digest, validate15, parity
from .integrity_sources import OfficialSources, quality

SOURCE='Binance USDT-M official archives and audited public REST v0.9.1'


def inside(root,relative):
    p=(root/relative).resolve()
    if root.resolve() not in p.parents:raise ValueError('검증 증거 경로 이탈')
    return p


def decode_artifact(root,item,symbol):
    path=inside(root,item['file'])
    if digest(path)!=item['sha256']:raise ValueError('복구 증거 SHA256 불일치')
    url=item['url'];p=urlparse(url)
    if p.scheme!='https' or p.username or p.password or p.port or p.query or p.fragment:raise ValueError('복구 출처 URL 오류')
    if p.hostname=='data.binance.vision':
        pattern=rf'/data/futures/um/(monthly|daily)/klines/{symbol}/(15m|1h)/{symbol}-\2-\d{{4}}-\d{{2}}(?:-\d{{2}})?\.zip'
        match=re.fullmatch(pattern,p.path)
        if not match or item.get('official_checksum')!=item['sha256']:raise ValueError('공식 ZIP 출처/checksum 증거 오류')
        interval=match.group(2);frame,_=OfficialSources.parse(path.read_bytes(),interval)
    elif url=='https://fapi.binance.com/fapi/v1/klines':
        params=item['params'];interval=params['interval']
        if params['symbol']!=symbol or interval not in ('15m','1h'):raise ValueError('복구 REST 출처/interval 오류')
        rows=json.loads(path.read_text());frame=normalize(rows);quality(frame,interval)
        left=pd.Timestamp(params['startTime'],unit='ms',tz='UTC');right=pd.Timestamp(params['endTime']+1,unit='ms',tz='UTC')
        if frame.open_time.lt(left).any() or frame.open_time.ge(right).any():raise ValueError('복구 REST 범위 이탈')
    else:raise ValueError('허용되지 않은 복구 출처')
    frame.attrs={}
    return interval,frame


def covered_by(frame,sources):
    cols=['open','high','low','close','volume'];target=frame.set_index('open_time')[cols]
    matched=pd.Series(False,index=target.index)
    for src in sources:
        source=src.set_index('open_time')[cols];common=target.index.intersection(source.index)
        if not len(common):continue
        same=np.ones(len(common),dtype=bool)
        for c in cols:
            atol,rtol=(1e-6,1e-8) if c=='volume' else (1e-8,1e-10)
            same &= np.isclose(target.loc[common,c],source.loc[common,c],atol=atol,rtol=rtol)
        matched.loc[common[same]]=True
    if not matched.all():raise ValueError(f'공식 증거와 일치하지 않는 복구 행 {int((~matched).sum())}개')


def validate_bundle(root,symbol,start,end):
    from .integrity_audit import decide_day,select,concatenate
    root=Path(root);meta=json.loads((root/'integrity.provenance.json').read_text())
    if meta.get('schema_version')!=1 or meta.get('status')!='verified' or meta.get('source')!=SOURCE:raise ValueError('검증 복구 manifest schema/status/source 오류')
    if meta['symbol']!=symbol or utc(meta['start'])!=start or utc(meta['end_exclusive'])!=end:raise ValueError('검증 복구 종목/기간 불일치')
    record_path=inside(root,meta['audit_record'])
    if digest(record_path)!=meta['audit_record_sha256']:raise ValueError('복구 감사 기록 hash 불일치')
    record=json.loads(record_path.read_text())
    if record.get('status')!='verified' or record.get('unresolved') or record.get('final_missing_15m',{}).get('count')!=0 or record.get('final_missing_reference_1h',{}).get('count')!=0 or record.get('final_parity',{}).get('mismatches'):
        raise ValueError('미해결/부분 감사 기록으로 연구 불가')
    if record['symbol']!=symbol or utc(record['start'])!=start or utc(record['end_exclusive'])!=end:raise ValueError('감사 기록 기간/종목 불일치')
    for name,key in [('15m.parquet','parquet_sha256'),('1h.parquet','hourly_sha256'),('original_1h.parquet','original_hourly_sha256')]:
        if digest(root/name)!=meta[key]:raise ValueError(f'복구 {name} SHA256 불일치')
    if meta['original_hourly_sha256']!=record['local_1h']['sha256']:raise ValueError('원본 보존 hash 기록 불일치')
    candidate=validate15(pd.read_parquet(root/'15m.parquet'),start,end)
    reference=pd.read_parquet(root/'1h.parquet');quality(reference,'1h')
    original=pd.read_parquet(root/'original_1h.parquet');quality(original,'1h')
    if meta['rows']!=len(candidate):raise ValueError('복구 행 수 불일치')
    artifacts=[]
    for item in meta['artifacts']:
        interval,frame=decode_artifact(root,item,symbol);artifacts.append((item,interval,frame))
    covered_by(candidate,[f for _,interval,f in artifacts if interval=='15m'])
    covered_by(reference,[original,*[f for _,interval,f in artifacts if interval=='1h']])
    required_days={utc(t).normalize().isoformat() for t in record.get('initial_missing_15m',{}).get('timestamps',[])}
    required_days.update(utc(t).normalize().isoformat() for t in record.get('initial_missing_reference_1h',{}).get('timestamps',[]))
    required_days.update(utc(d['time']).normalize().isoformat() for d in record.get('initial_local_parity',{}).get('mismatches',[]))
    if not required_days<=set(record['source_days']):raise ValueError('필수 복구 날짜 판정 기록 누락')
    for day,d in record['source_days'].items():
        if not d['recovery_verified']:raise ValueError('검증되지 않은 복구 날짜')
        a,b=utc(d['start']),utc(d['end_exclusive'])
        r15=concatenate([select(f,a,b) for item,interval,f in artifacts if 'fapi.binance.com' in item['url'] and interval=='15m'])
        r1=concatenate([select(f,a,b) for item,interval,f in artifacts if 'fapi.binance.com' in item['url'] and interval=='1h'])
        daily15=next((select(f,a,b) for item,interval,f in artifacts if '/daily/' in item['url'] and interval=='15m' and len(select(f,a,b))),None)
        hours=[select(f,a,b) for item,interval,f in artifacts if 'data.binance.vision' in item['url'] and interval=='1h' and len(select(f,a,b))]
        okay,reason=decide_day(r15,r1,daily15,hours,a,b)
        if not okay:raise ValueError(f'복구 날짜 독립 재검증 실패: {reason}')
    checked=parity(candidate,reference,start,end)
    meta['parity']=checked
    return candidate,meta
