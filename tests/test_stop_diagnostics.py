"""Synthetic long/short post-hoc semantics; never modifies a strategy engine."""
from types import SimpleNamespace
import json

import numpy as np
import pandas as pd
import pytest

from backtesting.intraday_engine import simulate
from backtesting.intraday_stop_diagnostics import (analyze_paths,summarize,prior_atr,verify_reproduction,main)


def bars(n=48):
    return pd.DataFrame(dict(open_time=pd.date_range('2025-02-01',periods=n,freq='15min',tz='UTC'),
                             open=100.,high=100.2,low=99.8,close=100.,volume=1.))


def stopped(side=1,recover=True,ambiguous=False,close_recovers=False,gap=False):
    f=bars()
    if side==1:
        f.loc[2,['open','high','low','close']]=[98.5 if gap else 100.,104. if ambiguous else 100.2,98.,103. if close_recovers else 98.5]
        f.loc[3:,['open','high','low','close']]=[102.,103.,101.,102.5] if recover else [98.5,98.9,98.,98.5]
    else:
        f.loc[2,['open','high','low','close']]=[101.5 if gap else 100.,102.,96. if ambiguous else 99.8,97. if close_recovers else 101.5]
        f.loc[3:,['open','high','low','close']]=[98.,99.,97.,97.5] if recover else [101.5,102.,101.1,101.5]
    signal=np.zeros(len(f),dtype=int);signal[0]=side
    t,e,_,s=simulate(f,signal)
    d,cuts=analyze_paths(t,f)
    return f,t,e,s,d


@pytest.mark.parametrize('side',[1,-1])
@pytest.mark.parametrize('recover',[False,True])
def test_confirmed_rebound_vs_larger_endpoint_loss(side,recover):
    f,t,e,s,d=stopped(side,recover)
    r=d.iloc[0];stats=summarize(d)
    assert t.reason.iloc[0]=='stop' and len(t)==1
    assert bool(r.confirmed_post_stop_recovery)==recover
    assert bool(r.post_stop_tp_touch)==recover
    assert bool(r.h6_worse_than_actual_stop_net)!=recover
    assert stats['stops']['confirmed_recovery']==dict(numerator=int(recover),denominator=1,rate_pct=100.*recover)
    assert stats['stops']['stop_limited_worse_h6_fixed_qty_loss']['numerator']==int(not recover)
    assert r.observed_bars==24 and not r.censored
    assert r.mfe_fill_pct>=0 and r.mae_fill_pct>0
    assert stats['stop_endpoint_cross_counts']['denominator']==1


@pytest.mark.parametrize('side',[1,-1])
def test_same_stop_bar_high_does_not_prove_after_stop_recovery(side):
    _,_,_,_,d=stopped(side,recover=False,ambiguous=True)
    r=d.iloc[0]
    assert r.stop_take_same_bar_ambiguous and r.stop_same_bar_tp_order_unknown
    assert r.stop_same_bar_recovery_order_unknown
    assert not r.confirmed_post_stop_recovery and not r.post_stop_tp_touch
    assert r.first_10_same_bar_order_unknown
    assert r.pre_stop_mfe_lower_pct<r.pre_stop_mfe_upper_pct
    assert r.pre_stop_mae_known_pct<r.pre_stop_mae_upper_pct
    assert summarize(d)['same_bar_stop_tp_count']==1


@pytest.mark.parametrize('side',[1,-1])
def test_stop_bar_close_can_confirm_recovery_but_first_touch_order_unknown(side):
    _,_,_,_,d=stopped(side,recover=False,ambiguous=True,close_recovers=True)
    r=d.iloc[0]
    assert r.stop_close_confirms_recovery and r.stop_close_confirms_tp
    assert r.confirmed_post_stop_recovery and r.post_stop_tp_touch
    assert not r.stop_same_bar_recovery_order_unknown
    assert r.stop_take_same_bar_ambiguous


@pytest.mark.parametrize('side',[1,-1])
def test_gap_at_open_later_extrema_are_after_stop(side):
    _,t,_,_,d=stopped(side,recover=False,ambiguous=True,gap=True)
    assert t.reason.iloc[0]=='stop_gap'
    assert d.iloc[0].confirmed_post_stop_recovery
    assert not d.iloc[0].stop_take_same_bar_ambiguous # gap observed at open


def test_partial_horizon_and_data_ends_on_entry_excluded():
    f=bars(2);signals=[1,0];t,_,_,_=simulate(f,signals)
    d,_=analyze_paths(t,f);s=summarize(d)
    assert d.iloc[0].observed_bars==1 and d.iloc[0].censored
    assert pd.isna(d.iloc[0].h6_fill_return_pct)
    assert s['censored']==1 and s['full_horizon']==0
    assert s['all']['h6_raw']['n']==0


def test_fixed_quantity_cost_adjustment_does_not_double_charge_slippage():
    f,t,_,_,d=stopped()
    a=t.iloc[0];r=d.iloc[0];price=f.close.iloc[24]
    exit_fill=price*(1-.0002)
    expected=a.quantity*(exit_fill-a.entry_price)-a.quantity*(a.entry_price+exit_fill)*.0004
    assert r.h6_fixed_qty_net_usdt==pytest.approx(expected)
    assert r.h6_raw_return_pct==pytest.approx((price/100-1)*100)
    assert r.h6_fill_return_pct==pytest.approx((price/a.entry_price-1)*100)
    assert r.h6_raw_return_pct!=r.h6_fill_return_pct


def test_recovery_then_larger_loss_can_both_happen():
    f,t,_,_,_=stopped(recover=False)
    f.loc[3,['open','high','low','close']]=[100.,103.,99.5,100.]
    d,_=analyze_paths(t,f);s=summarize(d)
    assert d.iloc[0].confirmed_post_stop_recovery and d.iloc[0].h6_worse_than_actual_stop_net
    assert s['stop_endpoint_cross_counts']['recovered_and_worse']==1


def test_prior_atr_and_trend_never_read_entry_or_later_prices():
    f=bars(48);a=prior_atr(f);g=f.copy();g.loc[20:,['high','low','close']]=[200.,1.,150.]
    b=prior_atr(g)
    assert a.iloc[20]==b.iloc[20]
    assert pd.isna(a.iloc[14]) and pd.notna(a.iloc[15])
    assert a.iloc[21]!=b.iloc[21]


def test_reentry_stop_observations_overlap_without_becoming_portfolio_pnl():
    f=bars(48);f.loc[[2,5],'low']=98.
    sig=np.zeros(len(f),dtype=int);sig[[0,3]]=1
    t,_,_,_=simulate(f,sig);d,_=analyze_paths(t,f)
    assert len(t)==2 and d.reason.eq('stop').all()
    assert d.horizon_time.iloc[0]>d.entry_time.iloc[1]
    assert d.posthoc_only.all()


def test_replay_gate_full_paths_counts_and_deliberate_corruption(tmp_path):
    _,t,e,s,_=stopped()
    t.to_csv(tmp_path/'trades.csv',index=False);e.to_csv(tmp_path/'equity.csv',index=False)
    (tmp_path/'summary.json').write_text(json.dumps({'summary':s}))
    with pytest.raises(ValueError,match='1031'):verify_reproduction(t,e,s,tmp_path)
    expected=dict(total_trades=1,stop=1,take_profit=0,max_holding=0)
    assert verify_reproduction(t,e,s,tmp_path,expected,expected_net=None)['verified']
    old=(tmp_path/'equity.csv').read_bytes()
    changed=t.copy();changed.loc[0,'quantity']+=.1;changed.to_csv(tmp_path/'trades.csv',index=False)
    with pytest.raises(ValueError,match='trades.csv'):verify_reproduction(t,e,s,tmp_path,expected,expected_net=None)
    assert (tmp_path/'equity.csv').read_bytes()==old


def test_no_outputs_or_simulation_on_data_gate_failure(tmp_path,monkeypatch):
    import backtesting.intraday_stop_diagnostics as module
    inp=tmp_path/'input';case=tmp_path/'study/ETHUSDT/breakout_20_cost1';case.mkdir(parents=True)
    for name in ['trades.csv','equity.csv','summary.json']:(case/name).write_text('sentinel')
    root=inp/'klines/ETHUSDT';root.mkdir(parents=True)
    for name in ['15m.parquet','1h.parquet','integrity.provenance.json']:(root/name).write_text('sentinel')
    originals={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    def reject(*args):raise ValueError('parity denied')
    monkeypatch.setattr(module,'load_verified',reject)
    monkeypatch.setattr(module,'features',lambda *_:pytest.fail('data gate bypassed'))
    args=['--data-dir',str(inp),'--research-dir',str(tmp_path/'study'),'--output-dir',str(tmp_path/'out')]
    assert main(args)==1
    assert not (tmp_path/'out/ETHUSDT/per_trade.csv').exists()
    assert json.loads((tmp_path/'out/status.json').read_text())['status']=='failed'
    for p,value in originals.items():assert p.read_bytes()==value
    assert main(args)==1
    assert main(args[:-2]+['--output-dir',str(inp/'nested')])==1


def test_determinism_grouping_and_boundary_rejection():
    from backtesting.intraday_stop_diagnostics import subgroup_summaries
    f,t,_,_,a=stopped();b,_=analyze_paths(t,f)
    pd.testing.assert_frame_equal(a,b)
    groups=subgroup_summaries(a)
    assert groups['population']['all_filled']['total']==1 and groups['population']['stop']['total']==1
    assert groups['side']['long']['total']==1 and groups['year']['2025']['total']==1
    changed=t.copy();changed.loc[0,'entry_time']+=pd.Timedelta(minutes=1)
    with pytest.raises(ValueError,match='경계'):analyze_paths(changed,f)


def test_gap_exactly_at_h6_is_not_mistaken_for_post_stop_h6_observation():
    f=bars(32);f.loc[25,['open','high','low','close']]=[80.,81.,79.,80.]
    signals=np.zeros(len(f),dtype=int);signals[0]=1
    t,_,_,_=simulate(f,signals);assert t.reason.iloc[0]=='stop_gap'
    assert t.holding_hours.iloc[0]==6
    d,_=analyze_paths(t,f);r=d.iloc[0]
    assert not r.censored and not r.h6_after_stop_observable
    assert pd.isna(r.h6_worse_than_actual_stop_net)
    s=summarize(d)
    assert s['stops']['stop_limited_worse_h6_fixed_qty_loss']['denominator']==0
    assert s['stop_endpoint_cross_counts']['h6_stop_comparison_unavailable']==1


def test_gap_first_adverse_at_open_is_not_unknown_order():
    _,_,_,_,d=stopped(gap=True,ambiguous=True,recover=False)
    assert d.iloc[0].first_10_pair_order=='adverse_at_open'
    assert not d.iloc[0].first_10_same_bar_order_unknown
    assert not d.iloc[0].post_stop_recovery_stop_same_bar_unknown


def test_known_stop_bar_close_is_post_stop_adverse_evidence():
    _,_,_,_,d=stopped(recover=True)
    r=d.iloc[0]
    # Close of the stop bar is below the original stop; next bars rebound.
    assert r.post_stop_further_adverse_touch and r.post_stop_stop_level_touch
    assert not r.h6_worse_than_actual_stop_net
    assert summarize(d)['stops']['post_stop_original_stop_level_touch']['numerator']==1
