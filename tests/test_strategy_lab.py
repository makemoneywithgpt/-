"""All prices and reports in these tests are synthetic, never user data."""
from dataclasses import asdict
import json

import numpy as np
import pandas as pd
import pytest

from backtesting.lab_strategies import Donchian20, NoTrade
from backtesting.strategy_lab import simulate, research_symbol, main, summarize
from backtesting.engine import Config, run
from backtesting.strategy import SMACrossover
from backtesting.experiments import compare_symbol
from backtesting.report import save


def bars(n=320):
    p=100+np.sin(np.arange(n)/12)*5
    return pd.DataFrame(dict(open_time=pd.date_range('2023-01-01',periods=n,freq='h',tz='UTC'),
                             open=p,high=p+.05,low=p-.05,close=p,volume=1.))


@pytest.mark.parametrize('n',[0,1,19,20])
def test_donchian_warmup(n):
    assert Donchian20().target(bars(n))==0


@pytest.mark.parametrize('price,signal',[(101.,0),(99.,0),(101.1,1),(98.9,-1),(100.,0)])
def test_donchian_excludes_current_and_strict_boundaries(price,signal):
    f=bars(21);f[['open','close']]=100.;f['high']=101.;f['low']=99.
    f.loc[20,['close','high','low']]=[price,10000.,.01]
    assert Donchian20().target(f)==signal


def test_future_values_changes_and_removal_leave_signals_unchanged():
    f=bars();before=[Donchian20().target(f.iloc[:i]) for i in range(1,241)]
    f.loc[240:,['open','high','low','close']]=float('nan')
    assert [Donchian20().target(f.iloc[:i]) for i in range(1,241)]==before
    assert [Donchian20().target(f.iloc[:240].iloc[:i]) for i in range(1,241)]==before


def test_next_open_and_no_signal_bar_fill():
    f=bars(24);f[['open','close']]=100.;f['high']=101.;f['low']=99.
    f.loc[20,['close','high']]=[102.,103.]
    f.loc[21:,['open','high','low','close']]=[105.,106.,104.,105.]
    result=run(f,'BTCUSDT',Donchian20(),Config(),start=f.open_time.iloc[0],end=f.open_time.iloc[-1]+pd.Timedelta(hours=1))
    assert result.trades.iloc[0].entry_time==f.open_time.iloc[21]
    assert result.trades.iloc[0].entry_price==pytest.approx(105*(1+Config().slippage))


def test_all_configs_independent_cash_and_causal_equity():
    f=bars();kwargs=dict(config=Config(),start=f.open_time.iloc[0],end=f.open_time.iloc[-1]+pd.Timedelta(hours=1))
    a=simulate(f,'BTCUSDT',**kwargs)
    for r in a.values():assert r.summary['config']==asdict(Config())
    cash=a['E_cash'];assert cash.trades.empty
    assert cash.summary['net_return_pct']==cash.summary['total_fees']==cash.summary['max_drawdown_pct']==0
    assert cash.equity.equity.eq(10000).all()
    assert not a['C_donchian'].trades.empty
    g=f.copy();g.loc[260:,['open','high','low','close']]=[200.,201.,199.,200.]
    b=simulate(g,'BTCUSDT',**kwargs)
    for key in a:pd.testing.assert_frame_equal(a[key].equity.iloc[:260],b[key].equity.iloc[:260])


def setup(root,symbol='BTCUSDT',n=320):
    f=bars(n);start=f.open_time.iloc[0];end=f.open_time.iloc[-1]+pd.Timedelta(hours=1)
    r=run(f,symbol,SMACrossover(),Config(),start=start,end=end)
    r.summary['strategy_parameters']={'fast':20,'slow':50}
    save(r,root/'backtests'/symbol)
    p=root/'data/klines'/symbol;p.mkdir(parents=True);f.to_parquet(p/'1h.parquet')
    return r


@pytest.mark.parametrize('symbol',['BTCUSDT','ETHUSDT'])
def test_ab_v04_a_v01_full_paths_match_and_double_costs(tmp_path,symbol):
    setup(tmp_path,symbol)
    a,b,_=compare_symbol(tmp_path/'data',tmp_path/'backtests',symbol)
    save(a,tmp_path/'v04'/symbol/'baseline');save(b,tmp_path/'v04'/symbol/'non_neutral_entry')
    result,sensitivity,report=research_symbol(tmp_path/'data',tmp_path/'backtests',tmp_path/'v04',symbol,True)
    pd.testing.assert_frame_equal(result['A_sma'].trades,a.trades)
    pd.testing.assert_frame_equal(result['B_sma_filtered'].equity,b.equity)
    assert '대조 통과' in report['reference_validation']
    assert len(result)==len(sensitivity)==5
    for key in sensitivity:
        assert sensitivity[key].summary['config']['fee_rate']==.0008
        assert sensitivity[key].summary['config']['slippage']==.0004
    assert report['policies']['E_cash']['max_consecutive_losses']==0


def test_v04_mismatch_detected(tmp_path):
    setup(tmp_path)
    a,b,_=compare_symbol(tmp_path/'data',tmp_path/'backtests','BTCUSDT')
    save(a,tmp_path/'v04/BTCUSDT/baseline');save(b,tmp_path/'v04/BTCUSDT/non_neutral_entry')
    p=tmp_path/'v04/BTCUSDT/non_neutral_entry/trades.csv';t=pd.read_csv(p)
    t.loc[0,'reason']='changed';t.to_csv(p,index=False)
    with pytest.raises(ValueError,match='v0.4 결과 불일치'):
        research_symbol(tmp_path/'data',tmp_path/'backtests',tmp_path/'v04','BTCUSDT')


@pytest.mark.parametrize('fault',['gap','invalid'])
def test_bad_price_data(tmp_path,fault):
    setup(tmp_path);p=tmp_path/'data/klines/BTCUSDT/1h.parquet';f=pd.read_parquet(p)
    if fault=='gap':f=f.drop(index=100)
    else:f.loc[100,'high']=1.
    f.to_parquet(p)
    with pytest.raises(ValueError):research_symbol(tmp_path/'data',tmp_path/'backtests',tmp_path/'missing','BTCUSDT')


def test_zero_trades_and_reference_missing_explicitly_reported(tmp_path):
    setup(tmp_path,n=20)
    result,_,report=research_symbol(tmp_path/'data',tmp_path/'backtests',tmp_path/'missing','BTCUSDT')
    assert all(r.trades.empty for r in result.values())
    assert '미실행' in report['reference_validation']
    assert all(not p['below_cash_benchmark'] for p in report['policies'].values())


def test_cli_structure_preservation_existing_outputs_and_partial_errors(tmp_path):
    setup(tmp_path,'BTCUSDT');setup(tmp_path,'ETHUSDT')
    original={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    args=['--data-dir',str(tmp_path/'data'),'--input-dir',str(tmp_path/'backtests'),
          '--v04-dir',str(tmp_path/'missing'),'--output-dir',str(tmp_path/'lab')]
    assert main(args)==0
    for p,data in original.items():assert p.read_bytes()==data
    for symbol in ['BTCUSDT','ETHUSDT']:
        for policy in ['A_sma','B_sma_filtered','C_donchian','D_donchian_filtered','E_cash']:
            assert (tmp_path/'lab'/symbol/policy/'yearly.csv').exists()
    files={p:p.read_bytes() for p in (tmp_path/'lab').rglob('*') if p.is_file()}
    assert main(args)==1
    for p,data in files.items():assert p.read_bytes()==data
    assert main(args[:-2]+['--output-dir',str(tmp_path/'backtests')])==1
    (tmp_path/'backtests/ETHUSDT/summary.json').unlink()
    assert main(args[:-2]+['--output-dir',str(tmp_path/'partial')])==1
    assert json.loads((tmp_path/'partial/comparison.json').read_text())['status']=='partial_or_failed'
