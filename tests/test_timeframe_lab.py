"""Synthetic-only UTC aggregation, causal 4h signals and 1h execution tests."""
import json

import numpy as np
import pandas as pd
import pytest

from backtesting.timeframe_signals import build_four_hour, ClosedSignal
from backtesting.timeframe_lab import execute, slippage_ledger, main
from backtesting.strategy import SMACrossover
from backtesting.lab_strategies import Donchian20, NoTrade
from backtesting.exit_simulator import run_exit_policy
from backtesting.engine import Config
from backtesting.experiments import allow_non_neutral
from backtesting.report import save


def bars(n=40):
    return pd.DataFrame(dict(open_time=pd.date_range('2023-01-01',periods=n,freq='h',tz='UTC'),
                             open=100.,high=101.,low=99.,close=100.,volume=1.))


def kw(f):return dict(start=f.open_time.iloc[0],end=f.open_time.iloc[-1]+pd.Timedelta(hours=1))


class Always:
    def target(self,h):return 1


def test_utc_aggregation_and_partial_buckets():
    f=bars(13);f.loc[:3,'open']=[100.,101.,102.,103.]
    f.loc[:3,'high']=[101.,102.,103.,104.];f.loc[:3,'close']=[100.,101.,102.,103.]
    result=build_four_hour(f)
    assert len(result)==3
    a=result.iloc[0]
    assert (a.open,a.high,a.low,a.close,a.volume)==(100.,104.,99.,103.,4.)
    assert a.available_at==f.open_time.iloc[4]
    assert len(build_four_hour(f.iloc[1:]))==2 # leading/trailing incomplete groups dropped
    assert build_four_hour(f.iloc[:3]).empty


@pytest.mark.parametrize('fault',['missing','duplicate','reverse','unaligned','unfinished','invalid'])
def test_broken_input_never_generates_4h(fault):
    f=bars()
    if fault=='missing':f=f.drop(index=5)
    elif fault=='duplicate':f=pd.concat([f,f.iloc[-1:]])
    elif fault=='reverse':f=f.iloc[::-1]
    elif fault=='unaligned':f.loc[0,'open_time']+=pd.Timedelta(minutes=1)
    elif fault=='invalid':f.loc[5,'low']=-1
    else:f['open_time']=pd.date_range(pd.Timestamp.now(tz='UTC').floor('h'),periods=len(f),freq='h')
    with pytest.raises(ValueError):build_four_hour(f)


def test_4h_signal_once_at_close_and_next_1h_open_fill():
    f=bars(16);adapter=ClosedSignal(Always(),4,f)
    signals=[adapter.target(f.iloc[:i]) for i in range(1,len(f))]
    assert signals==[int(i%4==0) for i in range(1,len(f))]
    r=run_exit_policy(f,'BTCUSDT',adapter,Config(),**kw(f))
    assert r.trades.iloc[0].entry_time==f.open_time.iloc[4]
    assert r.trades.iloc[0].entry_price==pytest.approx(100*(1+Config().slippage))


def test_4h_filter_uses_only_200_closed_4h_bars():
    f=bars(810);price=100+np.arange(len(f))*.1
    f['open']=price;f['close']=price;f['high']=price+1;f['low']=price-1
    adapter=ClosedSignal(Always(),4,f)
    assert not adapter.entry_filter(f.iloc[:796],f.open_time.iloc[796])
    assert adapter.entry_filter(f.iloc[:800],f.open_time.iloc[800])
    assert not adapter.entry_filter(f.iloc[:801],f.open_time.iloc[801])
    g=f.copy();g.loc[800:,['open','high','low','close']]=[1000.,1001.,999.,1000.]
    altered=ClosedSignal(Always(),4,g)
    assert altered.entry_filter(g.iloc[:800],g.open_time.iloc[800])==adapter.entry_filter(f.iloc[:800],f.open_time.iloc[800])


@pytest.mark.parametrize('case',['stop','target','gap','both','timeout'])
def test_1h_risk_management_between_4h_signal_boundaries(case):
    f=bars(85)
    if case=='stop':f.loc[5,'low']=95.
    elif case=='target':f.loc[5,'high']=110.
    elif case=='gap':f.loc[5,['open','high','low','close']]=[80.,81.,79.,80.]
    elif case=='both':f.loc[5,['high','low']]=[110.,90.]
    r=run_exit_policy(f,'BTCUSDT',ClosedSignal(Always(),4,f),Config(),**kw(f))
    t=r.trades.iloc[0]
    assert t.reason=={'stop':'stop','target':'take_profit','gap':'stop_gap','both':'stop','timeout':'max_holding'}[case]
    if case=='timeout':assert t.holding_hours==72 and t.exit_time==f.open_time.iloc[76]
    elif case=='gap':assert t.exit_time==f.open_time.iloc[5]
    else:assert t.exit_time==f.open_time.iloc[6]
    assert r.summary['net_pnl']==pytest.approx(r.trades.net_pnl.sum())
    assert np.allclose(r.equity.cash+r.equity.unrealized_pnl,r.equity.equity)


@pytest.mark.parametrize('name,strategy',[('B_sma_filtered',SMACrossover()),('D_donchian_filtered',Donchian20())])
@pytest.mark.parametrize('policy',['base','close_only'])
def test_1h_adapter_full_path_parity(name,strategy,policy):
    f=prices(900)
    original=run_exit_policy(f,'BTCUSDT',strategy,Config(),entry_filter=allow_non_neutral,exit_policy=policy,**kw(f))
    adapted,events=execute(f,'BTCUSDT',Config(),name,1,policy)
    pd.testing.assert_frame_equal(original.trades,adapted.trades)
    pd.testing.assert_frame_equal(original.equity,adapted.equity)
    assert len(events)>0


def prices(n=900):
    f=bars(n);p=100+np.sin(np.arange(n)/40)*10
    f['open']=p;f['close']=p;f['high']=p+.05;f['low']=p-.05
    return f


def test_future_changed_or_removed_signals_and_equity_stay_causal():
    f=prices(900);g=f.copy();g.loc[840:,['open','high','low','close']]=[200.,201.,199.,200.]
    a=ClosedSignal(Donchian20(),4,f);b=ClosedSignal(Donchian20(),4,g);c=ClosedSignal(Donchian20(),4,f.iloc[:840])
    for i in range(1,841):
        assert a.target(f.iloc[:i])==b.target(g.iloc[:i])==c.target(f.iloc[:i])
    ra,_=execute(f,'BTCUSDT',Config(),'D_donchian_filtered',4,'base')
    rb,_=execute(g,'BTCUSDT',Config(),'D_donchian_filtered',4,'base')
    pd.testing.assert_frame_equal(ra.equity.iloc[:840],rb.equity.iloc[:840])


def test_modeled_slippage_and_cash_reference():
    f=bars(16);r=run_exit_policy(f,'BTCUSDT',ClosedSignal(Always(),4,f),Config(),**kw(f))
    t=r.trades.iloc[0];ledger=slippage_ledger(r.trades,.0002)
    assert ledger.modeled_fill_adjustment.iloc[0]==pytest.approx(t.quantity*(100*.0002+100*.0002))
    cash=run_exit_policy(f,'BTCUSDT',NoTrade(),Config(),**kw(f))
    assert cash.summary['net_return_pct']==cash.summary['max_drawdown_pct']==cash.summary['total_fees']==0


def setup(root,symbol):
    f=prices(900)
    for name,strategy in [('B_sma_filtered',SMACrossover()),('D_donchian_filtered',Donchian20())]:
        for policy in ['base','close_only']:
            r=run_exit_policy(f,symbol,strategy,Config(),entry_filter=allow_non_neutral,exit_policy=policy,**kw(f))
            save(r,root/'v07'/symbol/name/policy)
    p=root/'data/klines'/symbol;p.mkdir(parents=True);f.to_parquet(p/'1h.parquet')


def test_cli_both_symbols_file_protection_and_failure(tmp_path,capsys):
    setup(tmp_path,'BTCUSDT');setup(tmp_path,'ETHUSDT')
    original={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    args=['--data-dir',str(tmp_path/'data'),'--v07-dir',str(tmp_path/'v07'),'--output-dir',str(tmp_path/'timeframe')]
    assert main(args)==0
    for p,v in original.items():assert p.read_bytes()==v
    for symbol in ['BTCUSDT','ETHUSDT']:
        report=json.loads((tmp_path/'timeframe'/symbol/'timeframe.json').read_text())
        assert len(report['cases'])==9 and report['execution_hours']==1
        assert report['cases']['cash']['summary']['net_return_pct']==0
        for name,case in report['cases'].items():
            if '_1h_' in name:assert case['v07_path_verified']
    existing={p:p.read_bytes() for p in (tmp_path/'timeframe').rglob('*') if p.is_file()}
    assert main(args)==1
    for p,v in existing.items():assert p.read_bytes()==v
    assert main(args[:-2]+['--output-dir',str(tmp_path/'v07')])==1
    capsys.readouterr()
    p=tmp_path/'v07/BTCUSDT/B_sma_filtered/base/trades.csv';t=pd.read_csv(p);t.loc[0,'reason']='bad';t.to_csv(p,index=False)
    assert main(args[:-2]+['--symbols','BTCUSDT','--output-dir',str(tmp_path/'failure')])==1
    assert '시간축 비교 완료' not in capsys.readouterr().out
    assert not (tmp_path/'failure/BTCUSDT/report.md').exists()
    assert json.loads((tmp_path/'failure/comparison.json').read_text())['status']=='partial_or_failed'
