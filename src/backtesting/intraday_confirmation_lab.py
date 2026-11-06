"""v0.9.4 fixed ETH confirmation experiment. Existing engines are immutable."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .data import utc
from .intraday_data import STEP,digest
from .intraday_window import V092_START,V092_END
from .intraday_lab import load_verified,period_metrics,markouts
from .intraday_engine import IntradayConfig,simulate
from .intraday_signals import features
from .intraday_stop_diagnostics import verify_reproduction
from .root_cause import grouped,decomposition
from .diagnostics import table
from .experiments import NOTICE

POLICIES=['breakout_20','breakout_20_delay1','breakout_20_confirm1']


def build_policies(frame,causal):
    """Signal at row j always executes at j+1 by the unchanged simulator.

    Delayed/confirmed row j uses only the source signal/close at j-1 and close,
    closed-hour trend at j. One candidate per source row; consecutive candidates
    remain independent. Delay-only does NOT recheck trend at j.
    """
    if len(frame)!=len(causal):raise ValueError('신호 데이터 길이 불일치')
    base=causal.breakout_20.to_numpy(dtype=int).copy();trend=causal.trend.to_numpy(dtype=int)
    if not np.isin(base,[-1,0,1]).all() or not np.isin(trend,[-1,0,1]).all():raise ValueError('신호/추세 값 오류')
    previous=np.zeros(len(base),dtype=int);previous[1:]=base[:-1]
    close=frame.close.to_numpy();delta=np.r_[0.,np.diff(close)]
    progress=previous*delta>0
    same_trend=(trend==previous)&(previous!=0)
    confirm=np.where(progress&same_trend,previous,0)
    delay=previous.copy()
    stats=dict(source_candidates=int(np.count_nonzero(base)),
        no_confirmation_bar=int(bool(len(base) and base[-1])),
        confirm_failed_progress=int(np.count_nonzero((previous!=0)&~progress)),
        confirm_failed_trend=int(np.count_nonzero((previous!=0)&~same_trend)),
        confirm_passed=int(np.count_nonzero(confirm)),
        confirmed_no_execution_bar=int(bool(len(confirm) and confirm[-1])),
        delay_no_execution_bar=int(bool(len(delay) and delay[-1])),
        failure_counts_can_overlap=True,delay_trend_recheck=False)
    # The last closed candle has no next open inside the frozen window.
    if len(confirm):confirm[-1]=0;delay[-1]=0
    arrays=dict(breakout_20=base,breakout_20_delay1=delay,breakout_20_confirm1=confirm)
    events=pd.DataFrame(dict(signal_time=frame.open_time+STEP,original_candidate=base,previous_candidate=previous,
        previous_close=frame.close.shift(1),current_close=close,closed_hour_trend=trend,
        delay1_signal=delay,confirm1_signal=confirm,confirm_progress=progress,confirm_trend_pass=same_trend))
    return arrays,events,stats


def populations(frame,causal,signals,filled):
    raw=causal.breakout_20_raw.to_numpy(dtype=int)
    raw_idx=np.flatnonzero(raw[:-1])+1;signal_idx=np.flatnonzero(signals[:-1])+1
    filled_idx=pd.Index(frame.open_time).get_indexer(pd.DatetimeIndex(filled.time)) if len(filled) else np.array([],dtype=int)
    return dict(original_raw_count=len(raw_idx),policy_signal_count=len(signal_idx),filled_count=len(filled),
        original_raw_markouts=markouts(frame,raw_idx,raw[raw_idx-1]),
        policy_signal_markouts=markouts(frame,signal_idx,signals[signal_idx-1]),
        filled_markouts=markouts(frame,filled_idx,filled.side.to_numpy(dtype=int)))


def dual_touch_count(trades,frame):
    """Actual held intervals; never used by a signal or a selection rule."""
    count=0
    index=pd.Index(frame.open_time)
    for t in trades.itertuples(index=False):
        left=index.get_indexer([t.entry_time])[0]
        right=index.searchsorted(t.exit_time,side='left')
        held=frame.iloc[left:right]
        side=1 if t.side=='long' else -1
        stop=t.entry_price*(1-side*.01);take=t.entry_price*(1+side*.02)
        both=(held.low<=stop)&(held.high>=take) if side==1 else (held.high>=stop)&(held.low<=take)
        count+=int(both.sum())
    return count


def case_details(frame,causal,signals,trades,equity,filled,summary,start,end):
    stops=int(trades.reason.isin(['stop','stop_gap']).sum())
    yearly=period_metrics(equity,trades,start,end,'Y');monthly=period_metrics(equity,trades,start,end,'M')
    for period in [*yearly,*monthly]:
        exits=pd.to_datetime(trades.exit_time,utc=True)
        selected=trades.loc[(exits>utc(period['start']))&(exits<=utc(period['end_exclusive']))]
        n=int(selected.reason.isin(['stop','stop_gap']).sum())
        period.update(stops=n,stop_ratio_pct=100*n/len(selected) if len(selected) else 0.)
    return dict(summary=summary,stop_count=stops,stop_ratio_pct=100*stops/len(trades) if len(trades) else 0.,
        decomposition=decomposition(trades),by_side=grouped(trades,'side'),by_exit_reason=grouped(trades,'reason'),
        yearly=yearly,monthly=monthly,signals=populations(frame,causal,signals,filled),
        double_touch_ambiguity_count=dual_touch_count(trades,frame))


def save_case(output,trades,equity,filled,details):
    output.mkdir(parents=True,exist_ok=False)
    trades.to_csv(output/'trades.csv',index=False,encoding='utf-8-sig');equity.to_csv(output/'equity.csv',index=False,encoding='utf-8-sig')
    filled.to_csv(output/'filled_events.csv',index=False,encoding='utf-8-sig')
    for key in ['yearly','monthly','by_side','by_exit_reason']:pd.DataFrame(details[key]).to_csv(output/f'{key}.csv',index=False,encoding='utf-8-sig')
    (output/'summary.json').write_text(json.dumps(details,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def write_report(report,output):
    rows=[]
    for name,case in report['cases'].items():
        s=case['summary'];rows.append(dict(policy=name,**{k:s[k] for k in ['total_trades','net_pnl','net_return_pct','max_drawdown_pct','profit_factor','win_rate_pct','total_fees','total_slippage_adjustment','average_net_pnl']},
            stops=case['stop_count'],stop_ratio_pct=case['stop_ratio_pct'],policy_signals=case['signals']['policy_signal_count'],filled_entries=case['signals']['filled_count']))
    report['differences']={}
    for multiplier in [1,2]:
        c=report['cases'][f'breakout_20_confirm1_cost{multiplier}']
        for control in ['breakout_20','breakout_20_delay1']:
            b=report['cases'][f'{control}_cost{multiplier}']
            report['differences'][f'confirm1_minus_{control}_cost{multiplier}']=dict(net_return_percentage_points=c['summary']['net_return_pct']-b['summary']['net_return_pct'],
                mdd_percentage_points=c['summary']['max_drawdown_pct']-b['summary']['max_drawdown_pct'],stop_ratio_percentage_points=c['stop_ratio_pct']-b['stop_ratio_pct'],
                markout_changes=[dict(horizon_hours=x['horizon_hours'],signal_mean_percentage_points=x['mean_return_pct']-y['mean_return_pct'] if x['mean_return_pct'] is not None and y['mean_return_pct'] is not None else None,
                    filled_mean_percentage_points=u['mean_return_pct']-v['mean_return_pct'] if u['mean_return_pct'] is not None and v['mean_return_pct'] is not None else None)
                    for x,y,u,v in zip(c['signals']['policy_signal_markouts'],b['signals']['policy_signal_markouts'],c['signals']['filled_markouts'],b['signals']['filled_markouts'])])
    normals=[report['cases'][f'{name}_cost1']['summary']['net_pnl'] for name in POLICIES]
    report['research_verdict']='all_normal_policies_loss_reject' if all(x<0 for x in normals) else 'unproven_do_not_adopt'
    text='# ETHUSDT v0.9.4 1봉 추가 확인 고정 실험\n\n'+NOTICE+'\n\n'
    text+=f'기간 UTC {report["start"]} ~ {report["end_exclusive"]} (종료 제외). 기준 재현: {json.dumps(report["baseline_reproduction"],ensure_ascii=False)}\n\n'
    text+='기존 breakout_20은 t 완료 신호→t+1 시가입니다. delay1은 같은 신호를 한 봉 늦춰 t+2 시가로 보내며 t+1 추세 재검사는 하지 않습니다. confirm1은 t+1 완료 종가가 t 종가보다 방향상 엄격히 추가 진행하고, t+1의 완료 1h 추세가 같은 방향일 때만 t+2 시가 진입합니다. 같거나 역행·중립·반전이면 취소합니다.\n'
    text+='후보는 원래 각 신호 봉마다 독립입니다. 연속 중첩 후보는 한 봉씩 이동하며, 포지션 보유 중 신호 무시는 기존 엔진 그대로입니다. 말단 미확인/확인후 다음시가 없는 후보는 버리고 횟수를 공개합니다. 진행/추세 실패 사유 수는 겹칠 수 있습니다.\n'
    text+='초기 10,000 USDT/위험1%/최대1배/SL1%/TP2%/6h(실제진입부터24봉)/stop-first 및 양쪽 비용은 불변입니다. 비용 1x/2x마다 새 계정으로 독립 재실행하고 기존 거래를 삭제하는 방식은 쓰지 않습니다. 슬리피지는 net에 포함되므로 재차 빼지 않습니다.\n'
    text+='\n## 전체 비교\n\n'+table(rows)+'\n후보 통계: '+json.dumps(report['candidate_counts'],ensure_ascii=False)+'\n'
    for name,case in report['cases'].items():
        text+=f'\n## {name}\n\n연도별 (연말 원금 리셋 없음, partial=True는 부분 연도):\n\n'+table(case['yearly'])
        text+='\n월별 집중:\n\n'+table(case['monthly'])+'\n방향별:\n\n'+table(case['by_side'])+'\n청산 사유:\n\n'+table(case['by_exit_reason'])
        text+=f'\n동시 SL/TP 터치 봉 {case["double_touch_ambiguity_count"]}개. 기존 stop-first 유지.\n'
        for key in ['original_raw_markouts','policy_signal_markouts','filled_markouts']:
            text+=f'\n{key}: 겹치는 표본·고정단위·비용제외의 사후 가격수익률, 실제 거래 수익 아님:\n\n'+table(case['signals'][key])
    text+='\n## 확인 대 기준/지연 통제 차이\n\n'+table([dict(comparison=k,**{a:b for a,b in v.items() if a!='markout_changes'}) for k,v in report['differences'].items()])
    for name,value in report['differences'].items():text+=f'\n{name}: 사후 markout 차이(표본 선택이 다르며 인과효과 아님)\n\n'+table(value['markout_changes'])
    text+='\n## 판단과 한계\n\n'+report['research_verdict']+'\n\n'
    text+='기본 비용 세 정책이 모두 순손실이면 연구 실패입니다. 어느 정책이 양수여도 자동 채택하지 않습니다. delay1은 지연 효과를 구분하기 위한 통제이며 추가 승자 탐색 후보가 아닙니다. 확인/지연은 자본·포지션 경로도 바꾸므로 결과 차이를 순수 인과 효과로 단정하지 않습니다.\n'
    text+='같은 관찰 자료의 손절 분석 후 제안한 변형으로 in-sample/후향적·다중비교 편향이 있습니다. ATR 분위/새 임계값을 사용하지 않았습니다. 기존 -45.126% 및 기존 보고서는 그대로입니다. 기간이 다른 4h 스윙과 비교하지 않습니다. 비용2배/월·연도 안정성/독립 미래30일 이상 오프라인 모의 추적 전 실거래 가능성을 주장하지 않습니다. 원본 SHA·규칙 snapshot은 별도 파일과 comparison.json에 남깁니다.\n'
    (output/'report.md').write_text(text,encoding='utf-8')
    (output/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    pd.DataFrame(rows).to_csv(output/'comparison.csv',index=False,encoding='utf-8-sig')
    return rows


def main(argv=None):
    parser=argparse.ArgumentParser(description='ETH v0.9.4 고정 1봉 확인/지연 통제 실험')
    for flag,default in [('data-dir','data/intraday_v092_verified_input'),('research-dir','data/intraday_v092_window_research'),('v08-dir','data/timeframe_v08'),('output-dir','data/intraday_v094_confirmation_research')]:
        parser.add_argument('--'+flag,type=Path,default=Path(default))
    args=parser.parse_args(argv);out=args.output_dir.resolve()
    protected=[args.data_dir.resolve(),args.research_dir.resolve(),args.v08_dir.resolve(),Path('.venv').resolve()]
    protected += [(Path('data')/k).resolve() for k in ['klines','backtests','diagnostics','regimes','experiments','strategy_lab_v05','root_cause_v06','exit_policy_v07','timeframe_v08','intraday_downloads_v09','intraday_v09','intraday_v09_verified_input','intraday_v091_research','intraday_integrity_v091','intraday_integrity_v091_cache','intraday_integrity_v092_window','intraday_v092_verified_input','intraday_v092_window_research','intraday_v093_stop_diagnostics']]
    if any(out==p or p in out.parents or out in p.parents for p in protected):
        print('원본 데이터/결과/.venv와 겹치는 출력 금지',file=sys.stderr);return 1
    try:out.mkdir(parents=True,exist_ok=False)
    except OSError as exc:print(f'새 출력 경로 필요: {exc}',file=sys.stderr);return 1
    target=out/'ETHUSDT';target.mkdir();pins={}
    rules=dict(period=[V092_START,V092_END],policies=POLICIES,delay_rechecks_trend=False,confirmation='strict directional close progress + unchanged closed-hour trend at next completed 15m',
        execution='source t close -> confirmation t+1 close -> t+2 open',config={**vars(IntradayConfig()),'fee_rate':IntradayConfig().fee_rate,'slippage':IntradayConfig().slippage},stress_multipliers=[1,2],
        source_code_sha256={name:digest(Path(__file__).with_name(name)) for name in ['intraday_confirmation_lab.py','intraday_engine.py','intraday_signals.py']})
    (out/'frozen_rules.json').write_text(json.dumps(rules,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    try:
        source=args.research_dir/'ETHUSDT/breakout_20_cost1'
        paths=[source/k for k in ['trades.csv','equity.csv','summary.json']]+[args.data_dir/'klines/ETHUSDT'/k for k in ['15m.parquet','1h.parquet','integrity.provenance.json']]
        pins={str(p):digest(p) for p in paths}
        (out/'input_sha256.json').write_text(json.dumps(pins,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        start,end=utc(V092_START),utc(V092_END)
        frame,provenance,a,b=load_verified(args.data_dir,args.v08_dir,'ETHUSDT',start,end)
        if (a,b)!=(start,end) or len(frame)!=60864:raise ValueError('고정 연구 기간/60864봉 불일치')
        causal=features(frame);arrays,signal_events,counts=build_policies(frame,causal)
        bt,be,bf,bs=simulate(frame,arrays['breakout_20'],IntradayConfig())
        reproduced=verify_reproduction(bt,be,bs,source)
        signal_events.to_csv(target/'policy_signals.csv',index=False,encoding='utf-8-sig')
        report=dict(symbol='ETHUSDT',start=start.isoformat(),end_exclusive=end.isoformat(),notice=NOTICE,rules=rules,candidate_counts=counts,
                    source_sha256=pins,parity=provenance['parity'],baseline_reproduction=reproduced,cases={})
        for name in [*POLICIES,'cash']:
            for multiplier in ([1,2] if name!='cash' else [1]):
                signals=arrays[name] if name!='cash' else np.zeros(len(frame),dtype=int)
                if name=='breakout_20' and multiplier==1:t,e,filled,summary=bt,be,bf,bs
                else:t,e,filled,summary=simulate(frame,signals,IntradayConfig(cost_multiplier=multiplier))
                case=case_details(frame,causal,signals,t,e,filled,summary,start,end)
                key=f'{name}_cost{multiplier}';save_case(target/key,t,e,filled,case);report['cases'][key]=case
        if any(digest(Path(p))!=sha for p,sha in pins.items()):raise ValueError('연구 중 원본 SHA 변경 감지')
        rows=write_report(report,target)
        (out/'comparison.json').write_text(json.dumps(dict(status='success',research_verdict=report['research_verdict'],rows=rows),ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        pd.DataFrame(rows).to_csv(out/'comparison.csv',index=False,encoding='utf-8-sig')
        (out/'status.json').write_text(json.dumps(dict(status='success',baseline_reproduction_verified=True),indent=2)+'\n')
        print(f'ETHUSDT: 1031/502 기준 재현 및 독립 3정책 비교 완료. {target}/report.md');return 0
    except (OSError,ValueError,TypeError,KeyError) as exc:
        (target/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n')
        (out/'status.json').write_text(json.dumps(dict(status='failed',baseline_reproduction_verified=False),indent=2)+'\n')
        (out/'comparison.json').write_text(json.dumps(dict(status='failed',error=str(exc)),ensure_ascii=False)+'\n')
        print(f'ETHUSDT 비교 중단 — {exc}',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
