"""Бэктест арбитража funding (cash-and-carry): спот-лонг + шорт на фьючерсе на ту же сумму.

Пример: python -m bot.carry --days 1460

Цена монеты почти не влияет на результат: сколько заработал спот, столько потерял
шорт, и наоборот. Доход — выплаты funding, которые получает шорт, пока ставка
положительная. Расходы — комиссии и проскальзывание на 4 ордера за цикл
(спот и фьючерс, вход и выход), поэтому монеты нельзя часто перекладывать.

Что НЕ учитывается (реальные риски):
* ликвидация шорта при резком росте цены, если вовремя не довнести маржу со спота;
* разница цен спота и фьючерса (базис) при входе и выходе — только через проскальзывание;
* изменение размера позиций вместе с ценой: считаем объём в USDT постоянным.
"""
import argparse
import json
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

import ccxt

from .backtest import fetch_funding
from .broker import make_exchange
from .config import load_config
from .portfolio import CACHE_DIR, DAY_MS, top_symbols

YEAR_MS = 365 * DAY_MS
REBALANCE_MS = 8 * 3600 * 1000  # решения о входе и выходе — раз в 8 часов


@dataclass
class CarryParams:
    capital: float = 100.0
    leverage: float = 3.0        # плечо шорта: маржа = объём / плечо
    max_coins: int = 3           # монет в работе одновременно; капитал делится поровну
    lookback_days: float = 3.0   # за сколько дней усреднять ставку для решения
    enter_apr: float = 15.0      # входить, если средняя ставка выше, % годовых
    exit_apr: float = 0.0        # выходить, если средняя ставка ниже, % годовых
    spot_fee: float = 0.1        # комиссия спота, % за ордер
    perp_fee: float = 0.04       # комиссия фьючерса, % за ордер
    slippage: float = 0.05       # проскальзывание, % за ордер
    static: list = field(default_factory=list)  # если задано — держать эти монеты всё время


def cycle_cost_pct(p: CarryParams):
    """Издержки на вход ИЛИ выход в % от объёма: спот + фьючерс, комиссии и проскальзывание."""
    return p.spot_fee + p.perp_fee + 2 * p.slippage


def notional_for(capital_slot, leverage):
    """Объём позиции из выделенного капитала: спот на весь объём + маржа объём/плечо."""
    return capital_slot * leverage / (leverage + 1)


def simulate(funding, p: CarryParams, start_ms=None, end_ms=None):
    """funding: {symbol: [(ts, rate), ...]} по возрастанию времени, rate — доля за выплату."""
    events = sorted((ts, sym, rate) for sym, lst in funding.items() for ts, rate in lst)
    if not events:
        return None
    start_ms = events[0][0] if start_ms is None else start_ms
    end_ms = events[-1][0] if end_ms is None else end_ms
    lookback_ms = p.lookback_days * DAY_MS
    history = {s: [] for s in funding}  # прошедшие выплаты, видимые на момент решения
    held = {}  # symbol -> объём, USDT
    equity = peak = p.capital
    max_dd, rotations, invested_ms, last_ts = 0.0, 0, 0, start_ms
    paid = received = costs = 0.0
    curve = []
    next_decision = start_ms
    year_start = {}

    def apr(sym, now):
        """Средняя ставка за lookback в % годовых (интервал выплат бывает 8ч, 4ч, 1ч)."""
        h = history[sym]
        recent = [r for t, r in h if t > now - lookback_ms]
        if len(recent) < 3:
            return None
        times = [t for t, _ in h[-10:]]
        interval = statistics.median(b - a for a, b in zip(times, times[1:]))
        return statistics.mean(recent) * YEAR_MS / interval * 100

    def trade_cost(notional):
        return notional * cycle_cost_pct(p) / 100

    def decide(now):
        nonlocal equity, rotations, costs
        if p.static:
            for sym in p.static:
                if sym in funding and sym not in held and history[sym]:
                    n = notional_for(equity / len(p.static), p.leverage)
                    held[sym] = n
                    equity -= trade_cost(n)
                    costs += trade_cost(n)
            return
        rates = {s: apr(s, now) for s in funding}
        for sym in list(held):
            if rates[sym] is None or rates[sym] < p.exit_apr:
                n = held.pop(sym)
                equity -= trade_cost(n)
                costs += trade_cost(n)
                rotations += 1
        candidates = sorted((r, s) for s, r in rates.items()
                            if r is not None and r > p.enter_apr and s not in held)
        for r, sym in reversed(candidates):
            if len(held) >= p.max_coins:
                break
            n = notional_for(equity / p.max_coins, p.leverage)
            held[sym] = n
            equity -= trade_cost(n)
            costs += trade_cost(n)

    for ts, sym, rate in events:
        if ts > end_ms:
            break
        # Решения строго до выплаты: вход в момент выплаты её уже не получает,
        # а решение в этот момент видит только уже опубликованные ставки
        while next_decision < ts:
            if next_decision >= start_ms:
                if held:
                    invested_ms += next_decision - last_ts
                last_ts = next_decision
                decide(next_decision)
            next_decision += REBALANCE_MS
        history[sym].append((ts, rate))
        if ts < start_ms:
            continue
        if sym in held:
            amount = held[sym] * rate  # шорт получает при положительной ставке и платит при отрицательной
            equity += amount
            if amount >= 0:
                received += amount
            else:
                paid -= amount
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        year = datetime.fromtimestamp(ts / 1000, timezone.utc).year
        year_start.setdefault(year, equity)
        curve.append((ts, equity))
    # закрываем всё в конце
    for sym in list(held):
        n = held.pop(sym)
        equity -= trade_cost(n)
        costs += trade_cost(n)

    years = max((end_ms - start_ms) / YEAR_MS, 1e-9)
    mid = start_ms + (end_ms - start_ms) // 2
    mid_equity = next((e for t, e in curve if t >= mid), equity)
    by_year = {}
    years_sorted = sorted(year_start)
    for i, y in enumerate(years_sorted):
        end_eq = year_start[years_sorted[i + 1]] if i + 1 < len(years_sorted) else equity
        by_year[y] = (end_eq / year_start[y] - 1) * 100
    return {
        "end": equity,
        "return_pct": (equity / p.capital - 1) * 100,
        "apr_pct": ((equity / p.capital) ** (1 / years) - 1) * 100 if equity > 0 else -100.0,
        "max_drawdown_pct": max_dd,
        "rotations": rotations,
        "invested_pct": invested_ms / max(end_ms - start_ms, 1) * 100,
        "received": received, "paid": paid, "costs": costs,
        "first_half_pct": (mid_equity / p.capital - 1) * 100,
        "second_half_pct": (equity / mid_equity - 1) * 100 if mid_equity else 0.0,
        "by_year": by_year,
    }


def cached_funding(ex, symbol, days):
    os.makedirs(CACHE_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = os.path.join(CACHE_DIR, f"funding_{symbol.replace('/', '_').replace(':', '_')}_{days}_{day}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return [tuple(x) for x in json.load(f)]
    data = fetch_funding(ex, symbol, ex.milliseconds() - days * DAY_MS)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--leverage", type=float, default=3.0)
    ap.add_argument("--spot-fee", type=float, default=0.1, help="комиссия спота, %% (0.075 с BNB)")
    ap.add_argument("--slippage", type=float, default=0.05)
    args = ap.parse_args()

    cfg = load_config()
    cfg.mode = "paper"
    ex = make_exchange(cfg)
    spot = ccxt.binance({"enableRateLimit": True})
    spot.load_markets()
    print(f"Выбираю {args.top} самых ликвидных фьючерсов, у которых есть спот к USDT...")
    symbols = [s for s in top_symbols(ex, args.top * 2) if f"{s.split('/')[0]}/USDT" in spot.markets]
    symbols = symbols[:args.top]
    for need in ("BTC/USDT:USDT", "ETH/USDT:USDT"):
        if need not in symbols:
            symbols.append(need)
    funding = {}
    for i, s in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] funding {s}...", flush=True)
        try:
            funding[s] = cached_funding(ex, s, args.days)
        except Exception as e:
            print(f"  пропуск: {e}")
    print(f"Монеты: {', '.join(s.split('/')[0] for s in funding)}")

    base = dict(leverage=args.leverage, spot_fee=args.spot_fee, slippage=args.slippage)
    variants = [
        ("BTC всё время", CarryParams(static=["BTC/USDT:USDT"], **base)),
        ("BTC+ETH всё время", CarryParams(static=["BTC/USDT:USDT", "ETH/USDT:USDT"], **base)),
        ("ротация 3 монеты >15%", CarryParams(max_coins=3, enter_apr=15, **base)),
        ("ротация 3 монеты >30%", CarryParams(max_coins=3, enter_apr=30, **base)),
        ("ротация 5 монет >15%", CarryParams(max_coins=5, enter_apr=15, **base)),
    ]
    p0 = variants[0][1]
    print(f"\nКапитал 100 USDT, плечо шорта x{args.leverage:g} (в работе {notional_for(100, args.leverage):.0f}% "
          f"капитала), издержки {cycle_cost_pct(p0):.2f}% объёма на вход и столько же на выход.\n")
    print(f"{'Вариант':<24}{'Итог %':>8}{'В год %':>9}{'Просадка':>10}{'В работе':>10}"
          f"{'Перекладок':>11}{'1-я пол.':>10}{'2-я пол.':>10}")
    results = []
    for name, p in variants:
        r = simulate(funding, p)
        results.append((name, r))
        print(f"{name:<24}{r['return_pct']:>+8.1f}{r['apr_pct']:>+9.1f}{r['max_drawdown_pct']:>9.1f}%"
              f"{r['invested_pct']:>9.0f}%{r['rotations']:>11}{r['first_half_pct']:>+10.1f}"
              f"{r['second_half_pct']:>+10.1f}")
    print("\nПо годам, %:")
    years = sorted({y for _, r in results for y in r["by_year"]})
    print(f"{'Вариант':<24}" + "".join(f"{y:>8}" for y in years))
    for name, r in results:
        print(f"{name:<24}" + "".join(f"{r['by_year'].get(y, 0):>+8.1f}" for y in years))
    print("\nПолучено / заплачено funding / издержки, USDT:")
    for name, r in results:
        print(f"{name:<24}+{r['received']:.1f} / -{r['paid']:.1f} / -{r['costs']:.1f}")
    print("\nНе учтено: ликвидация шорта при резком росте цены, базис спот/фьючерс, риск биржи.")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
