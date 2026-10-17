"""Основной цикл бота.  Запуск: python -m bot.main"""
import logging
import time
from datetime import datetime, timezone

from .broker import BinanceBroker, PaperBroker, make_exchange
from .config import load_config
from .journal import append_trade, fmt_ts, trade_row
from .news import blocking_event, load_events
from .risk import RiskGuard, position_size
from .strategy import generate_signal
from .telegram import Notifier

log = logging.getLogger("bot")
POLL_SECONDS = 10
MAX_CANDLES = 1500  # больше Binance не отдаёт за один запрос
JOURNAL_FILE = "journal.csv"


def current_funding(ex, symbol):
    try:
        return ex.fetch_funding_rate(symbol).get("fundingRate")
    except Exception as e:
        log.warning("Не удалось получить funding: %s", e)
        return None


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
    )
    cfg = load_config()
    if cfg.strategy.warmup + 1 > MAX_CANDLES:
        raise SystemExit(f"Стратегии нужно {cfg.strategy.warmup} свечей истории, "
                         f"а Binance отдаёт {MAX_CANDLES}. Уменьшите TREND_EMA.")
    limit = min(max(500, cfg.strategy.warmup + 100), MAX_CANDLES)
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
    guard = RiskGuard(start, cfg.target_profit_pct, cfg.max_drawdown_pct,
                      cfg.max_consecutive_losses, cfg.pause_hours)
    events = load_events(cfg.news_file)
    log.info("Старт: режим=%s, %s %s, депозит %.2f USDT, риск %.1f%%, "
             "событий в %s: %d", cfg.mode, cfg.symbol, cfg.timeframe, start, cfg.risk_per_trade_pct, cfg.news_file, len(events))
    tg = Notifier(cfg.telegram_token, cfg.telegram_chat_id)
    tg.send(f"🚀 Бот запущен\nРежим: {cfg.mode}\n{cfg.symbol} {cfg.timeframe}\n"
            f"Депозит: {start:.2f} USDT, риск {cfg.risk_per_trade_pct}% на сделку")

    last_candle_ts = None
    last_report_day = datetime.now(timezone.utc).date()
    day_trades = []
    open_trade = None  # (signal, amount, open_ms) для журнала
    while True:
        try:
            candles = ex.fetch_ohlcv(cfg.symbol, cfg.timeframe, limit=limit)
            closed, current = candles[:-1], candles[-1]
            now_ms = ex.milliseconds()

            pnl = broker.check_exit(current[2], current[3])
            if pnl is not None:
                guard.record_trade(pnl, now_ms)
                equity = broker.equity()
                log.info("Сделка %+.4f USDT, депозит %.2f (%+.1f%% от старта)",
                         pnl, equity, (equity / start - 1) * 100)
                day_trades.append(pnl)
                if open_trade:
                    append_trade(JOURNAL_FILE, trade_row(cfg.symbol, *open_trade,
                                                         now_ms, pnl, equity))
                    open_trade = None
                tg.send(f"{'✅' if pnl >= 0 else '❌'} Сделка закрыта: {pnl:+.2f} USDT\n"
                        f"Депозит: {equity:.2f} USDT ({(equity / start - 1) * 100:+.1f}% от старта)")
                if guard.is_paused(now_ms):
                    log.warning("Серия убытков — перерыв %.0f ч", cfg.pause_hours)
                    tg.send(f"⏸ {cfg.max_consecutive_losses} убытка подряд — "
                            f"перерыв {cfg.pause_hours:.0f} ч")
                reason = guard.stop_reason(equity)
                if reason:
                    log.warning("Бот остановлен: %s", reason)
                    tg.send(f"🛑 Бот остановлен: {reason}\nДепозит: {equity:.2f} USDT")
                    return

            today = datetime.now(timezone.utc).date()
            if today != last_report_day:
                last_report_day = today
                equity = broker.equity()
                tg.send(f"📊 Сводка за сутки\nСделок: {len(day_trades)}, "
                        f"результат {sum(day_trades):+.2f} USDT\n"
                        f"Депозит: {equity:.2f} USDT ({(equity / start - 1) * 100:+.1f}% от старта)\n"
                        f"Позиция: {'открыта' if broker.in_position else 'нет'}")
                day_trades = []

            if not broker.in_position and closed[-1][0] != last_candle_ts:
                last_candle_ts = closed[-1][0]
                now = datetime.now(timezone.utc)
                event = blocking_event(events, now, cfg.news_hours_before, cfg.news_hours_after)
                if guard.is_paused(now_ms):
                    log.info("Перерыв после серии убытков, новые сделки не открываются")
                elif event:
                    log.info("Рядом важное событие (%s), новые сделки не открываются", event)
                else:
                    funding = current_funding(ex, cfg.symbol) if cfg.strategy.max_funding_pct > 0 else None
                    sig = generate_signal(closed, cfg.strategy, funding)
                    if sig:
                        amount = position_size(broker.equity(), sig.entry, sig.stop,
                                               cfg.risk_per_trade_pct, cfg.leverage)
                        if broker.open(sig, amount) is not False:
                            open_trade = (sig, amount, now_ms)
                            tg.send(f"{'🟢 Лонг' if sig.side == 'long' else '🔴 Шорт'} {cfg.symbol}\n"
                                    f"Вход: {sig.entry:.6g}\nСтоп: {sig.stop:.6g}\n"
                                    f"Тейк: {sig.take:.6g}\nОбъём: {amount:.6g}")
                    else:
                        log.info("Свеча %s UTC закрыта по %.2f, funding %s: сигнала нет",
                                 fmt_ts(closed[-1][0]), closed[-1][4],
                                 "—" if funding is None else f"{funding * 100:.4f}%")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.exception("Ошибка в цикле, повтор через %d с", POLL_SECONDS)
            tg.error(f"{type(e).__name__}: {e}"[:500])
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Остановлено пользователем. Открытые позиции на бирже НЕ закрываются автоматически.")
