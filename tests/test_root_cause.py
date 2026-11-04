"""Forensic tests use only hand-built/synthetic candles, never actual prices."""
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from backtesting.audit_simulator import run_audit
from backtesting.engine import Config, run
from backtesting.comparison_engine import run_filtered
from backtesting.root_cause import decomposition, event_stats, trade_paths, repetition, main
from backtesting.strategy_lab import simulate
from backtesting.report import save
from backtesting.strategy import SMACrossover


def bars(n=80):
    return pd.DataFrame(dict(open_time=pd.date_range('2023-01-01',periods=n,freq='h',tz='UTC'),
                             open=100.,high=101.,low=99.,close=100.,volume=1.))


class Signal:
    def __init__(self,side=1):self.side=side
    def target(self,h):return self.side if len(h)==1 else -self.side if len(h)==3 else 0


def kwargs(f):return dict(start=f.open_time.iloc[0],end=f.open_time.iloc[-1]+pd.Timedelta(hours=1))


@pytest.mark.parametrize('side',[1,-1])
@pytest.mark.parametrize('case',['normal','gap','both','timeout','target'])
def test_audit_parity_fee_slippage_cash_and_exits(side,case):
    f=bars()
    class Hold:
        def target(self,h):return side if len(h)==1 else 0
    strategy=Hold() if case=='timeout' else Signal(side)
    if case=='gap':f.loc[2,['open','high','low','close']]=[80.,81.,79.,80.] if side==1 else [120.,121.,119.,120.]
    elif case=='both':f.loc[1,['high','low']]=[110.,90.]
    elif case=='target':f.loc[1,['high','low']]=[110.,99.] if side==1 else [101.,90.]
    c=Config();a=run(f,'BTCUSDT',strategy,c,**kwargs(f));events=[]
    b=run_audit(f,'BTCUSDT',strategy,c,events=events,**kwargs(f))
    pd.testing.assert_frame_equal(a.trades,b.trades);pd.testing.assert_frame_equal(a.equity,b.equity)
    assert a.summary==b.summary
    assert np.allclose(b.equity.cash+b.equity.unrealized_pnl,b.equity.equity)
    assert b.trades.net_pnl.sum()==pytest.approx(b.equity.equity.iloc[-1]-10000)
    assert np.allclose(b.trades.net_pnl,b.trades.gross_pnl-b.trades.fees-b.trades.funding_cost)
    t=b.trades.iloc[0];assert t.entry_price==pytest.approx(100*(1+side*c.slippage))
    assert t.fees==pytest.approx(t.quantity*(t.entry_price+t.exit_price)*c.fee_rate)
    if case=='gap':assert any(e['event']=='stop_gap' for e in events)
    if case=='both':assert t.reason=='stop' and sum(x['both_touch_count'] for x in event_stats(events))==1
    if case=='timeout':assert t.reason=='max_holding' and t.holding_hours==72
    if case=='target':assert t.reason=='take_profit'


def test_take_first_is_only_optimistic_diagnostic():
    f=bars(5);f.loc[1,['high','low']]=[110.,90.]
    a=run_audit(f,'BTCUSDT',Signal(),Config(),**kwargs(f))
    b=run_audit(f,'BTCUSDT',Signal(),Config(),take_first=True,**kwargs(f))
    assert a.trades.iloc[0].reason=='stop' and b.trades.iloc[0].reason=='take_profit'
    assert b.trades.iloc[0].net_pnl>a.trades.iloc[0].net_pnl


def test_gate_parity_with_reversal():
    f=bars();gate=lambda h,t:len(h)==1
    a=run_filtered(f,'BTCUSDT',Signal(),Config(),entry_filter=gate,**kwargs(f))
    b=run_audit(f,'BTCUSDT',Signal(),Config(),entry_filter=gate,**kwargs(f))
    pd.testing.assert_frame_equal(a.trades,b.trades);pd.testing.assert_frame_equal(a.equity,b.equity)
    assert len(a.trades)==1 and a.trades.iloc[0].reason=='signal'


def test_decomposition_costs_and_repeated_signals():
    f=bars(8);r=run_audit(f,'BTCUSDT',Signal(),Config(),**kwargs(f))
    d=decomposition(r.trades)
    assert d['fills']==2*len(r.trades)
    assert d['net_pnl']==pytest.approx(d['gross_with_slippage']-d['fees'])
    class Repeat:
        def target(self,h):return 1
    t=r.trades.copy();t.loc[0,'reason']='stop';t.loc[1,'side']=t.loc[0,'side']
    rep=repetition(Repeat(),f,t)
    assert rep['consecutive_same_direction_signals']==6
    assert rep['short_reentry_count']==1
    assert rep['signal_forward_returns'][0]['samples']==7


def test_causal_path_features_and_causal_execution():
    f=bars(30);r=run_audit(f,'BTCUSDT',Signal(),Config(),**kwargs(f))
    paths=trade_paths(r.trades,f)
    g=f.copy();g.loc[5:,['open','high','low','close']]=[200.,201.,199.,200.]
    changed=trade_paths(r.trades,g)
    assert paths[0]['prior_atr20']==changed[0]['prior_atr20']
    b=run_audit(g,'BTCUSDT',Signal(),Config(),**kwargs(g))
    pd.testing.assert_frame_equal(r.equity.iloc[:5],b.equity.iloc[:5])
    assert all(p['posthoc_only'] for p in paths)


def setup(root,symbol):
    f=bars(240);p=100+np.sin(np.arange(240)/10)*5
    f['open']=p;f['close']=p;f['high']=p+.05;f['low']=p-.05
    for name,r in simulate(f,symbol,Config(),**kwargs(f)).items():save(r,root/'lab'/symbol/name)
    out=root/'data/klines'/symbol;out.mkdir(parents=True);f.to_parquet(out/'1h.parquet')


def test_cli_full_audit_preserves_inputs_and_reexecution(tmp_path):
    setup(tmp_path,'BTCUSDT');setup(tmp_path,'ETHUSDT')
    original={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    args=['--data-dir',str(tmp_path/'data'),'--lab-dir',str(tmp_path/'lab'),'--output-dir',str(tmp_path/'audit')]
    assert main(args)==0
    for p,v in original.items():assert p.read_bytes()==v
    for symbol in ['BTCUSDT','ETHUSDT']:
        report=json.loads((tmp_path/'audit'/symbol/'audit.json').read_text())
        assert report['confirmed_facts'] and report['hypotheses'] and report['no_evidence']
        assert len(report['priorities'])==3
        for policy in report['policies']:
            assert set(report['policies'][policy]['cases'])=={'zero_cost','no_fee','original','double_cost'}
    snapshots={p:p.read_bytes() for p in (tmp_path/'audit').rglob('*') if p.is_file()}
    assert main(args)==1
    for p,v in snapshots.items():assert p.read_bytes()==v
    assert main(args[:-2]+['--output-dir',str(tmp_path/'lab')])==1


def test_mismatch_marks_failure(tmp_path):
    setup(tmp_path,'BTCUSDT');p=tmp_path/'lab/BTCUSDT/C_donchian/trades.csv';t=pd.read_csv(p)
    t.loc[0,'reason']='different';t.to_csv(p,index=False)
    assert main(['--symbols','BTCUSDT','--data-dir',str(tmp_path/'data'),'--lab-dir',str(tmp_path/'lab'),
                 '--output-dir',str(tmp_path/'audit')])==1
    assert (tmp_path/'audit/BTCUSDT/error.json').exists()
    assert not (tmp_path/'audit/BTCUSDT/report.md').exists()


def test_hand_calculated_equity_return_and_mdd():
    f=bars(4);f.loc[1,['high','close']]=[101.,101.]
    f.loc[2:,['open','high','low','close']]=[99.,100.,99.,99.]
    class Hold:
        def target(self,h):return 1 if len(h)==1 else 0
    r=run_audit(f,'BTCUSDT',Hold(),Config(fee_rate=0,slippage=0),**kwargs(f))
    assert r.trades.iloc[0].quantity==50
    assert r.equity.equity.tolist()==[10000.,10050.,9950.,9950.]
    assert r.equity.unrealized_pnl.tolist()==[0.,50.,-50.,0.]
    assert r.summary['net_return_pct']==pytest.approx(-.5)
    assert r.summary['max_drawdown_pct']==pytest.approx(100*100/10050)


def test_atr_uses_only_completed_entry_history():
    f=bars(30);r=run_audit(f,'BTCUSDT',Signal(),Config(),**kwargs(f))
    t=r.trades.iloc[:1].copy();t.loc[t.index[0],'entry_time']=f.open_time.iloc[22]
    t.loc[t.index[0],'exit_time']=f.open_time.iloc[25]
    a=trade_paths(t,f)
    f.loc[22:,['high','low','close']]=[1000.,1.,500.]
    b=trade_paths(t,f)
    assert a[0]['prior_atr20']==b[0]['prior_atr20']==2.
    assert a[0]['mfe_envelope_pct']!=b[0]['mfe_envelope_pct']
