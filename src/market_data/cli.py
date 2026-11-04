import argparse
import logging
from pathlib import Path

import pandas as pd

from .binance import BinancePublic
from .collector import collect
from .core import inspect, read, resample_4h, utc, write


def main():
    parser = argparse.ArgumentParser(description="Public Binance USDT-M research data (no trading)")
    parser.add_argument("command", choices=["collect", "validate", "resample"])
    parser.add_argument("--symbols", nargs="+", choices=["BTCUSDT", "ETHUSDT"], default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", help="Exclusive UTC hour; default: current hour (closed candles only)")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("logs/market-data.log", encoding="utf-8")])
    try:
        start = utc(args.start)
        cutoff = pd.Timestamp.now(tz="UTC").floor("h")
        end = utc(args.end) if args.end else cutoff
        if start != start.floor("h") or end != end.floor("h") or end > cutoff or start >= end:
            raise ValueError("Use aligned UTC hours: start < end <= current hour")
        for symbol in args.symbols:
            path = args.data_dir / "klines" / symbol / "1h.parquet"
            if args.command == "collect":
                report = collect(BinancePublic(), symbol, start, end, args.data_dir)
            elif args.command == "resample":
                frame = read(path)
                if frame.empty:
                    raise ValueError(f"No data: {path}")
                write(resample_4h(frame), path.with_name("4h.parquet"))
                continue
            else:
                report = inspect(read(path), start, end)
            logging.info("%s rows=%s duplicates=%s invalid=%s missing=%s", symbol,
                         report["rows"], report["duplicates"], report["invalid_rows"], len(report["missing"]))
            if report["duplicates"] or report["invalid_rows"] or report["missing"]:
                raise ValueError(f"{symbol}: validation failed")
    except Exception:
        logging.exception("Operation failed; checkpointed data retained")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
