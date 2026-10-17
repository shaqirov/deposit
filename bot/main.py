"""Основной цикл бота с управлением через Telegram.  Запуск: python -m bot.main"""
import csv
import logging
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from .broker import BinanceBroker, PaperBroker, make_exchange
from .config import load_config
from .journal import append_trade, fmt_ts, trade_row
from .news import add_event, blocking_event, load_events, remove_event
from .risk import RiskGuard, position_size
from .state import ENV_FILE, ROOT, STATE_FILE, load_state, save_state, set_env_value
from .strategy import Signal, generate_signal
from .telegram import Notifier

log = logging.getLogger("bot")
POLL_SECONDS = 10
MAX_CANDLES = 1500  # больше Binance не отдаёт за один запрос
JOURNAL_FILE = os.path.join(ROOT, "journal.csv")
LOG_FILE = os.path.join(ROOT, "bot.log")
RESTART_EXIT_CODE = 3  # systemd перезапускает бота при ненулевом коде выхода

TIMEFRAMES = ("1h", "2h", "4h", "6h", "8h", "12h", "1d")
# Настройки, которые можно менять из Telegram: ключ -> (тип, минимум, максимум, описание).
# MODE и API-ключи — только на сервере.
SETTINGS = {
    "RISK_PER_TRADE_PCT": (float, 0.1, 5, "риск на сделку, %"),
    "LEVERAGE": (int, 1, 10, "максимальное плечо"),
    "MAX_DRAWDOWN_PCT": (float, 5, 90, "остановка при просадке, %"),
    "TARGET_PROFIT_PCT": (float, 0, 10000, "остановка при прибыли, % (0 — без цели)"),
    "MAX_CONSECUTIVE_LOSSES": (int, 1, 20, "убытков подряд до перерыва"),
    "PAUSE_HOURS": (float, 0, 720, "длина перерыва, ч (0 — полная остановка)"),
    "MAX_FUNDING_PCT": (float, 0, 1, "фильтр funding, % за 8ч (0 — выкл.)"),
    "DONCHIAN_PERIOD": (int, 5, 200, "период канала, свечей"),
    "TAKE_PROFIT_RR": (float, 1, 10, "тейк в стопах"),
    "STOP_ATR_MULT": (float, 0.5, 10, "стоп в ATR"),
    "SYMBOL": (str, None, None, "монета, например BTC или SOL"),
    "TIMEFRAME": (str, None, None, "таймфрейм: " + ", ".join(TIMEFRAMES)),
}
SETTINGS_ATTR = {
    "RISK_PER_TRADE_PCT": "risk_per_trade_pct", "LEVERAGE": "leverage",
    "MAX_DRAWDOWN_PCT": "max_drawdown_pct", "TARGET_PROFIT_PCT": "target_profit_pct",
    "MAX_CONSECUTIVE_LOSSES": "max_consecutive_losses", "PAUSE_HOURS": "pause_hours",
    "SYMBOL": "symbol", "TIMEFRAME": "timeframe",
    "MAX_FUNDING_PCT": "strategy.max_funding_pct", "DONCHIAN_PERIOD": "strategy.donchian_period",
    "TAKE_PROFIT_RR": "strategy.take_profit_rr", "STOP_ATR_MULT": "strategy.stop_atr_mult",
}

COMMANDS = [
    ("status", "депозит, позиция, состояние"),
    ("trades", "последние сделки и статистика"),
    ("journal", "журнал сделок файлом (Excel)"),
    ("pause", "не открывать новые сделки"),
    ("resume", "возобновить торговлю"),
    ("close", "закрыть позицию по рынку"),
    ("settings", "текущие настройки"),
    ("set", "изменить настройку: /set КЛЮЧ ЗНАЧЕНИЕ"),
    ("news", "события, вокруг которых бот не торгует"),
    ("news_add", "добавить событие: /news_add 2026-11-12 13:30 CPI"),
    ("news_del", "удалить событие: /news_del НОМЕР"),
    ("log", "последние строки лога"),
    ("update", "обновить код с GitHub и перезапустить"),
    ("restart", "перезапустить бота"),
    ("help", "список команд"),
]


def current_funding(ex, symbol):
    try:
        return ex.fetch_funding_rate(symbol).get("fundingRate")
    except Exception as e:
        log.warning("Не удалось получить funding: %s", e)
        return None


def get_attr(cfg, path):
    obj = cfg
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


class Restart(Exception):
    """Перезапуск процесса (после /restart, /set, /update)."""


class TradingBot:
    def __init__(self, cfg, ex, broker, tg):
        self.cfg, self.ex, self.broker, self.tg = cfg, ex, broker, tg
        self.limit = min(max(500, cfg.strategy.warmup + 100), MAX_CANDLES)
        self.news_path = cfg.news_file if os.path.isabs(cfg.news_file) else os.path.join(ROOT, cfg.news_file)
        self.events = load_events(self.news_path)
        self.guard = RiskGuard(broker.equity(), cfg.target_profit_pct, cfg.max_drawdown_pct,
                               cfg.max_consecutive_losses, cfg.pause_hours)
        self.trading_enabled = True
        self.halted_reason = None  # причина остановки по лимитам риска
        self.last_candle_ts = None
        self.last_check_ms = None
        self.last_price = None
        self.last_report_day = datetime.now(timezone.utc).date().isoformat()
        self.day_trades = []
        self.open_trade = None  # (signal, amount, open_ms) для журнала
        self.close_requested = False
        self.manual_close = False  # на бирже ручное закрытие увидит следующий check_exit
        self.restored = False

    # --- Состояние -------------------------------------------------------------

    def to_state(self):
        g = self.guard
        return {
            "mode": self.cfg.mode, "symbol": self.cfg.symbol, "timeframe": self.cfg.timeframe,
            "guard": {"start_equity": g.start_equity, "consecutive_losses": g.consecutive_losses,
                      "paused_until_ms": g.paused_until_ms, "trades": g.trades},
            "trading_enabled": self.trading_enabled, "halted_reason": self.halted_reason,
            "last_candle_ts": self.last_candle_ts, "last_report_day": self.last_report_day,
            "day_trades": self.day_trades,
            "open_trade": None if not self.open_trade else {
                "signal": asdict(self.open_trade[0]), "amount": self.open_trade[1],
                "open_ms": self.open_trade[2]},
            "broker": self.broker.to_state(),
        }

    def load(self, st):
        """Восстановить состояние после перезапуска (только для того же режима)."""
        if not st or st.get("mode") != self.cfg.mode:
            return
        same_symbol = st.get("symbol") == self.cfg.symbol
        same_market = same_symbol and st.get("timeframe") == self.cfg.timeframe
        g = st["guard"]
        self.guard.start_equity = g["start_equity"]
        self.guard.consecutive_losses = g["consecutive_losses"]
        self.guard.paused_until_ms = g["paused_until_ms"]
        self.guard.trades = g["trades"]
        self.trading_enabled = st["trading_enabled"]
        self.halted_reason = st["halted_reason"]
        self.last_report_day = st.get("last_report_day", self.last_report_day)
        self.day_trades = st.get("day_trades", [])
        if same_market:
            self.last_candle_ts = st.get("last_candle_ts")
        if same_symbol and st.get("open_trade"):
            t = st["open_trade"]
            self.open_trade = (Signal(**t["signal"]), t["amount"], t["open_ms"])
        self.broker.load_state(st["broker"], with_position=same_symbol)
        self.restored = True

    def save(self):
        try:
            save_state(self.to_state(), STATE_FILE)
        except OSError as e:
            log.warning("Не удалось сохранить состояние: %s", e)

    # --- Торговля --------------------------------------------------------------

    def tick(self):
        cfg = self.cfg
        candles = self.ex.fetch_ohlcv(cfg.symbol, cfg.timeframe, limit=self.limit)
        closed, current = candles[:-1], candles[-1]
        now_ms = self.ex.milliseconds()
        self.last_price = current[4]

        if self.close_requested:
            self.close_requested = False
            pnl = self.broker.close_now(self.last_price)
            if pnl is not None:
                self.on_closed(pnl, now_ms, manual=True)
            else:
                self.manual_close = True
        pnl = self.broker.check_exit(current[2], current[3])
        if pnl is not None:
            self.on_closed(pnl, now_ms, manual=self.manual_close)
            self.manual_close = False

        self.daily_report()

        if self.broker.in_position or closed[-1][0] == self.last_candle_ts:
            return
        self.last_candle_ts = closed[-1][0]
        self.last_check_ms = now_ms
        if not self.trading_enabled:
            log.info("Торговля выключена, свеча %s пропущена", fmt_ts(closed[-1][0]))
            return
        if self.guard.is_paused(now_ms):
            log.info("Перерыв после серии убытков, новые сделки не открываются")
            return
        event = blocking_event(self.events, datetime.now(timezone.utc),
                               cfg.news_hours_before, cfg.news_hours_after)
        if event:
            log.info("Рядом важное событие (%s), новые сделки не открываются", event)
            return
        funding = current_funding(self.ex, cfg.symbol) if cfg.strategy.max_funding_pct > 0 else None
        sig = generate_signal(closed, cfg.strategy, funding)
        if not sig:
            log.info("Свеча %s UTC закрыта по %.2f, funding %s: сигнала нет",
                     fmt_ts(closed[-1][0]), closed[-1][4],
                     "—" if funding is None else f"{funding * 100:.4f}%")
            return
        amount = position_size(self.broker.equity(), sig.entry, sig.stop,
                               cfg.risk_per_trade_pct, cfg.leverage)
        if self.broker.open(sig, amount) is not False:
            self.open_trade = (sig, amount, now_ms)
            self.tg.send(f"{'🟢 Лонг' if sig.side == 'long' else '🔴 Шорт'} {cfg.symbol}\n"
                         f"Вход: {sig.entry:.6g}\nСтоп: {sig.stop:.6g}\n"
                         f"Тейк: {sig.take:.6g}\nОбъём: {amount:.6g}")

    def on_closed(self, pnl, now_ms, manual=False):
        self.guard.record_trade(pnl, now_ms)
        self.day_trades.append(pnl)
        equity = self.broker.equity()
        log.info("Сделка %+.4f USDT, депозит %.2f (%+.1f%% от старта)",
                 pnl, equity, self.change_pct(equity))
        if self.open_trade:
            append_trade(JOURNAL_FILE, trade_row(self.cfg.symbol, *self.open_trade,
                                                 now_ms, pnl, equity))
            self.open_trade = None
        how = "вручную" if manual else ("по тейку" if pnl >= 0 else "по стопу")
        self.tg.send(f"{'✅' if pnl >= 0 else '❌'} Сделка закрыта {how}: {pnl:+.2f} USDT\n"
                     f"Депозит: {equity:.2f} USDT ({self.change_pct(equity):+.1f}% от старта)")
        if self.guard.is_paused(now_ms):
            log.warning("Серия убытков — перерыв %.0f ч", self.cfg.pause_hours)
            self.tg.send(f"⏸ {self.cfg.max_consecutive_losses} убытка подряд — "
                         f"перерыв {self.cfg.pause_hours:.0f} ч до "
                         f"{fmt_ts(self.guard.paused_until_ms)} UTC")
        reason = self.guard.stop_reason(equity)
        if reason and self.trading_enabled:
            self.trading_enabled = False
            self.halted_reason = reason
            log.warning("Торговля остановлена: %s", reason)
            self.tg.send(f"🛑 Торговля остановлена: {reason}\nДепозит: {equity:.2f} USDT\n"
                         "Бот продолжает отвечать на команды. /resume — возобновить.")

    def change_pct(self, equity):
        return (equity / self.guard.start_equity - 1) * 100 if self.guard.start_equity else 0.0

    def daily_report(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if today == self.last_report_day:
            return
        self.last_report_day = today
        equity = self.broker.equity()
        self.tg.send(f"📊 Сводка за сутки\nСделок: {len(self.day_trades)}, "
                     f"результат {sum(self.day_trades):+.2f} USDT\n"
                     f"Депозит: {equity:.2f} USDT ({self.change_pct(equity):+.1f}% от старта)\n"
                     f"Позиция: {'открыта' if self.broker.in_position else 'нет'}\n"
                     f"Торговля: {self.trading_state()}")
        self.day_trades = []

    def trading_state(self):
        if self.halted_reason:
            return f"остановлена ({self.halted_reason})"
        if not self.trading_enabled:
            return "на паузе (/resume — возобновить)"
        if self.guard.is_paused(self.ex.milliseconds()):
            return f"перерыв после убытков до {fmt_ts(self.guard.paused_until_ms)} UTC"
        return "включена"

    # --- Команды ---------------------------------------------------------------

    def handle(self, text):
        parts = text.split()
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        args = parts[1:]
        handler = getattr(self, f"cmd_{cmd}", None)
        if not handler:
            self.tg.send("Неизвестная команда. /help — список команд.")
            return
        log.info("Команда из Telegram: %s", text)
        try:
            handler(args)
        except Restart:
            raise
        except Exception as e:
            log.exception("Ошибка команды %s", text)
            self.tg.send(f"⚠️ Не удалось выполнить {text}: {e}")

    def cmd_start(self, args):
        self.cmd_help(args)

    def cmd_help(self, args):
        self.tg.send("Команды:\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS))

    def cmd_status(self, args):
        cfg, equity = self.cfg, self.broker.equity()
        lines = [f"🤖 {cfg.mode} | {cfg.symbol} {cfg.timeframe}",
                 f"Торговля: {self.trading_state()}",
                 f"Депозит: {equity:.2f} USDT ({self.change_pct(equity):+.1f}% "
                 f"от старта {self.guard.start_equity:.2f})"]
        if self.broker.in_position and self.open_trade:
            s, amount, opened = self.open_trade
            upnl = self.broker.unrealized_pnl(self.last_price)
            lines += [f"Позиция: {'🟢 лонг' if s.side == 'long' else '🔴 шорт'} {amount:.6g} "
                      f"с {fmt_ts(opened)} UTC",
                      f"Вход {s.entry:.6g}, стоп {s.stop:.6g}, тейк {s.take:.6g}",
                      f"Цена {self.last_price:.6g}, PnL {upnl:+.2f} USDT"]
        elif self.broker.in_position:
            lines.append(f"Позиция открыта, PnL {self.broker.unrealized_pnl(self.last_price):+.2f} USDT")
        else:
            lines.append("Позиция: нет")
        trades = self.guard.trades
        if trades:
            wins = sum(1 for t in trades if t > 0)
            lines.append(f"Сделок: {len(trades)}, прибыльных {wins} "
                         f"({wins / len(trades) * 100:.0f}%), итог {sum(trades):+.2f} USDT")
        if self.last_check_ms:
            lines.append(f"Последняя проверка сигнала: {fmt_ts(self.last_check_ms)} UTC")
        self.tg.send("\n".join(lines))

    def cmd_trades(self, args):
        rows = []
        if os.path.exists(JOURNAL_FILE):
            with open(JOURNAL_FILE, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        if not rows:
            self.tg.send("Сделок пока не было.")
            return
        pnls = [float(r["pnl"]) for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        lines = [f"Сделок: {len(rows)}, прибыльных {wins} ({wins / len(rows) * 100:.0f}%), "
                 f"итог {sum(pnls):+.2f} USDT", ""]
        for r in rows[-10:]:
            lines.append(f"{'✅' if float(r['pnl']) > 0 else '❌'} {r['close_time']} "
                         f"{r['side']} {r['entry']} → {float(r['pnl']):+.2f}")
        self.tg.send("\n".join(lines))

    def cmd_journal(self, args):
        if os.path.exists(JOURNAL_FILE):
            self.tg.send_document(JOURNAL_FILE, "Журнал сделок")
        else:
            self.tg.send("Сделок пока не было.")

    def cmd_pause(self, args):
        self.trading_enabled = False
        self.tg.send("⏸ Новые сделки не открываются. Открытая позиция сохраняет стоп и тейк.\n"
                     "/resume — возобновить, /close — закрыть позицию.")

    def cmd_resume(self, args):
        if self.halted_reason:
            equity = self.broker.equity()
            self.guard.start_equity = equity
            self.guard.consecutive_losses = 0
            self.tg.send(f"Лимиты риска сброшены: новая точка отсчёта — {equity:.2f} USDT.")
            self.halted_reason = None
        self.guard.paused_until_ms = 0
        self.trading_enabled = True
        self.tg.send("▶️ Торговля возобновлена.")

    def cmd_close(self, args):
        if not self.broker.in_position:
            self.tg.send("Открытой позиции нет.")
        elif args[:1] in (["да"], ["yes"]):
            self.close_requested = True
            self.tg.send("Закрываю позицию по рынку…")
        else:
            self.tg.send("Закрыть позицию по рынку? Отправьте: /close да")

    def cmd_settings(self, args):
        lines = ["Настройки (менять: /set КЛЮЧ ЗНАЧЕНИЕ):"]
        for key, (typ, lo, hi, desc) in SETTINGS.items():
            value = get_attr(self.cfg, SETTINGS_ATTR[key])
            rng = f" [{lo}…{hi}]" if lo is not None else ""
            lines.append(f"{key} = {value}  — {desc}{rng}")
        lines.append(f"\nMODE = {self.cfg.mode} (меняется только на сервере)")
        self.tg.send("\n".join(lines))

    def cmd_set(self, args):
        if len(args) != 2:
            self.tg.send("Формат: /set КЛЮЧ ЗНАЧЕНИЕ, например /set RISK_PER_TRADE_PCT 1.5\n"
                         "Список ключей: /settings")
            return
        key, raw = args[0].upper(), args[1]
        if key not in SETTINGS:
            if key == "MODE" or key.startswith(("BINANCE", "TELEGRAM")):
                self.tg.send("Эту настройку можно менять только на сервере.")
            else:
                self.tg.send(f"Неизвестный ключ {key}. Список: /settings")
            return
        typ, lo, hi, _ = SETTINGS[key]
        if key == "SYMBOL":
            value = raw.upper() if "/" in raw else f"{raw.upper()}/USDT:USDT"
            self.ex.load_markets()
            if value not in self.ex.markets:
                self.tg.send(f"Монеты {value} нет на Binance Futures.")
                return
            if self.broker.in_position:
                self.tg.send("Сначала дождитесь закрытия позиции или закройте её: /close")
                return
        elif key == "TIMEFRAME":
            value = raw.lower()
            if value not in TIMEFRAMES:
                self.tg.send(f"Допустимые таймфреймы: {', '.join(TIMEFRAMES)}")
                return
        else:
            try:
                value = typ(raw.replace(",", "."))
            except ValueError:
                self.tg.send(f"{raw} — не подходящее число для {key}.")
                return
            if not lo <= value <= hi:
                self.tg.send(f"{key} должен быть от {lo} до {hi}.")
                return
        set_env_value(key, value, ENV_FILE)
        self.tg.send(f"✅ {key} = {value}. Перезапускаю бота…")
        raise Restart

    def cmd_news(self, args):
        self.events = load_events(self.news_path)
        if not self.events:
            self.tg.send("Событий нет. Добавить: /news_add 2026-11-12 13:30 CPI (время UTC)")
            return
        lines = [f"Не торгую за {self.cfg.news_hours_before:.0f} ч до и "
                 f"{self.cfg.news_hours_after:.0f} ч после события (время UTC):"]
        for i, (when, title) in enumerate(self.events, start=1):
            lines.append(f"{i}. {when:%Y-%m-%d %H:%M} {title}")
        lines.append("\nУдалить: /news_del НОМЕР")
        self.tg.send("\n".join(lines))

    def cmd_news_add(self, args):
        if len(args) < 2:
            self.tg.send("Формат: /news_add ГГГГ-ММ-ДД ЧЧ:ММ описание (время UTC)")
            return
        try:
            add_event(self.news_path, f"{args[0]} {args[1]}", " ".join(args[2:]))
        except ValueError:
            self.tg.send("Неверная дата. Пример: /news_add 2026-11-12 13:30 CPI")
            return
        self.tg.send("✅ Событие добавлено.")
        self.cmd_news([])

    def cmd_news_del(self, args):
        if len(args) != 1 or not args[0].isdigit():
            self.tg.send("Формат: /news_del НОМЕР (номер из /news)")
            return
        removed = remove_event(self.news_path, int(args[0])) if os.path.exists(self.news_path) else None
        self.events = load_events(self.news_path)
        self.tg.send(f"Удалено: {removed}" if removed else "Нет события с таким номером.")

    def cmd_log(self, args):
        if not os.path.exists(LOG_FILE):
            self.tg.send("Лог пуст.")
            return
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-25:]
        self.tg.send("".join(lines)[-3900:])

    def cmd_restart(self, args):
        self.tg.send("🔄 Перезапускаю бота…")
        raise Restart

    def cmd_update(self, args):
        def git(*a):
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=120)

        old = git("rev-parse", "HEAD").stdout.strip()
        pull = git("pull", "--ff-only")
        if pull.returncode != 0:
            self.tg.send(f"⚠️ git pull не удался:\n{(pull.stderr or pull.stdout)[-1500:]}")
            return
        new = git("rev-parse", "HEAD").stdout.strip()
        if new == old:
            self.tg.send("Обновлений нет, код уже последней версии.")
            return
        changes = git("log", "--format=• %s", f"{old}..{new}").stdout.strip()
        self.tg.send(f"Скачаны изменения:\n{changes[:2000]}\nУстанавливаю и проверяю тестами…")
        pip = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                             cwd=ROOT, capture_output=True, text=True, timeout=600)
        tests = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                               cwd=ROOT, capture_output=True, text=True, timeout=600)
        if pip.returncode != 0 or tests.returncode != 0:
            git("reset", "--hard", old)
            out = (pip.stderr if pip.returncode != 0 else tests.stdout)[-1500:]
            self.tg.send(f"❌ Обновление отменено, бот работает на прежней версии.\n{out}")
            return
        summary = tests.stdout.strip().splitlines()[-1] if tests.stdout.strip() else ""
        self.tg.send(f"✅ Тесты: {summary}. Перезапускаю бота…")
        raise Restart

    # --- Цикл ------------------------------------------------------------------

    def run(self):
        cfg = self.cfg
        self.tg.set_commands(COMMANDS)
        self.tg.skip_pending()
        equity = self.broker.equity()
        log.info("Старт: режим=%s, %s %s, депозит %.2f USDT, риск %.1f%%, событий: %d%s",
                 cfg.mode, cfg.symbol, cfg.timeframe, equity, cfg.risk_per_trade_pct,
                 len(self.events), ", состояние восстановлено" if self.restored else "")
        self.tg.send(f"🚀 Бот запущен\nРежим: {cfg.mode}\n{cfg.symbol} {cfg.timeframe}\n"
                     f"Депозит: {equity:.2f} USDT, риск {cfg.risk_per_trade_pct}% на сделку\n"
                     f"Торговля: {self.trading_state()}"
                     + ("\nСостояние восстановлено после перезапуска" if self.restored else "")
                     + "\n/help — команды")
        while True:
            try:
                self.tick()
            except Exception as e:
                log.exception("Ошибка в цикле, повтор через %d с", POLL_SECONDS)
                self.tg.error(f"{type(e).__name__}: {e}"[:500])
            self.save()
            try:
                for text in self.tg.poll(POLL_SECONDS):
                    self.handle(text)
            except Restart:
                self.save()
                self.tg.ack()
                raise SystemExit(RESTART_EXIT_CODE)
            self.save()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")],
    )
    cfg = load_config()
    if cfg.strategy.warmup + 1 > MAX_CANDLES:
        raise SystemExit(f"Стратегии нужно {cfg.strategy.warmup} свечей истории, "
                         f"а Binance отдаёт {MAX_CANDLES}. Уменьшите TREND_EMA.")
    ex = make_exchange(cfg)
    if cfg.mode == "paper":
        broker = PaperBroker(cfg.paper_balance)
    else:
        if not cfg.api_key or not cfg.api_secret:
            raise SystemExit("Для режимов testnet/demo/live нужны BINANCE_API_KEY и BINANCE_API_SECRET")
        if cfg.mode == "live":
            log.warning("РЕЖИМ LIVE: торговля реальными деньгами, плечо x%d", cfg.leverage)
        broker = BinanceBroker(ex, cfg.symbol, cfg.leverage)
    bot = TradingBot(cfg, ex, broker, Notifier(cfg.telegram_token, cfg.telegram_chat_id))
    bot.load(load_state(STATE_FILE))
    bot.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Остановлено пользователем. Открытые позиции на бирже НЕ закрываются автоматически.")
