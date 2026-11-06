"""Synthetic, fixed confirmation vs pure delay; no historical fitting."""
import json

import numpy as np
import pandas as pd
import pytest

from backtesting.intraday_confirmation_lab import build_policies,main,case_details
from backtesting.intraday_signals import features
from backtesting.intraday_engine import simulate,IntradayConfig


def frame(closes):
    c=np.array(closes,dtype=float)
    return pd.DataFrame(dict(open_time=pd.date_range('2025-02-01',periods=len(c),freq='15min',tz='UTC'),
                             open=c,high=c+.01,low=c-.01,close=c,volume=1.))


def causal(signals,trend):
    return pd.DataFrame({'breakout_20':signals,'breakout_20_raw':signals,'trend':trend})


@pytest.mark.parametrize('side',[1,-1])
def test_confirm_signal_at_next_close_fill_at_t_plus_two(side):
    f=frame([100,100+side,100+2*side,100+2*side])
    c=causal([side,0,0,0],[side]*4)
    arrays,events,stats=build_policies(f,c)
    assert arrays['breakout_20_confirm1'].tolist()==[0,side,0,0]
    assert arrays['breakout_20_delay1'].tolist()==[0,side,0,0]
    assert stats['confirm_passed']==1
    t,_,_,_=simulate(f,arrays['breakout_20_confirm1'])
    assert t.entry_time.iloc[0]==f.open_time.iloc[2]
    assert t.entry_price.iloc[0]==pytest.approx(f.open.iloc[2]*(1+side*.0002))


@pytest.mark.parametrize('close,trend',[ (100,1),(99,1),(101,0),(101,-1) ])
def test_confirmation_rejects_equality_reversal_or_lost_hour_trend(close,trend):
    f=frame([100,close,close,close]);c=causal([1,0,0,0],[1,trend,trend,trend])
    arrays,_,_=build_policies(f,c)
    assert not arrays['breakout_20_confirm1'].any()
    assert arrays['breakout_20_delay1'].tolist()==[0,1,0,0] # no new trend test


def test_progress_compares_source_close_not_next_breakout_or_high():
    f=frame([100,101,101,101]);f.loc[0,'high']=150
    c=causal([1,0,0,0],[1]*4)
    arrays,_,_=build_policies(f,c)
    assert arrays['breakout_20_confirm1'][1]==1


def test_overlapping_candidates_are_independent_not_overwritten():
    f=frame([100,101,102,101,100,100]);c=causal([1,1,-1,-1,0,0],[1,1,1,-1,-1,-1])
    a,_,_=build_policies(f,c)
    assert a['breakout_20_confirm1'].tolist()==[0,1,1,-1,-1,0]
    assert a['breakout_20_delay1'].tolist()==[0,1,1,-1,-1,0]


@pytest.mark.parametrize('signals,expected_unconfirmed,expected_no_open', [([0,0,1],1,0),([0,1,0],0,1)])
def test_terminal_candidates_are_discarded_and_counted(signals,expected_unconfirmed,expected_no_open):
    f=frame([100,101,102]);c=causal(signals,[1,1,1])
    a,_,s=build_policies(f,c)
    assert s['no_confirmation_bar']==expected_unconfirmed
    assert s['confirmed_no_execution_bar']==expected_no_open
    assert a['breakout_20_confirm1'][-1]==a['breakout_20_delay1'][-1]==0
    assert not a['breakout_20_confirm1'].any()


def test_future_changes_and_removal_cannot_change_past_policy_signals():
    f=frame(np.arange(100.,130.));c=causal([1]*30,[1]*30)
    a,_,_=build_policies(f,c)
    g=f.copy();g.loc[20:,['open','high','low','close']]=[80.,81.,79.,80.]
    b,_,_=build_policies(g,c);short,_,_=build_policies(f.iloc[:20],c.iloc[:20])
    for name in a:
        np.testing.assert_array_equal(a[name][:20],b[name][:20])
        # Truncation intentionally discards the last unexecutable signal.
        np.testing.assert_array_equal(a[name][:19],short[name][:19])
    _,ea,_,_=simulate(f,a['breakout_20_confirm1']);_,eb,_,_=simulate(g,b['breakout_20_confirm1'])
    pd.testing.assert_frame_equal(ea.iloc[:20],eb.iloc[:20])


def test_existing_feature_builder_hour_closed_only_and_previous20():
    f=frame(100+np.arange(1000)*.02)
    c=features(f)
    assert c.trend.iloc[:799].eq(0).all()
    assert c.previous20_high.iloc[20]==pytest.approx(f.high.iloc[:20].max())
    assert (c.hour_available_at.dropna()<=c.signal_time.loc[c.hour_available_at.notna()]).all()
    a,_,_=build_policies(f,c)
    assert np.count_nonzero(a['breakout_20_confirm1'][:800])==0
    assert np.count_nonzero(a['breakout_20_confirm1'][800:])>0


@pytest.mark.parametrize('multiplier',[1,2])
def test_same_engine_costs_risk_ttl_and_stop_first(multiplier):
    f=frame([100.]*32);f.loc[2,['high','low']]=[105.,95.]
    c=causal([1]+[0]*31,[1]*32)
    a,_,_=build_policies(f,c)
    # delay enters at bar2 and both thresholds touch in that entry bar.
    t,e,filled,s=simulate(f,a['breakout_20_delay1'],IntradayConfig(cost_multiplier=multiplier))
    assert len(t)==1 and t.reason.iloc[0]=='stop'
    r=t.iloc[0];fee=.0004*multiplier
    assert r.fees==pytest.approx(r.quantity*(r.entry_price+r.exit_price)*fee)
    assert r.net_pnl==pytest.approx(r.gross_pnl-r.fees)
    assert r.entry_leverage<=1 and r.planned_risk_fraction<=.01+1e-12
    details=case_details(f,c,a['breakout_20_delay1'],t,e,filled,s,f.open_time.iloc[0],f.open_time.iloc[-1]+pd.Timedelta(minutes=15))
    assert details['double_touch_ambiguity_count']==1 and details['stop_ratio_pct']==100


def test_ttl_six_hours_from_delayed_entry_and_no_extra_positions():
    f=frame([100.]*40);a,_,_=build_policies(f,causal([1]*40,[1]*40))
    t,_,_,_=simulate(f,a['breakout_20_delay1'])
    assert t.reason.iloc[0]=='max_holding'
    assert t.holding_hours.iloc[0]==6
    assert t.exit_time.iloc[0]==f.open_time.iloc[26] # entry2 +24 quarter hours


def test_data_and_baseline_gates_precede_any_research_output(tmp_path,monkeypatch):
    import backtesting.intraday_confirmation_lab as module
    data=tmp_path/'input';case=tmp_path/'study/ETHUSDT/breakout_20_cost1';case.mkdir(parents=True)
    for name in ['trades.csv','equity.csv','summary.json']:(case/name).write_text('original')
    root=data/'klines/ETHUSDT';root.mkdir(parents=True)
    for name in ['15m.parquet','1h.parquet','integrity.provenance.json']:(root/name).write_text('original')
    original={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    args=['--data-dir',str(data),'--research-dir',str(tmp_path/'study'),'--output-dir',str(tmp_path/'out')]
    def reject(*a):raise ValueError('strict parity rejected')
    monkeypatch.setattr(module,'load_verified',reject)
    monkeypatch.setattr(module,'features',lambda *_:pytest.fail('gate bypassed'))
    assert main(args)==1
    assert json.loads((tmp_path/'out/status.json').read_text())['status']=='failed'
    assert not (tmp_path/'out/ETHUSDT/policy_signals.csv').exists()
    for p,v in original.items():assert p.read_bytes()==v
    assert main(args)==1
    assert main(args[:-2]+['--output-dir',str(data/'inside')])==1


def test_baseline_mismatch_never_runs_new_policies(tmp_path,monkeypatch):
    import backtesting.intraday_confirmation_lab as module
    f=frame([100.]*8);case=tmp_path/'study/ETHUSDT/breakout_20_cost1';case.mkdir(parents=True)
    inp=tmp_path/'input/klines/ETHUSDT';inp.mkdir(parents=True)
    for name in ['trades.csv','equity.csv','summary.json']:(case/name).write_text('original')
    for name in ['15m.parquet','1h.parquet','integrity.provenance.json']:(inp/name).write_text('original')
    # Stub only fixture loading/size for a synthetic gate test; production is frozen.
    class SizedFrame(pd.DataFrame):
        def __len__(self):return 60864
    supplied=SizedFrame(f)
    monkeypatch.setattr(module,'load_verified',lambda *a:(supplied,{},pd.Timestamp(module.V092_START),pd.Timestamp(module.V092_END)))
    monkeypatch.setattr(module,'features',lambda f:causal([0]*8,[0]*8))
    monkeypatch.setattr(module,'build_policies',lambda *a:({'breakout_20':np.zeros(8,dtype=int)},pd.DataFrame(),{}))
    calls=[]
    monkeypatch.setattr(module,'simulate',lambda *a:(calls.append('baseline') or (pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),{})))
    def mismatch(*a):raise ValueError('1031/502 baseline mismatch')
    monkeypatch.setattr(module,'verify_reproduction',mismatch)
    assert main(['--data-dir',str(tmp_path/'input'),'--research-dir',str(tmp_path/'study'),'--output-dir',str(tmp_path/'out')])==1
    assert calls==['baseline']
    assert not (tmp_path/'out/ETHUSDT/policy_signals.csv').exists()
