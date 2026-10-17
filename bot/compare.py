"""Сравнение результатов стратегии на разных монетах, таймфреймах и периодах канала.

Пример: python -m bot.compare --days 1460 --periods 15,20,30

Каждая комбинация прогоняется без остановок по лимитам (как --no-guard).
Отдельно показан результат на первой и второй половине периода: если
стратегия заработала только на одной половине, скорее всего ей просто повезло.
"""
import argparse
import copy

from .backtest import load_data, run_backtest
from .broker import make_exchange
from .config import load_config

DEFAULT_SYMBOLS = "BTC,ETH"
DEFAULT_TIMEFRAMES = "4h"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS,
                    help=f"тикеры через запятую (по умолчанию {DEFAULT_SYMBOLS})")
    ap.add_argument("--timeframes", default=DEFAULT_TIMEFRAMES,
                    help=f"таймфреймы через запятую (по умолчанию {DEFAULT_TIMEFRAMES})")
    ap.add_argument("--periods", default="",
                    help="периоды канала через запятую (по умолчанию DONCHIAN_PERIOD из .env)")
    args = ap.parse_args()

    base = load_config()
    base.mode = "paper"
    ex = make_exchange(base)
    periods = [int(x) for x in args.periods.split(",") if x.strip()] or [base.strategy.donchian_period]
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
            for period in periods:
                cfg = copy.deepcopy(base)
                cfg.symbol = symbol
                cfg.strategy.donchian_period = period
                if len(candles) <= cfg.strategy.warmup + 50:
                    print("  мало истории, пропуск")
                    continue
                r = run_backtest(candles, cfg, stop_on_guard=False, funding=funding)
                rows.append((period, ticker, tf, r))

    rows.sort(key=lambda row: row[3]["return_pct"], reverse=True)
    print()
    print(f"{'Канал':<7}{'Монета':<7}{'ТФ':<5}{'Итог %':>9}{'Сделок':>8}"
          f"{'Винрейт':>9}{'Просадка':>10}{'1-я пол.':>10}{'2-я пол.':>10}")
    for period, ticker, tf, r in rows:
        print(f"{period:<7}{ticker:<7}{tf:<5}{r['return_pct']:>+9.1f}{r['trades']:>8}"
              f"{r['winrate_pct']:>8.1f}%{r['max_drawdown_pct']:>9.1f}%"
              f"{r['first_half_pct']:>+10.1f}{r['second_half_pct']:>+10.1f}")
    print(f"\nРиск на сделку {base.risk_per_trade_pct}%, плечо до x{base.leverage}, "
          "комиссия учтена. Смотрите на комбинации, где обе половины в плюсе и "
          "сделок не меньше ~30.")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
