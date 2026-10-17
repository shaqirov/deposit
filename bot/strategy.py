"""Стратегия: пробой канала Дончиана по направлению тренда.

Long:  закрытие выше максимума последних DONCHIAN_PERIOD свечей, цена выше
       EMA TREND_EMA (торгуем только по тренду).
Short: закрытие ниже минимума последних DONCHIAN_PERIOD свечей, цена ниже EMA.

Стоп = STOP_ATR_MULT * ATR от входа, тейк — TAKE_PROFIT_RR стопов.

Фильтр funding: лонг не открывается при ставке выше MAX_FUNDING_PCT
(перегретые лонги), шорт — при ставке ниже -MAX_FUNDING_PCT.
"""
from dataclasses import dataclass

from .indicators import atr, ema


@dataclass
class StrategyParams:
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    take_profit_rr: float = 2.0
    donchian_period: int = 20
    trend_ema: int = 200
    max_funding_pct: float = 0.05  # за 8 часов; 0 — фильтр выключен

    @property
    def warmup(self):
        return max(self.atr_period, self.donchian_period, self.trend_ema) + 2


@dataclass
class Signal:
    side: str  # "long" | "short"
    entry: float
    stop: float
    take: float


def generate_signal(candles, p: StrategyParams, funding_rate=None):
    """Сигнал на последней закрытой свече (вход по её цене закрытия) или None.

    funding_rate — текущая ставка финансирования (доля, 0.0001 = 0.01%).
    """
    if len(candles) < p.warmup:
        return None
    funding = [None] * (len(candles) - 1) + [funding_rate]
    return generate_signals(candles, p, funding)[-1]


def generate_signals(candles, p: StrategyParams, funding=None):
    """Сигналы для каждой свечи за один проход — используется в бэктесте.

    candles — список закрытых свечей [ts, open, high, low, close, volume].
    funding — ставки финансирования для каждой свечи (или None).
    """
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]
    atrs = atr(highs, lows, closes, p.atr_period)
    trend = ema(closes, p.trend_ema)
    n = p.donchian_period
    max_funding = p.max_funding_pct / 100

    out = [None] * len(candles)
    for i in range(p.warmup - 1, len(candles)):
        a = atrs[i]
        if a is None or a <= 0 or trend[i] is None:
            continue
        price, risk = closes[i], a * p.stop_atr_mult
        f = funding[i] if funding and max_funding > 0 else None
        if price > max(highs[i - n:i]) and price > trend[i]:
            if f is None or f <= max_funding:
                out[i] = Signal("long", price, price - risk, price + risk * p.take_profit_rr)
        elif price < min(lows[i - n:i]) and price < trend[i]:
            if f is None or f >= -max_funding:
                out[i] = Signal("short", price, price + risk, price - risk * p.take_profit_rr)
    return out
