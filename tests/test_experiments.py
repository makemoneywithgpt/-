"""Offline synthetic-policy parity and entry-only restriction tests."""
import json

import numpy as np
import pandas as pd
import pytest

from backtesting.engine import Config, run
from backtesting.strategy import SMACrossover
from backtesting.comparison_engine import run_filtered
from backtesting.experiments import allow_non_neutral, compare_symbol, main, policy_report, assert_baseline
from backtesting.report import save


def bars(n=320):
    p=100+np.sin(np.arange(n)/12)*5
    return pd.DataFrame(dict(open_time=pd.date_range('2023-01-01',periods=n,freq='h',tz='UTC'),
                             open=p,high=p+1,low=p-1,close=p,volume=1.))


def simulate(f,strategy=None,gate=None):
    kwargs=dict(start=f.open_time.iloc[0],end=f.open_time.iloc[-1]+pd.Timedelta(hours=1))
    if gate is None:return run(f,'BTCUSDT',strategy or SMACrossover(),Config(),**kwargs)
    return run_filtered(f,'BTCUSDT',strategy or SMACrossover(),Config(),entry_filter=gate,**kwargs)


def test_unchanged_engine_parity_when_gate_allows_everything():
    f=bars();a=simulate(f);b=simulate(f,gate=lambda h,t:True)
    pd.testing.assert_frame_equal(a.trades,b.trades)
    pd.testing.assert_frame_equal(a.equity,b.equity)
    assert a.summary==b.summary
    assert len(a.trades)>0


class Signals:
    def target(self,h):return {1:1,2:-1}.get(len(h),0)


def test_block_only_new_entry_keep_original_reversal_exit():
    f=bars(5);f[['open','close']]=100.;f['high']=101.;f['low']=99.
    a=simulate(f,Signals())
    seen=[]
    def gate(h,t):
        assert (h.open_time+pd.Timedelta(hours=1)<=t).all()
        seen.append(t)
        return len(h)==1
    b=simulate(f,Signals(),gate)
    assert len(a.trades)==2 and len(b.trades)==1
    assert b.trades.iloc[0].reason=='signal'
    assert a.trades.iloc[0].exit_time==b.trades.iloc[0].exit_time
    assert a.trades.iloc[0].net_pnl==b.trades.iloc[0].net_pnl
    assert len(seen)==2


@pytest.mark.parametrize('sign',[-1,1])
def test_fixed_neutral_boundary_and_warmup(sign):
    f=bars(205);f['close']=100.
    assert not allow_non_neutral(f.iloc[:199],f.open_time.iloc[199])
    f.loc[:149,'close']=100-sign/6;f.loc[150:199,'close']=100+sign*.5
    assert not allow_non_neutral(f.iloc[:200],f.open_time.iloc[200])
    f.loc[150:199,'close']+=sign*.01
    assert allow_non_neutral(f.iloc[:200],f.open_time.iloc[200])


def test_future_changes_and_removal_do_not_change_past_policy():
    f=bars();t=f.open_time.iloc[240]
    old=allow_non_neutral(f,t)
    f.loc[240:,['open','high','low','close']]=float('nan')
    assert allow_non_neutral(f,t)==old
    assert allow_non_neutral(f.iloc[:240],t)==old
    a=simulate(bars(),gate=allow_non_neutral)
    g=bars();g.loc[260:,['open','high','low','close']]=[200.,201.,199.,200.]
    b=simulate(g,gate=allow_non_neutral)
    pd.testing.assert_frame_equal(a.equity.iloc[:260],b.equity.iloc[:260])


def setup(root,symbol='BTCUSDT',n=320):
    f=bars(n);a=simulate(f);a.summary['symbol']=symbol
    a.summary['strategy_parameters']={'fast':20,'slow':50}
    save(a,root/'backtests'/symbol)
    p=root/'data/klines'/symbol;p.mkdir(parents=True);f.to_parquet(p/'1h.parquet')
    return a


@pytest.mark.parametrize('symbol',['BTCUSDT','ETHUSDT'])
def test_reference_match_and_same_conditions(tmp_path,symbol):
    setup(tmp_path,symbol)
    a,b,r=compare_symbol(tmp_path/'data',tmp_path/'backtests',symbol)
    assert r['baseline_verified']
    assert a.summary['config']==b.summary['config']
    assert a.summary['start']==b.summary['start'] and a.summary['end_exclusive']==b.summary['end_exclusive']
    assert len(b.trades)<len(a.trades)
    assert not b.trades.empty
    assert r['policies']['baseline']['yearly'][0]['partial']
    assert '미관측 OOS가 아닙니다' in r['notice']


def test_reference_mismatch_is_error(tmp_path):
    setup(tmp_path);p=tmp_path/'backtests/BTCUSDT/summary.json';s=json.loads(p.read_text())
    s['max_drawdown_pct']+=1;p.write_text(json.dumps(s))
    with pytest.raises(ValueError,match='baseline 불일치'):compare_symbol(tmp_path/'data',tmp_path/'backtests','BTCUSDT')


def test_reference_path_not_just_aggregate_checked(tmp_path):
    setup(tmp_path);p=tmp_path/'backtests/BTCUSDT/trades.csv';f=pd.read_csv(p)
    f.loc[0,'reason']='different';f.to_csv(p,index=False)
    with pytest.raises(ValueError,match='trades.csv'):compare_symbol(tmp_path/'data',tmp_path/'backtests','BTCUSDT')


def test_no_trades_and_missing_candle(tmp_path):
    setup(tmp_path,n=20)
    a,b,_=compare_symbol(tmp_path/'data',tmp_path/'backtests','BTCUSDT')
    assert a.trades.empty and b.trades.empty
    assert policy_report(a)['yearly'][0]['profit_factor'] is None
    p=tmp_path/'data/klines/BTCUSDT/1h.parquet';f=pd.read_parquet(p);f.drop(index=5).to_parquet(p)
    with pytest.raises(ValueError,match='누락'):compare_symbol(tmp_path/'data',tmp_path/'backtests','BTCUSDT')


def test_two_symbol_cli_preserves_all_inputs_and_existing_output(tmp_path):
    setup(tmp_path,'BTCUSDT');setup(tmp_path,'ETHUSDT')
    old={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    args=['--data-dir',str(tmp_path/'data'),'--input-dir',str(tmp_path/'backtests'),
          '--output-dir',str(tmp_path/'experiment')]
    assert main(args)==0
    for p,value in old.items():assert p.read_bytes()==value
    for symbol in ['BTCUSDT','ETHUSDT']:
        assert (tmp_path/'experiment'/symbol/'baseline/trades.csv').exists()
        assert (tmp_path/'experiment'/symbol/'non_neutral_entry/yearly_sides.csv').exists()
    outputs={p:p.read_bytes() for p in (tmp_path/'experiment').rglob('*') if p.is_file()}
    assert main(args)==1
    for p,value in outputs.items():assert p.read_bytes()==value
    assert main(args[:-2]+['--output-dir',str(tmp_path/'backtests')])==1


def test_failure_not_claimed_as_success(tmp_path):
    setup(tmp_path)
    args=['--data-dir',str(tmp_path/'data'),'--input-dir',str(tmp_path/'backtests'),
          '--output-dir',str(tmp_path/'experiment')]
    assert main(args)==1  # ETH missing, BTC still completes.
    result=json.loads((tmp_path/'experiment/comparison.json').read_text())
    assert result['status']=='partial_or_failed'
    assert (tmp_path/'experiment/ETHUSDT/error.json').exists()


def test_issue_fixed_full_window_reference_is_enforced(tmp_path):
    a=setup(tmp_path)
    # Synthetic result relabeled with the issue window MUST NOT pass as the
    # known actual baseline, even though its CSV and summary agree internally.
    a.summary['start']='2023-01-01T00:00:00Z'
    a.summary['end_exclusive']='2026-11-04T13:00:00Z'
    with pytest.raises(ValueError,match='고정 전체 기간 기준값'):
        assert_baseline(a,a.summary,a.equity,a.trades)
