"""Strategies receive ONLY candles already completed at the decision time."""
from dataclasses import dataclass
from typing import Protocol

import pandas as pd


class Strategy(Protocol):
    def target(self, completed: pd.DataFrame) -> int:
        """Return +1/-1 on a new long/short signal; 0 means no new signal.

        completed is a read-only historical prefix, ending in the current closed
        candle. A signal expires at the next open; risk/time exits do not reenter
        until a later, new signal. Never mutate this frame.
        """
        ...


@dataclass(frozen=True)
class SMACrossover:
    fast: int = 20
    slow: int = 50

    def __post_init__(self):
        if not (isinstance(self.fast, int) and isinstance(self.slow, int) and 1 <= self.fast < self.slow):
            raise ValueError('SMA는 정수 1 <= fast < slow여야 합니다.')

    def target(self, completed):
        if len(completed) < self.slow + 1:
            return 0  # Need two fully formed moving averages to identify a cross.
        close = completed.close.iloc[-self.slow-1:]
        fast = close.rolling(self.fast).mean()
        slow = close.rolling(self.slow).mean()
        if fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
            return 1
        if fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]:
            return -1
        return 0
