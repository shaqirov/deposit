"""Стратегия: импульсный вход по пересечению EMA с фильтром RSI.

Long:  быстрая EMA пересекла медленную снизу вверх, цена выше медленной EMA,
       RSI в зоне 50..70 (есть импульс, но нет перекупленности).
Short: зеркально, RSI в зоне 30..50.

Стоп ставится на расстоянии STOP_ATR_MULT * ATR, тейк — на TAKE_PROFIT_RR
стопов. Выигрыш в RR раз больше проигрыша позволяет разгонять депозит даже
при винрейте ниже 50%.
"""
from dataclasses import dataclass

from .indicators import atr, ema, rsi


@dataclass
class StrategyParams:
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    take_profit_rr: float = 2.0

    @property
    def warmup(self):
        return max(self.ema_slow, self.rsi_period, self.atr_period) + 2


@dataclass
class Signal:
    side: str  # "long" | "short"
    entry: float
    stop: float
    take: float


def generate_signal(candles, p: StrategyParams):
    """candles — список закрытых свечей [ts, open, high, low, close, volume].

    Возвращает Signal для входа по цене закрытия последней свечи или None.
    """
    if len(candles) < p.warmup:
        return None
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]

    fast = ema(closes, p.ema_fast)
    slow = ema(closes, p.ema_slow)
    r = rsi(closes, p.rsi_period)[-1]
    a = atr(highs, lows, closes, p.atr_period)[-1]
    if None in (fast[-2], slow[-2], r, a) or a <= 0:
        return None

    price = closes[-1]
    crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
    crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
    risk = a * p.stop_atr_mult

    if crossed_up and price > slow[-1] and 50 < r < 70:
        return Signal("long", price, price - risk, price + risk * p.take_profit_rr)
    if crossed_down and price < slow[-1] and 30 < r < 50:
        return Signal("short", price, price + risk, price - risk * p.take_profit_rr)
    return None
