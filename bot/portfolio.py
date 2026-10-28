"""Портфельный бэктест: стратегия на многих монетах с общим депозитом.

Пример: python -m bot.portfolio --top 30 --days 1460

Что учитывается:
* общий депозит и одновременные позиции на разных монетах;
* лимит числа позиций и суммарного риска открытых позиций;
* минимальный объём ордера Binance и ограничение по плечу;
* комиссии и фильтр funding.

Калькулятор разгона: бэктест стартует с разных дат (каждые --step дней) и
смотрит, чем кончилось за --horizon дней — депозит удвоился (цель), упал до
аварийной остановки или ни то ни другое.
"""
import argparse
import json
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from .backtest import load_data
from .broker import TAKER_FEE, make_exchange
from .config import load_config
from .risk import MARGIN_BUFFER
from .state import ROOT
from .strategy import StrategyParams, generate_signals

CACHE_DIR = os.path.join(ROOT, "cache")
DAY_MS = 24 * 3600 * 1000
# Стейблкоины и индексы — не торгуем
EXCLUDE_BASES = {"USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDP", "USDE", "BTCDOM", "DEFI"}
DEFAULT_MIN_NOTIONAL = 5.0


@dataclass
class PortfolioParams:
    risk_pct: float = 2.0            # риск на сделку, % от депозита
    max_positions: int = 6           # одновременно открытых позиций
    max_total_risk_pct: float = 20.0  # суммарный риск открытых позиций, %
    leverage: float = 10.0           # общий объём позиций не больше депозит * плечо
    target_pct: float = 100.0        # цель (x2 = 100%)
    ruin_pct: float = 50.0           # аварийная остановка при просадке от старта
    start_balance: float = 100.0


def top_symbols(ex, top):
    """Самые ликвидные бессрочные фьючерсы к USDT по объёму за 24 часа."""
    ex.load_markets()
    tickers = ex.fetch_tickers()
    rows = []
    for symbol, t in tickers.items():
        m = ex.markets.get(symbol)
        if not m or not m.get("swap") or not m.get("linear") or m.get("quote") != "USDT":
            continue
        if not m.get("active", True) or m.get("base") in EXCLUDE_BASES:
            continue
        rows.append((t.get("quoteVolume") or 0, symbol))
    rows.sort(reverse=True)
    return [s for _, s in rows[:top]]


def min_notional(ex, symbol):
    m = ex.markets.get(symbol) or {}
    return ((m.get("limits") or {}).get("cost") or {}).get("min") or DEFAULT_MIN_NOTIONAL


def cached_data(ex, symbol, timeframe, days, with_funding):
    """Свечи и funding с кэшем на диске (обновляется раз в сутки)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    name = f"{symbol.replace('/', '_').replace(':', '_')}_{timeframe}_{days}_{int(with_funding)}_{day}.json"
    path = os.path.join(CACHE_DIR, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d["candles"], d["funding"]
    candles, funding = load_data(ex, symbol, timeframe, days, with_funding)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"candles": candles, "funding": funding}, f)
    return candles, funding


def prepare(data, strategy: StrategyParams):
    """data: {symbol: (candles, funding, min_notional)} в порядке приоритета.

    Возвращает (timestamps, candles_at, signals_at): свечи и сигналы по времени.
    """
    candles_at, signals_at = {}, {}
    for rank, (symbol, (candles, funding, min_cost)) in enumerate(data.items()):
        signals = generate_signals(candles, strategy, funding)
        for c, sig in zip(candles, signals):
            candles_at.setdefault(c[0], {})[symbol] = c
            if sig:
                signals_at.setdefault(c[0], []).append((rank, symbol, sig, min_cost))
    for lst in signals_at.values():
        lst.sort(key=lambda x: x[0])
    return sorted(candles_at), candles_at, signals_at


def simulate(prepared, pp: PortfolioParams, start_ts=None, end_ts=None, stop_at_target=True):
    """Прогон портфеля. Позиция открывается по закрытию сигнальной свечи,
    стоп/тейк проверяются со следующей свечи; если за свечу задеты оба — стоп."""
    timestamps, candles_at, signals_at = prepared
    balance = pp.start_balance
    positions = {}  # symbol -> dict(sig, amount, risk, open_ts)
    trades = []
    peak, max_dd = balance, 0.0
    outcome, outcome_ts = None, None
    start_ts = timestamps[0] if start_ts is None else start_ts
    mid_ts = start_ts + ((end_ts or timestamps[-1]) - start_ts) // 2
    mid_balance = None

    for ts in timestamps:
        if ts < start_ts:
            continue
        if end_ts is not None and ts > end_ts:
            break
        if mid_balance is None and ts >= mid_ts:
            mid_balance = balance
        bar = candles_at[ts]

        for symbol in list(positions):
            c = bar.get(symbol)
            if c is None:
                continue
            p = positions[symbol]
            s, amount = p["sig"], p["amount"]
            if s.side == "long":
                exit_price = s.stop if c[3] <= s.stop else s.take if c[2] >= s.take else None
            else:
                exit_price = s.stop if c[2] >= s.stop else s.take if c[3] <= s.take else None
            if exit_price is None:
                continue
            direction = 1 if s.side == "long" else -1
            gross = (exit_price - s.entry) * amount * direction
            exit_fee = exit_price * amount * TAKER_FEE
            balance += gross - exit_fee
            pnl = gross - exit_fee - s.entry * amount * TAKER_FEE
            trades.append({"symbol": symbol, "side": s.side, "open_ts": p["open_ts"],
                           "close_ts": ts, "pnl": pnl, "r": pnl / p["risk"]})
            del positions[symbol]
            peak = max(peak, balance)
            max_dd = max(max_dd, (peak - balance) / peak * 100)

        change = (balance / pp.start_balance - 1) * 100
        if change <= -pp.ruin_pct:
            outcome, outcome_ts = "ruin", ts
            break
        if stop_at_target and change >= pp.target_pct:
            outcome, outcome_ts = "target", ts
            break

        for _, symbol, sig, min_cost in signals_at.get(ts, ()):
            if len(positions) >= pp.max_positions:
                break
            if symbol in positions:
                continue
            stop_dist = abs(sig.entry - sig.stop)
            risk = balance * pp.risk_pct / 100
            open_risk = sum(p["risk"] for p in positions.values())
            if open_risk + risk > balance * pp.max_total_risk_pct / 100:
                continue
            amount = risk / stop_dist
            used = sum(p["sig"].entry * p["amount"] for p in positions.values())
            free = balance * pp.leverage * MARGIN_BUFFER - used
            if amount * sig.entry > free:  # не хватает плеча — уменьшаем позицию
                amount = max(free, 0) / sig.entry
                risk = amount * stop_dist
            if amount * sig.entry < min_cost:
                continue
            balance -= sig.entry * amount * TAKER_FEE
            positions[symbol] = {"sig": sig, "amount": amount, "risk": risk, "open_ts": ts}

    wins = [t for t in trades if t["pnl"] > 0]
    last_ts = outcome_ts or (end_ts if end_ts is not None else timestamps[-1])
    mid_balance = mid_balance or balance
    return {
        "end": balance,
        "return_pct": (balance / pp.start_balance - 1) * 100,
        "first_half_pct": (mid_balance / pp.start_balance - 1) * 100,
        "second_half_pct": (balance / mid_balance - 1) * 100 if mid_balance > 0 else 0.0,
        "trades": len(trades),
        "winrate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "max_drawdown_pct": max_dd,
        "outcome": outcome,
        "days": (last_ts - start_ts) / DAY_MS,
        "trade_list": trades,
    }


def acceleration_odds(prepared, pp, horizon_days, step_days, first_ts):
    """Старт с разных дат: доли исходов 'цель', 'аварийная остановка', 'ни то ни другое'."""
    timestamps = prepared[0]
    starts = []
    ts = first_ts
    while ts + horizon_days * DAY_MS <= timestamps[-1]:
        starts.append(ts)
        ts += step_days * DAY_MS
    results = [simulate(prepared, pp, s, s + horizon_days * DAY_MS) for s in starts]
    n = len(results) or 1
    hit = [r for r in results if r["outcome"] == "target"]
    ruin = [r for r in results if r["outcome"] == "ruin"]
    return {
        "runs": len(results),
        "target_pct": len(hit) / n * 100,
        "ruin_pct": len(ruin) / n * 100,
        "neither_pct": (len(results) - len(hit) - len(ruin)) / n * 100,
        "median_days_to_target": statistics.median(r["days"] for r in hit) if hit else None,
        "median_return_pct": statistics.median(r["return_pct"] for r in results) if results else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30, help="сколько самых ликвидных монет взять")
    ap.add_argument("--symbols", default="", help="или свой список тикеров через запятую")
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--risks", default="1,2,3,5,7,10", help="риск на сделку, %% через запятую")
    ap.add_argument("--max-positions", type=int, default=6)
    ap.add_argument("--max-total-risk", type=float, default=20.0)
    ap.add_argument("--leverage", type=float, default=10.0)
    ap.add_argument("--target", type=float, default=100.0, help="цель, %% (100 = x2)")
    ap.add_argument("--ruin", type=float, default=50.0, help="аварийная остановка, %% просадки")
    ap.add_argument("--horizon", type=int, default=180, help="срок разгона, дней")
    ap.add_argument("--step", type=int, default=14, help="шаг между стартами, дней")
    ap.add_argument("--no-funding", action="store_true", help="не загружать funding (быстрее)")
    args = ap.parse_args()

    cfg = load_config()
    cfg.mode = "paper"
    ex = make_exchange(cfg)
    ex.load_markets()
    if args.symbols:
        symbols = [f"{t.strip().upper()}/USDT:USDT" for t in args.symbols.split(",") if t.strip()]
    else:
        print(f"Выбираю {args.top} самых ликвидных монет...")
        symbols = top_symbols(ex, args.top)
    print(f"Монеты: {', '.join(s.split('/')[0] for s in symbols)}")

    with_funding = not args.no_funding and cfg.strategy.max_funding_pct > 0
    data = {}
    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] загрузка {symbol} {cfg.timeframe}...", flush=True)
        try:
            candles, funding = cached_data(ex, symbol, cfg.timeframe, args.days, with_funding)
        except Exception as e:
            print(f"  пропуск: {e}")
            continue
        if len(candles) <= cfg.strategy.warmup + 50:
            print("  мало истории, пропуск")
            continue
        data[symbol] = (candles, funding, min_notional(ex, symbol))

    prepared = prepare(data, cfg.strategy)
    tf_ms = ex.parse_timeframe(cfg.timeframe) * 1000
    first_ts = prepared[0][0] + cfg.strategy.warmup * tf_ms
    years = (prepared[0][-1] - first_ts) / DAY_MS / 365

    print(f"\nМонет в портфеле: {len(data)}, период {years:.1f} г., старт 100 USDT, "
          f"до {args.max_positions} позиций, суммарный риск до {args.max_total_risk:g}%, "
          f"плечо до x{args.leverage:g}\n")
    print("Весь период (без остановки на цели; аварийная остановка — при "
          f"-{args.ruin:g}%):")
    print(f"{'Риск':>6}{'Итог %':>10}{'Сделок':>8}{'Винрейт':>9}{'Просадка':>10}"
          f"{'1-я пол.':>10}{'2-я пол.':>10}  Чем кончилось")
    for risk in [float(x) for x in args.risks.split(",")]:
        pp = PortfolioParams(risk, args.max_positions, args.max_total_risk, args.leverage,
                             args.target, args.ruin)
        r = simulate(prepared, pp, first_ts, stop_at_target=False)
        end = "аварийная остановка" if r["outcome"] == "ruin" else "доработал до конца"
        print(f"{risk:>5g}%{r['return_pct']:>+10.1f}{r['trades']:>8}{r['winrate_pct']:>8.1f}%"
              f"{r['max_drawdown_pct']:>9.1f}%{r['first_half_pct']:>+10.1f}"
              f"{r['second_half_pct']:>+10.1f}  {end}")

    print(f"\nШансы разгона: старт с разных дат каждые {args.step} дн., срок {args.horizon} дн., "
          f"цель +{args.target:g}%, аварийная остановка -{args.ruin:g}%:")
    print(f"{'Риск':>6}{'Стартов':>9}{'Цель':>8}{'Авария':>8}{'Ни то ни др.':>14}"
          f"{'Дней до цели':>14}{'Медиана итога':>15}")
    for risk in [float(x) for x in args.risks.split(",")]:
        pp = PortfolioParams(risk, args.max_positions, args.max_total_risk, args.leverage,
                             args.target, args.ruin)
        o = acceleration_odds(prepared, pp, args.horizon, args.step, first_ts)
        days = f"{o['median_days_to_target']:.0f}" if o["median_days_to_target"] else "—"
        print(f"{risk:>5g}%{o['runs']:>9}{o['target_pct']:>7.0f}%{o['ruin_pct']:>7.0f}%"
              f"{o['neither_pct']:>13.0f}%{days:>14}{o['median_return_pct']:>+14.1f}%")

    print("\nВажно: монеты выбраны по сегодняшнему объёму торгов. Те, что за эти годы "
          "рухнули или были удалены с биржи, в проверку не попали, поэтому реальные "
          "результаты на альткоинах, скорее всего, хуже.")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
