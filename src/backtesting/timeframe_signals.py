"""Closed UTC 4h signal adapter over immutable 1h execution candles."""
import pandas as pd

from .data import validate
from .experiments import allow_non_neutral
from .regimes import THRESHOLD

HOUR=pd.Timedelta(hours=1)


def build_four_hour(candles):
    """Reject broken input. Drop ONLY contiguous leading/trailing partial bins.

    Aggregation can be precomputed, but no feature at availability T depends on
    data opening at/after T. The adapter always selects available_at <= T.
    """
    columns=['open_time','open','high','low','close','volume','available_at']
    if candles.empty:return pd.DataFrame(columns=columns)
    times=pd.to_datetime(candles.open_time,utc=True)
    if not times.is_monotonic_increasing or times.duplicated().any():
        raise ValueError('4h 입력: 역순 또는 중복 시간봉')
    f=validate(candles,times.iloc[0],times.iloc[-1]+HOUR)
    grouped=f.groupby(f.open_time.dt.floor('4h'),sort=True)
    result=grouped.agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'))
    result=result.loc[grouped.size().eq(4)].reset_index()
    result['available_at']=result.open_time+4*HOUR
    return result[columns]


class ClosedSignal:
    """A strategy gets closed signal bars; the engine still gets raw 1h bars."""
    def __init__(self,strategy,hours,candles):
        if hours not in (1,4):raise ValueError('신호 시간축은 1h 또는 4h만 허용')
        self.strategy=strategy;self.hours=hours
        self.four=build_four_hour(candles) if hours==4 else None

    def closed_four(self,time):
        n=self.four.available_at.searchsorted(time,side='right')
        return self.four.iloc[:n]

    def target(self,completed):
        if self.hours==1:return self.strategy.target(completed)
        if completed.empty:return 0
        now=completed.open_time.iloc[-1]+HOUR
        if now!=now.floor('4h'):return 0
        closed=self.closed_four(now)
        if closed.empty or closed.available_at.iloc[-1]!=now:return 0
        return self.strategy.target(closed)

    def entry_filter(self,completed,entry_time):
        if self.hours==1:return allow_non_neutral(completed,entry_time)
        closed=self.closed_four(entry_time).tail(200)
        if len(closed)<200 or closed.available_at.iloc[-1]!=entry_time:return False
        if not closed.open_time.diff().iloc[1:].eq(4*HOUR).all():return False
        slow=float(closed.close.mean());fast=float(closed.close.tail(50).mean())
        return abs((fast-slow)/slow)>THRESHOLD
