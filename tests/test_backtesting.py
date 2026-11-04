"""Only synthetic prices; no real market data or network access."""
import json

import numpy as np
import pandas as pd
import pytest

from backtesting.data import DataError, validate
from backtesting.engine import Config, run
from backtesting.strategy import SMACrossover
from backtesting.cli import main
from backtesting.report import save

START = pd.Timestamp('2023-01-01', tz='UTC')
NOW = pd.Timestamp('2024-01-01', tz='UTC')


def bars(n=8, price=100.):
    return pd.DataFrame({'open_time': pd.date_range(START, periods=n, freq='h'),
                         'open': price, 'high': price+1, 'low': price-1,
                         'close': price, 'volume': 10.})


class Schedule:
    def __init__(self, targets): self.targets = targets
    def target(self, completed): return self.targets.get(len(completed)-1, 0)


def simulate(f=None, strategy=None, **kw):
    f = bars() if f is None else f
    return run(f, 'BTCUSDT', strategy or Schedule({0: 1}), Config(**kw),
               start=START, end=START+pd.Timedelta(hours=len(f)), now=NOW)


def test_sma_warmup_cross_and_prefix_invariance():
    f=bars(8); f['close']=[5.,4.,3.,2.,3.,5.,6.,1.]
    s=SMACrossover(2,3)
    targets=[s.target(f.iloc[:i+1]) for i in range(len(f))]
    assert targets[:4] == [0,0,0,0]
    assert targets[5] == 1 and targets[-1] == -1
    altered=f.copy(); altered.loc[6:,'close']=1000.
    assert [s.target(altered.iloc[:i+1]) for i in range(6)] == targets[:6]


def test_strategy_never_receives_future_and_next_open_execution():
    class Spy:
        def __init__(self): self.lengths=[]
        def target(self, history):
            self.lengths.append(len(history)); return 1
    f=bars(4); f.loc[1,['open','high','low','close']]=[110.,111.,109.,110.]
    s=Spy(); r=simulate(f,s,stop_pct=.5,take_profit_pct=.5,fee_rate=0,slippage=0)
    t=r.trades.iloc[0]
    assert t.entry_time == START+pd.Timedelta(hours=1)
    assert t.entry_price == 110 and s.lengths == [1,2,3]


@pytest.mark.parametrize('side', [1,-1])
def test_fees_slippage_and_net_pnl_both_sides(side):
    r=simulate(bars(4),Schedule({0:side}),fee_rate=.001,slippage=.002,stop_pct=.2,take_profit_pct=.2)
    t=r.trades.iloc[0]
    assert t.entry_price == pytest.approx(100*(1+side*.002))
    assert t.exit_price == pytest.approx(100*(1-side*.002))
    fees=t.quantity*(t.entry_price+t.exit_price)*.001
    assert t.fees == pytest.approx(fees)
    assert t.net_pnl == pytest.approx(side*t.quantity*(t.exit_price-t.entry_price)-fees)
    assert r.summary['net_pnl'] == pytest.approx(t.net_pnl)
    assert t.net_pnl < 0


def test_risk_budget_and_leverage_cap_include_costs():
    c=Config(initial_capital=10000,risk_fraction=.001,stop_pct=.02,fee_rate=.001,slippage=.001)
    r=simulate(fee_rate=c.fee_rate,slippage=c.slippage,risk_fraction=c.risk_fraction,stop_pct=c.stop_pct)
    t=r.trades.iloc[0]
    stop=t.entry_price*(1-c.stop_pct)*(1-c.slippage)
    worst=t.quantity*((t.entry_price-stop)+c.fee_rate*(t.entry_price+stop))
    assert worst <= c.initial_capital*c.risk_fraction+1e-8
    r=simulate(risk_fraction=1,stop_pct=.01,max_leverage=.5)
    t=r.trades.iloc[0]
    assert t.quantity*t.entry_price <= 5000+1e-8


def test_72_hour_exit_and_no_immediate_reentry():
    r=simulate(bars(80),Schedule({0:1}),fee_rate=0,slippage=0)
    t=r.trades.iloc[0]
    assert t.reason == 'max_holding'
    assert (t.exit_time-t.entry_time)/pd.Timedelta(hours=1) == 72
    assert len(r.trades)==1


@pytest.mark.parametrize('side,opening,expected',[(1,80.,80.),(-1,120.,120.)])
def test_stop_gap_uses_adverse_open(side,opening,expected):
    f=bars(4); f.loc[2,['open','high','low','close']]=[opening,opening+1,opening-1,opening]
    r=simulate(f,Schedule({0:side}),fee_rate=0,slippage=0,stop_pct=.05,take_profit_pct=.5)
    assert r.trades.iloc[0].exit_price == expected
    assert r.trades.iloc[0].reason == 'stop_gap'


@pytest.mark.parametrize('side', [1,-1])
def test_both_touches_stop_first(side):
    f=bars(3); f.loc[1,['high','low']]=[120.,80.]
    t=simulate(f,Schedule({0:side}),fee_rate=0,slippage=0,stop_pct=.05,take_profit_pct=.05).trades.iloc[0]
    assert t.reason=='stop' and t.exit_price==100*(1-side*.05)


def test_reverse_closes_and_opens_only_one_position():
    r=simulate(bars(5),Schedule({0:1,1:-1}),fee_rate=0,slippage=0)
    assert list(r.trades.side)==['long','short']
    assert r.trades.iloc[0].exit_time == r.trades.iloc[1].entry_time
    assert r.trades.iloc[0].reason=='signal'


def test_deterministic_and_equity_drawdown():
    f=bars(5); f.loc[2:,['open','high','low','close']]=[95.,96.,94.,95.]
    a=simulate(f,fee_rate=0,slippage=0,stop_pct=.5,take_profit_pct=.5)
    b=simulate(f,fee_rate=0,slippage=0,stop_pct=.5,take_profit_pct=.5)
    pd.testing.assert_frame_equal(a.trades,b.trades)
    pd.testing.assert_frame_equal(a.equity,b.equity)
    assert a.summary==b.summary
    assert a.summary['max_drawdown_pct']>0
    assert a.equity.iloc[-1].equity == pytest.approx(10000+a.summary['net_pnl'])


def test_missing_hours_report_ranges():
    f=bars(6).drop(index=[2,3])
    with pytest.raises(DataError,match='2023-01-01T02:00:00'):
        validate(f,START,START+pd.Timedelta(hours=6),now=NOW)


@pytest.mark.parametrize('kind', ['duplicate','nan','infinity','negative','ohlc','unaligned','unfinished'])
def test_reject_bad_candles(kind):
    f=bars(4); now=NOW
    if kind=='duplicate': f=pd.concat([f,f.iloc[:1]],ignore_index=True)
    elif kind=='nan': f.loc[0,'close']=np.nan
    elif kind=='infinity': f.loc[0,'volume']=np.inf
    elif kind=='negative': f.loc[0,'volume']=-1
    elif kind=='ohlc': f.loc[0,'high']=90
    elif kind=='unaligned': f.loc[0,'open_time']+=pd.Timedelta(minutes=1)
    else: now=START+pd.Timedelta(hours=3,minutes=30)
    with pytest.raises(DataError): validate(f,START,START+pd.Timedelta(hours=4),now=now)


def test_timezone_sort_and_empty():
    f=bars(4).iloc[::-1].copy(); f['open_time']=f.open_time.dt.tz_convert('Asia/Seoul')
    result=validate(f,START,START+pd.Timedelta(hours=4),now=NOW)
    assert result.open_time.is_monotonic_increasing
    with pytest.raises(DataError): validate(bars(0),START,START+pd.Timedelta(hours=4),now=NOW)


@pytest.mark.parametrize('kwargs',[{'max_leverage':2},{'max_holding_hours':73},{'risk_fraction':0}, {'fee_rate':-1},{'stop_pct':0},{'slippage':float('nan')},{'initial_capital':0}])
def test_invalid_config(kwargs):
    with pytest.raises(ValueError): Config(**kwargs)


def test_no_trades_report_and_funding_warning(tmp_path):
    r=simulate(bars(5),Schedule({}))
    save(r,tmp_path)
    s=json.loads((tmp_path/'summary.json').read_text())
    assert s['total_trades']==0 and s['net_pnl']==0 and s['profit_factor'] is None
    assert '펀딩비' in s['warnings'][0]
    assert len(pd.read_csv(tmp_path/'equity.csv'))==5
    assert pd.read_csv(tmp_path/'trades.csv').empty


def test_cli_reports_and_failure_exit(tmp_path,capsys):
    data=tmp_path/'data'/'klines'/'BTCUSDT'; data.mkdir(parents=True)
    f=bars(80); f.to_parquet(data/'1h.parquet')
    args=['--data-dir',str(tmp_path/'data'),'--output-dir',str(tmp_path/'out'),'--symbols','BTCUSDT',
          '--start','2023-01-01','--end','2023-01-04T08:00:00Z']
    assert main(args)==0
    assert (tmp_path/'out'/'BTCUSDT'/'summary.json').exists()
    f.drop(index=20).to_parquet(data/'1h.parquet')
    assert main(args)==1
    assert '누락' in capsys.readouterr().err


def test_engine_causality_when_future_prices_change():
    f=bars(12)
    f['close']=[100.,99.,98.,97.,99.,102.,103.,104.,105.,106.,105.,104.]
    f['open']=f.close; f['high']=f.close+1; f['low']=f.close-1
    g=f.copy(); g.loc[8:,['open','high','low','close']]=[200.,201.,199.,200.]
    a=simulate(f,SMACrossover(2,3),stop_pct=.2,take_profit_pct=.5)
    b=simulate(g,SMACrossover(2,3),stop_pct=.2,take_profit_pct=.5)
    pd.testing.assert_frame_equal(a.equity.iloc[:8],b.equity.iloc[:8])
    assert a.trades.iloc[0].entry_time >= START+pd.Timedelta(hours=4)


def test_take_profit_and_accounting_metrics():
    f=bars(4); f.loc[2,['open','high','low','close']]=[100.,110.,99.,106.]
    r=simulate(f,Schedule({0:1}),fee_rate=0,slippage=0,stop_pct=.05,take_profit_pct=.05)
    t=r.trades.iloc[0]
    assert t.reason=='take_profit' and t.exit_price==105
    assert r.summary['win_rate_pct']==100
    assert r.summary['net_pnl']==pytest.approx(t.net_pnl)
    assert r.summary['average_pnl']==pytest.approx(t.net_pnl)
    assert r.summary['net_return_pct']==pytest.approx(t.net_pnl/10000*100)
    assert r.summary['profit_factor'] is None


def test_profit_factor_uses_net_trade_results():
    f=bars(5); f.loc[2,['open','high','low','close']]=[100.,106.,99.,100.]
    f.loc[3,['open','high','low','close']]=[100.,101.,94.,100.]
    r=simulate(f,Schedule({0:1,2:1}),fee_rate=0,slippage=0,stop_pct=.05,take_profit_pct=.05)
    assert list(r.trades.reason)==['take_profit','stop']
    # Risk sizing compounds: first gain 100, then 1% of 10,100 loses 101.
    assert r.summary['profit_factor']==pytest.approx(100/101)
    assert r.summary['net_pnl']==pytest.approx(-1)
    assert r.summary['win_rate_pct']==50


def test_funding_adapter_accounting():
    class FixedFunding:
        label='synthetic_test_only'
        def cost(self, **kwargs): return 2.
    r=run(bars(4),'BTCUSDT',Schedule({0:1}),Config(fee_rate=0,slippage=0),
          start=START,end=START+pd.Timedelta(hours=4),now=NOW,funding=FixedFunding())
    assert r.summary['total_funding_cost']==6
    assert r.summary['net_pnl']==-6
    assert r.trades.iloc[0].net_pnl==-6


@pytest.mark.parametrize('field,value',[('trades',1.5),('taker_buy_volume',11.),('open',0.)])
def test_optional_market_columns_and_zero_price(field,value):
    f=bars(4); f[field]=value
    with pytest.raises(DataError): validate(f,START,START+pd.Timedelta(hours=4),now=NOW)


def test_cli_independent_symbols_and_stale_report_handling(tmp_path):
    data=tmp_path/'data'/'klines'; (data/'ETHUSDT').mkdir(parents=True)
    bars(80).to_parquet(data/'ETHUSDT'/'1h.parquet')
    out=tmp_path/'out'; (out/'BTCUSDT').mkdir(parents=True)
    (out/'BTCUSDT'/'summary.json').write_text('{"old":true}')
    assert main(['--data-dir',str(tmp_path/'data'),'--output-dir',str(out),
                 '--start','2023-01-01','--end','2023-01-04T08:00Z'])==1
    assert not (out/'BTCUSDT'/'summary.json').exists()
    assert (out/'BTCUSDT'/'error.json').exists()
    assert (out/'ETHUSDT'/'summary.json').exists()


def test_invalid_strategy_signal_rejected():
    with pytest.raises(ValueError,match='전략 신호'):
        simulate(strategy=Schedule({0:2}))


def test_single_hour_does_not_execute_unavailable_next_open():
    r=simulate(bars(1),Schedule({0:1}))
    assert r.trades.empty and r.summary['net_pnl']==0


def test_stop_gap_cancels_pending_reversal():
    f=bars(4); f.loc[2,['open','high','low','close']]=[80.,81.,79.,80.]
    r=simulate(f,Schedule({0:1,1:-1}),fee_rate=0,slippage=0,stop_pct=.05)
    assert len(r.trades)==1 and r.trades.iloc[0].reason=='stop_gap'
