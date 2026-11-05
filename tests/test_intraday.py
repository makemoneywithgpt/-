"""Synthetic-only data, clock, fixed strategy and accounting tests."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
import pytest

from backtesting.intraday_data import (validate15,aggregate_hourly,parity,Archives,ArchiveGap,collect,digest,STEP,main as data_main)
from backtesting.intraday_signals import features
from backtesting.intraday_engine import IntradayConfig,simulate
from backtesting.intraday_lab import main

START=pd.Timestamp('2023-01-01',tz='UTC')


def bars(n=32):
    return pd.DataFrame(dict(open_time=pd.date_range(START,periods=n,freq='15min'),
                             open=100.,high=100.1,low=99.9,close=100.,volume=10.))


def end(f):return f.open_time.iloc[-1]+STEP


@pytest.mark.parametrize('fault',['duplicate','reverse','gap','nan','negative','ohlc','unaligned','unfinished'])
def test_15m_validation(fault):
    f=bars();now=pd.Timestamp('2024-01-01',tz='UTC')
    if fault=='duplicate':f=pd.concat([f,f.iloc[-1:]])
    elif fault=='reverse':f=f.iloc[::-1]
    elif fault=='gap':f=f.drop(index=2)
    elif fault=='nan':f.loc[2,'close']=float('nan')
    elif fault=='negative':f.loc[2,'volume']=-1
    elif fault=='ohlc':f.loc[2,'high']=90
    elif fault=='unaligned':f.loc[2,'open_time']+=pd.Timedelta(minutes=1)
    else:now=START+pd.Timedelta(minutes=29)
    with pytest.raises(ValueError):validate15(f,START,START+pd.Timedelta(hours=8),now=now)


def test_utc_sort_and_1h_strict_parity():
    f=bars();f['open_time']=f.open_time.dt.tz_convert('Asia/Seoul')
    g=validate15(f,START,end(f));h=aggregate_hourly(g)
    assert parity(g,h,START,end(f))['hours']==8
    assert h.volume.iloc[0]==40
    h.loc[1,'close']+=.05
    with pytest.raises(ValueError,match='소스 혼용'):parity(g,h,START,end(f))


def zipped(f,header=False):
    from market_data.core import COLUMNS
    lines=[]
    if header:lines.append(','.join(COLUMNS))
    for b in f.itertuples(index=False):
        lines.append(','.join(map(str,[int(b.open_time.timestamp()*1000),b.open,b.high,b.low,b.close,b.volume,0,1000,5,4,400,0])))
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:z.writestr('x.csv','\n'.join(lines))
    return buf.getvalue()


class Response:
    def __init__(self,data):self.content=data;self.text=data.decode(errors='replace')


class Client:
    def __init__(self,responses):self.responses=iter(responses);self.calls=0
    def get(self,*a,**kw):self.calls+=1;return next(self.responses)


@pytest.mark.parametrize('header',[False,True])
def test_archive_checksum_cache_and_header(tmp_path,header):
    data=zipped(bars(),header);sha=hashlib.sha256(data).hexdigest()
    client=Client([Response(data),Response(sha.encode())]);a=Archives(tmp_path,client)
    f,meta=a.fetch('BTCUSDT',START,False)
    assert len(f)==32 and meta['official_checksum']==sha and client.calls==2
    same,_=a.fetch('BTCUSDT',START,False);pd.testing.assert_frame_equal(f,same)
    assert client.calls==2
    path=next(tmp_path.rglob('*.zip'));path.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='무결성'):a.fetch('BTCUSDT',START,False)


def test_bad_checksum_and_gap_never_cached(tmp_path):
    a=Archives(tmp_path,Client([Response(zipped(bars())),Response(b'bad')]))
    with pytest.raises(ValueError,match='checksum'):a.fetch('BTCUSDT',START,False)
    assert not list(tmp_path.rglob('*.zip'))
    with pytest.raises(ArchiveGap):Archives.parse(zipped(bars().drop(index=2)))


def setup_hourly(root,symbol,f):
    p=root/'klines'/symbol;p.mkdir(parents=True,exist_ok=True)
    aggregate_hourly(f).to_parquet(p/'1h.parquet');return p


class Source:
    def __init__(self,f):self.f=f;self.calls=0
    def fetch(self,symbol,date,monthly):
        self.calls+=1
        return self.f.loc[(self.f.open_time>=date)&(self.f.open_time<date+pd.Timedelta(days=1))].copy(),dict(url='synthetic')


def test_complete_collection_parity_reuse_and_no_overwrite(tmp_path):
    f=bars(96);p=setup_hourly(tmp_path,'BTCUSDT',f);old=(p/'1h.parquet').read_bytes();s=Source(f)
    info=collect('BTCUSDT',START,end(f),tmp_path,tmp_path/'cache',s)
    assert info['rows']==96 and info['parity']['status']=='passed'
    calls=s.calls;collect('BTCUSDT',START,end(f),tmp_path,tmp_path/'cache',s)
    assert s.calls==calls and (p/'1h.parquet').read_bytes()==old
    with pytest.raises(ValueError,match='덮어쓰기'):collect('BTCUSDT',START,end(f)+pd.Timedelta(hours=1),tmp_path,tmp_path/'cache',s)


def test_parity_failure_never_publishes_15m(tmp_path):
    f=bars(96);p=setup_hourly(tmp_path,'BTCUSDT',f);h=pd.read_parquet(p/'1h.parquet');h.loc[0,'high']+=1;h.to_parquet(p/'1h.parquet')
    with pytest.raises(ValueError,match='parity'):collect('BTCUSDT',START,end(f),tmp_path,tmp_path/'cache',Source(f))
    assert not (p/'15m.parquet').exists() and not (p/'15m.provenance.json').exists()


def test_month_internal_gap_uses_daily_fallback(tmp_path):
    f=bars(31*96);setup_hourly(tmp_path,'BTCUSDT',f)
    class Gapped(Source):
        def fetch(self,symbol,date,monthly):
            if monthly:raise ArchiveGap('synthetic monthly missing')
            return super().fetch(symbol,date,monthly)
    s=Gapped(f);info=collect('BTCUSDT',START,end(f),tmp_path,tmp_path/'cache',s)
    assert info['rows']==len(f) and s.calls==31


def test_closed_hour_filter_ema_warmup_and_excluded_breakout_bar():
    f=bars(820);p=100+np.arange(len(f))*.02
    f['open']=p;f['close']=p;f['high']=p+.01;f['low']=p-.01
    s=features(f)
    assert s.trend.iloc[:799].eq(0).all() and s.trend.iloc[799]==1
    assert s.hour_available_at.iloc[799]==f.open_time.iloc[800]
    assert s.pullback_reclaim_raw.iloc[:20].eq(0).all()
    assert s.breakout_20_raw.iloc[:20].eq(0).all()
    assert s.previous20_high.iloc[20]==pytest.approx(f.high.iloc[:20].max())
    assert s.breakout_20_raw.iloc[20]==1
    assert s.breakout_20.iloc[799]==1
    for i in range(799,len(f)):
        assert s.hour_available_at.iloc[i]<=s.signal_time.iloc[i]


def test_ema_reclaim_is_one_cross_event():
    f=bars(60);price=np.r_[np.arange(120.,90.,-1),np.arange(90.,120.)]
    f['open']=price;f['close']=price;f['high']=price+.1;f['low']=price-.1
    s=features(f)
    long_rows=np.flatnonzero(s.pullback_reclaim_raw.to_numpy()==1)
    assert len(long_rows)==1
    assert s.ema20.iloc[:19].isna().all()


def test_future_prices_changed_or_removed_leave_features_and_past_fills_unchanged():
    f=bars(1000);p=100+np.arange(len(f))*.02
    f['open']=p;f['close']=p;f['high']=p+.1;f['low']=p-.1
    g=f.copy();g.loc[880:,['open','high','low','close']]=[200.,201.,199.,200.]
    a=features(f);b=features(g);short=features(f.iloc[:881])
    pd.testing.assert_frame_equal(a.iloc[:880],b.iloc[:880])
    pd.testing.assert_frame_equal(a.iloc[:880],short.iloc[:880])
    _,e,_,_=simulate(f,a.breakout_20);_,q,_,_=simulate(g,b.breakout_20)
    pd.testing.assert_frame_equal(e.iloc[:880],q.iloc[:880])


@pytest.mark.parametrize('side',[1,-1])
@pytest.mark.parametrize('case',['gap','both','target','timeout'])
def test_next_open_costs_risk_and_6h_in_15m(side,case):
    f=bars(40);signal=np.zeros(len(f),dtype=int);signal[0]=side;signal[2]=-side
    if case=='gap':f.loc[2,['open','high','low','close']]=[80.,81.,79.,80.] if side==1 else [120.,121.,119.,120.]
    elif case=='both':f.loc[1,['high','low']]=[110.,90.]
    elif case=='target':f.loc[1,['high','low']]=[110.,99.9] if side==1 else [100.1,90.]
    t,e,events,s=simulate(f,signal)
    first=t.iloc[0];assert first.entry_time==f.open_time.iloc[1]
    assert first.entry_price==pytest.approx(100*(1+side*.0002))
    assert first.reason=={'gap':'stop_gap','both':'stop','target':'take_profit','timeout':'max_holding'}[case]
    assert first.fees==pytest.approx(first.quantity*(first.entry_price+first.exit_price)*.0004)
    assert first.net_pnl==pytest.approx(first.gross_pnl-first.fees)
    assert t.planned_risk_fraction.le(.01+1e-12).all() and t.entry_leverage.le(1.).all()
    assert np.allclose(e.cash+e.unrealized_pnl,e.equity)
    assert s['net_pnl']==pytest.approx(t.net_pnl.sum())
    if case=='timeout':
        assert first.holding_hours==6 and first.exit_time==f.open_time.iloc[25]
        assert len(t)==1 # opposite signal while held did not reverse


def test_cost_2x_and_cash_no_trade():
    f=bars();signal=np.zeros(len(f),dtype=int);signal[0]=1
    a,_,_,sa=simulate(f,signal);b,_,_,sb=simulate(f,signal,IntradayConfig(cost_multiplier=2))
    assert b.entry_price.iloc[0]==pytest.approx(100*1.0004)
    assert sb['config']['fee_rate']==.0008 and sb['config']['slippage']==.0004
    assert sb['net_pnl']<sa['net_pnl']
    t,e,_,s=simulate(f,np.zeros(len(f)))
    assert t.empty and e.equity.eq(10000).all()
    assert s['net_return_pct']==s['max_drawdown_pct']==s['total_fees']==0
    with pytest.raises(ValueError):IntradayConfig(max_leverage=2)


def setup_research(root,symbol,n=820):
    f=bars(n);setup_hourly(root/'data',symbol,f)
    collect(symbol,START,end(f),root/'data',root/'cache',Source(f))
    p=root/'v08'/symbol;p.mkdir(parents=True)
    (p/'timeframe.json').write_text(json.dumps(dict(start=START.isoformat(),end_exclusive=end(f).isoformat())))


def test_offline_cli_empty_trades_preserves_all_files_and_reexecution(tmp_path,capsys):
    setup_research(tmp_path,'BTCUSDT');setup_research(tmp_path,'ETHUSDT')
    old={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    args=['--data-dir',str(tmp_path/'data'),'--v08-dir',str(tmp_path/'v08'),'--output-dir',str(tmp_path/'study')]
    assert main(args)==0
    for p,v in old.items():assert p.read_bytes()==v
    result=json.loads((tmp_path/'study/comparison.json').read_text());assert result['status']=='success'
    for symbol in ['BTCUSDT','ETHUSDT']:
        report=json.loads((tmp_path/'study'/symbol/'research.json').read_text())
        assert len(report['cases'])==5
        assert report['cases']['cash_cost1']['summary']['net_return_pct']==0
    previous={p:p.read_bytes() for p in (tmp_path/'study').rglob('*') if p.is_file()}
    assert main(args)==1
    for p,v in previous.items():assert p.read_bytes()==v
    assert main(args[:-2]+['--output-dir',str(tmp_path/'data/klines')])==1
    (tmp_path/'data/klines/BTCUSDT/15m.provenance.json').unlink()
    capsys.readouterr()
    assert main(args[:-2]+['--symbols','BTCUSDT','--output-dir',str(tmp_path/'failed')])==1
    assert '연구 완료' not in capsys.readouterr().out
    assert json.loads((tmp_path/'failed/comparison.json').read_text())['status']=='partial_or_failed'
    assert not (tmp_path/'failed/BTCUSDT/causal_features.csv').exists()


def test_symlink_cache_and_destination_escape_rejected(tmp_path):
    data=tmp_path/'data';(data/'klines').mkdir(parents=True)
    outside=tmp_path/'outside';outside.mkdir()
    (data/'klines/BTCUSDT').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match='밖으로'):
        collect('BTCUSDT',START,START+pd.Timedelta(hours=1),data,tmp_path/'cache',Source(bars(4)))
    cache=tmp_path/'cache';cache.mkdir();(cache/'BTCUSDT').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match='밖으로'):Archives(cache,Client([])).fetch('BTCUSDT',START,False)
    assert list(outside.iterdir())==[]


def test_collector_cli_rejects_venv_and_protected_cache(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert data_main(['--data-dir','.venv'])==1
    assert data_main(['--cache-dir','data/klines/cache'])==1


def test_no_double_cost_deduction_and_exact_sizing_bound():
    f=bars();s=np.zeros(len(f),dtype=int);s[0]=1
    t,_,_,summary=simulate(f,s,IntradayConfig(cost_multiplier=2));a=t.iloc[0]
    fee=.0008;slip=.0004;stop_exit=a.entry_price*.99*(1-slip)
    expected=min(100/(a.entry_price-stop_exit+fee*(a.entry_price+stop_exit)),10000/(a.entry_price*(1+fee)))
    assert a.quantity==pytest.approx(expected)
    assert a.fees==pytest.approx(a.quantity*(a.entry_price+a.exit_price)*fee)
    assert summary['net_pnl']==pytest.approx(a.quantity*(a.exit_price-a.entry_price)-a.fees)


def test_missing_official_day_or_archive_duplicate_blocks_publish(tmp_path):
    f=bars(96);p=setup_hourly(tmp_path,'BTCUSDT',f)
    class Missing:
        def fetch(self,*a):return None,None
    with pytest.raises(ValueError,match='아카이브 미게시'):
        collect('BTCUSDT',START,end(f),tmp_path,tmp_path/'cache',Missing())
    assert not (p/'15m.parquet').exists()
    with pytest.raises(ValueError,match='중복'):
        Archives.parse(zipped(pd.concat([f,f.iloc[-1:]],ignore_index=True)))


def test_checksum_unavailable_explicit_provenance_and_quarter_hour_units(tmp_path):
    a=Archives(tmp_path,Client([Response(zipped(bars())),None]))
    _,meta=a.fetch('BTCUSDT',START,False)
    assert not meta['checksum_available'] and meta['official_checksum'] is None
    f=bars();f.loc[0,'open_time']+=pd.Timedelta(minutes=1)
    with pytest.raises(ValueError,match='millisecond'):Archives.parse(zipped(f))


def test_offline_nonzero_trades_reports_cost_stress_and_no_adoption(tmp_path):
    f=bars(1000);p=100+np.arange(len(f))*.02
    f['open']=p;f['close']=p;f['high']=p+.01;f['low']=p-.01
    setup_hourly(tmp_path/'data','BTCUSDT',f)
    collect('BTCUSDT',START,end(f),tmp_path/'data',tmp_path/'cache',Source(f))
    ref=tmp_path/'v08/BTCUSDT';ref.mkdir(parents=True)
    (ref/'timeframe.json').write_text(json.dumps(dict(start=START.isoformat(),end_exclusive=end(f).isoformat())))
    assert main(['--symbols','BTCUSDT','--data-dir',str(tmp_path/'data'),'--v08-dir',str(tmp_path/'v08'),
                 '--output-dir',str(tmp_path/'study')])==0
    report=json.loads((tmp_path/'study/BTCUSDT/research.json').read_text())
    case=report['cases']['breakout_20_cost1'];stress=report['cases']['breakout_20_cost2']
    assert case['summary']['total_trades']>0
    assert case['signal_populations']['raw']['count']>case['signal_populations']['admitted']['count']>=case['signal_populations']['filled']['count']
    assert case['summary']['net_pnl']>stress['summary']['net_pnl']
    assert report['research_verdict']=='unproven_do_not_adopt'
    assert (tmp_path/'study/forward_protocol.md').exists()
