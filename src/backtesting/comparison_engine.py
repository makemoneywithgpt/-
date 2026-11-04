"""v0.4 simulation variant: only new entries may be gated.

The existing engine is untouched. Its loop is reproduced here because the
Strategy.target interface cannot express "close existing but do not reverse".
The only execution change is the entry_filter guard before open_position.
Parity with the unchanged engine (filter disabled/always true) is tested.
"""
from dataclasses import asdict
import math
import pandas as pd
from .data import HOUR, validate, utc
from .strategy import Strategy
from .engine import Config, FundingModel, ZeroFunding, Result, Position, TRADE_COLUMNS, WARNINGS

def run_filtered(frame, symbol: str, strategy: Strategy, config: Config, *, start, end, now=None,
        funding: FundingModel | None = None, entry_filter=None) -> Result:
    f = validate(frame, start, end, now=now)
    funding = funding if funding is not None else ZeroFunding()
    c = config
    cash = c.initial_capital
    position = None
    pending = 0
    trades, curve = [], []

    def fill(price, order_side):
        return float(price) * (1 + order_side*c.slippage)

    def close(raw_price, timestamp, reason):
        nonlocal cash, position
        p = position
        price = fill(raw_price, -p.side)
        fee = p.quantity*price*c.fee_rate
        gross = p.side*p.quantity*(price-p.price)
        cash += gross-fee
        trades.append(dict(entry_time=p.time, exit_time=timestamp,
                           side='long' if p.side == 1 else 'short', quantity=p.quantity,
                           entry_price=p.price, exit_price=price, gross_pnl=gross,
                           fees=p.entry_fee+fee, funding_cost=p.funding,
                           net_pnl=gross-p.entry_fee-fee-p.funding,
                           holding_hours=float((timestamp-p.time)/HOUR), reason=reason))
        position = None

    def open_position(side, raw_price, timestamp):
        nonlocal cash, position
        if cash <= 0:
            return
        price = fill(raw_price, side)
        stop = price*(1-side*c.stop_pct)
        stop_fill = fill(stop, -side)
        unit_risk = side*(price-stop_fill)+c.fee_rate*(price+stop_fill)
        quantity = min(cash*c.risk_fraction/unit_risk,
                       cash*c.max_leverage/(price*(1+c.fee_rate)))
        fee = quantity*price*c.fee_rate
        cash -= fee
        position = Position(side, quantity, price, timestamp, fee, stop,
                            price*(1+side*c.take_profit_pct))

    for i, bar in enumerate(f.itertuples(index=False)):
        time = bar.open_time
        exited_at_open = False
        # Existing adverse stop gap has precedence over discretionary/time exits.
        if position is not None:
            p = position
            gap = bar.open <= p.stop if p.side == 1 else bar.open >= p.stop
            if gap:
                close(bar.open, time, 'stop_gap')
                exited_at_open = True
            elif time-p.time >= pd.Timedelta(hours=c.max_holding_hours):
                close(bar.open, time, 'max_holding')
                exited_at_open = True
        # A risk/time exit cancels the previous pending signal for this open.
        if pending and not exited_at_open:
            if position is not None and position.side != pending:
                close(bar.open, time, 'signal')
            if position is None and (entry_filter is None or entry_filter(f.iloc[:i], time)):
                open_position(pending, bar.open, time)
        pending = 0
        if position is not None:
            p = position
            stop_hit = bar.low <= p.stop if p.side == 1 else bar.high >= p.stop
            take_hit = bar.high >= p.take if p.side == 1 else bar.low <= p.take
            # Unknown OHLC path: conservatively assume stop before target.
            if stop_hit:
                close(p.stop, time+HOUR, 'stop')
            elif take_hit:
                # Never assume price improvement on a favorable target gap.
                close(p.take, time+HOUR, 'take_profit')
        if position is not None:
            charge = float(funding.cost(symbol=symbol, time=time+HOUR, side=position.side,
                                        quantity=position.quantity, mark_price=float(bar.close)))
            if not math.isfinite(charge):
                raise ValueError('펀딩 비용은 유한한 숫자여야 합니다.')
            cash -= charge
            position.funding += charge
        if i == len(f)-1 and position is not None:
            close(bar.close, time+HOUR, 'end_of_data')
        unrealized = (position.side*position.quantity*(bar.close-position.price)
                      if position is not None else 0.)
        curve.append(dict(time=time+HOUR, equity=cash+unrealized, cash=cash,
                          unrealized_pnl=unrealized,
                          position=position.side if position is not None else 0))
        if i < len(f)-1:
            # Never pass a future row to the strategy, including during warmup.
            signal = strategy.target(f.iloc[:i+1])
            if isinstance(signal, bool) or signal not in (-1, 0, 1):
                raise ValueError('전략 신호는 -1, 0, 1이어야 합니다.')
            pending = int(signal)

    trade_frame = pd.DataFrame(trades, columns=TRADE_COLUMNS)
    equity_frame = pd.DataFrame(curve)
    peaks = equity_frame.equity.cummax().clip(lower=c.initial_capital)
    drawdown = (peaks-equity_frame.equity)/peaks
    gains = sum(max(t['net_pnl'], 0) for t in trades)
    losses = -sum(min(t['net_pnl'], 0) for t in trades)
    net = cash-c.initial_capital
    warnings = list(WARNINGS)
    if not isinstance(funding, ZeroFunding):
        warnings[0] = f'사용자 제공 펀딩 모델: {funding.label}. 비용 데이터/정산 가정을 별도 검증하세요.'
    summary = dict(symbol=symbol, start=utc(start).isoformat(), end_exclusive=utc(end).isoformat(),
                   first_candle=f.open_time.iloc[0].isoformat(), last_candle=f.open_time.iloc[-1].isoformat(),
                   candle_count=len(f), total_trades=len(trades),
                   win_rate_pct=100*sum(t['net_pnl'] > 0 for t in trades)/len(trades) if trades else 0.,
                   net_pnl=net, net_return_pct=100*net/c.initial_capital,
                   final_equity=cash, max_drawdown_pct=float(100*drawdown.max()),
                   profit_factor=gains/losses if losses else None,
                   profit_factor_note='손실 거래가 없으면 null (무한/정의 불가)',
                   average_pnl=net/len(trades) if trades else 0.,
                   total_fees=sum(t['fees'] for t in trades),
                   total_funding_cost=sum(t['funding_cost'] for t in trades),
                   funding_model=funding.label, config=asdict(c),
                   strategy=type(strategy).__name__, warnings=warnings,
                   assumptions='가상 USDT 자본, 가격 영향/유동성/마진/청산 미모델링; 시간별 종가 평가 MDD')
    return Result(trade_frame, equity_frame, summary)
