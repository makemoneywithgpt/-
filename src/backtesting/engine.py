"""Deterministic OHLC simulation, not a futures margin/liquidation model."""
from dataclasses import asdict, dataclass
from typing import Protocol
import math

import pandas as pd

from .data import HOUR, validate, utc
from .strategy import Strategy

WARNINGS = [
    '펀딩비 데이터 미확보: 펀딩비를 0으로 가정한 실험입니다. 실거래 수익이 아닙니다.',
    'SMA 교차는 검증용 예제이며 수익이 입증된 전략이 아닙니다.',
    '종목별 독립 자본, 최대 1배 가정. 실제 마진·강제청산·호가 유동성은 모델링하지 않습니다.',
    '손절 위험 예산은 정상 체결 가정입니다. 갭 발생 시 손실은 예산을 초과할 수 있습니다.',
]
TRADE_COLUMNS = ['entry_time', 'exit_time', 'side', 'quantity', 'entry_price', 'exit_price',
                 'gross_pnl', 'fees', 'funding_cost', 'net_pnl', 'holding_hours', 'reason']


@dataclass(frozen=True)
class Config:
    initial_capital: float = 10000.
    fee_rate: float = .0004
    slippage: float = .0002
    max_leverage: float = 1.
    risk_fraction: float = .01
    stop_pct: float = .02
    take_profit_pct: float = .04
    max_holding_hours: int = 72

    def __post_init__(self):
        for key, value in asdict(self).items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f'{key}: 유한한 숫자가 필요합니다.')
        if self.initial_capital <= 0:
            raise ValueError('초기 자본은 양수여야 합니다.')
        if not 0 <= self.fee_rate < 1 or not 0 <= self.slippage < 1:
            raise ValueError('수수료와 슬리피지는 0 이상 1 미만이어야 합니다.')
        if not 0 < self.max_leverage <= 1 or not 0 < self.risk_fraction <= 1:
            raise ValueError('레버리지 상한과 위험 비율은 0 초과 1 이하여야 합니다.')
        if not 0 < self.stop_pct < 1 or not 0 < self.take_profit_pct < 1:
            raise ValueError('손절/익절 비율은 0 초과 1 미만이어야 합니다.')
        if not isinstance(self.max_holding_hours, int) or not 1 <= self.max_holding_hours <= 72:
            raise ValueError('최대 보유 시간은 1~72 정수여야 합니다.')


class FundingModel(Protocol):
    label: str

    def cost(self, *, symbol, time, side, quantity, mark_price) -> float:
        """Signed USDT charge (negative = credit) at a held candle's close.

        Future adapters must supply timestamped funding data and their own
        settlement convention. No exchange connection is made by this engine.
        """
        ...


class ZeroFunding:
    label = 'unavailable_assumed_zero'

    def cost(self, **kwargs):
        return 0.


@dataclass
class Result:
    trades: pd.DataFrame
    equity: pd.DataFrame
    summary: dict


@dataclass
class Position:
    side: int
    quantity: float
    price: float
    time: pd.Timestamp
    entry_fee: float
    stop: float
    take: float
    funding: float = 0.


def run(frame, symbol: str, strategy: Strategy, config: Config, *, start, end, now=None,
        funding: FundingModel | None = None) -> Result:
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
            if position is None:
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
