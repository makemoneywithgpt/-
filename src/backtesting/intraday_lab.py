"""v0.9 offline fixed intraday study. Only the data CLI may use network."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .intraday_data import validate15, parity, digest, window, STEP
from .intraday_signals import features
from .intraday_engine import IntradayConfig, simulate
from .diagnostics import periods, table
from .root_cause import grouped
from .experiments import NOTICE
from .intraday_window import selected_window,context

NAMES=['pullback_reclaim','breakout_20']


def load_verified(data_dir,v08_dir,symbol,start=None,end=None):
    root=Path(data_dir)/'klines'/symbol;path=root/'15m.parquet';meta=root/'15m.provenance.json'
    a,b=selected_window(symbol,v08_dir,start,end)
    if start is not None and not (root/'integrity.provenance.json').exists():raise ValueError('명시 기간 연구는 완전 검증된 integrity.provenance.json이 필요합니다.')
    if (root/'integrity.provenance.json').exists():
        if meta.exists():raise ValueError('일반/복구 manifest 동시 존재: 모호한 출처 차단')
        from .integrity_loader import validate_bundle
        start,end=a,b
        frame,info=validate_bundle(root,symbol,start,end)
        return frame,info,start,end
    if not path.is_file() or not meta.is_file():raise ValueError(f'{symbol}: 15m 수집/1h 대조가 완료되지 않았습니다. intraday_data 오류를 먼저 해결하세요 ({meta}).')
    info=json.loads(meta.read_text());start,end=a,b
    if info['symbol']!=symbol or info['start']!=start.isoformat() or info['end_exclusive']!=end.isoformat():
        raise ValueError('15m provenance 기간/종목 불일치')
    if info['parity']['status']!='passed' or info['source']!='Binance public data futures/um 15m':raise ValueError('15m 공식 소스/parity 확인 불가')
    if digest(path)!=info['parquet_sha256']:raise ValueError('15m SHA256 무결성 오류')
    f=validate15(pd.read_parquet(path),start,end)
    parity(f,pd.read_parquet(root/'1h.parquet'),start,end)
    return f,info,start,end


def markouts(frame,entries,sides):
    opens=frame.open.to_numpy();closes=frame.close.to_numpy();rows=[]
    for hours in [1,6]:
        valid=entries+hours*4<=len(frame);idx=entries[valid];direction=sides[valid]
        values=direction*(closes[idx+hours*4-1]/opens[idx]-1)*100
        rows.append(dict(horizon_hours=hours,samples=len(values),mean_return_pct=float(values.mean()) if len(values) else None,
                         win_rate_pct=float(np.mean(values>0)*100) if len(values) else None,posthoc_only=True))
    return rows


def period_metrics(equity,trades,start,end,frequency):
    t=trades.copy();t['exit_time']=pd.to_datetime(t.exit_time,utc=True)
    result=periods(equity,t,start,end,10000.,frequency)
    for row in result:
        g=t.loc[(t.exit_time>pd.Timestamp(row['start']))&(t.exit_time<=pd.Timestamp(row['end_exclusive']))]
        gain=float(g.net_pnl.clip(lower=0).sum());loss=-float(g.net_pnl.clip(upper=0).sum())
        row.update(total_trades=len(g),win_rate_pct=float(g.net_pnl.gt(0).mean()*100) if len(g) else 0.,
                   profit_factor=gain/loss if loss else None,total_fees=float(g.fees.sum()))
    return result


def research_symbol(data_dir,v08_dir,symbol,output,start=None,end=None):
    explicit=start is not None or end is not None
    f,provenance,start,end=load_verified(data_dir,v08_dir,symbol,start,end)
    sig=features(f);output.mkdir(parents=True,exist_ok=False)
    sig.to_csv(output/'causal_features.csv',index=False,encoding='utf-8-sig')
    report=dict(symbol=symbol,start=start.isoformat(),end_exclusive=end.isoformat(),notice=NOTICE,
                window_selection=context(start,end,explicit),data_rows=len(f),parity=provenance['parity'],cases={},source_sha256=provenance['parquet_sha256'])
    rows=[]
    for name in [*NAMES,'cash']:
        for multiplier in ([1,2] if name!='cash' else [1]):
            signals=sig[name].to_numpy() if name!='cash' else np.zeros(len(f),dtype=int)
            t,e,events,summary=simulate(f,signals,IntradayConfig(cost_multiplier=multiplier))
            key=f'{name}_cost{multiplier}';target=output/key;target.mkdir()
            t.to_csv(target/'trades.csv',index=False,encoding='utf-8-sig');e.to_csv(target/'equity.csv',index=False,encoding='utf-8-sig')
            events.to_csv(target/'filled_events.csv',index=False,encoding='utf-8-sig')
            raw=sig[name+'_raw'].to_numpy() if name!='cash' else signals
            raw_idx=np.flatnonzero(raw[:-1])+1;admitted_idx=np.flatnonzero(signals[:-1])+1
            filled_idx=pd.Index(f.open_time).get_indexer(pd.DatetimeIndex(events.time)) if len(events) else np.array([],dtype=int)
            populations=dict(raw=dict(count=len(raw_idx),markouts=markouts(f,raw_idx,raw[raw_idx-1])),
                             admitted=dict(count=len(admitted_idx),markouts=markouts(f,admitted_idx,signals[admitted_idx-1])),
                             filled=dict(count=len(events),markouts=markouts(f,filled_idx,events.side.to_numpy(dtype=int))))
            case=dict(summary=summary,monthly=period_metrics(e,t,start,end,'M'),yearly=period_metrics(e,t,start,end,'Y'),
                      by_side=grouped(t,'side'),by_exit_reason=grouped(t,'reason'),signal_populations=populations,
                      hourly_filter_pass_bars=int(sig.trend.ne(0).sum()),neutral_or_warmup_bars=int(sig.trend.eq(0).sum()),
                      average_holding_hours=float(t.holding_hours.mean()) if len(t) else 0.)
            for kind in ['monthly','yearly','by_side','by_exit_reason']:
                pd.DataFrame(case[kind]).to_csv(target/f'{kind}.csv',index=False,encoding='utf-8-sig')
            (target/'summary.json').write_text(json.dumps(case,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
            report['cases'][key]=case
            rows.append(dict(symbol=symbol,strategy=name,cost_multiplier=multiplier,**summary))
    normal=[report['cases'][name+'_cost1']['summary'] for name in NAMES]
    report['research_verdict']='both_loss_reject' if all(s['net_pnl']<0 for s in normal) else 'unproven_do_not_adopt'
    report['stress_robustness']={name:dict(normal_net=report['cases'][name+'_cost1']['summary']['net_pnl'],
        stress_net=report['cases'][name+'_cost2']['summary']['net_pnl'],
        positive_years=sum(y['equity_pnl']>0 for y in report['cases'][name+'_cost1']['yearly']),
        total_years=len(report['cases'][name+'_cost1']['yearly'])) for name in NAMES}
    report['historical_reference']='Issue #10 기재 2023-01-01~2026-11-04T13:00Z BTC 4h SMA +0.7425%는 사용자 과거 참조값(본 실행 검증 아님). 신호·청산·시간축이 달라 동일 정책의 인과 비교가 아니다.'
    if explicit:report['historical_reference']='기간이 다른 기존 스윙 수익률은 비교 기준으로 사용하지 않습니다. 동일 UTC 창·자본·비용의 신규 스윙 실행은 이번 범위에서 미실행입니다.'
    version='v0.9.2 명시 연구 창' if explicit else 'v0.9'
    text=f'# {symbol} {version} 15분봉 단타 연구\n\n기간 UTC: {start.isoformat()} ~ {end.isoformat()} (종료 제외).\n\n{NOTICE}\n\n'+report['historical_reference']+'\n\n'
    text+=report['window_selection']['notice']+'\n\n'
    text+='1h 추세는 닫힌 봉 SMA50/200과 종가/중립 임계값 ±0.005를 사용합니다. 15m EMA20 회복 또는 이전20봉 돌파를 다음 15m 시가에 체결합니다. 반대 신호 청산/반전 없음, SL 1%·TP 2%·최대 6h(24개 15m), 초기 10,000 USDT, 위험 예산 1%, 최대 1배. 모두 고정 사전 가정입니다.\n'
    text+='각 전략과 비용 2배는 처음부터 독립 재실행했습니다. 비용은 개인 계정 실제 등급 확정값이 아니며 펀딩비 0, 실제 마진/청산/호가 미구현입니다. 같은 봉 양쪽 터치는 손절 우선, 손절 갭은 불리한 시가 체결입니다. 갭은 계획 위험 1%를 넘길 수 있습니다.\n'
    flat=[{k:v for k,v in r.items() if k!='config'} for r in rows]
    text+='\n## 전체 비교\n\n'+table(flat)
    for key,case in report['cases'].items():
        text+=f'\n## {key}\n\n연도별 (초기자본 리셋 없음, 부분 기간 표시):\n\n'+table(case['yearly'])
        text+='\n월별:\n\n'+table(case['monthly'])+'\n방향별:\n\n'+table(case['by_side'])+'\n청산 사유:\n\n'+table(case['by_exit_reason'])
        text+=f'\n1h 필터 통과 15m 봉 {case["hourly_filter_pass_bars"]}개 / 중립·워밍업 {case["neutral_or_warmup_bars"]}개, 평균 보유 {case["average_holding_hours"]}h.\n'
        for pop,v in case['signal_populations'].items():text+=f'\n{pop}: {v["count"]}개, 다음 시가 기준 사후 1/6h markout (겹침·비용 제외·미래 진입 판단에 사용 금지):\n\n'+table(v['markouts'])
    text+=f'\n## 선정 판단\n\n{report["research_verdict"]}: 두 기본 정책이 모두 순손실이면 실패로 보고합니다. 양수라도 PF/MDD·월/연도 집중·비용 2배 견고성·장래 검증 없이는 채택하지 않습니다.\n'
    text+='슬리피지 조정분은 이미 체결가/net에 반영되었으며 재차 빼지 않습니다. PF=null은 손실 거래가 없어 정의 불가입니다. 시간봉 내부 청산 시각은 해당 15m 종료 경계로 기록합니다. 무거래 0%는 별도 현금 기준입니다. 보고서 성공은 계산/검증 완료이지 수익성 입증이 아닙니다.\n'
    (output/'report.md').write_text(text,encoding='utf-8')
    (output/'research.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    pd.DataFrame(flat).to_csv(output/'comparison.csv',index=False,encoding='utf-8-sig')
    return flat,report['research_verdict']


def main(argv=None):
    parser=argparse.ArgumentParser(description='v0.9 고정 15m 당일 단타 오프라인 연구')
    parser.add_argument('--symbols',nargs='+',choices=['BTCUSDT','ETHUSDT'],default=['BTCUSDT','ETHUSDT'])
    for flag,default in [('data-dir','data'),('v08-dir','data/timeframe_v08'),('output-dir',None)]:
        parser.add_argument('--'+flag,type=Path,default=Path(default) if default else None)
    parser.add_argument('--start');parser.add_argument('--end')
    args=parser.parse_args(argv);explicit=args.start is not None or args.end is not None
    try:
        chosen=selected_window(None,args.v08_dir,args.start,args.end) if explicit else None
    except ValueError as exc:print(f'기간 오류: {exc}',file=sys.stderr);return 1
    args.output_dir=args.output_dir or Path('data/intraday_v092_window_research' if explicit else 'data/intraday_v09')
    out=args.output_dir.resolve()
    protected=[args.v08_dir.resolve(),Path('.venv').resolve()]+[(args.data_dir/k).resolve() for k in
        ['klines','backtests','diagnostics','regimes','experiments','strategy_lab_v05','root_cause_v06','exit_policy_v07','timeframe_v08','intraday_downloads_v09']]
    # Verified inputs live under a new data-dir; keep original default results protected too.
    protected += [(Path('data')/k).resolve() for k in ['klines','backtests','diagnostics','regimes','experiments','strategy_lab_v05','root_cause_v06','exit_policy_v07','timeframe_v08','intraday_downloads_v09']]
    if explicit:protected += [Path(k).resolve() for k in ['data/intraday_v09','data/intraday_v091_research','data/intraday_integrity_v091','data/intraday_integrity_v092_window','data/intraday_v092_verified_input']]
    if any(out==p or p in out.parents or out in p.parents for p in protected):
        print('기존 데이터/결과/.venv와 겹치는 출력 금지',file=sys.stderr);return 1
    try:out.mkdir(parents=True,exist_ok=False)
    except OSError as exc:print(f'새 출력 폴더 필요: {exc}',file=sys.stderr);return 1
    # Freeze the exact specification before looking at any result.
    spec=dict(implementation_sha256={name:digest(Path(__file__).with_name(name)) for name in ['intraday_lab.py','intraday_engine.py','intraday_signals.py','intraday_data.py']},strategies=NAMES,config={**asdict(IntradayConfig()),'fee_rate':.0004,'slippage':.0002,'stress_multiplier':2},trend='closed 1h SMA50/200, abs gap >= .005 and close aligned',
              ema='span20 adjust=False min_periods20; prior/current crossing',breakout='prior20 excluding current',
              future_protocol='새로 수집한 관찰되지 않은 데이터 최소30일. 사전 정책/성과표를 동결하고 기존 연구와 별도 출력. 최적화/중간 재선정 금지. offline paper tracking only.')
    if chosen is not None:spec['window_selection']=context(*chosen,True)
    (out/'frozen_spec.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (out/'forward_protocol.md').write_text('''# 최소 30일 장래 오프라인 모의 추적 프로토콜

1. frozen_spec.json과 기존 성과표/입력 hash를 별도 보관하고 이번 두 정책/비용/청산을 먼저 동결한다.
2. 이미 관찰한 2023~2026 자료를 미관측 테스트로 재명명하지 않는다. 이후 새로 들어온 관찰되지 않은 연속 30일 이상을 새 출력 폴더에서만 추적한다.
3. 시작 전에 달력 UTC 시작/종료, 데이터 소스, 실패 기준(PF/낙폭/연도 안정성/비용 2배), 제외/누락 처리 규칙을 별도 파일에 기록한다. 누락일을 이익 때문에 선택적으로 제외하지 않는다.
4. 매일 완료된 데이터만 검증해 오프라인 누적 기록한다. 실제 주문/API 키는 사용하지 않는다. 동일 정책/워밍업/비용으로 재현하며 과거 성과와 섞지 않는다.
5. 30일 종료 전 임계값 수정/승자 재선정 금지. 고치면 새 버전과 새 미래 관측 기간을 시작한다. 두 정책 및 무거래를 모두 보고하고 손실/실패도 보존한다.
6. 종료 후 비용후 기대값/거래수/PF/MDD/비용 2배/기간 집중 및 한계를 함께 검토한다. 30일 양수만으로 실거래 수익성은 입증되지 않는다. 이 문서는 프로토콜이며 모의 추적을 실제 완료했다는 뜻이 아니다.
''',encoding='utf-8')
    rows=[];failed=False;verdicts={}
    for symbol in dict.fromkeys(args.symbols):
        try:
            result,verdict=research_symbol(args.data_dir,args.v08_dir,symbol,out/symbol,args.start,args.end)
            rows.extend(result);verdicts[symbol]=verdict
            print(f'{symbol}: 15m 연구 완료 {out/symbol}/report.md; 판단={verdict}')
        except (OSError,ValueError,TypeError,KeyError) as exc:
            failed=True;target=out/symbol;target.mkdir(exist_ok=True)
            (target/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n',encoding='utf-8')
            print(f'{symbol}: 연구 실패 — {exc}',file=sys.stderr)
    pd.DataFrame(rows).to_csv(out/'comparison.csv',index=False,encoding='utf-8-sig')
    (out/'comparison.json').write_text(json.dumps(dict(status='partial_or_failed' if failed else 'success',notice=NOTICE,
        research_verdicts=verdicts,independent_accounts=rows),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return int(failed)


if __name__=='__main__':raise SystemExit(main())
