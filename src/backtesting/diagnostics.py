"""Offline diagnostics of existing reports; no strategy execution or network."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .data import utc

WARNING = ('독립 계정 비교입니다. BTC와 ETH는 각각 별도의 초기 자본을 사용하며, '
           '공동 포트폴리오·마진 공유·리밸런싱을 계산하지 않습니다. '
           '펀딩비 0 가정, 시간별 종가 기준 낙폭이며 실제 마진·강제청산은 미포함입니다. '
           '가격 추세·횡보 등 시장 국면의 원인을 이 보고서로 확정할 수 없습니다. '
           '가격 결합 분석은 과거 시점 정보만 사용하는 별도 후속 작업입니다.')


class DiagnosticError(ValueError):
    pass


def fail(file, message, row=None):
    location = f' CSV {row+2}행' if row is not None else ''
    raise DiagnosticError(f'{file}{location}: {message}')


def require(frame, columns, file):
    missing = set(columns)-set(frame.columns)
    if missing:
        fail(file, f'필수 열 없음 {sorted(missing)}')


def numeric(frame, columns, file):
    for col in columns:
        try:
            frame[col] = pd.to_numeric(frame[col], errors='raise')
        except (TypeError, ValueError):
            bad = pd.to_numeric(frame[col], errors='coerce').isna()
            fail(file, f'{col}: 숫자가 아닌 값', int(np.flatnonzero(bad)[0]))
        bad = ~np.isfinite(frame[col])
        if bad.any():
            fail(file, f'{col}: 유한하지 않은 값', int(np.flatnonzero(bad)[0]))


def dates(frame, column, file):
    try:
        frame[column] = pd.to_datetime(frame[column], utc=True, errors='raise', format='mixed')
    except (ValueError, TypeError):
        fail(file, f'{column}: UTC 시간 변환 실패')
    if frame[column].isna().any():
        fail(file, f'{column}: 빈 시간', int(np.flatnonzero(frame[column].isna())[0]))


def load(directory, symbol):
    directory = Path(directory)
    if (directory/'error.json').exists():
        fail(directory/'error.json', '이전 백테스트가 실패한 결과입니다. 먼저 백테스트를 성공시키세요.')
    sfile, efile, tfile = [directory/n for n in ['summary.json','equity.csv','trades.csv']]
    try:
        summary = json.loads(sfile.read_text(encoding='utf-8'))
        equity, trades = pd.read_csv(efile), pd.read_csv(tfile)
    except (OSError, ValueError) as exc:
        raise DiagnosticError(f'{directory}: 파일 읽기 실패: {exc}') from exc
    try:
        initial = float(summary['config']['initial_capital'])
        start, end = utc(summary['start']), utc(summary['end_exclusive'])
        count = summary['candle_count']
    except (KeyError, ValueError, TypeError) as exc:
        fail(sfile, f'요약 설정 오류: {exc}')
    if not np.isfinite(initial) or initial <= 0 or end <= start:
        fail(sfile, '초기 자본/기간 오류')
    if start != start.floor('h') or end != end.floor('h'):
        fail(sfile, '기간은 UTC 정시여야 합니다.')
    if summary.get('symbol') != symbol:
        fail(sfile, '종목 불일치')
    require(equity, ['time','cash','unrealized_pnl','equity'], efile)
    dates(equity,'time',efile)
    numeric(equity,['cash','unrealized_pnl','equity'],efile)
    if equity.time.duplicated().any() or not equity.time.is_monotonic_increasing:
        bad = equity.time.diff().le(pd.Timedelta(0))
        fail(efile,'중복 또는 역순 시간',int(np.flatnonzero(bad)[0]))
    expected = pd.date_range(start+pd.Timedelta(hours=1),end,freq='h')
    if not pd.DatetimeIndex(equity.time).equals(expected):
        missing = expected.difference(pd.DatetimeIndex(equity.time))
        extra = pd.DatetimeIndex(equity.time).difference(expected)
        mismatch=next((i for i,(a,b) in enumerate(zip(equity.time,expected)) if a!=b),min(len(equity),len(expected)))
        fail(efile,f'기간/누락 시간 불일치. 누락={list(missing)}, 범위 밖={list(extra)}',mismatch)
    if len(equity) != count:
        fail(sfile,'candle_count와 equity.csv 행 수 불일치')
    bad = ~np.isclose(equity.cash+equity.unrealized_pnl,equity.equity,rtol=1e-9,atol=1e-6)
    if bad.any():
        fail(efile,'cash + unrealized_pnl != equity',int(np.flatnonzero(bad)[0]))
    require(trades,['entry_time','exit_time','side','reason','net_pnl','fees','gross_pnl','funding_cost'],tfile)
    for col in ['entry_time','exit_time']:
        dates(trades,col,tfile)
    numeric(trades,['net_pnl','fees','gross_pnl','funding_cost'],tfile)
    invalid = ((trades.entry_time < start) | (trades.exit_time > end)
               | (trades.exit_time < trades.entry_time) | ~trades.side.isin(['long','short'])
               | trades.reason.isna() | trades.fees.lt(0))
    if invalid.any():
        fail(tfile,'거래 시간/방향/비용/청산 사유 오류',int(np.flatnonzero(invalid)[0]))
    if not trades.exit_time.is_monotonic_increasing or trades.duplicated().any():
        bad = trades.exit_time.diff().lt(pd.Timedelta(0)) | trades.duplicated()
        fail(tfile,'거래 시간 역순 또는 중복 행',int(np.flatnonzero(bad)[0]))
    for i in range(1,len(trades)):
        if trades.entry_time.iloc[i] < trades.exit_time.iloc[i-1]:
            fail(tfile,'단일 포지션 거래 시간이 겹칩니다.',i)
    bad = ~np.isclose(trades.gross_pnl-trades.fees-trades.funding_cost,trades.net_pnl,rtol=1e-9,atol=1e-6)
    if bad.any():
        fail(tfile,'거래 순손익과 비용 불일치',int(np.flatnonzero(bad)[0]))
    for key, value in [('first_candle',start),('last_candle',end-pd.Timedelta(hours=1))]:
        if key in summary and utc(summary[key]) != value:
            fail(sfile,f'{key}와 요청 기간 불일치')
    delta = float(equity.equity.iloc[-1]-initial)
    if not np.isclose(trades.net_pnl.sum(),delta,rtol=1e-9,atol=1e-6):
        fail(tfile,'순손익 합과 최종 자산 증감 불일치')
    for key, value in [('total_trades',len(trades)),('net_pnl',delta),
                       ('final_equity',float(equity.equity.iloc[-1])),('total_fees',float(trades.fees.sum()))]:
        if key not in summary or not np.isclose(summary[key],value,rtol=1e-9,atol=1e-6):
            fail(sfile,f'{key}와 CSV 불일치')
    return summary, equity, trades


def group_stats(trades, column):
    rows=[]
    for name,g in trades.groupby(column,sort=True):
        rows.append(dict(group=str(name),count=len(g),win_rate_pct=float(g.net_pnl.gt(0).mean()*100),
                         net_pnl=float(g.net_pnl.sum()),fees=float(g.fees.sum()),
                         expected_pnl=float(g.net_pnl.mean())))
    return rows


def periods(equity, trades, start, end, initial, frequency):
    # Equity timestamps are candle CLOSE boundaries. Assign boundary events to
    # the interval ending there (Jan 1 00:00 belongs to the previous year).
    offset=pd.Timedelta(nanoseconds=1)
    keys=(equity.time-offset).dt.tz_localize(None).dt.to_period(frequency)
    exits=(trades.exit_time-offset).dt.tz_localize(None).dt.to_period(frequency)
    rows=[]; previous=initial
    for period,g in equity.groupby(keys,sort=True):
        left=max(start,period.start_time.tz_localize('UTC'))
        right=min(end,(period+1).start_time.tz_localize('UTC'))
        finish=float(g.equity.iloc[-1]); realized=trades.loc[exits==period,'net_pnl']
        peak=pd.concat([pd.Series([previous]),g.equity.reset_index(drop=True)]).cummax()
        vals=pd.concat([pd.Series([previous]),g.equity.reset_index(drop=True)])
        rows.append(dict(period=str(period),start=left.isoformat(),end_exclusive=right.isoformat(),
                         partial=left>period.start_time.tz_localize('UTC') or right<(period+1).start_time.tz_localize('UTC'),
                         starting_equity=previous,ending_equity=finish,equity_pnl=finish-previous,
                         return_pct=(finish/previous-1)*100 if previous>0 else None,
                         realized_pnl=float(realized.sum()),closed_trades=len(realized),
                         max_drawdown_pct=float(((peak-vals)/peak.where(peak>0)).max()*100) if (peak>0).any() else None))
        previous=finish
    return rows


def drawdowns(series):
    peak=float(series.iloc[0]); peak_time=series.index[0]; active=None; episodes=[]
    for time,value in series.iloc[1:].items():
        if value>=peak:
            if active:
                active['recovery_time']=time.isoformat(); active['recovered']=True
                active['duration_hours']=float((time-pd.Timestamp(active['peak_time']))/pd.Timedelta(hours=1))
                episodes.append(active); active=None
            peak=float(value); peak_time=time
        else:
            depth=float((peak-value)/peak*100)
            if active is None:
                active=dict(peak_time=peak_time.isoformat(),trough_time=time.isoformat(),depth_pct=depth,
                            recovered=False,recovery_time=None)
            elif depth>active['depth_pct']:
                active.update(trough_time=time.isoformat(),depth_pct=depth)
    if active:
        active['duration_hours']=float((series.index[-1]-pd.Timestamp(active['peak_time']))/pd.Timedelta(hours=1))
        episodes.append(active)
    return dict(maximum=max(episodes,key=lambda x:x['depth_pct']) if episodes else None,
                longest_underwater=max(episodes,key=lambda x:x['duration_hours']) if episodes else None,
                episodes=episodes)


def analyze(summary,equity,trades):
    initial=float(summary['config']['initial_capital']);start=utc(summary['start']);end=utc(summary['end_exclusive'])
    series=pd.Series([initial,*equity.equity.tolist()],index=pd.DatetimeIndex([start,*equity.time.tolist()]))
    windows=[]
    for i in range(720,len(series)):
        base=float(series.iloc[i-720]); finish=float(series.iloc[i])
        windows.append(dict(start=series.index[i-720].isoformat(),end=series.index[i].isoformat(),
                            equity_change=finish-base,return_pct=(finish/base-1)*100 if base>0 else None))
    streak=longest=0
    for pnl in trades.net_pnl:
        streak=streak+1 if pnl<0 else 0;longest=max(longest,streak)
    report=dict(symbol=summary['symbol'],initial_capital=initial,start=start.isoformat(),end_exclusive=end.isoformat(),
                warnings=[WARNING,*summary.get('warnings',[])],
                daily=periods(equity,trades,start,end,initial,'D'),
                monthly=periods(equity,trades,start,end,initial,'M'),
                yearly=periods(equity,trades,start,end,initial,'Y'),
                by_side=group_stats(trades,'side'),by_exit_reason=group_stats(trades,'reason'),
                max_consecutive_losses=longest,expected_pnl=float(trades.net_pnl.mean()) if len(trades) else 0.,
                drawdown=drawdowns(series),
                worst_30_days=min(windows,key=lambda x:x['equity_change']) if windows else None,
                best_30_days=max(windows,key=lambda x:x['equity_change']) if windows else None)
    report['worst_months']=sorted([r for r in report['monthly'] if r['equity_pnl']<0],key=lambda x:x['equity_pnl'])[:5]
    return report


def table(rows):
    if not rows:return '해당 항목 없음\n'
    cols=list(rows[0])
    labels={'period':'기간','start':'시작 UTC','end_exclusive':'종료 UTC(제외)','partial':'부분 기간',
            'starting_equity':'시작 자산','ending_equity':'종료 자산','equity_pnl':'평가자산 증감 USDT',
            'return_pct':'수익률 %','realized_pnl':'실현손익 USDT','closed_trades':'청산 거래 수',
            'max_drawdown_pct':'기간 내 낙폭 %','group':'분류','count':'거래 수','win_rate_pct':'승률 %',
            'net_pnl':'순손익 USDT','fees':'수수료 USDT','expected_pnl':'거래당 기대손익 USDT'}
    return '| '+' | '.join(labels.get(c,c) for c in cols)+' |\n| '+' | '.join(['---']*len(cols))+' |\n'+''.join(
        '| '+' | '.join(str(r.get(c,'')) for c in cols)+' |\n' for r in rows)


def write(report,directory):
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'diagnostics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    text=f'# {report["symbol"]} 백테스트 진단\n\n'+ '\n\n'.join(report['warnings'])+'\n\n'
    text+='계정 평가손익은 미실현 손익을 포함한 자산 변화이고, 실현손익은 청산된 거래의 비용 차감 후 손익입니다. '
    text+='기간 수익률은 직전 기간 말 자산(첫 기간은 초기 자본)을 분모로 합니다. partial=True는 부분 기간입니다.\n\n'
    for key,title in [('daily','날짜별 성과'),('monthly','월별 성과'),('yearly','연도별 성과'),
                      ('by_side','롱·숏별 성과'),('by_exit_reason','청산 사유별 성과'),('worst_months','자산 하락 상위 월')]:
        pd.DataFrame(report[key]).to_csv(directory/f'{key}.csv',index=False,encoding='utf-8-sig')
        text+=f'## {title}\n\n'+table(report[key])+'\n'
    text+=f'최대 연속 손실: {report["max_consecutive_losses"]}회. 거래당 기대손익: {report["expected_pnl"]} USDT.\n\n'
    text+='최대 낙폭 및 최장 미회복 구간 (시간은 UTC, 지속 시간은 고점부터 회복 또는 분석 종료까지):\n\n'
    text+='```json\n'+json.dumps(report['drawdown'],ensure_ascii=False,indent=2)+'\n```\n\n'
    text+='30일 구간은 정확한 720시간의 자산 증감(USDT)으로 순위를 정하며, 30일 미만이면 계산하지 않습니다.\n\n'
    for key in ['worst_30_days','best_30_days']:
        text+=f'{key}: {json.dumps(report[key],ensure_ascii=False)}\n\n'
    (directory/'report.md').write_text(text,encoding='utf-8')
    return text


def main(argv=None):
    parser=argparse.ArgumentParser(description='기존 결과 전용 오프라인 진단')
    parser.add_argument('--symbols',nargs='+',choices=['BTCUSDT','ETHUSDT'],default=['BTCUSDT','ETHUSDT'])
    parser.add_argument('--input-dir',type=Path,default=Path('data/backtests'))
    parser.add_argument('--output-dir',type=Path,default=Path('data/diagnostics'))
    args=parser.parse_args(argv);failed=False;comparison=[]
    for symbol in dict.fromkeys(args.symbols):
        try:
            summary,equity,trades=load(args.input_dir/symbol,symbol)
            report=analyze(summary,equity,trades)
            # Write nothing for a symbol until all its input has passed validation.
            print(write(report,args.output_dir/symbol))
            (args.output_dir/symbol/'error.json').unlink(missing_ok=True)
            comparison.append(dict(symbol=symbol,initial_capital=report['initial_capital'],
                                   start=report['start'],end_exclusive=report['end_exclusive'],
                                   net_pnl=summary['net_pnl'],mdd=report['drawdown']['maximum']))
        except (ValueError,TypeError,KeyError,OSError) as exc:
            failed=True;print(f'{symbol}: 진단 오류 — {exc}',file=sys.stderr)
            target=args.output_dir/symbol;target.mkdir(parents=True,exist_ok=True)
            for name in ['diagnostics.json','report.md','daily.csv','monthly.csv','yearly.csv','by_side.csv','by_exit_reason.csv','worst_months.csv']:
                (target/name).unlink(missing_ok=True)
            (target/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n',encoding='utf-8')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'comparison.json').write_text(json.dumps(dict(warning=WARNING,
        independent_accounts=comparison),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return 1 if failed else 0


if __name__=='__main__':
    raise SystemExit(main())
