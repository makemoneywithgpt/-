"""Official-source fixtures only; no live network/data or strategy returns."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pandas as pd
import pytest

from market_data.core import COLUMNS
from backtesting.intraday_data import aggregate_hourly,STEP,digest
from backtesting.integrity_sources import OfficialSources
from backtesting.integrity_audit import investigate,compare,decide_day,publish_verified,main

START=pd.Timestamp('2023-01-01',tz='UTC');END=START+pd.Timedelta(days=1)


def bars():
    return pd.DataFrame(dict(open_time=pd.date_range(START,periods=96,freq='15min'),open=100.,high=101.,low=99.,close=100.,volume=10.,
                            quote_volume=1000.,trades=5.,taker_buy_volume=4.,taker_buy_quote_volume=400.))


def zip_bytes(f):
    rows=[]
    for b in f.itertuples(index=False):
        rows.append([int(b.open_time.timestamp()*1000),b.open,b.high,b.low,b.close,b.volume,0,getattr(b,'quote_volume',1000),int(getattr(b,'trades',5)),getattr(b,'taker_buy_volume',4),getattr(b,'taker_buy_quote_volume',400),0])
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:z.writestr('data.csv',','.join(COLUMNS)+'\n'+'\n'.join(','.join(map(str,r)) for r in rows))
    return b.getvalue()


class Response:
    def __init__(self,status=200,content=b'',payload=None):
        self.status_code=status;self.content=content;self.text=content.decode(errors='replace');self.payload=payload
    def json(self):return self.payload


class Session:
    def __init__(self,items):self.items=iter(items);self.calls=0
    def get(self,*a,**kw):self.calls+=1;return next(self.items)


def test_archive_404_gap_checksum_and_repeat_cache(tmp_path):
    data=zip_bytes(bars().drop(index=[4,5]));sha=hashlib.sha256(data).hexdigest()
    session=Session([Response(404),Response(content=data),Response(content=sha.encode())])
    s=OfficialSources(tmp_path/'cache',tmp_path/'legacy',session)
    assert s.archive('BTCUSDT',START,'15m',False) is None
    f=s.archive('BTCUSDT',START,'15m',False)
    assert len(f)==94 and s.evidence[-2]['missing_count']==2 and not s.evidence[-2]['coverage_complete']
    again=s.archive('BTCUSDT',START,'15m',False)
    assert len(again)==94 and session.calls==3
    assert s.evidence[-1]['cached'] and s.evidence[-1]['http_status'] is None
    assert s.evidence[-1]['checksum_verified']
    p=next((tmp_path/'cache').rglob('*.zip'));p.write_bytes(b'wrong')
    with pytest.raises(ValueError,match='hash'):s.archive('BTCUSDT',START,'15m',False)


def test_checksum_fail_never_certified(tmp_path):
    data=zip_bytes(bars());s=OfficialSources(tmp_path/'cache',tmp_path/'legacy',Session([Response(content=data),Response(content=b'bad')]))
    with pytest.raises(ValueError,match='checksum mismatch'):s.archive('BTCUSDT',START,'15m',False)
    assert not list(tmp_path.rglob('*.zip'))


def rest_rows(f):
    return [[int(b.open_time.timestamp()*1000),b.open,b.high,b.low,b.close,b.volume,0,1000,5,4,400,0] for b in f.itertuples(index=False)]


def test_rest_gap_fill_is_validated_and_evidenced(tmp_path):
    f=bars();s=OfficialSources(tmp_path/'cache',tmp_path/'legacy',Session([Response(content=b'payload',payload=rest_rows(f))]))
    r=s.rest('BTCUSDT',START,END,'15m')
    assert len(r)==96 and s.evidence[0]['params']['interval']=='15m'
    assert s.evidence[0]['response_sha256']==hashlib.sha256(b'payload').hexdigest()
    assert Path(s.evidence[0]['response_path']).exists()
    s=OfficialSources(tmp_path/'cache2',tmp_path/'legacy',Session([Response(451,b'location restricted')]))
    assert s.rest('BTCUSDT',START,END,'15m') is None
    assert s.evidence[0]['http_status']==451
    missing=OfficialSources(tmp_path/'cache3',tmp_path/'legacy',Session([Response(payload=rest_rows(f.drop(index=4)))]))
    with pytest.raises(ValueError,match='누락'):missing.rest('BTCUSDT',START,END,'15m')


def test_strict_recovery_decisions_and_disagreement():
    f=bars();h=aggregate_hourly(f)
    assert decide_day(f,h,None,[h],START,END)[0]
    assert not decide_day(f,None,f,[h],START,END)[0]
    changed=h.copy();changed.loc[0,'high']+=.1
    assert not decide_day(f,h,f,[changed],START,END)[0]
    assert not decide_day(f,changed,f,[h],START,END)[0]
    changed15=f.copy();changed15.loc[0,'high']+=.1
    assert not decide_day(f,h,changed15,[h],START,END)[0]


class Sources:
    def __init__(self,missing_day=False,rest=True,conflict=False,proof_root=None):
        self.f=bars();self.h=aggregate_hourly(self.f);self.missing_day=missing_day;self.rest_ok=rest;self.conflict=conflict
        self.evidence=[];self.proof_root=Path(proof_root) if proof_root is not None else None
    def proof(self,frame,interval,date,monthly=None):
        if self.proof_root is None:return
        self.proof_root.mkdir(parents=True,exist_ok=True)
        if monthly is None:
            path=self.proof_root/f'{interval}-REST.json';content=json.dumps(rest_rows(frame)).encode();url='https://fapi.binance.com/fapi/v1/klines'
            path.write_bytes(content);self.evidence.append(dict(url=url,http_status=200,response_path=str(path),response_sha256=hashlib.sha256(content).hexdigest(),params=dict(symbol='BTCUSDT',interval=interval,startTime=int(START.timestamp()*1000),endTime=int(END.timestamp()*1000)-1)))
        else:
            stamp=date.strftime('%Y-%m' if monthly else '%Y-%m-%d');period='monthly' if monthly else 'daily'
            name=f'BTCUSDT-{interval}-{stamp}.zip';path=self.proof_root/name;content=zip_bytes(frame);path.write_bytes(content)
            self.evidence.append(dict(url=f'https://data.binance.vision/data/futures/um/{period}/klines/BTCUSDT/{interval}/{name}',cache_path=str(path),sha256=hashlib.sha256(content).hexdigest(),checksum_verified=True))
    def archive(self,symbol,date,interval,monthly):
        self.evidence.append(dict(url=f'synthetic/{interval}/{monthly}',http_status=404 if self.missing_day and interval=='15m' else 200,
                                  checksum_verified=True,checked_at=START.isoformat()))
        if interval=='15m':
            if self.missing_day:return None
            self.proof(self.f,interval,date,monthly);return self.f.copy()
        h=self.h.copy()
        if self.conflict:h.loc[0,'high']+=1
        self.proof(h,interval,date,monthly)
        return h
    def rest(self,symbol,start,end,interval):
        self.evidence.append(dict(url='synthetic REST',http_status=200 if self.rest_ok else 451))
        if not self.rest_ok:return None
        frame=self.f.copy() if interval=='15m' else self.h.copy()
        self.proof(frame,interval,start)
        return frame


def setup(root,conflict=False):
    p=root/'data/klines/BTCUSDT';p.mkdir(parents=True)
    h=aggregate_hourly(bars())
    if conflict:h.loc[0,'high']+=1
    h.to_parquet(p/'1h.parquet');return p/'1h.parquet'


def test_missing_day_verified_rest_recovery_immutable_and_atomic_repeat(tmp_path):
    original=setup(tmp_path);before=original.read_bytes()
    report=investigate('BTCUSDT',START,END,tmp_path/'data',Sources(missing_day=True,proof_root=tmp_path/'proof'),tmp_path/'audit',tmp_path/'verified')
    assert report['initial_missing_15m']['count']==96
    assert report['final_missing_15m']['count']==0
    assert report['status']=='verified'
    assert original.read_bytes()==before
    p=tmp_path/'verified/klines/BTCUSDT'
    assert (p/'15m.parquet').exists() and (p/'1h.parquet').exists()
    assert (p/'integrity.provenance.json').exists()
    assert not (p/'15m.provenance.json').exists() # no forged archive-only marker
    old=(p/'15m.parquet').read_bytes()
    with pytest.raises(ValueError,match='덮어쓰기'):
        publish_verified(bars(),aggregate_hourly(bars()),report,p)
    assert (p/'15m.parquet').read_bytes()==old


def test_missing_day_rest_denied_hard_fail_no_publication(tmp_path):
    original=setup(tmp_path);before=digest(original)
    report=investigate('BTCUSDT',START,END,tmp_path/'data',Sources(missing_day=True,rest=False),tmp_path/'audit',tmp_path/'verified')
    assert report['status']=='failed' and report['final_missing_15m']['count']==96
    assert report['unresolved'] and digest(original)==before
    assert not (tmp_path/'verified').exists()
    assert (tmp_path/'audit/error.json').exists()


def test_source_discrepancy_unsafe_parity_rejected(tmp_path):
    original=setup(tmp_path,conflict=True);before=original.read_bytes()
    report=investigate('BTCUSDT',START,END,tmp_path/'data',Sources(conflict=True),tmp_path/'audit',tmp_path/'verified')
    assert report['status']=='failed'
    assert report['initial_local_parity']['counts']['high']==1
    assert report['final_parity']['mismatches']
    assert original.read_bytes()==before and not (tmp_path/'verified').exists()
    assert report['source_days'] and report['unresolved']


def test_trustworthy_correction_separate_reference_only(tmp_path):
    original=setup(tmp_path,conflict=True);before=original.read_bytes()
    report=investigate('BTCUSDT',START,END,tmp_path/'data',Sources(proof_root=tmp_path/'proof'),tmp_path/'audit',tmp_path/'verified')
    assert report['status']=='verified'
    assert original.read_bytes()==before
    new=pd.read_parquet(tmp_path/'verified/klines/BTCUSDT/1h.parquet')
    assert new.high.iloc[0]==101
    assert report['source_days'][START.isoformat()]['new_1h_vs_immutable_original']['counts']['high']==1


def test_output_protection_and_partial_failure(tmp_path,monkeypatch):
    setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(['--data-dir','data','--output-dir','data/klines/nested'])==1
    ref=tmp_path/'ref/BTCUSDT';ref.mkdir(parents=True)
    (ref/'timeframe.json').write_text(json.dumps(dict(start=START.isoformat(),end_exclusive=END.isoformat())))
    import backtesting.integrity_audit as module
    monkeypatch.setattr(module,'OfficialSources',lambda *a:Sources(missing_day=True,rest=False))
    args=['--data-dir','data','--v08-dir','ref','--output-dir','audit','--verified-dir','verified','--cache-dir','cache','--legacy-cache','legacy']
    assert main(args)==1
    status=json.loads((tmp_path/'audit/status.json').read_text())
    assert not status['backtest_allowed'] and status['symbols']['ETHUSDT']=='failed'
    previous=(tmp_path/'audit/BTCUSDT/audit.json').read_bytes()
    assert main(args)==1 and (tmp_path/'audit/BTCUSDT/audit.json').read_bytes()==previous


def test_rest_can_fill_absent_partial_archive_rows_without_ignoring_conflicts():
    f=bars();h=aggregate_hourly(f)
    assert decide_day(f,h,f.iloc[:20],[h.iloc[:4],h.iloc[:0]],START,END)[0]
    bad=h.iloc[:4].copy();bad.loc[0,'volume']+=1
    assert not decide_day(f,h,f.iloc[:20],[bad],START,END)[0]


def verified_fixture(tmp_path):
    setup(tmp_path)
    report=investigate('BTCUSDT',START,END,tmp_path/'data',Sources(missing_day=True,proof_root=tmp_path/'proof'),tmp_path/'audit',tmp_path/'verified')
    assert report['status']=='verified'
    return tmp_path/'verified/klines/BTCUSDT'


def test_authorized_loader_uses_only_verified_bundle_and_keeps_archive_path(tmp_path):
    from backtesting.intraday_lab import load_verified
    verified_fixture(tmp_path)
    p=tmp_path/'v08/BTCUSDT';p.mkdir(parents=True)
    (p/'timeframe.json').write_text(json.dumps(dict(start=START.isoformat(),end_exclusive=END.isoformat())))
    f,info,a,b=load_verified(tmp_path/'verified',tmp_path/'v08','BTCUSDT')
    assert len(f)==96 and info['status']=='verified' and info['parity']['status']=='passed'
    assert (a,b)==(START,END)
    target=tmp_path/'verified/klines/BTCUSDT'
    (target/'15m.provenance.json').write_text('{}')
    with pytest.raises(ValueError,match='동시 존재'):load_verified(tmp_path/'verified',tmp_path/'v08','BTCUSDT')


@pytest.mark.parametrize('fault',['artifact','parquet','record','unresolved','missing_decision','unsafe_url'])
def test_verified_loader_corruption_and_forged_status_fail_closed(tmp_path,fault):
    from backtesting.integrity_loader import validate_bundle
    p=verified_fixture(tmp_path);mp=p/'integrity.provenance.json';meta=json.loads(mp.read_text())
    if fault=='artifact':(p/meta['artifacts'][0]['file']).write_bytes(b'wrong')
    elif fault=='parquet':(p/'15m.parquet').write_bytes(b'wrong')
    elif fault=='record':(p/'verification_record.json').write_text('{}')
    elif fault=='unsafe_url':
        meta['artifacts'][0]['url']='https://example.com/wrong'
        mp.write_text(json.dumps(meta))
    else:
        rp=p/'verification_record.json';record=json.loads(rp.read_text())
        if fault=='unresolved':record['unresolved']=[{'reason':'still unknown'}]
        else:record['source_days']={}
        rp.write_text(json.dumps(record));meta['audit_record_sha256']=digest(rp);mp.write_text(json.dumps(meta))
    with pytest.raises((ValueError,OSError)):validate_bundle(p,'BTCUSDT',START,END)


def test_remote_checksum_revision_is_not_silently_recached(tmp_path):
    content=zip_bytes(bars());sha=hashlib.sha256(content).hexdigest()
    session=Session([Response(content=content),Response(content=sha.encode()),Response(content=b'new-revision-hash')])
    s=OfficialSources(tmp_path/'cache',tmp_path/'legacy',session)
    s.archive('BTCUSDT',START,'15m',False);old=next((tmp_path/'cache').rglob('*.zip')).read_bytes()
    s.revalidate_cache=True
    with pytest.raises(ValueError,match='변경 감지'):s.archive('BTCUSDT',START,'15m',False)
    assert next((tmp_path/'cache').rglob('*.zip')).read_bytes()==old


def test_verified_new_root_does_not_allow_output_inside_original_results(tmp_path,monkeypatch):
    from backtesting.intraday_lab import main as lab_main
    monkeypatch.chdir(tmp_path)
    assert lab_main(['--data-dir','data/intraday_v09_verified_input','--output-dir','data/backtests/new'])==1
    assert main(['--data-dir','data/other-input','--output-dir','data/root_cause_v06/new'])==1
    assert not (tmp_path/'data/backtests/new').exists()
