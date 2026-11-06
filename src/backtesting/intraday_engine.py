"""Independent O(N) 15m simulator. No 1h validation/engine conversion."""
from dataclasses import dataclass, asdict
import numpy as np
import pandas as pd

from .intraday_data import validate15, STEP


@dataclass(frozen=True)
class IntradayConfig:
    cost_multiplier: int=1
    initial_capital: float=10000.
    risk_fraction: float=.01
    max_leverage: float=1.
    stop_pct: float=.01
    take_profit_pct: float=.02
    max_holding_hours: int=6

    def __post_init__(self):
        if type(self.cost_multiplier) is not int or self.cost_multiplier not in (1,2):raise ValueError('비용 배수는 고정 1/2만 허용')
        expected=dict(initial_capital=10000.,risk_fraction=.01,max_leverage=1.,stop_pct=.01,take_profit_pct=.02,max_holding_hours=6)
        if any(getattr(self,k)!=v for k,v in expected.items()):raise ValueError('v0.9 위험/자본/6h 조건은 고정')

    @property
    def fee_rate(self):return .0004*self.cost_multiplier
    @property
    def slippage(self):return .0002*self.cost_multiplier


def simulate(frame,signals,config=None):
    c=config or IntradayConfig()
    if frame.empty:raise ValueError('15m 실행 데이터 없음')
    f=validate15(frame,frame.open_time.iloc[0],frame.open_time.iloc[-1]+STEP)
    signal=np.asarray(signals)
    if signal.shape!=(len(f),) or not np.isin(signal,[-1,0,1]).all():raise ValueError('15m 신호 배열 오류')
    cash=c.initial_capital;p=None;pending=0;trades=[];curve=[];events=[]
    def fill(raw,side):return float(raw)*(1+side*c.slippage)
    def close(raw,time,reason):
        nonlocal p,cash
        price=fill(raw,-p['side']);fee=p['quantity']*price*c.fee_rate
        gross=p['side']*p['quantity']*(price-p['entry_price']);cash+=gross-fee
        trades.append(dict(entry_time=p['time'],exit_time=time,side='long' if p['side']==1 else 'short',quantity=p['quantity'],
            entry_price=p['entry_price'],exit_price=price,gross_pnl=gross,fees=p['fee']+fee,funding_cost=0.,
            net_pnl=gross-p['fee']-fee,holding_hours=float((time-p['time'])/pd.Timedelta(hours=1)),reason=reason,
            planned_risk_fraction=p['risk'],entry_leverage=p['leverage'],
            slippage_adjustment=p['quantity']*(abs(p['entry_price']-p['raw'])+abs(price-raw))))
        p=None
    for i,bar in enumerate(f.itertuples(index=False)):
        canceled=False
        if p is not None:
            gap=bar.open<=p['stop'] if p['side']==1 else bar.open>=p['stop']
            if gap:close(bar.open,bar.open_time,'stop_gap');canceled=True
            elif bar.open_time-p['time']>=pd.Timedelta(hours=6):close(bar.open,bar.open_time,'max_holding');canceled=True
        # No reversal: opposite/same signals are ignored while a position exists.
        if pending and p is None and not canceled and cash>0:
            price=fill(bar.open,pending);stop=price*(1-pending*c.stop_pct);stop_fill=fill(stop,-pending)
            unit_risk=pending*(price-stop_fill)+c.fee_rate*(price+stop_fill)
            qty=min(cash*c.risk_fraction/unit_risk,cash*c.max_leverage/(price*(1+c.fee_rate)))
            fee=qty*price*c.fee_rate
            p=dict(side=pending,quantity=qty,entry_price=price,raw=float(bar.open),time=bar.open_time,fee=fee,
                   stop=stop,take=price*(1+pending*c.take_profit_pct),risk=qty*unit_risk/cash,leverage=qty*price/cash)
            cash-=fee;events.append(dict(time=bar.open_time,side=pending,kind='filled'))
        pending=0
        if p is not None:
            stop=bar.low<=p['stop'] if p['side']==1 else bar.high>=p['stop']
            take=bar.high>=p['take'] if p['side']==1 else bar.low<=p['take']
            if stop:close(p['stop'],bar.open_time+STEP,'stop')
            elif take:close(p['take'],bar.open_time+STEP,'take_profit')
        if i==len(f)-1 and p is not None:close(bar.close,bar.open_time+STEP,'end_of_data')
        unrealized=p['side']*p['quantity']*(bar.close-p['entry_price']) if p is not None else 0.
        curve.append(dict(time=bar.open_time+STEP,cash=cash,unrealized_pnl=unrealized,equity=cash+unrealized,position=p['side'] if p else 0))
        if i<len(f)-1:pending=int(signal[i])
    columns=['entry_time','exit_time','side','quantity','entry_price','exit_price','gross_pnl','fees','funding_cost','net_pnl',
             'holding_hours','reason','planned_risk_fraction','entry_leverage','slippage_adjustment']
    t=pd.DataFrame(trades,columns=columns);e=pd.DataFrame(curve)
    peak=e.equity.cummax().clip(lower=c.initial_capital);net=cash-c.initial_capital
    gains=sum(max(x['net_pnl'],0) for x in trades);losses=-sum(min(x['net_pnl'],0) for x in trades)
    summary=dict(config={**asdict(c),'fee_rate':c.fee_rate,'slippage':c.slippage},total_trades=len(t),net_pnl=net,
        net_return_pct=net/c.initial_capital*100,final_equity=cash,max_drawdown_pct=float(((peak-e.equity)/peak).max()*100),
        win_rate_pct=100*sum(x['net_pnl']>0 for x in trades)/len(t) if len(t) else 0.,profit_factor=gains/losses if losses else None,
        total_fees=sum(x['fees'] for x in trades),average_net_pnl=net/len(t) if len(t) else 0.,
        total_slippage_adjustment=sum(x['slippage_adjustment'] for x in trades),
        average_planned_risk_fraction=float(t.planned_risk_fraction.mean()) if len(t) else 0.,
        maximum_entry_leverage=float(t.entry_leverage.max()) if len(t) else 0.)
    if not np.isclose(t.net_pnl.sum(),net,atol=1e-6,rtol=1e-9):raise ValueError('15m 회계 순손익 불일치')
    if not np.allclose(e.cash+e.unrealized_pnl,e.equity,atol=1e-6,rtol=1e-9):raise ValueError('15m 자산 회계 불일치')
    return t,e,pd.DataFrame(events,columns=['time','side','kind']),summary
