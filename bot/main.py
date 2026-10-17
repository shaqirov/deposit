"""Основной цикл бота.  Запуск: python -m bot.main"""
import logging
import time

from .broker import BinanceBroker, PaperBroker, make_exchange
from .config import load_config
from .risk import RiskGuard, position_size
from .strategy import generate_signal

log = logging.getLogger("bot")
POLL_SECONDS = 10


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
    )
    cfg = load_config()
    ex = make_exchange(cfg)
    if cfg.mode == "paper":
        broker = PaperBroker(cfg.paper_balance)
    else:
        if not cfg.api_key or not cfg.api_secret:
            raise SystemExit("Для режимов testnet/demo/live нужны BINANCE_API_KEY и BINANCE_API_SECRET")
        if cfg.mode == "live":
            log.warning("РЕЖИМ LIVE: торговля реальными деньгами, плечо x%d", cfg.leverage)
        broker = BinanceBroker(ex, cfg.symbol, cfg.leverage)

    start = broker.equity()
    guard = RiskGuard(start, cfg.target_profit_pct, cfg.max_drawdown_pct, cfg.max_consecutive_losses)
    log.info("Старт: режим=%s, %s %s, депозит %.2f USDT, цель +%.0f%%",
             cfg.mode, cfg.symbol, cfg.timeframe, start, cfg.target_profit_pct)

    last_candle_ts = None
    while True:
        try:
            candles = ex.fetch_ohlcv(cfg.symbol, cfg.timeframe, limit=max(500, cfg.strategy.warmup + 100))
            closed, current = candles[:-1], candles[-1]

            pnl = broker.check_exit(current[2], current[3])
            if pnl is not None:
                guard.record_trade(pnl)
                equity = broker.equity()
                log.info("Сделка %+.4f USDT, депозит %.2f (%+.1f%% от старта)",
                         pnl, equity, (equity / start - 1) * 100)
                reason = guard.stop_reason(equity)
                if reason:
                    log.warning("Бот остановлен: %s", reason)
                    return

            if not broker.in_position and closed[-1][0] != last_candle_ts:
                last_candle_ts = closed[-1][0]
                sig = generate_signal(closed, cfg.strategy)
                if sig:
                    amount = position_size(broker.equity(), sig.entry, sig.stop,
                                           cfg.risk_per_trade_pct, cfg.leverage)
                    broker.open(sig, amount)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("Ошибка в цикле, повтор через %d с", POLL_SECONDS)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Остановлено пользователем. Открытые позиции на бирже НЕ закрываются автоматически.")
