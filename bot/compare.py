"""Сравнение стратегий на нескольких монетах и таймфреймах.

Пример: python -m bot.compare --days 1460

Каждая комбинация прогоняется без остановок по лимитам (как --no-guard).
Отдельно показан результат на первой и второй половине периода: если
стратегия заработала только на одной половине, скорее всего ей просто повезло.
"""
import argparse
import copy

import ccxt

from .backtest import load_data, run_backtest
from .broker import make_exchange
from .config import load_config
from .strategy import STRATEGIES

DEFAULT_SYMBOLS = "BTC,ETH"
DEFAULT_TIMEFRAMES = "4h,1d"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1460)
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
        ticker = ticker.strip().upper()
        symbol = f"{ticker}/USDT:USDT"
        for tf in args.timeframes.split(","):
            tf = tf.strip()
            print(f"Загрузка {symbol} {tf}...")
            try:
                candles, funding = load_data(ex, symbol, tf, args.days,
                                             base.strategy.max_funding_pct > 0)
            except Exception as e:
                print(f"  пропуск: {e}")
                continue
            for name in args.strategies.split(","):
                cfg = copy.deepcopy(base)
                cfg.symbol = symbol
                cfg.strategy.name = name.strip()
                cfg.strategy.candles_per_day = 86400 / ccxt.Exchange.parse_timeframe(tf)
                if len(candles) <= cfg.strategy.warmup + 50:
                    print(f"  {cfg.strategy.name}: мало истории, пропуск")
                    continue
                r = run_backtest(candles, cfg, stop_on_guard=False, funding=funding)
                rows.append((cfg.strategy.name, ticker, tf, r))

    rows.sort(key=lambda row: row[3]["return_pct"], reverse=True)
    print()
    print(f"{'Стратегия':<15}{'Монета':<7}{'ТФ':<5}{'Итог %':>9}{'Сделок':>8}"
          f"{'Винрейт':>9}{'Просадка':>10}{'1-я пол.':>10}{'2-я пол.':>10}")
    for name, ticker, tf, r in rows:
        print(f"{name:<15}{ticker:<7}{tf:<5}{r['return_pct']:>+9.1f}{r['trades']:>8}"
              f"{r['winrate_pct']:>8.1f}%{r['max_drawdown_pct']:>9.1f}%"
              f"{r['first_half_pct']:>+10.1f}{r['second_half_pct']:>+10.1f}")
    print(f"\nРиск на сделку {base.risk_per_trade_pct}%, плечо до x{base.leverage}, "
          "комиссия учтена. Смотрите на комбинации, где обе половины в плюсе и "
          "сделок не меньше ~30.")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
