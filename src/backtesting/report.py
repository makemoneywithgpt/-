"""Write reports only; never upload data or contact an exchange."""
import json
from pathlib import Path

from .engine import Result


def save(result: Result, directory: Path):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result.trades.to_csv(directory/'trades.csv', index=False, encoding='utf-8-sig')
    result.equity.to_csv(directory/'equity.csv', index=False, encoding='utf-8-sig')
    (directory/'summary.json').write_text(
        json.dumps(result.summary, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    (directory/'주의사항.txt').write_text('\n'.join(result.summary['warnings'])+'\n', encoding='utf-8')
