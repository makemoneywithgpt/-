"""v0.5 fixed five-policy offline lab; all input files are read-only."""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .engine import Config, run
from .comparison_engine import run_filtered
from .strategy import SMACrossover
from .lab_strategies import Donchian20, NoTrade
from .data import load as load_prices, utc
from .diagnostics import load as load_result, table
from .experiments import compare_symbol, allow_non_neutral, policy_report, NOTICE as BASE_NOTICE
from .report import save

NOTICE=BASE_NOTICE+' 중립 필터는 사후 분석으로 선택한 탐색적/in-sample 정책입니다. 손실 축소만으로 실전 투입할 수 없습니다.'

POLICIES={'A_sma':'기존 SMA20/50','B_sma_filtered':'SMA20/50 + 중립 신규 진입 제한',
          'C_donchian':'돈치안20 돌파','D_donchian_filtered':'돈치안20 + 중립 신규 진입 제한',
          'E_cash':'무거래 현금 기준'}


def loss_streak(trades):
    longest=current=0
    for value in trades.net_pnl:
        current=current+1 if value<0 else 0
        longest=max(longest,current)
    return longest


def verify_result(new,directory,symbol):
    reference,equity,trades=load_result(directory,symbol)
    for key in ['start','end_exclusive']:
        if utc(new.summary[key])!=utc(reference[key]):raise ValueError(f'{directory}: {key} 불일치')
    if new.summary['config']!=reference['config']:raise ValueError(f'{directory}: config 불일치')
    for key in ['net_pnl','total_trades','total_fees','final_equity','net_return_pct','max_drawdown_pct']:
        if not np.isclose(new.summary[key],reference[key],rtol=1e-9,atol=1e-6):
            raise ValueError(f'{directory}: {key} 불일치')
    for name,a,b in [('trades.csv',new.trades,trades),('equity.csv',new.equity,equity)]:
        try:pd.testing.assert_frame_equal(a,b,check_dtype=False,check_exact=False,rtol=1e-9,atol=1e-6)
        except AssertionError as exc:raise ValueError(f'{directory}/{name}: v0.4 결과 불일치: {exc}') from exc


def simulate(prices,symbol,config,start,end):
    """Every call starts five independent engine runs with fresh strategies."""
    results={}
    for name,strategy,filtered in [('A_sma',SMACrossover(),False),('B_sma_filtered',SMACrossover(),True),
                                    ('C_donchian',Donchian20(),False),('D_donchian_filtered',Donchian20(),True),
                                    ('E_cash',NoTrade(),False)]:
        if filtered:
            r=run_filtered(prices,symbol,strategy,config,start=start,end=end,entry_filter=allow_non_neutral)
        else:r=run(prices,symbol,strategy,config,start=start,end=end)
        r.summary['research_policy']=name
        results[name]=r
    return results


def summarize(results):
    policies={}
    for name,result in results.items():
        p=policy_report(result)
        p['max_consecutive_losses']=loss_streak(result.trades)
        p['below_cash_benchmark']=result.summary['net_pnl']<0
        for y in p['yearly']:
            t=result.trades
            selected=t.loc[(pd.to_datetime(t.exit_time,utc=True)>utc(y['start'])) &
                           (pd.to_datetime(t.exit_time,utc=True)<=utc(y['end_exclusive']))]
            y['max_consecutive_losses']=loss_streak(selected)
        policies[name]=p
    return policies


def research_symbol(data_dir,input_dir,v04_dir,symbol,double_costs=False):
    # Mandatory v0.1 baseline audit and unchanged v0.4 A/B execution.
    a,b,_=compare_symbol(data_dir,input_dir,symbol)
    config=Config(**a.summary['config'])
    if asdict(config)!=asdict(Config()):
        raise ValueError('v0.5는 고정 기본 Config(10,000 USDT/1배/수수료 .0004/슬리피지 .0002/위험 1%/손절 2%/익절 4%/72h)만 허용합니다.')
    start,end=utc(a.summary['start']),utc(a.summary['end_exclusive'])
    prices=load_prices(Path(data_dir)/'klines'/symbol/'1h.parquet',start,end)
    # Reuse freshly re-simulated A/B, not any stored trade filtering.
    results={'A_sma':a,'B_sma_filtered':b}
    for name,strategy,filtered in [('C_donchian',Donchian20(),False),('D_donchian_filtered',Donchian20(),True),('E_cash',NoTrade(),False)]:
        runner=run_filtered if filtered else run
        kwargs={'entry_filter':allow_non_neutral} if filtered else {}
        results[name]=runner(prices,symbol,strategy,config,start=start,end=end,**kwargs)
    status='v0.4 원본 폴더 없음: A는 v0.1과 대조 완료, A/B는 기존 v0.4 코드로 새 실행. 저장 v0.4 대조는 미실행.'
    if Path(v04_dir).exists():
        for name,folder in [('A_sma','baseline'),('B_sma_filtered','non_neutral_entry')]:
            verify_result(results[name],Path(v04_dir)/symbol/folder,symbol)
        status='A/B 전체 거래·시간별 자산곡선·config를 저장 v0.4 결과와 대조 통과. A의 v0.1 대조도 통과.'
    report=dict(symbol=symbol,start=start.isoformat(),end_exclusive=end.isoformat(),notice=NOTICE,
                fixed_rules=POLICIES,reference_validation=status,policies=summarize(results))
    sensitivity={}
    if double_costs:
        sensitivity=simulate(prices,symbol,replace(config,fee_rate=config.fee_rate*2,slippage=config.slippage*2),start,end)
        report['double_costs']=summarize(sensitivity)
    else:report['double_costs_status']='미실행 (--double-costs로 별도 민감도 실행 가능)'
    return results,sensitivity,report


def save_policies(results,policies,output):
    for name,result in results.items():
        save(result,output/name)
        p=policies[name]
        pd.DataFrame([{k:v for k,v in y.items() if k!='by_side'} for y in p['yearly']]).to_csv(output/name/'yearly.csv',index=False,encoding='utf-8-sig')
        pd.DataFrame([dict(year=y['period'],**row) for y in p['yearly'] for row in y['by_side']]).to_csv(output/name/'yearly_sides.csv',index=False,encoding='utf-8-sig')
        pd.DataFrame(p['by_side']).to_csv(output/name/'by_side.csv',index=False,encoding='utf-8-sig')


def write(results,sensitivity,report,output):
    save_policies(results,report['policies'],output)
    if sensitivity:save_policies(sensitivity,report['double_costs'],output/'double_costs')
    rows=[]
    text=f'# {report["symbol"]} 전략 연구 v0.5\n\n{NOTICE}\n\n{report["reference_validation"]}\n\n'
    text+='고정 돈치안 규칙: 현 완료 봉은 채널 계산에서 제외하고 이전 20봉 high 최대/low 최소를 사용합니다. close가 상단을 초과하면 롱, 하단 미만이면 숏, 경계는 무신호입니다. 최소 21봉 필요. 다음 시가 체결입니다.\n\n'
    text+='중립 제한은 완료된 SMA50/200 차이 비율 절댓값 0.005 이하 또는 200봉 미만이면 신규 진입만 막고, 반대 신호의 기존 포지션 청산은 유지합니다.\n\n'
    for name,p in report['policies'].items():
        s=p['summary'];rows.append(dict(policy=name,**{k:s[k] for k in ['net_pnl','net_return_pct','total_trades','win_rate_pct','total_fees','profit_factor','max_drawdown_pct']},
                                       max_consecutive_losses=p['max_consecutive_losses'],below_cash_benchmark=p['below_cash_benchmark']))
        text+=f'## {name}: {POLICIES[name]}\n\n무거래보다 낮은가: {p["below_cash_benchmark"]} (True면 순손익이 0 미만).\n\n'+table([{k:v for k,v in y.items() if k!='by_side'} for y in p['yearly']])+'\n롱숏별:\n\n'+table(p['by_side'])+'\n'
    text+='\n## 전체 비교\n\n'+table(rows)
    text+='\n연도별 수익률은 평가자산 변화, net_pnl은 그 해 청산 거래 손익으로 서로 다를 수 있습니다. partial=True는 부분 연도이며 연간 환산하지 않습니다. PF=null은 손실 거래 없음/정의 불가입니다. 모든 시간은 UTC입니다.\n'
    if sensitivity:
        text+='\n## 비용 2배 민감도 (별도 전체 재실행)\n\n'+table([dict(policy=k,net_return_pct=v['summary']['net_return_pct'],net_pnl=v['summary']['net_pnl']) for k,v in report['double_costs'].items()])
    else:text+='\n비용 2배 민감도 미실행: --double-costs 옵션으로 실행할 수 있습니다.\n'
    (output/'report.md').write_text(text,encoding='utf-8')
    pd.DataFrame(rows).to_csv(output/'comparison.csv',index=False,encoding='utf-8-sig')
    (output/'research.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return rows


def main(argv=None):
    parser=argparse.ArgumentParser(description='v0.5 고정 5정책 독립 연구 (오프라인)')
    parser.add_argument('--symbols',nargs='+',choices=['BTCUSDT','ETHUSDT'],default=['BTCUSDT','ETHUSDT'])
    for flag,default in [('data-dir','data'),('input-dir','data/backtests'),('v04-dir','data/experiments'),('output-dir','data/strategy_lab_v05')]:
        parser.add_argument('--'+flag,type=Path,default=Path(default))
    parser.add_argument('--double-costs',action='store_true')
    args=parser.parse_args(argv);output=args.output_dir.resolve()
    protected=[args.input_dir.resolve(),args.v04_dir.resolve(),Path('.venv').resolve()]
    protected += [(args.data_dir/f).resolve() for f in ['klines','backtests','diagnostics','regimes','experiments']]
    if any(output==p or p in output.parents or output in p.parents for p in protected):
        print('출력 폴더가 기존 데이터/보고서/.venv와 겹칩니다.',file=sys.stderr);return 1
    try:output.mkdir(parents=True,exist_ok=False)
    except OSError as exc:print(f'새 출력 폴더 필요: {exc}',file=sys.stderr);return 1
    failed=False;rows=[]
    for symbol in dict.fromkeys(args.symbols):
        try:
            results,sensitivity,report=research_symbol(args.data_dir,args.input_dir,args.v04_dir,symbol,args.double_costs)
            rows.extend([dict(symbol=symbol,**r) for r in write(results,sensitivity,report,output/symbol)])
            print(f'{symbol}: 5정책 연구 완료. {output/symbol}/report.md')
        except (ValueError,TypeError,KeyError,OSError) as exc:
            failed=True;target=output/symbol;target.mkdir(exist_ok=True)
            (target/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n',encoding='utf-8')
            print(f'{symbol}: 연구 실패 — {exc}',file=sys.stderr)
    pd.DataFrame(rows).to_csv(output/'comparison.csv',index=False,encoding='utf-8-sig')
    (output/'comparison.json').write_text(json.dumps(dict(notice=NOTICE,independent_accounts=rows,
        status='partial_or_failed' if failed else 'success'),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return 1 if failed else 0


if __name__=='__main__':raise SystemExit(main())
