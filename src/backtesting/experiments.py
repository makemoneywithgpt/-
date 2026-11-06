"""Fixed-policy, full independent re-simulations. Never edit input reports."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .data import load as load_prices, utc
from .diagnostics import load as load_reference, analyze as diagnose, table
from .engine import Config, run
from .comparison_engine import run_filtered
from .strategy import SMACrossover
from .regimes import classify
from .report import save

NOTICE=('전체 2023~2026 데이터는 이미 사람이 관찰한 연구 데이터이며 2025/2026도 미관측 OOS가 아닙니다. '
        '중립 제한의 수익성은 입증되지 않았습니다. 이후 새 데이터로 장래 모의투자 검증이 필요합니다. '
        '종목별 독립 자본, 최대 1배, 펀딩비 0 가정이며 실제 마진·강제청산·호가 비용은 미구현입니다. '
        '과최적화 위험이 있습니다. 고정 규칙만 비교하며 임계값 탐색은 하지 않습니다.')
REFERENCE={'BTCUSDT':(-5031.588774607464,799),'ETHUSDT':(-4486.0350760987985,784)}


def allow_non_neutral(completed, entry_time):
    return classify(completed,entry_time)['regime'] in ('uptrend','downtrend')


def metrics(trades,initial,final,curve):
    pnl=trades.net_pnl
    gains=float(pnl.clip(lower=0).sum());losses=-float(pnl.clip(upper=0).sum())
    peak=pd.Series([initial,*curve]).cummax()
    vals=pd.Series([initial,*curve])
    mdd=float(((peak-vals)/peak.where(peak>0)).max()*100)
    return dict(total_trades=len(trades),win_rate_pct=float(pnl.gt(0).mean()*100) if len(trades) else 0.,
                net_pnl=float(pnl.sum()),equity_change=float(final-initial),
                net_return_pct=float((final/initial-1)*100) if initial>0 else None,
                total_fees=float(trades.fees.sum()),max_drawdown_pct=mdd,
                profit_factor=gains/losses if losses else None,
                average_pnl=float(pnl.mean()) if len(trades) else 0.)


def policy_report(result):
    s=result.summary;t=result.trades.copy();e=result.equity
    for column in ['entry_time','exit_time']:
        t[column]=pd.to_datetime(t[column],utc=True)
    for column in ['net_pnl','fees','gross_pnl','funding_cost']:
        t[column]=pd.to_numeric(t[column])
    diag=diagnose(s,e,t)
    yearly=[]
    for p in diag['yearly']:
        left,right=utc(p['start']),utc(p['end_exclusive'])
        selected=t.loc[(t.exit_time>left)&(t.exit_time<=right)]
        values=e.loc[(e.time>left)&(e.time<=right),'equity'].tolist()
        yearly.append({**p,**metrics(selected,p['starting_equity'],p['ending_equity'],values),
                       'by_side':side_stats(selected)})
    return dict(summary=s,yearly=yearly,by_side=side_stats(t))


def side_stats(t):
    return [dict(side=side,total_trades=len(g),net_pnl=float(g.net_pnl.sum()),
                 win_rate_pct=float(g.net_pnl.gt(0).mean()*100) if len(g) else 0.,
                 total_fees=float(g.fees.sum()),average_pnl=float(g.net_pnl.mean()) if len(g) else 0.)
            for side in ['long','short'] for g in [t.loc[t.side==side]]]


def assert_baseline(result,reference,reference_equity,reference_trades):
    for key in ['total_trades','net_pnl','total_fees','final_equity','net_return_pct','max_drawdown_pct']:
        if not np.isclose(result.summary[key],reference[key],rtol=1e-9,atol=1e-6):
            raise ValueError(f'baseline 불일치: summary.json {key}: 재실행={result.summary[key]}, 기존={reference[key]}')
    if result.summary['profit_factor'] is None or reference['profit_factor'] is None:
        if result.summary['profit_factor'] != reference['profit_factor']:raise ValueError('baseline PF 불일치')
    elif not np.isclose(result.summary['profit_factor'],reference['profit_factor'],rtol=1e-9,atol=1e-6):
        raise ValueError('baseline PF 불일치')
    # Compare the whole paths, not just aggregate PnL.
    for name,new,old in [('trades.csv',result.trades,reference_trades),('equity.csv',result.equity,reference_equity)]:
        try:pd.testing.assert_frame_equal(new.reset_index(drop=True),old.reset_index(drop=True),
                                          check_dtype=False,check_exact=False,rtol=1e-9,atol=1e-6)
        except AssertionError as exc:raise ValueError(f'baseline 불일치: {name}: {exc}') from exc
    if (utc(reference['start'])==utc('2023-01-01') and
        utc(reference['end_exclusive'])==utc('2026-11-04T13:00Z')):
        expected,count=REFERENCE[reference['symbol']]
        if result.summary['total_trades']!=count or not np.isclose(result.summary['net_pnl'],expected,rtol=1e-9,atol=1e-6):
            raise ValueError('baseline이 Issue #5의 고정 전체 기간 기준값과 불일치합니다.')


def compare_symbol(data_dir,input_dir,symbol):
    reference,old_equity,old_trades=load_reference(Path(input_dir)/symbol,symbol)
    if reference.get('strategy')!='SMACrossover' or reference.get('strategy_parameters',{'fast':20,'slow':50})!={'fast':20,'slow':50}:
        raise ValueError('기준 결과는 SMA 20/50이어야 합니다.')
    if reference.get('funding_model')!='unavailable_assumed_zero':raise ValueError('기준 결과의 펀딩비 0 가정 불일치')
    config=Config(**reference['config'])
    start,end=utc(reference['start']),utc(reference['end_exclusive'])
    prices=load_prices(Path(data_dir)/'klines'/symbol/'1h.parquet',start,end)
    # Both start from identical capital and empty positions, on the same frame.
    baseline=run(prices,symbol,SMACrossover(20,50),config,start=start,end=end)
    assert_baseline(baseline,reference,old_equity,old_trades)
    alternative=run_filtered(prices,symbol,SMACrossover(20,50),config,start=start,end=end,
                             entry_filter=allow_non_neutral)
    for r in [baseline,alternative]:r.summary['strategy_parameters']={'fast':20,'slow':50}
    alternative.summary['entry_policy']='SMA50/200 abs gap > .005; otherwise block NEW entry only'
    report=dict(symbol=symbol,start=start.isoformat(),end_exclusive=end.isoformat(),config=reference['config'],
                baseline_verified=True,notice=NOTICE,
                policies={'baseline':policy_report(baseline),'non_neutral_entry':policy_report(alternative)})
    return baseline,alternative,report


def write_symbol(output,baseline,alternative,report):
    for policy,result in [('baseline',baseline),('non_neutral_entry',alternative)]:
        save(result,output/policy)
        yearly=report['policies'][policy]['yearly']
        pd.DataFrame([{k:v for k,v in row.items() if k!='by_side'} for row in yearly]).to_csv(
            output/policy/'yearly.csv',index=False,encoding='utf-8-sig')
        pd.DataFrame([dict(year=row['period'],**side) for row in yearly for side in row['by_side']]).to_csv(
            output/policy/'yearly_sides.csv',index=False,encoding='utf-8-sig')
        pd.DataFrame(report['policies'][policy]['by_side']).to_csv(output/policy/'by_side.csv',index=False,encoding='utf-8-sig')
    (output/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    rows=[]
    for name,policy in report['policies'].items():
        s=policy['summary']
        rows.append(dict(policy=name,**{key:s[key] for key in ['total_trades','win_rate_pct','net_pnl','net_return_pct','total_fees','max_drawdown_pct','profit_factor']}))
    pd.DataFrame(rows).to_csv(output/'comparison.csv',index=False,encoding='utf-8-sig')
    text=f'# {report["symbol"]} 전략 비교 v0.4\n\n{NOTICE}\n\n기존 baseline과 전체 거래·자산곡선 대조 통과.\n\n'+table(rows)
    text+='\n두 정책은 각각 초기 자본부터 새로 실행했습니다. 사후 거래 삭제 방식이 아닙니다. '
    text+='비용·위험·보유시간·데이터 구간은 동일합니다. 중립 또는 워밍업 부족이면 신규 진입만 차단하고 반대 신호의 기존 포지션 청산은 유지합니다.\n'
    text+='\nSMA20/50 교차와 SMA50/200 ±0.005 고정 기준입니다. 신호 t봉까지 완료된 과거만 사용하고 t+1 시가 진입입니다. '
    text+='처음 200개 이전에는 제한 정책 신규 진입을 막습니다. 시작 이전 봉은 두 정책 모두 사용하지 않습니다.\n'
    for name,policy in report['policies'].items():
        text+=f'\n## {name}: 연도별\n\n'+table([{k:v for k,v in y.items() if k!='by_side'} for y in policy['yearly']])
        text+='\n연도별 평가손익/수익률은 직전 연말 자산 기준이며, 거래 순손익은 해당 연도 청산 거래만 포함하므로 서로 다를 수 있습니다. partial=True는 부분 연도입니다. 손실 거래가 없으면 PF=null입니다.\n'
        text+='\n롱숏별 전체 성과:\n\n'+table(policy['by_side'])
    (output/'report.md').write_text(text,encoding='utf-8')
    return rows


def main(argv=None):
    parser=argparse.ArgumentParser(description='고정 A/B 정책 전체 재백테스트 비교 v0.4')
    parser.add_argument('--symbols',nargs='+',choices=['BTCUSDT','ETHUSDT'],default=['BTCUSDT','ETHUSDT'])
    for flag,default in [('data-dir','data'),('input-dir','data/backtests'),('output-dir','data/experiments')]:
        parser.add_argument('--'+flag,type=Path,default=Path(default))
    args=parser.parse_args(argv)
    output=args.output_dir.resolve()
    protected=[args.input_dir.resolve(),(args.data_dir/'klines').resolve(),
               (args.data_dir/'backtests').resolve(),(args.data_dir/'diagnostics').resolve(),(args.data_dir/'regimes').resolve()]
    if any(output==p or p in output.parents or output in p.parents for p in protected):
        print('출력은 기존 데이터/결과 폴더와 겹치면 안 됩니다.',file=sys.stderr);return 1
    try:output.mkdir(parents=True,exist_ok=False)
    except OSError as exc:print(f'새 출력 폴더 필요: {exc}',file=sys.stderr);return 1
    failed=False;all_rows=[]
    for symbol in dict.fromkeys(args.symbols):
        try:
            a,b,report=compare_symbol(args.data_dir,args.input_dir,symbol)
            rows=write_symbol(output/symbol,a,b,report)
            all_rows.extend([dict(symbol=symbol,**row) for row in rows])
            print(f'{symbol}: 기준 일치 확인·독립 재실행 완료. {output/symbol}/report.md')
        except (OSError,ValueError,TypeError,KeyError) as exc:
            failed=True;target=output/symbol;target.mkdir(exist_ok=True)
            (target/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n',encoding='utf-8')
            print(f'{symbol}: 비교 실패 — {exc}',file=sys.stderr)
    pd.DataFrame(all_rows).to_csv(output/'comparison.csv',index=False,encoding='utf-8-sig')
    (output/'comparison.json').write_text(json.dumps(dict(notice=NOTICE,independent_accounts=all_rows,
        status='partial_or_failed' if failed else 'success'),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return 1 if failed else 0


if __name__=='__main__':raise SystemExit(main())
