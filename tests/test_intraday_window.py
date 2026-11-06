"""Explicit audited-window selection only; synthetic data, no return fitting."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtesting.intraday_window import selected_window,context,V092_START,V092_END
from backtesting.intraday_lab import load_verified,main as lab_main
from backtesting.integrity_audit import main as audit_main
from backtesting.intraday_data import digest
from backtesting.intraday_signals import features
from test_integrity_audit import verified_fixture,setup,Sources,START,END


def wrong_v08(root):
    p=root/'v08/BTCUSDT';p.mkdir(parents=True)
    (p/'timeframe.json').write_text(json.dumps(dict(start='2023-01-01T00:00:00Z',end_exclusive='2026-11-04T13:00:00Z')))
    return root/'v08'


def test_preset_is_fixed_before_any_returns_and_pair_required(tmp_path):
    a,b=selected_window('BTCUSDT',tmp_path,V092_START,V092_END)
    assert a.isoformat()=='2025-02-01T00:00:00+00:00'
    assert b.isoformat()=='2026-10-28T00:00:00+00:00'
    info=context(a,b,True)
    assert info['preset']=='Issue12_v0.9.2' and '선택 편향' in info['notice'] and 'OOS' in info['notice']
    for start,end in [(V092_START,None),(None,V092_END),('2025-02-01T00:01Z',V092_END),(V092_END,V092_START)]:
        with pytest.raises(ValueError):selected_window('BTCUSDT',tmp_path,start,end)


def test_explicit_override_requires_exact_verified_manifest(tmp_path):
    verified_fixture(tmp_path);v08=wrong_v08(tmp_path)
    before={p:p.read_bytes() for p in (tmp_path/'verified').rglob('*') if p.is_file()}
    f,meta,a,b=load_verified(tmp_path/'verified',v08,'BTCUSDT',START,END)
    assert (a,b)==(START,END) and len(f)==96 and meta['status']=='verified'
    with pytest.raises(ValueError,match='기간'):load_verified(tmp_path/'verified',v08,'BTCUSDT')
    with pytest.raises(ValueError,match='기간'):load_verified(tmp_path/'verified',v08,'BTCUSDT',START,END-pd.Timedelta(hours=1))
    for p,value in before.items():assert p.read_bytes()==value
    # An unrelated corrupt v08 window cannot override the explicit audited pair.
    (v08/'BTCUSDT/timeframe.json').write_text('invalid')
    assert len(load_verified(tmp_path/'verified',v08,'BTCUSDT',START,END)[0])==96


@pytest.mark.parametrize('fault',['price15','volume15','missing15','missing1h','price1h','volume1h'])
def test_any_new_quality_failure_blocks_explicit_research(tmp_path,monkeypatch,fault):
    root=verified_fixture(tmp_path);v08=wrong_v08(tmp_path)
    interval='15m' if fault in ['price15','volume15','missing15'] else '1h'
    path=root/f'{interval}.parquet';f=pd.read_parquet(path)
    if fault.startswith('missing'):f=f.drop(index=2)
    elif fault.startswith('price'):f.loc[0,'high']+=.01
    else:f.loc[0,'volume']+=.01
    f.to_parquet(path,index=False)
    # Updating just the file hash cannot bypass source evidence or strict parity.
    meta_path=root/'integrity.provenance.json';meta=json.loads(meta_path.read_text())
    meta['parquet_sha256' if interval=='15m' else 'hourly_sha256']=digest(path);meta_path.write_text(json.dumps(meta))
    import backtesting.intraday_lab as lab
    monkeypatch.setattr(lab,'features',lambda *_:pytest.fail('simulation/feature gate bypassed'))
    out=tmp_path/'failed'
    args=['--symbols','BTCUSDT','--data-dir',str(tmp_path/'verified'),'--v08-dir',str(v08),'--output-dir',str(out),'--start',START.isoformat(),'--end',END.isoformat()]
    assert lab_main(args)==1
    assert json.loads((out/'comparison.json').read_text())['status']=='partial_or_failed'
    assert not (out/'BTCUSDT/causal_features.csv').exists()


def test_unaudited_explicit_window_cannot_use_legacy_marker(tmp_path):
    p=tmp_path/'data/klines/BTCUSDT';p.mkdir(parents=True)
    (p/'15m.provenance.json').write_text('{}')
    with pytest.raises(ValueError,match='완전 검증'):load_verified(tmp_path/'data',tmp_path,'BTCUSDT',START,END)


def test_cli_defaults_freeze_before_work_and_preserve_old_results(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);setup(tmp_path);v08=wrong_v08(tmp_path)
    for name in ['intraday_v09','intraday_v091_research','intraday_integrity_v091']:
        p=tmp_path/'data'/name;p.mkdir();(p/'sentinel.txt').write_text('preserve')
    import backtesting.integrity_audit as audit
    monkeypatch.setattr(audit,'OfficialSources',lambda *a:Sources(proof_root=tmp_path/'proof'))
    old={p:p.read_bytes() for p in (tmp_path/'data').rglob('*') if p.is_file()}
    args=['--symbols','BTCUSDT','--v08-dir',str(v08),'--start',START.isoformat(),'--end',END.isoformat()]
    assert audit_main(args)==0
    status=json.loads(Path('data/intraday_integrity_v092_window/status.json').read_text())
    assert status['backtest_allowed']
    pinned=json.loads(Path('data/intraday_integrity_v092_window/window_spec.json').read_text())
    assert pinned['start']==START.isoformat() and pinned['end_exclusive']==END.isoformat()
    assert lab_main(args+['--data-dir','data/intraday_v092_verified_input'])==0
    out=Path('data/intraday_v092_window_research')
    spec=json.loads((out/'frozen_spec.json').read_text())
    report=json.loads((out/'BTCUSDT/research.json').read_text())
    assert spec['window_selection']['explicit'] and report['window_selection']['notice']
    assert '+0.7425%' not in report['historical_reference']
    assert report['cases']['cash_cost1']['summary']['net_return_pct']==0
    for p,value in old.items():assert p.read_bytes()==value
    assert audit_main(args)==1 and lab_main(args+['--data-dir','data/intraday_v092_verified_input'])==1
    assert audit_main(args+['--output-dir','data/intraday_integrity_v091/new'])==1
    assert lab_main(args+['--output-dir','data/intraday_v09/new'])==1


def test_half_window_does_not_create_outputs(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert audit_main(['--start',V092_START])==1
    assert lab_main(['--end',V092_END])==1
    assert not (tmp_path/'data').exists()


def test_window_warmup_and_closed_only_signal_features_unchanged():
    n=1000;p=100+np.arange(n)*.02
    f=pd.DataFrame(dict(open_time=pd.date_range(V092_START,periods=n,freq='15min'),open=p,high=p+.01,low=p-.01,close=p,volume=1.))
    a=features(f)
    assert a.trend.iloc[:799].eq(0).all() and a.trend.iloc[799]==1
    assert a.ema20.iloc[:19].isna().all()
    assert (a.hour_available_at.dropna()<=a.signal_time.loc[a.hour_available_at.notna()]).all()
    g=f.copy();g.loc[880:,['open','high','low','close']]=[200.,201.,199.,200.]
    pd.testing.assert_frame_equal(a.iloc[:880],features(g).iloc[:880])
