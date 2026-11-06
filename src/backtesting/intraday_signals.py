"""O(N) causal 15m features; hourly trend values join at CLOSED hour only."""
import numpy as np
import pandas as pd

from .intraday_data import aggregate_hourly, STEP


def features(frame):
    f=frame
    boundary=(f.open_time.iloc[-1]+STEP).floor('h')
    hour=aggregate_hourly(f.loc[f.open_time<boundary])
    hour['available_at']=hour.open_time+pd.Timedelta(hours=1)
    fast=hour.close.rolling(50,min_periods=50).mean();slow=hour.close.rolling(200,min_periods=200).mean()
    gap=fast/slow-1
    hour['trend']=np.select([(fast>slow)&(hour.close>slow)&(gap.abs()>=.005),
                            (fast<slow)&(hour.close<slow)&(gap.abs()>=.005)],[1,-1],default=0)
    timing=pd.DataFrame({'signal_time':f.open_time+STEP})
    joined=pd.merge_asof(timing,hour[['available_at','trend']],left_on='signal_time',right_on='available_at',direction='backward')
    trend=joined.trend.fillna(0).astype(int)
    ema=f.close.ewm(span=20,adjust=False,min_periods=20).mean()
    reclaim=np.select([(f.close.shift(1)<=ema.shift(1))&(f.close>ema),
                       (f.close.shift(1)>=ema.shift(1))&(f.close<ema)],[1,-1],default=0)
    upper=f.high.rolling(20,min_periods=20).max().shift(1)
    lower=f.low.rolling(20,min_periods=20).min().shift(1)
    breakout=np.select([f.close>upper,f.close<lower],[1,-1],default=0)
    result=pd.DataFrame({'signal_time':timing.signal_time,'hour_available_at':joined.available_at,'trend':trend,
                         'ema20':ema,'previous20_high':upper,'previous20_low':lower,
                         'pullback_reclaim_raw':reclaim,'breakout_20_raw':breakout})
    for name in ['pullback_reclaim','breakout_20']:
        result[name]=np.where((result[name+'_raw']==trend)&trend.ne(0),result[name+'_raw'],0)
    return result
