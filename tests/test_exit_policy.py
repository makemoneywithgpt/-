"""Synthetic-only opposite-policy and corrected signal-population tests."""
import json

import numpy as np
import pandas as pd
import pytest

from backtesting.engine import Config
from backtesting.comparison_engine import run_filtered
from backtesting.exit_simulator import run_exit_policy
from backtesting.exit_policy import funnel, main
from backtesting.lab_strategies import NoTrade
from backtesting.strategy_lab import simulate
from backtesting.report import save


def bars(n=10):
    return pd.DataFrame(dict(open_time=pd.date_range('2023-01-01',periods=n,freq='h',tz='UTC'),
                             open=100.,high=101.,low=99.,close=100.,volume=1.))


class Signals:
    def __init__(self,schedule=None):self.schedule={1:1,3:-1,5:-1} if schedule is None else schedule
    def target(self,h):return self.schedule.get(len(h),0)


def kw(f):return dict(start=f.open_time.iloc[0],end=f.open_time.iloc[-1]+pd.Timedelta(hours=1))


@pytest.mark.parametrize('gate',[None,lambda h,t:len(h)<3])
def test_base_entire_path_matches_existing_engine(gate):
    f=bars();a=run_filtered(f,'BTCUSDT',Signals(),Config(),entry_filter=gate,**kw(f));events=[]
    b=run_exit_policy(f,'BTCUSDT',Signals(),Config(),entry_filter=gate,events=events,**kw(f))
    pd.testing.assert_frame_equal(a.trades,b.trades);pd.testing.assert_frame_equal(a.equity,b.equity)
    assert a.summary==b.summary
    assert sum(e['kind']=='filled' for e in events)==len(a.trades)


def test_close_only_no_immediate_or_deferred_reversal_fresh_signal_only():
    f=bars()
    r=run_exit_policy(f,'BTCUSDT',Signals(),Config(),exit_policy='close_only',**kw(f))
    assert r.trades.iloc[0].reason=='signal'
    assert r.trades.iloc[0].exit_time==f.open_time.iloc[3]
    assert len(r.trades)==2
    assert r.trades.iloc[1].entry_time==f.open_time.iloc[5]
    assert r.trades.iloc[1].side=='short'
    r=run_exit_policy(f,'BTCUSDT',Signals({1:1,3:-1}),Config(),exit_policy='close_only',**kw(f))
    assert len(r.trades)==1


def test_ignore_no_new_entry_while_held():
    f=bars()
    events=[]
    r=run_exit_policy(f,'BTCUSDT',Signals(),Config(),exit_policy='ignore_opposite_until_flat',events=events,**kw(f))
    assert len(r.trades)==1 and r.trades.iloc[0].side=='long'
    assert r.trades.iloc[0].reason=='end_of_data'
    assert len([e for e in events if e['kind']=='filled'])==1
    assert len([e for e in events if e['kind']=='opposite_while_held'])==2


@pytest.mark.parametrize('policy',['base','close_only','ignore_opposite_until_flat'])
@pytest.mark.parametrize('case',['gap','both','target','timeout'])
def test_priority_risk_time_and_new_entry_rules(policy,case):
    f=bars(80)
    if case=='gap':f.loc[3,['open','high','low','close']]=[80.,81.,79.,80.]
    elif case=='both':f.loc[2,['high','low']]=[110.,90.]
    elif case=='target':f.loc[2,['high','low']]=[110.,99.]
    schedule={1:1,3:-1} if case!='timeout' else {1:1,73:-1}
    r=run_exit_policy(f,'BTCUSDT',Signals(schedule),Config(),exit_policy=policy,**kw(f))
    assert r.trades.iloc[0].reason=={'gap':'stop_gap','both':'stop','target':'take_profit','timeout':'max_holding'}[case]
    if case in ('gap','timeout'):
        assert len(r.trades)==1 # pending signal canceled by risk/time exit
    if case=='both':assert r.trades.iloc[0].exit_price<100
    if case=='timeout':assert r.trades.iloc[0].holding_hours==72


def test_raw_admitted_and_filled_populations_differ_and_markouts():
    f=bars();f['close']=np.arange(100.,110.);f['high']=f.close+1;f['open']=f.close;f['low']=f.close-1;f['close']+=.5
    events=[]
    r=run_exit_policy(f,'BTCUSDT',Signals({1:1,2:1,3:-1}),Config(),entry_filter=lambda h,t:len(h)!=3,events=events,**kw(f))
    stats=funnel(events,f)
    assert stats['raw']['count']==3 and stats['admitted']['count']==2 and stats['filled']['count']==1
    assert stats['raw']['forward_returns'][0]['samples']==3
    assert stats['admitted']['forward_returns'][0]['samples']==2
    assert stats['filled']['forward_returns'][0]['samples']==1
    assert stats['raw']['forward_returns'][0]['mean_directional_return_pct']!=stats['admitted']['forward_returns'][0]['mean_directional_return_pct']
    assert r.trades.iloc[0].reason=='signal' # rejected reverse still closes


def test_causal_filter_and_future_changes_only_affect_posthoc_markouts():
    f=bars(20);g=f.copy();g.loc[12:,['open','high','low','close']]=[110.,111.,109.,110.]
    def gate(h,t):
        assert (h.open_time+pd.Timedelta(hours=1)<=t).all()
        return h.close.iloc[-1]>0
    a=[];b=[]
    ra=run_exit_policy(f,'BTCUSDT',Signals(),Config(),entry_filter=gate,events=a,**kw(f))
    rb=run_exit_policy(g,'BTCUSDT',Signals(),Config(),entry_filter=gate,events=b,**kw(g))
    assert a==b
    pd.testing.assert_frame_equal(ra.equity.iloc[:12],rb.equity.iloc[:12])


def test_no_trade_benchmark_and_invalid_policy():
    f=bars()
    for p in ['base','close_only','ignore_opposite_until_flat']:
        r=run_exit_policy(f,'BTCUSDT',NoTrade(),Config(),exit_policy=p,**kw(f))
        assert r.summary['net_return_pct']==r.summary['total_fees']==r.summary['max_drawdown_pct']==0
        assert r.trades.empty
    with pytest.raises(ValueError):run_exit_policy(f,'BTCUSDT',Signals(),Config(),exit_policy='bad',**kw(f))


def setup(root,symbol):
    f=bars(240);price=100+np.sin(np.arange(240)/10)*5
    f['open']=price;f['close']=price;f['high']=price+.05;f['low']=price-.05
    for name,r in simulate(f,symbol,Config(),**kw(f)).items():save(r,root/'lab'/symbol/name)
    p=root/'data/klines'/symbol;p.mkdir(parents=True);f.to_parquet(p/'1h.parquet')


def test_cli_both_symbols_readonly_report_and_reexecution(tmp_path):
    setup(tmp_path,'BTCUSDT');setup(tmp_path,'ETHUSDT')
    original={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    args=['--data-dir',str(tmp_path/'data'),'--lab-dir',str(tmp_path/'lab'),'--output-dir',str(tmp_path/'exit')]
    assert main(args)==0
    for p,v in original.items():assert p.read_bytes()==v
    for symbol in ['BTCUSDT','ETHUSDT']:
        p=tmp_path/'exit'/symbol
        report=json.loads((p/'exit_policy.json').read_text())
        assert 'raw/admitted/filled' in report['signal_statistics_correction']
        assert report['confirmed_facts'] and report['hypotheses']
        for strategy in report['strategies']:
            assert set(report['strategies'][strategy])=={'base','close_only','ignore_opposite_until_flat'}
            assert (p/strategy/'base/signal_events.csv').exists()
    snapshot={p:p.read_bytes() for p in (tmp_path/'exit').rglob('*') if p.is_file()}
    assert main(args)==1
    for p,v in snapshot.items():assert p.read_bytes()==v
    assert main(args[:-2]+['--output-dir',str(tmp_path/'lab')])==1


def test_failed_base_does_not_emit_success(tmp_path,capsys):
    setup(tmp_path,'BTCUSDT')
    p=tmp_path/'lab/BTCUSDT/B_sma_filtered/trades.csv';t=pd.read_csv(p);t.loc[0,'reason']='changed';t.to_csv(p,index=False)
    assert main(['--symbols','BTCUSDT','--data-dir',str(tmp_path/'data'),'--lab-dir',str(tmp_path/'lab'),'--output-dir',str(tmp_path/'exit')])==1
    result=json.loads((tmp_path/'exit/comparison.json').read_text())
    assert result['status']=='partial_or_failed'
    assert '정책 비교 완료' not in capsys.readouterr().out
    assert not (tmp_path/'exit/BTCUSDT/report.md').exists()


@pytest.mark.parametrize('fresh',[False,True])
def test_ignore_can_enter_only_after_flat_with_fresh_signal(fresh):
    f=bars();f.loc[3,['low','high']]=[90.,101.]
    schedule={1:1,2:-1}
    if fresh:schedule[4]=-1
    r=run_exit_policy(f,'BTCUSDT',Signals(schedule),Config(),exit_policy='ignore_opposite_until_flat',**kw(f))
    assert r.trades.iloc[0].reason=='stop'
    assert len(r.trades)==(2 if fresh else 1)
    if fresh:
        assert r.trades.iloc[1].side=='short' and r.trades.iloc[1].entry_time==f.open_time.iloc[4]
