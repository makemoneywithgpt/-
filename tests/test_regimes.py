"""Synthetic-only causality, CSV reconciliation and CLI tests."""
import json

import pandas as pd
import pytest

from backtesting.regimes import classify, analyze, load_inputs, main
from backtesting.diagnostics import DiagnosticError
from backtesting.engine import Config, run
from backtesting.report import save


def bars(n=250,descending=False):
    price=pd.Series([100.+(-i*.1 if descending else i*.1) for i in range(n)])
    return pd.DataFrame(dict(open_time=pd.date_range('2023-01-01',periods=n,freq='h',tz='UTC'),
                             open=price,high=price+1,low=price-1,close=price,volume=1.))


@pytest.mark.parametrize('descending,regime',[(False,'uptrend'),(True,'downtrend')])
def test_direction_and_exact_entry_boundary(descending,regime):
    f=bars(descending=descending);entry=f.open_time.iloc[200]
    r=classify(f,entry)
    assert r['regime']==regime
    assert r['last_completed_open']==f.open_time.iloc[199].isoformat()
    assert r['sma50']==pytest.approx(f.close.iloc[150:200].mean())
    assert r['sma200']==pytest.approx(f.close.iloc[:200].mean())


def test_future_values_never_affect_past_classification():
    f=bars();entry=f.open_time.iloc[200];before=classify(f,entry)
    # Includes entry candle: none of its OHLCV values may be read.
    f.loc[200:,['open','high','low','close','volume']]=float('nan')
    assert classify(f,entry)==before
    assert classify(f.iloc[:200],entry)==before


def test_unfinished_warmup_cannot_use_future():
    f=bars();entry=f.open_time.iloc[199]
    assert classify(f,entry)['regime']=='unclassified'
    assert classify(f,entry+pd.Timedelta(minutes=30))['regime']=='unclassified'
    assert classify(f.drop(index=180),f.open_time.iloc[201])['regime']=='unclassified'


def test_neutral_and_equality_boundary():
    f=bars();f['close']=100.
    assert classify(f,f.open_time.iloc[200])['regime']=='neutral'
    # last 50 mean=100.5, last 200 mean=100: exact positive boundary.
    f.loc[:149,'close']=100-1/6;f.loc[150:199,'close']=100.5
    assert classify(f,f.open_time.iloc[200])['regime']=='neutral'


class Signals:
    def target(self,history):return 1 if len(history)==201 else 0


def setup(tmp_path,symbol='BTCUSDT'):
    f=bars();start=f.open_time.iloc[0];end=f.open_time.iloc[-1]+pd.Timedelta(hours=1)
    result=run(f,symbol,Signals(),Config(),start=start,end=end)
    save(result,tmp_path/'backtests'/symbol)
    p=tmp_path/'data/klines'/symbol;p.mkdir(parents=True);f.to_parquet(p/'1h.parquet')
    return f,result


@pytest.mark.parametrize('symbol',['BTCUSDT','ETHUSDT'])
def test_csv_reconciliation_and_optional_diagnostics(tmp_path,symbol):
    _,result=setup(tmp_path,symbol)
    summary,trades,f=load_inputs(tmp_path/'backtests',tmp_path/'data',symbol)
    labeled,r=analyze(summary,trades,f)
    assert len(labeled)==result.summary['total_trades']
    assert sum(g['net_pnl'] for g in r['by_regime'])==pytest.approx(summary['net_pnl'])
    assert sum(g['fees'] for g in r['by_regime_side'])==pytest.approx(summary['total_fees'])
    assert 'JSON 없음' in r['diagnostic_status']
    assert r['loss_windows'][0]['count']==0
    assert not r['loss_windows'][0]['fully_covered']


@pytest.mark.parametrize('kind',['gap','duplicate','reverse','invalid','symbol','pnl','fees','count','missing'])
def test_error_paths(tmp_path,kind):
    f,_=setup(tmp_path);p=tmp_path/'data/klines/BTCUSDT/1h.parquet'
    sp=tmp_path/'backtests/BTCUSDT/summary.json';s=json.loads(sp.read_text())
    if kind=='gap':f=f.drop(index=80)
    elif kind=='duplicate':f=pd.concat([f,f.iloc[-1:]],ignore_index=True)
    elif kind=='reverse':f=f.iloc[::-1]
    elif kind=='invalid':f.loc[80,'low']=-1
    elif kind=='missing':p.unlink()
    elif kind=='symbol':s['symbol']='ETHUSDT'
    elif kind=='pnl':s['net_pnl']+=1
    elif kind=='fees':s['total_fees']+=1
    else:s['total_trades']+=1
    if kind!='missing':f.to_parquet(p)
    sp.write_text(json.dumps(s))
    with pytest.raises((DiagnosticError,OSError)):
        load_inputs(tmp_path/'backtests',tmp_path/'data','BTCUSDT')


def test_loss_window_uses_exit_but_label_uses_entry(tmp_path):
    _,_=setup(tmp_path)
    s,t,f=load_inputs(tmp_path/'backtests',tmp_path/'data','BTCUSDT')
    d=dict(symbol='BTCUSDT',start=s['start'],end_exclusive=s['end_exclusive'],
           worst_30_days={'start':t.entry_time.iloc[0].isoformat(),'end':t.exit_time.iloc[0].isoformat()})
    labeled,r=analyze(s,t,f,d)
    assert r['loss_windows'][1]['count']==len(t)
    assert r['loss_windows'][1]['net_pnl']==pytest.approx(t.net_pnl.sum())
    d['symbol']='ETHUSDT'
    with pytest.raises(DiagnosticError):analyze(s,t,f,d)


def test_cli_read_only_and_no_overwrite(tmp_path):
    setup(tmp_path,'BTCUSDT');setup(tmp_path,'ETHUSDT')
    original={str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    args=['--input-dir',str(tmp_path/'backtests'),'--data-dir',str(tmp_path/'data'),
          '--diagnostics-dir',str(tmp_path/'diagnostics'),'--output-dir',str(tmp_path/'out')]
    assert main(args)==0
    for p,value in original.items():assert open(p,'rb').read()==value
    for symbol in ['BTCUSDT','ETHUSDT']:
        report=json.loads((tmp_path/'out'/symbol/'regimes.json').read_text())
        assert report['symbol']==symbol and '인과관계' in report['notice']
        assert (tmp_path/'out'/symbol/'labeled_trades.csv').exists()
    assert main(args)==1
    assert main(args[:-2]+['--output-dir',str(tmp_path/'backtests')])==1


def test_empty_trades(tmp_path):
    class NoSignals:
        def target(self,h):return 0
    f=bars();s=f.open_time.iloc[0];end=f.open_time.iloc[-1]+pd.Timedelta(hours=1)
    result=run(f,'BTCUSDT',NoSignals(),Config(),start=s,end=end)
    labeled,r=analyze(result.summary,result.trades,f)
    assert labeled.empty and r['by_regime']==[] and r['net_pnl']==0
