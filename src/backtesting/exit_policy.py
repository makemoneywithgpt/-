"""v0.7 fixed opposite-signal experiment + corrected signal funnel audit."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .engine import Config
from .data import load as load_prices, utc
from .strategy import SMACrossover
from .lab_strategies import Donchian20
from .strategy_lab import verify_result
from .diagnostics import load as load_result, table
from .experiments import allow_non_neutral, policy_report, NOTICE
from .root_cause import decomposition, grouped
from .exit_simulator import run_exit_policy
from .report import save

POLICIES=['base','close_only','ignore_opposite_until_flat']
STRATEGIES=['A_sma','B_sma_filtered','C_donchian','D_donchian_filtered']


def markouts(events,prices):
    """Post-hoc fixed-unit returns, overlapping observations, NOT strategy PnL."""
    index=pd.Index(prices.open_time)
    rows=[]
    for h in [1,6,24,72]:
        values=[];held=[];marked=[]
        for e in events:
            i=index.get_indexer([e['time']])[0]
            if i<0 or i+h>len(prices):continue
            change=float(prices.close.iloc[i+h-1]/prices.open.iloc[i]-1)*100
            values.append(e['side']*change)
            if 'held_side' in e:
                held.append(e['held_side']*change);marked.append(e['marked_pnl'])
        rows.append(dict(horizon_hours=h,samples=len(values),mean_directional_return_pct=float(np.mean(values)) if values else None,
                         win_rate_pct=100*sum(v>0 for v in values)/len(values) if values else None,
                         held_direction_mean_return_pct=float(np.mean(held)) if held else None,
                         marked_pnl_at_opposite_mean=float(np.mean(marked)) if marked else None,
                         posthoc_only=True))
    return rows


def funnel(events,prices):
    return {kind:dict(count=sum(e['kind']==kind for e in events),
                      forward_returns=markouts([e for e in events if e['kind']==kind],prices))
            for kind in ['raw','admitted','filled','opposite_while_held']}


def research_symbol(data_dir,lab_dir,symbol,strategies,output):
    reference,_,_=load_result(Path(lab_dir)/symbol/strategies[0],symbol)
    config=Config(**reference['config']);start=utc(reference['start']);end=utc(reference['end_exclusive'])
    if reference.get('funding_model')!='unavailable_assumed_zero':raise ValueError('원본은 펀딩비 0 가정이어야 합니다.')
    prices=load_prices(Path(data_dir)/'klines'/symbol/'1h.parquet',start,end)
    output.mkdir(parents=True,exist_ok=False)
    report=dict(symbol=symbol,start=start.isoformat(),end_exclusive=end.isoformat(),config=reference['config'],notice=NOTICE,
                signal_statistics_correction='기존 v0.6 repetition은 raw 신호만 집계하여 A/B 및 C/D에서 동일했다. 과거 보고서는 수정하지 않으며 여기서 raw/admitted/filled를 분리한다.',strategies={})
    rows=[]
    for name in strategies:
        strategy=SMACrossover() if name.startswith(('A','B')) else Donchian20()
        gate=allow_non_neutral if name.startswith(('B','D')) else None
        result={}
        for policy in POLICIES:
            events=[]
            r=run_exit_policy(prices,symbol,strategy,config,start=start,end=end,entry_filter=gate,exit_policy=policy,events=events)
            if policy=='base':verify_result(r,Path(lab_dir)/symbol/name,symbol)
            p=policy_report(r);t=r.trades.copy()
            p.update(decomposition=decomposition(t),signal_funnel=funnel(events,prices),
                     by_exit_reason=grouped(t,'reason'))
            p['holding_distribution']=dict(mean_hours=float(t.holding_hours.mean()) if len(t) else 0.,
                median_hours=float(t.holding_hours.median()) if len(t) else 0.,max_hours=float(t.holding_hours.max()) if len(t) else 0.)
            for g in p['by_exit_reason']:g['trade_share_pct']=100*g['trades']/len(t) if len(t) else 0.
            p['filled_trade_outcomes']=[dict(group=k,**decomposition(g)) for k,g in t.groupby('side')]
            target=output/name/policy;save(r,target)
            pd.DataFrame(events).to_csv(target/'signal_events.csv',index=False,encoding='utf-8-sig')
            for kind,value in p['signal_funnel'].items():
                pd.DataFrame(value['forward_returns']).to_csv(target/f'{kind}_markouts.csv',index=False,encoding='utf-8-sig')
            pd.DataFrame([{k:v for k,v in y.items() if k!='by_side'} for y in p['yearly']]).to_csv(target/'yearly.csv',index=False,encoding='utf-8-sig')
            pd.DataFrame([dict(year=y['period'],**side) for y in p['yearly'] for side in y['by_side']]).to_csv(target/'yearly_sides.csv',index=False,encoding='utf-8-sig')
            pd.DataFrame(p['by_side']).to_csv(target/'by_side.csv',index=False,encoding='utf-8-sig')
            pd.DataFrame(p['by_exit_reason']).to_csv(target/'by_exit_reason.csv',index=False,encoding='utf-8-sig')
            result[policy]=p
            rows.append(dict(symbol=symbol,strategy=name,exit_policy=policy,**{k:r.summary[k] for k in
                ['total_trades','net_pnl','net_return_pct','total_fees','max_drawdown_pct','profit_factor']},
                raw_signals=p['signal_funnel']['raw']['count'],admitted_signals=p['signal_funnel']['admitted']['count'],
                filled_entries=p['signal_funnel']['filled']['count']))
        report['strategies'][name]=result
    report['confirmed_facts']=['base는 저장 v0.5 결과와 거래·시간별 자산곡선·동일 Config 대조를 통과했다.',
                               'raw는 원래 신호, admitted는 포지션 상태와 무관한 진입필터 통과, filled는 실제 신규 진입이다. 세 집계는 다른 모집단이다.']
    report['hypotheses']=['signal 청산 손실이 크다는 관찰만으로 청산 규칙이 손실 원인이라고 확정할 수 없다.',
                           '반대 신호 무시는 손절·보유시간·MDD를 늘릴 수 있으므로 수익과 위험을 함께 비교해야 한다.']
    report['unverified']=['실거래 수익성·실제 호가/마진/강제청산·펀딩비 포함 결과는 미검증.',
                         '새 데이터의 장래 모의검증 전에는 과거 양의 성과도 탐색적 in-sample 결과다.']
    (output/'exit_policy.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    pd.DataFrame(rows).to_csv(output/'comparison.csv',index=False,encoding='utf-8-sig')
    text=f'# {symbol} v0.7 반대 신호 정책 비교\n\n{NOTICE}\n\n'+table(rows)+'\n'
    text+='base: 반대 신호 청산+필터 허용 시 반대 진입. close_only: 청산만 하고 해당 시가 재진입 금지. ignore_opposite_until_flat: 보유 중 반대 신호 무시. 세 정책 모두 손절/익절/72h/종료 청산과 비용·위험 조건은 동일합니다.\n\n'
    text+='위험/시간 청산은 예약 신호를 취소하며, close_only가 취소한 신호를 다음 봉으로 미루지 않습니다. 이후 새로 발생한 신호만 진입할 수 있습니다.\n\n'
    text+=report['signal_statistics_correction']+'\n\nraw/admitted는 포지션 상태와 무관하므로 청산 정책끼리 같을 수 있지만, 필터 전후는 다를 수 있습니다. filled는 실거래 주문이 아닌 백테스트 가상 진입입니다.\n'
    text+='각 markout은 다음 시가→1/6/24/72시간 완료 종가의 방향 조정 고정 단위 가격 수익률입니다. 비용·수량을 제외하고 겹치는 신호도 포함한 사후 분석으로, 실제 거래 기대손익과 혼동하지 마세요. 말단 관측 부족은 해당 표본에서 제외합니다.\n'
    for name,policies in report['strategies'].items():
        for policy,p in policies.items():
            text+=f'\n## {name}/{policy}\n\n연도별 (partial=True는 부분 연도, PF=null은 손실 거래 없음/정의 불가):\n\n'+table([{k:v for k,v in y.items() if k!='by_side'} for y in p['yearly']])
            text+='\n롱숏별:\n\n'+table(p['by_side'])+'\n청산 사유/기여도:\n\n'+table(p['by_exit_reason'])
            text+=f'\n보유시간: {json.dumps(p["holding_distribution"],ensure_ascii=False)}\n'
            for kind,v in p['signal_funnel'].items():text+=f'\n{kind}: {v["count"]}건 (사후 분석)\n\n'+table(v['forward_returns'])
    for key,title in [('confirmed_facts','확인된 사실'),('hypotheses','원인 가설'),('unverified','미검증')]:
        text+=f'\n## {title}\n\n'+'\n\n'.join(report[key])
    text+='\n슬리피지는 모든 시나리오에 동일 체결가 비율로 반영되고 별도 단순 차감 비용으로 추정하지 않습니다. 모든 시간은 UTC입니다. 연도별 평가손익과 청산 거래 실현손익은 다를 수 있습니다.\n'
    (output/'report.md').write_text(text,encoding='utf-8')
    return rows


def main(argv=None):
    parser=argparse.ArgumentParser(description='v0.7 청산 정책 비교와 신호 통계 교정')
    parser.add_argument('--symbols',nargs='+',choices=['BTCUSDT','ETHUSDT'],default=['BTCUSDT','ETHUSDT'])
    parser.add_argument('--strategies',nargs='+',choices=STRATEGIES,default=['B_sma_filtered','D_donchian_filtered'])
    for flag,default in [('data-dir','data'),('lab-dir','data/strategy_lab_v05'),('output-dir','data/exit_policy_v07')]:
        parser.add_argument('--'+flag,type=Path,default=Path(default))
    args=parser.parse_args(argv);out=args.output_dir.resolve()
    protected=[args.lab_dir.resolve(),Path('.venv').resolve()]+[(args.data_dir/k).resolve() for k in ['klines','backtests','diagnostics','regimes','experiments','strategy_lab_v05','root_cause_v06']]
    if any(out==p or p in out.parents or out in p.parents for p in protected):
        print('출력 폴더가 기존 데이터/보고서/.venv와 겹칩니다.',file=sys.stderr);return 1
    try:out.mkdir(parents=True,exist_ok=False)
    except OSError as exc:print(f'새 출력 폴더 필요: {exc}',file=sys.stderr);return 1
    rows=[];failed=False
    for symbol in dict.fromkeys(args.symbols):
        try:
            rows.extend(research_symbol(args.data_dir,args.lab_dir,symbol,list(dict.fromkeys(args.strategies)),out/symbol))
            print(f'{symbol}: 정책 비교 완료 {out/symbol}/report.md')
        except (OSError,ValueError,TypeError,KeyError) as exc:
            failed=True;target=out/symbol;target.mkdir(exist_ok=True)
            (target/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n',encoding='utf-8')
            print(f'{symbol}: 비교 실패 — {exc}',file=sys.stderr)
    pd.DataFrame(rows).to_csv(out/'comparison.csv',index=False,encoding='utf-8-sig')
    (out/'comparison.json').write_text(json.dumps(dict(status='partial_or_failed' if failed else 'success',notice=NOTICE,
        independent_accounts=rows),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return int(failed)


if __name__=='__main__':raise SystemExit(main())
