"""Бэктест стратегии на исторических свечах Binance Futures.

Пример: python -m bot.backtest --days 30
"""
import argparse
import time

from .broker import PaperBroker, make_exchange
from .config import load_config
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
    return candles


def run_backtest(candles, cfg, stop_on_guard=True):
    broker = PaperBroker(cfg.paper_balance)
    guard = RiskGuard(cfg.paper_balance, cfg.target_profit_pct,
                      cfg.max_drawdown_pct, cfg.max_consecutive_losses)
    peak, max_dd, stop_reason = cfg.paper_balance, 0.0, None
    signals = generate_signals(candles, cfg.strategy)

    for i in range(cfg.strategy.warmup, len(candles)):
        c = candles[i]
        if broker.position:
            pnl = broker.check_exit(c[2], c[3])
            if pnl is not None:
                guard.record_trade(pnl)
                eq = broker.equity()
                peak = max(peak, eq)
                max_dd = max(max_dd, (peak - eq) / peak * 100)
                if stop_on_guard:
                    stop_reason = guard.stop_reason(eq)
                    if stop_reason:
                        break
            continue
        sig = signals[i]
        if sig:
            amount = position_size(broker.equity(), sig.entry, sig.stop,
                                   cfg.risk_per_trade_pct, cfg.leverage)
            if amount > 0:
                broker.open(sig, amount)

    trades = guard.trades
    wins = [t for t in trades if t > 0]
    return {
        "start": cfg.paper_balance,
        "end": broker.equity(),
        "return_pct": (broker.equity() / cfg.paper_balance - 1) * 100,
        "trades": len(trades),
        "winrate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "max_drawdown_pct": max_dd,
        "stop_reason": stop_reason,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-guard", action="store_true",
                    help="не останавливаться по цели/просадке, прогнать весь период")
    args = ap.parse_args()
    cfg = load_config()
    cfg.mode = "paper"
    ex = make_exchange(cfg)
    print(f"Загрузка {args.days} дн. свечей {cfg.symbol} {cfg.timeframe}...")
    candles = fetch_history(ex, cfg.symbol, cfg.timeframe, args.days)
    print(f"Свечей: {len(candles)}")
    r = run_backtest(candles, cfg, stop_on_guard=not args.no_guard)
    print(f"Депозит: {r['start']:.2f} -> {r['end']:.2f} USDT ({r['return_pct']:+.1f}%)")
    print(f"Сделок: {r['trades']}, винрейт {r['winrate_pct']:.1f}%, "
          f"макс. просадка {r['max_drawdown_pct']:.1f}%")
    if r["stop_reason"]:
        print(f"Остановка: {r['stop_reason']}")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
