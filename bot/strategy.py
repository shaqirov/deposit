"""Торговые стратегии. Выбирается параметром STRATEGY.

ema_cross       Импульс: быстрая EMA пересекает медленную, фильтр RSI
                (лонг 50..70, шорт 30..50).
breakout        Пробой канала Дончиана: закрытие выше максимума (ниже минимума)
                последних N свечей по направлению тренда (цена выше/ниже EMA 200).
                Мало сделок, ставка на сильные движения — классика для разгона.
mean_reversion  Возврат к среднему: закрытие за полосой Боллинджера при
                перепроданности/перекупленности по RSI, тейк — средняя линия.
                Высокий винрейт, небольшие прибыли.

Во всех стратегиях стоп = STOP_ATR_MULT * ATR от входа. Тейк для ema_cross и
breakout — TAKE_PROFIT_RR стопов, для mean_reversion — средняя полоса.
"""
from dataclasses import dataclass
from statistics import pstdev

from .indicators import atr, ema, rsi

STRATEGIES = ("ema_cross", "breakout", "mean_reversion")


@dataclass
class StrategyParams:
    name: str = "ema_cross"
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    take_profit_rr: float = 2.0
    donchian_period: int = 20
    trend_ema: int = 200
    bb_period: int = 20
    bb_std: float = 2.0

    @property
    def warmup(self):
        periods = [self.rsi_period, self.atr_period]
        if self.name == "ema_cross":
            periods += [self.ema_slow]
        elif self.name == "breakout":
            periods += [self.trend_ema, self.donchian_period]
        elif self.name == "mean_reversion":
            periods += [self.bb_period]
        return max(periods) + 2


@dataclass
class Signal:
    side: str  # "long" | "short"
    entry: float
    stop: float
    take: float


def generate_signal(candles, p: StrategyParams):
    """Сигнал на последней закрытой свече (вход по её цене закрытия) или None."""
    if len(candles) < p.warmup:
        return None
    return generate_signals(candles, p)[-1]


def generate_signals(candles, p: StrategyParams):
    """Сигналы для каждой свечи за один проход — используется в бэктесте.

    candles — список закрытых свечей [ts, open, high, low, close, volume].
    """
    if p.name not in STRATEGIES:
        raise ValueError(f"Неизвестная стратегия {p.name}, доступны: {', '.join(STRATEGIES)}")
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]
    atrs = atr(highs, lows, closes, p.atr_period)
    rsis = rsi(closes, p.rsi_period)
    make = {"ema_cross": _ema_cross, "breakout": _breakout,
            "mean_reversion": _mean_reversion}[p.name]
    check = make(highs, lows, closes, rsis, p)

    out = [None] * len(candles)
    for i in range(p.warmup - 1, len(candles)):
        a, r = atrs[i], rsis[i]
        if a is None or r is None or a <= 0:
            continue
        out[i] = check(i, closes[i], a * p.stop_atr_mult, r)
    return out


def _with_rr(side, price, risk, rr):
    if side == "long":
        return Signal("long", price, price - risk, price + risk * rr)
    return Signal("short", price, price + risk, price - risk * rr)


def _ema_cross(highs, lows, closes, rsis, p):
    fast = ema(closes, p.ema_fast)
    slow = ema(closes, p.ema_slow)

    def check(i, price, risk, r):
        if fast[i - 1] is None or slow[i - 1] is None:
            return None
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i] and price > slow[i] and 50 < r < 70:
            return _with_rr("long", price, risk, p.take_profit_rr)
        if fast[i - 1] >= slow[i - 1] and fast[i] < slow[i] and price < slow[i] and 30 < r < 50:
            return _with_rr("short", price, risk, p.take_profit_rr)
        return None
    return check


def _breakout(highs, lows, closes, rsis, p):
    trend = ema(closes, p.trend_ema)
    n = p.donchian_period

    def check(i, price, risk, r):
        if trend[i] is None or i < n:
            return None
        upper = max(highs[i - n:i])
        lower = min(lows[i - n:i])
        if price > upper and price > trend[i]:
            return _with_rr("long", price, risk, p.take_profit_rr)
        if price < lower and price < trend[i]:
            return _with_rr("short", price, risk, p.take_profit_rr)
        return None
    return check


def _mean_reversion(highs, lows, closes, rsis, p):
    n = p.bb_period

    def check(i, price, risk, r):
        if i < n - 1:
            return None
        window = closes[i - n + 1:i + 1]
        mid = sum(window) / n
        dev = pstdev(window) * p.bb_std
        # Тейк на средней линии; слишком близкий тейк не окупает комиссии
        if price < mid - dev and r < 30 and mid - price >= risk * 0.5:
            return Signal("long", price, price - risk, mid)
        if price > mid + dev and r > 70 and price - mid >= risk * 0.5:
            return Signal("short", price, price + risk, mid)
        return None
    return check
