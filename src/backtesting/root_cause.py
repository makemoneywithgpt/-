"""Offline forensic audit, fixed experiments, no strategy selection/optimization."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .engine import Config
from .data import load as load_prices, utc
from .diagnostics import load as load_result, table
from .strategy import SMACrossover
from .lab_strategies import Donchian20
from .strategy_lab import verify_result
from .experiments import allow_non_neutral, NOTICE
from .audit_simulator import run_audit
from .report import save

POLICIES=['A_sma','B_sma_filtered','C_donchian','D_donchian_filtered']
CASES={'zero_cost':(0.,0.),'no_fee':(0.,1.),'original':(1.,1.),'double_cost':(2.,2.)}


def decomposition(trades):
    pnl=trades.net_pnl; wins=pnl[pnl>0];losses=pnl[pnl<0]
    win=float(wins.mean()) if len(wins) else None
    loss=float(-losses.mean()) if len(losses) else None
    return dict(trades=len(trades),fills=2*len(trades),wins=len(wins),losses=len(losses),
                breakeven_trades=int(pnl.eq(0).sum()),win_rate_pct=float(pnl.gt(0).mean()*100) if len(pnl) else 0.,
                winning_sum=float(wins.sum()),losing_sum=float(losses.sum()),mean_win=win,mean_loss=loss,
                breakeven_win_rate_pct=100*loss/(win+loss) if win is not None and loss is not None else None,
                expected_net=float(pnl.mean()) if len(pnl) else 0.,
                gross_with_slippage=float(trades.gross_pnl.sum()),net_pnl=float(pnl.sum()),
                fees=float(trades.fees.sum()),funding=float(trades.funding_cost.sum()),
                fee_per_trade=float(trades.fees.mean()) if len(pnl) else 0.,
                gross_per_trade=float(trades.gross_pnl.mean()) if len(pnl) else 0.,
                mean_holding_hours=float(trades.holding_hours.mean()) if len(pnl) else 0.)


def grouped(trades,column):
    return [dict(group=str(k),**decomposition(g)) for k,g in trades.groupby(column,sort=True)]


def trade_paths(trades,prices):
    rows=[]
    for i,t in enumerate(trades.itertuples(index=False)):
        history=prices.loc[prices.open_time+pd.Timedelta(hours=1)<=t.entry_time].tail(21)
        atr=None
        if len(history)>=21:
            previous=history.close.shift(1)
            ranges=pd.concat([history.high-history.low,(history.high-previous).abs(),(history.low-previous).abs()],axis=1).max(axis=1)
            atr=float(ranges.iloc[-20:].mean())
        # Full held-hour extrema are POST-HOC envelopes, not known exit path.
        held=prices.loc[(prices.open_time>=t.entry_time)&(prices.open_time<t.exit_time)]
        low=min(float(held.low.min()),t.exit_price) if len(held) else min(t.entry_price,t.exit_price)
        high=max(float(held.high.max()),t.exit_price) if len(held) else max(t.entry_price,t.exit_price)
        side=1 if t.side=='long' else -1
        mae=max(0.,(t.entry_price-low) if side==1 else (high-t.entry_price))/t.entry_price*100
        mfe=max(0.,(high-t.entry_price) if side==1 else (t.entry_price-low))/t.entry_price*100
        rows.append(dict(trade_index=i,entry_time=t.entry_time,exit_time=t.exit_time,side=t.side,
                         prior_atr20=atr,mae_envelope_pct=mae,mfe_envelope_pct=mfe,
                         posthoc_only=True,net_pnl=t.net_pnl))
    return rows


def repetition(strategy,prices,trades):
    previous=0;run_length=0;longest=0;repeat=0;signals=0
    markouts={h:[] for h in [1,6,24,72]}
    for i in range(1,len(prices)):
        signal=strategy.target(prices.iloc[:i])
        if signal:
            signals+=1
            for horizon,values in markouts.items():
                if i+horizon<=len(prices):
                    values.append(float(signal*(prices.close.iloc[i+horizon-1]/prices.open.iloc[i]-1)*100))
            run_length=run_length+1 if signal==previous else 1
            repeat+=int(signal==previous);longest=max(longest,run_length)
        else:run_length=0
        previous=signal
    pairs=[]
    for i in range(1,len(trades)):
        a,b=trades.iloc[i-1],trades.iloc[i]
        gap=float((b.entry_time-a.exit_time)/pd.Timedelta(hours=1))
        if a.reason in ('stop','stop_gap') and a.side==b.side and 0<=gap<=6:
            pairs.append(dict(previous_trade=i-1,next_trade=i,gap_hours=gap,next_net_pnl=float(b.net_pnl),
                              next_fees=float(b.fees),side=b.side))
    return dict(signal_forward_returns=[dict(horizon_hours=h,samples=len(v),mean_directional_return_pct=float(np.mean(v)) if v else None,win_rate_pct=100*sum(x>0 for x in v)/len(v) if v else None,posthoc_only=True) for h,v in markouts.items()],signal_count=signals,consecutive_same_direction_signals=repeat,longest_signal_streak=longest,
                short_reentry_definition='연속 거래 중 직전 손절/손절갭 이후 같은 방향 0~6시간 재진입 (고정 기술 통계)',
                short_reentries=pairs,short_reentry_count=len(pairs),
                short_reentry_fees=sum(p['next_fees'] for p in pairs),
                short_reentry_net_pnl=sum(p['next_net_pnl'] for p in pairs))


def event_stats(events):
    result=[]
    for side in [1,-1]:
        held=[e for e in events if e['event']=='exposure' and e['side']==side]
        both=[e for e in held if e['both_touch']]
        gaps=[e for e in events if e['event']=='stop_gap' and e['side']==side]
        result.append(dict(side='long' if side==1 else 'short',held_bar_count=len(held),both_touch_count=len(both),
                           both_touch_pct=100*len(both)/len(held) if held else 0.,stop_gap_count=len(gaps),
                           stop_gap_adverse_price_impact=sum(e['adverse_price_impact'] for e in gaps)))
    return result


def audit_symbol(data_dir,lab_dir,symbol,output):
    reference,_,_=load_result(Path(lab_dir)/symbol/POLICIES[0],symbol)
    config=Config(**reference['config']);start=utc(reference['start']);end=utc(reference['end_exclusive'])
    if reference.get('funding_model')!='unavailable_assumed_zero':raise ValueError('펀딩비 0 원본만 지원합니다.')
    prices=load_prices(Path(data_dir)/'klines'/symbol/'1h.parquet',start,end)
    output.mkdir(parents=True,exist_ok=False)
    report=dict(symbol=symbol,start=start.isoformat(),end_exclusive=end.isoformat(),config=reference['config'],notice=NOTICE,policies={})
    for policy in POLICIES:
        strategy=SMACrossover() if policy.startswith(('A','B')) else Donchian20()
        gate=allow_non_neutral if policy.startswith(('B','D')) else None
        cases={};base=None;base_events=[]
        for case,(fee,slip) in CASES.items():
            events=[]
            r=run_audit(prices,symbol,strategy,replace(config,fee_rate=config.fee_rate*fee,slippage=config.slippage*slip),
                        start=start,end=end,entry_filter=gate,events=events)
            if case=='original':
                verify_result(r,Path(lab_dir)/symbol/policy,symbol)
                base=r;base_events=events
            save(r,output/policy/case)
            pd.DataFrame(events).to_csv(output/policy/case/'events.csv',index=False,encoding='utf-8-sig')
            cases[case]=dict(summary=r.summary,decomposition=decomposition(r.trades))
        optimistic=run_audit(prices,symbol,strategy,config,start=start,end=end,entry_filter=gate,take_first=True)
        save(optimistic,output/policy/'take_first_extreme')
        t=base.trades.copy()
        t['entry_hour_utc']=pd.to_datetime(t.entry_time,utc=True).dt.hour
        t['holding_bucket']=pd.cut(pd.to_numeric(t.holding_hours),[0,6,24,48,72,float('inf')],include_lowest=True).astype(str)
        paths=trade_paths(t,prices)
        pd.DataFrame(paths).to_csv(output/policy/'trade_paths.csv',index=False,encoding='utf-8-sig')
        details=dict(cases=cases,base_path_verified=True,by_reason=grouped(t,'reason'),by_side=grouped(t,'side'),
                     by_entry_hour=grouped(t,'entry_hour_utc'),by_holding=grouped(t,'holding_bucket'),
                     repetition=repetition(strategy,prices,t),ohlc=event_stats(base_events),
                     take_first_extreme_net=optimistic.summary['net_pnl'],stop_first_net=base.summary['net_pnl'],
                     extreme_difference=optimistic.summary['net_pnl']-base.summary['net_pnl'])
        report['policies'][policy]=details
        for name in ['by_reason','by_side','by_entry_hour','by_holding','ohlc']:
            pd.DataFrame(details[name]).to_csv(output/policy/f'{name}.csv',index=False,encoding='utf-8-sig')
    return report


def write_report(report,output):
    rows=[];text=f'# {report["symbol"]} v0.6 근본 원인 감사\n\n{NOTICE}\n\n'
    text+='이 보고서는 새 전략이나 수익성 인증이 아닙니다. 원본 기록과 기본 감사 실행의 전체 경로 대조를 통과한 종목만 성공으로 표시합니다.\n\n'
    facts=[]
    for policy,p in report['policies'].items():
        for case,c in p['cases'].items():
            rows.append(dict(policy=policy,case=case,**c['decomposition']))
        d=p['cases']['original']['decomposition']
        zero=p['cases']['zero_cost']['decomposition']['net_pnl']
        if zero<0:facts.append(f'{policy}: 해당 신호·청산·위험 조합은 비용 0 재실행에서도 순손실 {zero} USDT. 비용만이 전부라는 설명과 일치하지 않습니다.')
        facts.append(f'{policy}: 원설정 {d["trades"]}거래, 순손익 {d["net_pnl"]}, 수수료 {d["fees"]}, 수수료 전(슬리피지 포함) {d["gross_with_slippage"]} USDT.')
        text+=f'## {policy}\n\n'+table([dict(case=k,**v['decomposition']) for k,v in p['cases'].items()])
        text+='\n청산 사유별:\n\n'+table(p['by_reason'])+'\n방향별:\n\n'+table(p['by_side'])+'\nOHLC 모호성:\n\n'+table(p['ohlc'])
        rep=p['repetition'];text+='\n신호별 다음 시가→1/6/24/72h 완료 종가의 방향 조정 수익률 (무비용·고정 단위·겹치는 신호 포함·사후 설명 전용, 포트폴리오 성과 아님):\n\n'+table(rep['signal_forward_returns'])
        text+=f'\n동방향 연속 신호 {rep["consecutive_same_direction_signals"]}회, 최장 {rep["longest_signal_streak"]}봉. 손절 후 같은 방향 6시간 이내 재진입 {rep["short_reentry_count"]}회, 후속 수수료 {rep["short_reentry_fees"]} USDT.\n'
        text+=f'\n손절 우선 순손익 {p["stop_first_net"]}, 익절 우선 극단 순손익 {p["take_first_extreme_net"]}, 차이 {p["extreme_difference"]} USDT. 이후 자본·포지션 경로가 달라지므로 엄밀한 참값 범위나 신뢰구간이 아닙니다.\n'
    report['confirmed_facts']=facts
    report['hypotheses']=['비용 대비 진입 신호의 우위가 약할 수 있음. 비용 0 재실행과 기대값 분해로 우선 점검하되 미래 수익은 미확인.',
                          '손절 후 반복 진입이 손실과 수수료를 집중시킬 수 있음. 빈도와 손익은 관측해도 재진입 금지의 개선 효과는 미검증.',
                          '시간봉 내 체결 순서 가정이 성과에 영향을 줄 수 있음. 더 세밀한 데이터 없이는 실제 순서 미확인.']
    report['no_evidence']=['1h 노이즈가 손실의 원인이라는 인과 증거 없음.', '4h 전략 개선 및 실거래 수익성 미검증.',
                           '선물 실제 마진·강제청산·호가·펀딩비를 포함한 실행 정확성 미검증.']
    report['priorities']=['1. 비용 0에서도 기대값이 약한지 먼저 확인하고 신호 자체 문제와 비용 민감도를 분리. 재실행 차이는 자본/거래 변화도 포함한다.',
                           '2. 반복 진입 집중 구간의 증거를 확인한 뒤, 별도 사전 고정 재진입 정책을 훈련/검증 분리로 연구. 이번 감사는 정책을 변경하지 않는다.',
                           '3. 양쪽 터치/갭이 많은 구간은 세부 캔들로 체결 가정을 확인. 4h는 전용 엔진 설계 이후 별도 검증하며 1h 엔진에 4h 파일을 넣지 않는다.']
    text+='\n## 확인된 사실\n\n'+'\n\n'.join(facts)
    for key,title in [('hypotheses','원인 가설'),('no_evidence','아직 증거 없음'),('priorities','우선 연구 과제')]:
        text+=f'\n\n## {title}\n\n'+'\n\n'.join(report[key])
    text+='\n\n비용 0은 실현 가능한 거래 성과가 아닙니다. gross는 슬리피지를 포함하며 슬리피지 비용을 단순 차감한 인과 효과로 해석하지 마세요. 손익분기 승률은 손익이 0인 거래를 제외한 평균 승/패 크기에 기초한 설명값입니다.\n'
    text+='ATR20은 진입 전 완료 봉만 사용합니다. trade_paths의 MAE/MFE는 사후 설명용 시간봉 전체 가격 범위이며 청산 이후 같은 봉 움직임을 포함할 수 있어 실제 경로가 아닌 포락선입니다. 필터 성과로 재사용하지 마세요. stop gap 가격 영향은 손절 기준가 대비 시가의 명목 차이이며 비용/후속 자본 효과 전체가 아닙니다.\n'
    (output/'report.md').write_text(text,encoding='utf-8')
    (output/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    pd.DataFrame(rows).to_csv(output/'comparison.csv',index=False,encoding='utf-8-sig')
    return [dict(symbol=report['symbol'],**r) for r in rows]


def main(argv=None):
    parser=argparse.ArgumentParser(description='v0.6 오프라인 근본 원인 감사 (새 전략 없음)')
    parser.add_argument('--symbols',nargs='+',choices=['BTCUSDT','ETHUSDT'],default=['BTCUSDT','ETHUSDT'])
    for flag,default in [('data-dir','data'),('lab-dir','data/strategy_lab_v05'),('output-dir','data/root_cause_v06')]:
        parser.add_argument('--'+flag,type=Path,default=Path(default))
    args=parser.parse_args(argv);out=args.output_dir.resolve()
    protected=[args.lab_dir.resolve(),Path('.venv').resolve()]+[(args.data_dir/k).resolve() for k in ['klines','backtests','diagnostics','regimes','experiments','strategy_lab_v05']]
    if any(out==p or p in out.parents or out in p.parents for p in protected):
        print('기존 데이터/결과/.venv와 겹치는 출력 금지',file=sys.stderr);return 1
    try:out.mkdir(parents=True,exist_ok=False)
    except OSError as exc:print(f'새 출력 폴더 필요: {exc}',file=sys.stderr);return 1
    rows=[];failed=False
    for symbol in dict.fromkeys(args.symbols):
        try:
            report=audit_symbol(args.data_dir,args.lab_dir,symbol,out/symbol)
            rows.extend(write_report(report,out/symbol));print(f'{symbol}: 감사 완료 {out/symbol}/report.md')
        except (OSError,ValueError,TypeError,KeyError) as exc:
            failed=True;target=out/symbol;target.mkdir(exist_ok=True)
            (target/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n',encoding='utf-8')
            print(f'{symbol}: 감사 실패 — {exc}',file=sys.stderr)
    pd.DataFrame(rows).to_csv(out/'comparison.csv',index=False,encoding='utf-8-sig')
    (out/'status.json').write_text(json.dumps({'status':'partial_or_failed' if failed else 'success','notice':NOTICE,
        'independent_accounts':rows},ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return int(failed)


if __name__=='__main__':raise SystemExit(main())
