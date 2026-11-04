"""Offline CLI: one independent account per symbol, no network/API keys."""
import argparse
import json
from pathlib import Path
import sys

import pandas as pd

from .data import load, utc
from .engine import Config, run, WARNINGS
from .report import save
from .strategy import SMACrossover


def main(argv=None):
    parser = argparse.ArgumentParser(description='로컬 Parquet 전용 백테스트 (실제 주문 없음)')
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--output-dir', type=Path, default=Path('data/backtests'))
    parser.add_argument('--symbols', choices=['BTCUSDT', 'ETHUSDT'], nargs='+', default=['BTCUSDT', 'ETHUSDT'])
    parser.add_argument('--start', default='2023-01-01')
    parser.add_argument('--end', help='포함하지 않는 UTC 정시. 생략하면 현재 시각의 마지막 완료 봉까지')
    parser.add_argument('--fast', type=int, default=20)
    parser.add_argument('--slow', type=int, default=50)
    for flag, default in [('initial-capital',10000.),('fee-rate',.0004),('slippage',.0002),
                          ('max-leverage',1.),('risk-fraction',.01),('stop-pct',.02),('take-profit-pct',.04)]:
        parser.add_argument('--'+flag,type=float,default=default)
    parser.add_argument('--max-holding-hours', type=int, default=72)
    args = parser.parse_args(argv)
    # One common clock/window for every symbol in this invocation.
    now = pd.Timestamp.now(tz='UTC')
    try:
        start, end = utc(args.start), utc(args.end) if args.end else now.floor('h')
        config = Config(**{key:getattr(args,key) for key in Config.__dataclass_fields__})
        strategy = SMACrossover(args.fast,args.slow)
    except (ValueError, TypeError) as exc:
        print(f'설정 오류: {exc}',file=sys.stderr)
        return 1
    print('\n'.join(WARNINGS))
    failed = False
    for symbol in dict.fromkeys(args.symbols):
        output = args.output_dir/symbol
        try:
            frame = load(args.data_dir/'klines'/symbol/'1h.parquet',start,end,now=now)
            result = run(frame,symbol,strategy,config,start=start,end=end,now=now)
            result.summary['strategy_parameters'] = {'fast':args.fast,'slow':args.slow}
            save(result,output)
            # Prior failures must not be mistaken for the current result.
            (output/'error.json').unlink(missing_ok=True)
            print(f'{symbol}: 가상 거래 {result.summary["total_trades"]}개, 보고서 {output}')
        except (ValueError, TypeError, OSError, ImportError) as exc:
            failed = True
            output.mkdir(parents=True,exist_ok=True)
            # Prevent a previous successful report from masquerading as this run.
            for filename in ['summary.json','trades.csv','equity.csv','주의사항.txt']:
                (output/filename).unlink(missing_ok=True)
            (output/'error.json').write_text(json.dumps(
                dict(symbol=symbol,start=start.isoformat(),end_exclusive=end.isoformat(),error=str(exc)),
                ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(f'{symbol}: 오류 — {exc}',file=sys.stderr)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
