"""Synthetic CSV tests only; no user prices or actual strategy returns."""
import json

import pandas as pd
import pytest

from backtesting.diagnostics import DiagnosticError, load, analyze, periods, drawdowns, main


def fixture(tmp_path, values=None, start='2023-12-31T22:00:00Z'):
    values=[100.,90.,80.,105.] if values is None else values
    start=pd.Timestamp(start);end=start+pd.Timedelta(hours=len(values))
    p=tmp_path/'BTCUSDT';p.mkdir(parents=True,exist_ok=True)
    e=pd.DataFrame({'time':pd.date_range(start+pd.Timedelta(hours=1),periods=len(values),freq='h'),
                    'equity':values,'cash':values,'unrealized_pnl':0.})
    pnl=values[-1]-100
    t=pd.DataFrame([dict(entry_time=start+pd.Timedelta(hours=1),exit_time=end,side='long',reason='signal',
                         net_pnl=pnl,gross_pnl=pnl+2,fees=2.,funding_cost=0.)])
    s=dict(symbol='BTCUSDT',start=start.isoformat(),end_exclusive=end.isoformat(),candle_count=len(e),
           total_trades=1,net_pnl=pnl,final_equity=values[-1],total_fees=2.,config={'initial_capital':100.})
    e.to_csv(p/'equity.csv',index=False);t.to_csv(p/'trades.csv',index=False)
    (p/'summary.json').write_text(json.dumps(s))
    return p,s,e,t


def test_csv_roundtrip_and_initial_boundary(tmp_path):
    p,_,_,_=fixture(tmp_path)
    s,e,t=load(p,'BTCUSDT');r=analyze(s,e,t)
    assert [x['equity_pnl'] for x in r['yearly']]==[-10.,15.]
    assert [x['realized_pnl'] for x in r['yearly']]==[0.,5.]
    assert r['yearly'][0]['return_pct']==pytest.approx(-10)
    assert r['yearly'][1]['return_pct']==pytest.approx(100*15/90)
    assert all(x['partial'] for x in r['yearly'])
    assert r['drawdown']['maximum']['depth_pct']==20
    assert r['drawdown']['maximum']['recovered'] is True
    assert r['by_side'][0]['net_pnl']==5
    assert r['by_exit_reason'][0]['fees']==2
    assert r['best_30_days'] is None


@pytest.mark.parametrize('change', ['cash','gap','duplicate','order','pnl','symbol','count','fees','time','overlap','nan'])
def test_invalid_inputs_report_file(tmp_path,change):
    p,s,e,t=fixture(tmp_path)
    if change=='cash':e.loc[1,'cash']=12
    elif change=='gap':e=e.drop(index=1)
    elif change=='duplicate':e.loc[1,'time']=e.loc[0,'time']
    elif change=='order':e=e.iloc[::-1]
    elif change=='pnl':t.loc[0,'net_pnl']=4
    elif change=='symbol':s['symbol']='ETHUSDT'
    elif change=='count':s['candle_count']=9
    elif change=='fees':s['total_fees']=10
    elif change=='time':t.loc[0,'exit_time']=pd.Timestamp('2025-01-01',tz='UTC')
    elif change=='overlap':t=pd.concat([t,t],ignore_index=True)
    else:e.loc[0,'equity']=float('nan')
    e.to_csv(p/'equity.csv',index=False);t.to_csv(p/'trades.csv',index=False)
    (p/'summary.json').write_text(json.dumps(s))
    with pytest.raises(DiagnosticError,match=r'csv|json'):load(p,'BTCUSDT')


def test_unrecovered_and_longest_drawdown():
    times=pd.date_range('2023-01-01',periods=8,freq='h',tz='UTC')
    s=pd.Series([100,90,100,95,94,93,92,91],index=times)
    r=drawdowns(s)
    assert r['maximum']['depth_pct']==10 and r['maximum']['recovered']
    assert not r['longest_underwater']['recovered']
    assert r['longest_underwater']['duration_hours']==5
    assert drawdowns(pd.Series([100,101],index=times[:2]))['maximum'] is None


def test_30_day_windows(tmp_path):
    p,_,_,_=fixture(tmp_path,values=[100.+i for i in range(750)])
    r=analyze(*load(p,'BTCUSDT'))
    assert r['best_30_days']['equity_change']==720
    assert r['worst_30_days']['equity_change']==719
    assert pd.Timestamp(r['best_30_days']['end'])-pd.Timestamp(r['best_30_days']['start'])==pd.Timedelta(days=30)


def test_loss_streak_side_and_exit_stats(tmp_path):
    _,s,e,t=fixture(tmp_path)
    t=pd.concat([t]*5,ignore_index=True)
    t['net_pnl']=[-3.,-2.,0.,-1.,4.]
    t['side']=['long','short','long','short','long']
    t['reason']=['stop','stop','signal','max_holding','take_profit']
    r=analyze(s,e,t)
    assert r['max_consecutive_losses']==2
    assert r['expected_pnl']==pytest.approx(-.4)
    assert next(x for x in r['by_side'] if x['group']=='short')['net_pnl']==-3
    assert next(x for x in r['by_exit_reason'] if x['group']=='stop')['count']==2


def test_empty_trades_and_flat_account(tmp_path):
    p,s,e,t=fixture(tmp_path,values=[100.]*4)
    t=t.iloc[:0];s.update(total_trades=0,total_fees=0)
    t.to_csv(p/'trades.csv',index=False);(p/'summary.json').write_text(json.dumps(s))
    r=analyze(*load(p,'BTCUSDT'))
    assert r['by_side']==[] and r['expected_pnl']==0
    assert r['drawdown']['maximum'] is None


def test_complete_month_and_partial_year(tmp_path):
    p,_,_,_=fixture(tmp_path,values=[100.]*744,start='2026-01-01T00:00Z')
    r=analyze(*load(p,'BTCUSDT'))
    assert r['monthly'][0]['partial'] is False
    assert r['yearly'][0]['partial'] is True


def test_cli_reports_independent_accounts_and_stale_failure(tmp_path,capsys):
    fixture(tmp_path/'input')
    args=['--input-dir',str(tmp_path/'input'),'--output-dir',str(tmp_path/'out'),'--symbols','BTCUSDT']
    assert main(args)==0
    text=(tmp_path/'out/BTCUSDT/report.md').read_text()
    assert '펀딩비' in text and '공동 포트폴리오' in text and 'partial=True' in text
    assert (tmp_path/'out/BTCUSDT/monthly.csv').exists()
    assert main(args+['ETHUSDT'])==1
    c=json.loads((tmp_path/'out/comparison.json').read_text())
    assert len(c['independent_accounts'])==1
    (tmp_path/'input/BTCUSDT/error.json').write_text('{}')
    assert main(args)==1
    assert not (tmp_path/'out/BTCUSDT/report.md').exists()
    assert (tmp_path/'out/BTCUSDT/error.json').exists()


def test_existing_engine_report_compatibility(tmp_path):
    from backtesting.engine import run, Config
    from backtesting.report import save
    from backtesting.strategy import SMACrossover
    times=pd.date_range('2023-01-01',periods=80,freq='h',tz='UTC')
    f=pd.DataFrame({'open_time':times,'open':100.,'high':101.,'low':99.,'close':100.,'volume':1.})
    r=run(f,'BTCUSDT',SMACrossover(),Config(),start=times[0],end=times[-1]+pd.Timedelta(hours=1))
    save(r,tmp_path/'BTCUSDT')
    result=analyze(*load(tmp_path/'BTCUSDT','BTCUSDT'))
    assert result['monthly'][0]['equity_pnl']==0
    assert result['initial_capital']==10000
