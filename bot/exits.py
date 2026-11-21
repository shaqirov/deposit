"""Правила выхода из позиции: фиксированные стоп и тейк, безубыток или трейлинг-стоп.

R — расстояние от входа до начального стопа.

fixed         стоп на месте, тейк — TAKE_PROFIT_RR стопов (как раньше)
breakeven     когда цена прошла +ACTIVATE_R, стоп переносится в безубыток; тейк прежний
trail         после +ACTIVATE_R стоп тянется за лучшей ценой на расстоянии TRAIL_R; тейк прежний
trail_notake  как trail, но без тейка — прибыль не ограничена, выход только по стопу

Стоп подтягивается только по закрытой свече и только в сторону прибыли.
"""

EXIT_MODES = ("fixed", "breakeven", "trail", "trail_notake")
ACTIVATE_R = 1.0
TRAIL_R = 1.0
# Безубыток чуть дальше входа: покрывает комиссии за вход и выход (2 x 0.04%)
BREAKEVEN_BUFFER = 0.001


def initial_take(mode, take):
    return None if mode == "trail_notake" else take


def hit_price(side, stop, take, high, low):
    """Цена выхода, если свеча задела стоп или тейк; если оба — консервативно стоп."""
    if side == "long":
        if low <= stop:
            return stop
        return take if take is not None and high >= take else None
    if high >= stop:
        return stop
    return take if take is not None and low <= take else None


def next_stop(mode, side, entry, initial_stop, stop, best):
    """Стоп после закрытия свечи. best — лучшая цена с момента входа
    (максимум для лонга, минимум для шорта)."""
    if mode == "fixed":
        return stop
    r = abs(entry - initial_stop)
    d = 1 if side == "long" else -1
    if (best - entry) * d < ACTIVATE_R * r:
        return stop
    if mode == "breakeven":
        new = entry * (1 + d * BREAKEVEN_BUFFER)
    else:
        new = best - d * TRAIL_R * r
    return max(stop, new) if side == "long" else min(stop, new)


def best_price(side, best, high, low):
    return max(best, high) if side == "long" else min(best, low)


def open_risk(side, entry, stop, amount):
    """Сколько ещё можно потерять по позиции при текущем стопе (0, если стоп в прибыли)."""
    d = 1 if side == "long" else -1
    return max(0.0, (entry - stop) * d * amount)
