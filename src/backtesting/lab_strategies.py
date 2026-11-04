"""Fixed v0.5 offline research signals. No optimization or order client."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Donchian20:
    """Current CLOSED close vs prior 20 highs/lows (exclude signal bar)."""
    def target(self, completed):
        if len(completed)<21:
            return 0
        previous=completed.iloc[-21:-1]
        close=completed.close.iloc[-1]
        if close>previous.high.max():
            return 1
        if close<previous.low.min():
            return -1
        return 0


@dataclass(frozen=True)
class NoTrade:
    def target(self, completed):
        return 0
