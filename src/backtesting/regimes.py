"""Read-only offline entry-time regime attribution. No strategy/order changes."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .data import validate, utc
from .diagnostics import DiagnosticError, dates, numeric, require, fail, table

HOUR = pd.Timedelta(hours=1)
FAST, SLOW, THRESHOLD = 50, 200, .005
NOTICE = ('고정 SMA50/SMA200 차이÷SMA200가 +0.5% 초과이면 상승추세, -0.5% 미만이면 하락추세, '
          '경계값 포함 나머지는 이 규칙에서 중립으로 분류합니다. 실제 횡보의 증명이 아닙니다. '
          '완료된 과거 200개 시간봉만 사용하며 임계값 탐색/전략 최적화는 수행하지 않습니다. '
          '손익과 분류의 관계는 기술 통계이며 인과관계나 미래 수익성을 증명하지 않습니다. '
          '종목별 독립 계정, 펀딩비 0 가정, 최대 1배, 시간별 종가 MDD, '
          '실제 마진·강제청산·호가 미반영입니다. Windows 실제 파일 성과를 클라우드에서 검증한 결과가 아닙니다.')


def classify(candles, entry):
    """Select by timestamp BEFORE reading any price feature, including entry bar."""
    history = candles.loc[candles.open_time + HOUR <= entry].tail(SLOW)
    result = dict(regime='unclassified',classification_reason='진입 전 완료 봉 200개 부족',
                  last_completed_open=None,sma50=None,sma200=None,trend_direction='unknown',
                  trend_strength=None,signed_sma_gap=None)
    if len(history)<SLOW:
        return result
    if not history.open_time.diff().iloc[1:].eq(HOUR).all() or history.open_time.iloc[-1]+HOUR != entry:
        result['classification_reason']='진입 직전 과거 시간봉 누락 또는 정시 경계 불일치'
        return result
    fast=float(history.close.tail(FAST).mean());slow=float(history.close.mean())
    gap=(fast-slow)/slow
    regime='uptrend' if gap>THRESHOLD else 'downtrend' if gap < -THRESHOLD else 'neutral'
    result.update(regime=regime,classification_reason='고정 SMA 규칙',last_completed_open=history.open_time.iloc[-1].isoformat(),
                  sma50=fast,sma200=slow,signed_sma_gap=gap,trend_strength=abs(gap),
                  trend_direction='up' if gap>0 else 'down' if gap<0 else 'flat')
    return result


def load_inputs(root, prices, symbol):
    root=Path(root)/symbol;file=root/'summary.json';tfile=root/'trades.csv'
    if (root/'error.json').exists():fail(root/'error.json','원본 백테스트 실패 상태')
    summary=json.loads(file.read_text(encoding='utf-8'))
    if summary.get('symbol')!=symbol:fail(file,'종목 불일치')
    start,end=utc(summary['start']),utc(summary['end_exclusive'])
    trades=pd.read_csv(tfile)
    require(trades,['entry_time','exit_time','side','reason','net_pnl','fees','gross_pnl','funding_cost'],tfile)
    for col in ['entry_time','exit_time']:dates(trades,col,tfile)
    numeric(trades,['net_pnl','fees','gross_pnl','funding_cost'],tfile)
    bad=((trades.entry_time<start)|(trades.entry_time>=end)|(trades.exit_time>end)|(trades.entry_time>trades.exit_time)
         |~trades.side.isin(['long','short'])|trades.reason.isna()|trades.fees.lt(0)
         |trades.entry_time.ne(trades.entry_time.dt.floor('h')))
    if bad.any():fail(tfile,'거래 기간/방향/비용/시간 오류',int(np.flatnonzero(bad)[0]))
    bad=trades.exit_time.diff().lt(pd.Timedelta(0))|trades.duplicated()
    if bad.any():fail(tfile,'중복 또는 역순 거래',int(np.flatnonzero(bad)[0]))
    for i in range(1,len(trades)):
        if trades.entry_time.iloc[i]<trades.exit_time.iloc[i-1]:fail(tfile,'거래 시간 겹침',i)
    if len(trades)!=summary['total_trades']:fail(file,'거래 수 불일치')
    for key,column in [('net_pnl','net_pnl'),('total_fees','fees')]:
        if not np.isclose(trades[column].sum(),summary[key],rtol=1e-9,atol=1e-6):fail(file,f'{key} 합 불일치')
    bad=~np.isclose(trades.gross_pnl-trades.fees-trades.funding_cost,trades.net_pnl,rtol=1e-9,atol=1e-6)
    if bad.any():fail(tfile,'거래 순손익 회계 불일치',int(np.flatnonzero(bad)[0]))
    p=Path(prices)/'klines'/symbol/'1h.parquet';candles=pd.read_parquet(p)
    require(candles,['open_time','open','high','low','close','volume'],p)
    dates(candles,'open_time',p)
    bad=candles.open_time.diff().le(pd.Timedelta(0))
    if bad.any():fail(p,'중복 또는 역순 시간봉',int(np.flatnonzero(bad)[0]))
    # Validate the whole available past prefix, including pre-start warmup.
    candles=candles.loc[candles.open_time<end].copy()
    if candles.empty:fail(p,'기간 내 가격 데이터 없음')
    numeric(candles,['open','high','low','close','volume'],p)
    invalid=(candles[['open','high','low','close']].le(0).any(axis=1) | candles.volume.lt(0)
             | candles.high.lt(candles[['open','low','close']].max(axis=1))
             | candles.low.gt(candles[['open','high','close']].min(axis=1))
             | candles.open_time.ne(candles.open_time.dt.floor('h')))
    if invalid.any():fail(p,'비정상 가격/거래량/시간',int(np.flatnonzero(invalid)[0]))
    try:candles=validate(candles,min(start,candles.open_time.iloc[0]),end)
    except ValueError as exc:fail(p,str(exc))
    expected=int((end-start)/HOUR)
    if expected!=summary['candle_count']:fail(file,'기간과 candle_count 불일치')
    if 'first_candle' in summary and utc(summary['first_candle'])!=start:fail(file,'시작 봉 불일치')
    if 'last_candle' in summary and utc(summary['last_candle'])!=end-HOUR:fail(file,'마지막 봉 불일치')
    return summary,trades,candles


def aggregate(trades,columns):
    rows=[]
    for keys,g in trades.groupby(columns,dropna=False,sort=True):
        if not isinstance(keys,tuple):keys=(keys,)
        rows.append({**dict(zip(columns,keys)), 'count':len(g),'win_rate_pct':float(g.net_pnl.gt(0).mean()*100),
                     'expected_pnl':float(g.net_pnl.mean()),'net_pnl':float(g.net_pnl.sum()),'fees':float(g.fees.sum())})
    return rows


def analyze(summary,trades,candles,diagnostic=None):
    labeled=trades.copy()
    labels=pd.DataFrame([classify(candles,t) for t in trades.entry_time],columns=list(classify(candles,utc(summary['start']))))
    for col in labels:labeled[col]=labels[col].to_numpy()
    windows=[]
    month='2023-07-01' if summary['symbol']=='BTCUSDT' else '2024-03-01'
    left=utc(month);right=left+pd.offsets.MonthBegin(1)
    windows.append(('requested_loss_month',left,right))
    if diagnostic is not None:
        if diagnostic['symbol']!=summary['symbol'] or utc(diagnostic['start'])!=utc(summary['start']) or utc(diagnostic['end_exclusive'])!=utc(summary['end_exclusive']):
            raise DiagnosticError('diagnostics.json: 종목/기간 불일치')
        worst=diagnostic.get('worst_30_days')
        if worst:windows.append(('worst_30_days',utc(worst['start']),utc(worst['end'])))
    details=[]
    for name,left,right in windows:
        selected=labeled.loc[(labeled.exit_time>left)&(labeled.exit_time<=right)]
        details.append(dict(name=name,start=left.isoformat(),end=right.isoformat(),
                            selection='청산 시각: 시작 초과, 종료 이하. 분류는 각 진입 직전 완료 봉 기준.',
                            fully_covered=utc(summary['start'])<=left and utc(summary['end_exclusive'])>=right,
                            count=len(selected),net_pnl=float(selected.net_pnl.sum()),
                            by_regime=aggregate(selected,['regime']),
                            by_regime_reason=aggregate(selected,['regime','reason'])))
    report=dict(symbol=summary['symbol'],start=summary['start'],end_exclusive=summary['end_exclusive'],
                rule=dict(fast=FAST,slow=SLOW,threshold=THRESHOLD,boundary='neutral at equality',
                          latest_allowed='open_time + 1h <= entry_time'),notice=NOTICE,
                total_trades=len(labeled),net_pnl=float(labeled.net_pnl.sum()),total_fees=float(labeled.fees.sum()),
                by_regime=aggregate(labeled,['regime']),by_regime_side=aggregate(labeled,['regime','side']),
                by_regime_reason=aggregate(labeled,['regime','reason']),loss_windows=details,
                diagnostic_status='진단 JSON 사용' if diagnostic is not None else '진단 JSON 없음: trades + summary + 가격만 사용. 최악 30일 구간은 미분석.')
    return labeled,report


def write(labeled,report,output):
    output.mkdir(parents=True,exist_ok=True)
    labeled.to_csv(output/'labeled_trades.csv',index=False,encoding='utf-8-sig')
    (output/'regimes.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    text=f'# {report["symbol"]} 진입 시점 시장 국면 분석 v0.3\n\n{NOTICE}\n\n{report["diagnostic_status"]}\n\n'
    text+='상승/하락/중립/분류 불가는 uptrend/downtrend/neutral/unclassified로 표기합니다. trend_strength는 SMA 차이 비율의 절댓값입니다.\n\n'
    for key,title in [('by_regime','국면별'),('by_regime_side','국면·롱숏별'),('by_regime_reason','국면·청산 사유별')]:
        pd.DataFrame(report[key],columns=list(report[key][0]) if report[key] else ['regime','count','win_rate_pct','expected_pnl','net_pnl','fees']).to_csv(output/f'{key}.csv',index=False,encoding='utf-8-sig')
        text+=f'## {title}\n\n'+table(report[key])+'\n'
    for i,w in enumerate(report['loss_windows']):
        text+=f'## 손실 집중 구간 {w["name"]}\n\n{w["start"]} ~ {w["end"]}\n\n{w["selection"]}\n\n전체 구간 포함: {w["fully_covered"]}, 거래 수: {w["count"]}, 순손익: {w["net_pnl"]} USDT\n\n'+table(w['by_regime_reason'])+'\n'
        pd.DataFrame(w['by_regime_reason']).to_csv(output/f'loss_window_{i}.csv',index=False,encoding='utf-8-sig')
    (output/'report.md').write_text(text,encoding='utf-8')


def main(argv=None):
    parser=argparse.ArgumentParser(description='과거 완료 봉 전용 시장 국면 분석 v0.3')
    parser.add_argument('--symbols',nargs='+',choices=['BTCUSDT','ETHUSDT'],default=['BTCUSDT','ETHUSDT'])
    for flag,default in [('data-dir','data'),('input-dir','data/backtests'),('diagnostics-dir','data/diagnostics'),('output-dir','data/regimes')]:
        parser.add_argument('--'+flag,type=Path,default=Path(default))
    args=parser.parse_args(argv);failed=False
    # Avoid any output inside input directories, even if a user customizes paths.
    for symbol in args.symbols:
        output=(args.output_dir/symbol).resolve()
        for source in [args.input_dir,args.data_dir/'klines',args.diagnostics_dir]:
            if output==source.resolve() or source.resolve() in output.parents:
                print('출력 폴더는 원본 입력 폴더 밖에 지정하세요.',file=sys.stderr);return 1
    for symbol in dict.fromkeys(args.symbols):
        try:
            summary,trades,candles=load_inputs(args.input_dir,args.data_dir,symbol)
            path=args.diagnostics_dir/symbol/'diagnostics.json'
            diagnostic=json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
            labeled,report=analyze(summary,trades,candles,diagnostic)
            output=args.output_dir/symbol
            # A new run gets its own directory: never overwrite existing results.
            if output.exists():raise DiagnosticError(f'{output}: 기존 결과가 있습니다. 새 --output-dir을 지정하세요.')
            write(labeled,report,output)
            print(f'{symbol}: {len(labeled)}개 거래 분석 완료. {output}/report.md')
        except (OSError,ValueError,TypeError,KeyError) as exc:
            failed=True;print(f'{symbol}: 분석 오류 — {exc}',file=sys.stderr)
    return 1 if failed else 0


if __name__=='__main__':raise SystemExit(main())
