"""Торговые стратегии. Выбирается параметром STRATEGY.

ema_cross       Импульс: быстрая EMA пересекает медленную, фильтр RSI
                (лонг 50..70, шорт 30..50).
breakout        Пробой канала Дончиана: закрытие выше максимума (ниже минимума)
                последних N свечей по направлению тренда (цена выше/ниже EMA 200).
                Мало сделок, ставка на сильные движения — классика для разгона.
mean_reversion  Возврат к среднему: закрытие за полосой Боллинджера при
                перепроданности/перекупленности по RSI, тейк — средняя линия.
                Высокий винрейт, небольшие прибыли.
trend_pullback  Следование за трендом на старших таймфреймах (4h, 1d). Тренд —
                цена выше 200-дневной SMA и максимумы растут (для шорта
                зеркально). Вход на откате: свеча коснулась EMA и закрылась
                обратно в сторону тренда. Стоп за локальным минимумом
                (максимумом) с запасом 0.5 ATR, тейк — TAKE_PROFIT_RR стопов.

Фильтр funding для всех стратегий: лонг не открывается при ставке выше
MAX_FUNDING_PCT (перегретые лонги), шорт — при ставке ниже -MAX_FUNDING_PCT.

Во всех остальных стратегиях стоп = STOP_ATR_MULT * ATR от входа. Тейк для ema_cross и
breakout — TAKE_PROFIT_RR стопов, для mean_reversion — средняя полоса.
"""
from dataclasses import dataclass
from statistics import pstdev

from .indicators import atr, ema, rsi, sma

STRATEGIES = ("ema_cross", "breakout", "mean_reversion", "trend_pullback")


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
    trend_sma_days: int = 200
    swing_lookback: int = 10
    pullback_ema: int = 20
    allow_shorts: bool = True
    max_funding_pct: float = 0.05  # за 8 часов; 0 — фильтр выключен
    candles_per_day: float = 1.0   # выставляется из таймфрейма

    @property
    def trend_sma_period(self):
        return max(1, round(self.trend_sma_days * self.candles_per_day))

    @property
    def warmup(self):
        periods = [self.rsi_period, self.atr_period]
        if self.name == "ema_cross":
            periods += [self.ema_slow]
        elif self.name == "breakout":
            periods += [self.trend_ema, self.donchian_period]
        elif self.name == "mean_reversion":
            periods += [self.bb_period]
        elif self.name == "trend_pullback":
            periods += [self.trend_sma_period, self.swing_lookback * 2, self.pullback_ema]
        return max(periods) + 2


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
    if p.name not in STRATEGIES:
        raise ValueError(f"Неизвестная стратегия {p.name}, доступны: {', '.join(STRATEGIES)}")
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]
    atrs = atr(highs, lows, closes, p.atr_period)
    rsis = rsi(closes, p.rsi_period)
    make = {"ema_cross": _ema_cross, "breakout": _breakout,
            "mean_reversion": _mean_reversion, "trend_pullback": _trend_pullback}[p.name]
    check = make(candles, highs, lows, closes, atrs, p)
    max_funding = p.max_funding_pct / 100

    out = [None] * len(candles)
    for i in range(p.warmup - 1, len(candles)):
        a, r = atrs[i], rsis[i]
        if a is None or r is None or a <= 0:
            continue
        sig = check(i, closes[i], a * p.stop_atr_mult, r)
        if sig and funding and funding[i] is not None and max_funding > 0:
            if (sig.side == "long" and funding[i] > max_funding) or \
                    (sig.side == "short" and funding[i] < -max_funding):
                sig = None
        out[i] = sig
    return out


def _with_rr(side, price, risk, rr):
    if side == "long":
        return Signal("long", price, price - risk, price + risk * rr)
    return Signal("short", price, price + risk, price - risk * rr)


def _ema_cross(candles, highs, lows, closes, atrs, p):
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


def _breakout(candles, highs, lows, closes, atrs, p):
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


def _mean_reversion(candles, highs, lows, closes, atrs, p):
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


def _trend_pullback(candles, highs, lows, closes, atrs, p):
    trend = sma(closes, p.trend_sma_period)
    pull = ema(closes, p.pullback_ema)
    n = p.swing_lookback

    def check(i, price, risk, r):
        if trend[i] is None or pull[i] is None or i < 2 * n:
            return None
        recent_high, prev_high = max(highs[i - n + 1:i + 1]), max(highs[i - 2 * n + 1:i - n + 1])
        recent_low, prev_low = min(lows[i - n + 1:i + 1]), min(lows[i - 2 * n + 1:i - n + 1])
        opened = candles[i][1]
        buffer = atrs[i] * 0.5
        if price > trend[i] and recent_high > prev_high \
                and lows[i] <= pull[i] < price and price > opened:
            stop = recent_low - buffer
            if stop < price:
                return Signal("long", price, stop, price + (price - stop) * p.take_profit_rr)
        if p.allow_shorts and price < trend[i] and recent_low < prev_low \
                and highs[i] >= pull[i] > price and price < opened:
            stop = recent_high + buffer
            if stop > price:
                return Signal("short", price, stop, price - (stop - price) * p.take_profit_rr)
        return None
    return check
