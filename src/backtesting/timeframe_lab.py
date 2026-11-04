"""v0.8 single-factor signal-timeframe research, always 1h execution."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .data import load as load_prices, utc
from .engine import Config
from .exit_simulator import run_exit_policy
from .exit_policy import funnel
from .strategy import SMACrossover
from .lab_strategies import Donchian20, NoTrade
from .timeframe_signals import ClosedSignal, build_four_hour
from .diagnostics import load as load_result, table
from .strategy_lab import verify_result
from .experiments import policy_report, NOTICE
from .root_cause import decomposition, grouped
from .report import save

STRATEGIES=['B_sma_filtered','D_donchian_filtered']
POLICIES=['base','close_only']


def slippage_ledger(trades,rate):
    rows=[]
    for i,t in enumerate(trades.itertuples(index=False)):
        side=1 if t.side=='long' else -1
        entry_raw=t.entry_price/(1+side*rate);exit_raw=t.exit_price/(1-side*rate)
        adjustment=t.quantity*(abs(t.entry_price-entry_raw)+abs(t.exit_price-exit_raw))
        rows.append(dict(trade_index=i,exit_time=t.exit_time,modeled_fill_adjustment=adjustment))
    return pd.DataFrame(rows,columns=['trade_index','exit_time','modeled_fill_adjustment'])


def execute(prices,symbol,config,strategy_name,hours,policy):
    strategy=SMACrossover() if strategy_name=='B_sma_filtered' else Donchian20()
    adapter=ClosedSignal(strategy,hours,prices);events=[]
    result=run_exit_policy(prices,symbol,adapter,config,start=prices.open_time.iloc[0],
                          end=prices.open_time.iloc[-1]+pd.Timedelta(hours=1),exit_policy=policy,
                          entry_filter=adapter.entry_filter,events=events)
    return result,events


def describe(result,events,prices):
    report=policy_report(result);t=result.trades
    report.update(signal_funnel=funnel(events,prices),by_exit_reason=grouped(t,'reason'),decomposition=decomposition(t))
    slip=slippage_ledger(t,result.summary['config']['slippage'])
    total=float(slip.modeled_fill_adjustment.sum())
    report['modeled_fill_slippage_total']=total
    report['modeled_fill_slippage_per_trade']=total/len(t) if len(t) else 0.
    for year in report['yearly']:
        if len(slip):
            exits=pd.to_datetime(slip.exit_time,utc=True)
            value=float(slip.loc[(exits>utc(year['start']))&(exits<=utc(year['end_exclusive'])),'modeled_fill_adjustment'].sum())
        else:value=0.
        year['modeled_fill_slippage']=value
    return report,slip


def save_case(result,events,report,slip,output):
    save(result,output)
    pd.DataFrame(events).to_csv(output/'signal_events.csv',index=False,encoding='utf-8-sig')
    slip.to_csv(output/'slippage_ledger.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame([{k:v for k,v in y.items() if k!='by_side'} for y in report['yearly']]).to_csv(output/'yearly.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame([dict(year=y['period'],**side) for y in report['yearly'] for side in y['by_side']]).to_csv(output/'yearly_sides.csv',index=False,encoding='utf-8-sig')
    for key in ['by_side','by_exit_reason']:
        pd.DataFrame(report[key]).to_csv(output/f'{key}.csv',index=False,encoding='utf-8-sig')
    for kind,value in report['signal_funnel'].items():
        pd.DataFrame(value['forward_returns']).to_csv(output/f'{kind}_markouts.csv',index=False,encoding='utf-8-sig')


def research_symbol(data_dir,v07_dir,symbol,output):
    source=Path(v07_dir)/symbol/'B_sma_filtered'/'base'
    reference,_,_=load_result(source,symbol)
    config=Config(**reference['config']);start=utc(reference['start']);end=utc(reference['end_exclusive'])
    if asdict(config)!=asdict(Config()) or reference.get('funding_model')!='unavailable_assumed_zero':
        raise ValueError('v0.8은 고정 기본 Config와 펀딩비 0 가정만 허용합니다.')
    prices=load_prices(Path(data_dir)/'klines'/symbol/'1h.parquet',start,end)
    output.mkdir(parents=True,exist_ok=False)
    four=build_four_hour(prices)
    four.to_csv(output/'constructed_4h.csv',index=False,encoding='utf-8-sig')
    report=dict(symbol=symbol,start=start.isoformat(),end_exclusive=end.isoformat(),notice=NOTICE,
                execution_hours=1,max_holding_hours=config.max_holding_hours,
                four_hour_count=len(four),closed_4h_last_available=four.available_at.iloc[-1].isoformat() if len(four) else None,
                leading_or_trailing_unaggregated_hours=len(prices)-4*len(four),cases={})
    rows=[]
    for name in STRATEGIES:
        for policy in POLICIES:
            for hours in [1,4]:
                result,events=execute(prices,symbol,config,name,hours,policy)
                if hours==1:verify_result(result,Path(v07_dir)/symbol/name/policy,symbol)
                key=f'{name}_{hours}h_{policy}'
                r,slip=describe(result,events,prices);r['v07_path_verified']=hours==1
                save_case(result,events,r,slip,output/key);report['cases'][key]=r
                s=result.summary
                rows.append(dict(symbol=symbol,strategy=name,signal_hours=hours,exit_policy=policy,**{k:s[k] for k in
                    ['net_pnl','net_return_pct','total_trades','win_rate_pct','total_fees','profit_factor','max_drawdown_pct']},
                    fee_per_trade=r['decomposition']['fee_per_trade'],average_pnl=r['decomposition']['expected_net'],
                    modeled_fill_slippage=r['modeled_fill_slippage_total'],slippage_per_trade=r['modeled_fill_slippage_per_trade'],
                    raw_signals=r['signal_funnel']['raw']['count'],admitted_signals=r['signal_funnel']['admitted']['count'],
                    filled_entries=r['signal_funnel']['filled']['count']))
    cash_events=[]
    cash=run_exit_policy(prices,symbol,NoTrade(),config,start=start,end=end,events=cash_events)
    r,slip=describe(cash,cash_events,prices);report['cases']['cash']=r
    save_case(cash,cash_events,r,slip,output/'cash')
    rows.append(dict(symbol=symbol,strategy='cash',signal_hours=0,exit_policy='none',net_pnl=0.,net_return_pct=0.,
                     total_trades=0,win_rate_pct=0.,total_fees=0.,profit_factor=None,max_drawdown_pct=0.,
                     fee_per_trade=0.,average_pnl=0.,modeled_fill_slippage=0.,slippage_per_trade=0.,raw_signals=0,admitted_signals=0,filled_entries=0))
    text=f'# {symbol} v0.8 신호 시간축 비교\n\n{NOTICE}\n\n'+table(rows)+'\n'
    text+='모든 실행은 같은 1h 입력·초기 자본·비용·위험으로 독립 재실행합니다. 1h 네 경우는 저장 v0.7 거래/자산곡선 대조를 통과했습니다. cash는 무거래 0% 참조입니다. 공동 포트폴리오가 아닌 독립 계정입니다.\n'
    text+='4h는 UTC 00/04/08/12/16/20 시작의 정확한 4개 1h 봉을 OHLC 합성합니다. 시작/끝 부분 버킷은 제외하며 내부 누락/중복/역순은 오류입니다. 1h 실행 데이터 자체도 완료 봉만 허용합니다.\n'
    text+='4h 신호/필터는 종료 시각까지 닫힌 4h 봉만 사용합니다. 예: 00~04 봉이 닫히면 04:00 1h 시가에서 최초 처리합니다. 중간 3시간은 신호 0이며 다음 경계까지 신호를 반복하지 않습니다. 신호가 없는 시간에도 손절/익절/갭/72시간은 매시간 검사합니다.\n'
    text+='SMA20/50, 돈치안20, 필터 SMA50/200과 ±0.005는 고정입니다. 4h의 20/50/200은 4h 봉 개수이므로 실제 관측 기간과 워밍업도 4배 길어집니다. 임계값 탐색이나 시간창 보정은 하지 않습니다. 시작 이전 가격을 워밍업에 추가하지 않습니다.\n'
    text+='markout은 1/6/24/72 실제 시간 후 1h 종가 기준의 겹치는 사후 표본입니다. 비용·수량을 제외한 방향 조정 가격수익률이며 거래 기대손익이 아닙니다. raw/admitted/filled를 구분하고 말단 관측 부족은 제외합니다.\n'
    text+='slippage_ledger는 체결가의 지정 비율 조정분을 원래 기준가로 역산한 명목 비용입니다. 손절 기준가/수량/경로도 영향을 받으므로 슬리피지의 전체 인과 효과가 아닙니다. 이미 net에 포함되어 있어 다시 차감하면 안 됩니다.\n'
    for key,r in report['cases'].items():
        text+=f'\n## {key}\n\n연도별 (partial=True는 부분 연도, 연간 원금 리셋 없음):\n\n'+table([{k:v for k,v in y.items() if k!='by_side'} for y in r['yearly']])
        text+='\n롱숏:\n\n'+table(r['by_side'])+'\n청산 이유:\n\n'+table(r['by_exit_reason'])
        for kind,value in r['signal_funnel'].items():text+=f'\n{kind}: {value["count"]}건 (사후 설명)\n\n'+table(value['forward_returns'])
    text+='\n## 해석의 한계\n\n4h가 1h 손실 원인을 해결한다는 인과 증거가 아닙니다. 비용후 수익과 MDD를 함께 비교하세요. PF=null은 손실 거래 없음/정의 불가입니다. 연도별 평가손익과 청산 거래 손익은 다를 수 있습니다. 2023~2026은 전부 관찰한 탐색적 데이터이며 양의 성과도 미래 수익 인증이 아닙니다. 사용자 Windows 실데이터 실행은 별도입니다.\n'
    (output/'report.md').write_text(text,encoding='utf-8')
    (output/'timeframe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    pd.DataFrame(rows).to_csv(output/'comparison.csv',index=False,encoding='utf-8-sig')
    return rows


def main(argv=None):
    parser=argparse.ArgumentParser(description='v0.8 1h 실행 유지 신호 시간축 비교')
    parser.add_argument('--symbols',nargs='+',choices=['BTCUSDT','ETHUSDT'],default=['BTCUSDT','ETHUSDT'])
    for flag,default in [('data-dir','data'),('v07-dir','data/exit_policy_v07'),('output-dir','data/timeframe_v08')]:
        parser.add_argument('--'+flag,type=Path,default=Path(default))
    args=parser.parse_args(argv);out=args.output_dir.resolve()
    protected=[args.v07_dir.resolve(),Path('.venv').resolve()]+[(args.data_dir/k).resolve() for k in
        ['klines','backtests','diagnostics','regimes','experiments','strategy_lab_v05','root_cause_v06','exit_policy_v07']]
    if any(out==p or p in out.parents or out in p.parents for p in protected):
        print('기존 데이터/결과/.venv와 겹치는 출력 금지',file=sys.stderr);return 1
    try:out.mkdir(parents=True,exist_ok=False)
    except OSError as exc:print(f'새 출력 폴더 필요: {exc}',file=sys.stderr);return 1
    rows=[];failed=False
    for symbol in dict.fromkeys(args.symbols):
        try:
            rows.extend(research_symbol(args.data_dir,args.v07_dir,symbol,out/symbol))
            print(f'{symbol}: 시간축 비교 완료 {out/symbol}/report.md')
        except (OSError,ValueError,TypeError,KeyError) as exc:
            failed=True;target=out/symbol;target.mkdir(exist_ok=True)
            (target/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n',encoding='utf-8')
            print(f'{symbol}: 비교 실패 — {exc}',file=sys.stderr)
    pd.DataFrame(rows).to_csv(out/'comparison.csv',index=False,encoding='utf-8-sig')
    (out/'comparison.json').write_text(json.dumps(dict(status='partial_or_failed' if failed else 'success',notice=NOTICE,
        independent_accounts=rows),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return int(failed)


if __name__=='__main__':raise SystemExit(main())
