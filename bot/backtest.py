"""Бэктест стратегии на исторических свечах Binance Futures.

Пример: python -m bot.backtest --days 730
"""
import argparse
import bisect
import os
import time

from .broker import PaperBroker, make_exchange
from .config import load_config
from .journal import append_trade, trade_row
from .risk import RiskGuard, position_size
from .strategy import generate_signals


def fetch_history(exchange, symbol, timeframe, days):
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    since = exchange.milliseconds() - days * 24 * 3600 * 1000
    candles = []
    while since < exchange.milliseconds() - tf_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1500)
        if not batch:
            break
        candles.extend(batch)
        since = batch[-1][0] + tf_ms
        time.sleep(exchange.rateLimit / 1000)
    # Последняя свеча ещё не закрыта
    return [c for c in candles if c[0] + tf_ms <= exchange.milliseconds()]


def fetch_funding(exchange, symbol, since_ms):
    """История ставок финансирования: список (timestamp_ms, rate)."""
    out = []
    while True:
        batch = exchange.fetch_funding_rate_history(symbol, since=since_ms, limit=1000)
        if not batch:
            break
        out.extend((f["timestamp"], f["fundingRate"]) for f in batch)
        if len(batch) < 1000:
            break
        since_ms = batch[-1]["timestamp"] + 1
        time.sleep(exchange.rateLimit / 1000)
    return out


def align_funding(candles, funding, tf_ms):
    """Для каждой свечи — последняя известная ставка на момент её закрытия."""
    if not funding:
        return None
    times = [t for t, _ in funding]
    out = []
    for c in candles:
        k = bisect.bisect_right(times, c[0] + tf_ms) - 1
        out.append(funding[k][1] if k >= 0 else None)
    return out


def run_backtest(candles, cfg, stop_on_guard=True, funding=None, journal=None):
    """Прогон стратегии. funding — ставки, выровненные по свечам (или None)."""
    broker = PaperBroker(cfg.paper_balance)
    guard = RiskGuard(cfg.paper_balance, cfg.target_profit_pct, cfg.max_drawdown_pct,
                      cfg.max_consecutive_losses, cfg.pause_hours)
    peak, max_dd, stop_reason = cfg.paper_balance, 0.0, None
    signals = generate_signals(candles, cfg.strategy, funding)
    mid = (cfg.strategy.warmup + len(candles)) // 2
    mid_equity = cfg.paper_balance
    opened_ms = 0

    for i in range(cfg.strategy.warmup, len(candles)):
        if i == mid:
            mid_equity = broker.equity()
        c = candles[i]
        if broker.position:
            pos = broker.position
            pnl = broker.check_exit(c[2], c[3])
            if pnl is not None:
                guard.record_trade(pnl, c[0])
                eq = broker.equity()
                if journal:
                    append_trade(journal, trade_row(cfg.symbol, pos["signal"], pos["amount"],
                                                    opened_ms, c[0], pnl, eq))
                peak = max(peak, eq)
                max_dd = max(max_dd, (peak - eq) / peak * 100)
                if stop_on_guard:
                    stop_reason = guard.stop_reason(eq)
                    if stop_reason:
                        break
        # Как в живом боте: после выхода внутри свечи на её закрытии можно войти снова
        if broker.position:
            continue
        sig = signals[i]
        if sig and not guard.is_paused(c[0]):
            amount = position_size(broker.equity(), sig.entry, sig.stop,
                                   cfg.risk_per_trade_pct, cfg.leverage)
            if amount > 0:
                broker.open(sig, amount)
                opened_ms = c[0]

    trades = guard.trades
    wins = [t for t in trades if t > 0]
    end = broker.equity()
    return {
        "start": cfg.paper_balance,
        "end": end,
        "return_pct": (end / cfg.paper_balance - 1) * 100,
        # Доходность по половинам торгового периода (после разогрева индикаторов)
        "first_half_pct": (mid_equity / cfg.paper_balance - 1) * 100,
        "second_half_pct": (end / mid_equity - 1) * 100 if mid_equity > 0 else 0.0,
        "trades": len(trades),
        "winrate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "max_drawdown_pct": max_dd,
        "stop_reason": stop_reason,
    }


def load_data(ex, symbol, timeframe, days, with_funding=True):
    """Свечи и выровненные по ним ставки funding (None, если не загрузились)."""
    candles = fetch_history(ex, symbol, timeframe, days)
    funding = None
    if with_funding and candles:
        try:
            funding = align_funding(candles, fetch_funding(ex, symbol, candles[0][0]),
                                    ex.parse_timeframe(timeframe) * 1000)
        except Exception as e:
            print(f"  funding не загружен ({e}), фильтр funding в бэктесте выключен")
    return candles, funding


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--no-guard", action="store_true",
                    help="не останавливаться по цели/просадке, прогнать весь период")
    ap.add_argument("--journal", help="сохранить сделки в CSV, например trades.csv")
    args = ap.parse_args()
    cfg = load_config()
    cfg.mode = "paper"
    ex = make_exchange(cfg)
    print(f"Загрузка {args.days} дн. свечей {cfg.symbol} {cfg.timeframe}...")
    candles, funding = load_data(ex, cfg.symbol, cfg.timeframe, args.days,
                                 cfg.strategy.max_funding_pct > 0)
    print(f"Свечей: {len(candles)}, из них на разогрев индикаторов: {cfg.strategy.warmup}")
    if len(candles) <= cfg.strategy.warmup:
        raise SystemExit("Мало истории для этой стратегии — увеличьте --days")
    if args.journal and os.path.exists(args.journal):
        os.remove(args.journal)
    r = run_backtest(candles, cfg, stop_on_guard=not args.no_guard,
                     funding=funding, journal=args.journal)
    print(f"Депозит: {r['start']:.2f} -> {r['end']:.2f} USDT ({r['return_pct']:+.1f}%)")
    print(f"Сделок: {r['trades']}, винрейт {r['winrate_pct']:.1f}%, "
          f"макс. просадка {r['max_drawdown_pct']:.1f}%")
    print(f"1-я половина: {r['first_half_pct']:+.1f}%, 2-я половина: {r['second_half_pct']:+.1f}%")
    if r["stop_reason"]:
        print(f"Остановка: {r['stop_reason']}")
    if args.journal:
        print(f"Журнал сделок: {args.journal}")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
