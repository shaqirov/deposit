"""Сравнение стратегий на нескольких монетах и таймфреймах.

Пример: python -m bot.compare --days 180

Каждая комбинация прогоняется без остановок по лимитам (как --no-guard).
Отдельно считается результат на первой и второй половине периода: если
стратегия заработала только на одной половине, скорее всего ей просто повезло.
"""
import argparse
import copy

from .backtest import fetch_history, run_backtest
from .broker import make_exchange
from .config import load_config
from .strategy import STRATEGIES

DEFAULT_SYMBOLS = "BTC,ETH,SOL,XRP,DOGE"
DEFAULT_TIMEFRAMES = "15m,1h,4h"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS,
                    help=f"тикеры через запятую (по умолчанию {DEFAULT_SYMBOLS})")
    ap.add_argument("--timeframes", default=DEFAULT_TIMEFRAMES,
                    help=f"таймфреймы через запятую (по умолчанию {DEFAULT_TIMEFRAMES})")
    ap.add_argument("--strategies", default=",".join(STRATEGIES))
    args = ap.parse_args()

    base = load_config()
    base.mode = "paper"
    ex = make_exchange(base)
    rows = []
    for ticker in args.symbols.split(","):
        symbol = f"{ticker.strip().upper()}/USDT:USDT"
        for tf in args.timeframes.split(","):
            tf = tf.strip()
            print(f"Загрузка {symbol} {tf}...")
            try:
                candles = fetch_history(ex, symbol, tf, args.days)
            except Exception as e:
                print(f"  пропуск: {e}")
                continue
            half = len(candles) // 2
            for name in args.strategies.split(","):
                cfg = copy.deepcopy(base)
                cfg.strategy.name = name.strip()
                full = run_backtest(candles, cfg, stop_on_guard=False)
                first = run_backtest(candles[:half], cfg, stop_on_guard=False)
                second = run_backtest(candles[half:], cfg, stop_on_guard=False)
                rows.append((cfg.strategy.name, ticker.strip().upper(), tf, full,
                             first["return_pct"], second["return_pct"]))

    rows.sort(key=lambda r: r[3]["return_pct"], reverse=True)
    print()
    print(f"{'Стратегия':<15}{'Монета':<7}{'ТФ':<5}{'Итог %':>9}{'Сделок':>8}"
          f"{'Винрейт':>9}{'Просадка':>10}{'1-я пол.':>10}{'2-я пол.':>10}")
    for name, ticker, tf, r, h1, h2 in rows:
        print(f"{name:<15}{ticker:<7}{tf:<5}{r['return_pct']:>+9.1f}{r['trades']:>8}"
              f"{r['winrate_pct']:>8.1f}%{r['max_drawdown_pct']:>9.1f}%{h1:>+10.1f}{h2:>+10.1f}")
    print("\nРиск на сделку {}%, комиссия учтена. Смотрите на комбинации, где обе"
          " половины в плюсе и сделок не меньше ~30.".format(base.risk_per_trade_pct))


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
