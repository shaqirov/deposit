"""Бот: стратегия на многих монетах с общим депозитом и управлением через Telegram.

Запуск: python -m bot.main
"""
import csv
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from .broker import BinancePortfolio, PaperPortfolio, make_exchange
from .config import load_config
from .journal import append_trade, fmt_ts, trade_row
from .news import add_event, blocking_event, load_events, remove_event
from .portfolio import min_notional, top_symbols
from .risk import RiskGuard, portfolio_size
from .state import ENV_FILE, ROOT, STATE_FILE, load_state, save_state, set_env_value
from .strategy import generate_signal
from .telegram import Notifier

log = logging.getLogger("bot")
POLL_SECONDS = 10
MAX_CANDLES = 1500   # больше Binance не отдаёт за один запрос
SCAN_DELAY_MS = 15_000  # ждём после закрытия свечи, пока биржа её сформирует
JOURNAL_FILE = os.path.join(ROOT, "journal.csv")
LOG_FILE = os.path.join(ROOT, "bot.log")
RESTART_EXIT_CODE = 3  # systemd перезапускает бота при ненулевом коде выхода
STATE_VERSION = 2

TIMEFRAMES = ("1h", "2h", "4h", "6h", "8h", "12h", "1d")
# Настройки, которые можно менять из Telegram: ключ -> (тип, минимум, максимум, описание).
# MODE и API-ключи — только на сервере.
SETTINGS = {
    "RISK_PER_TRADE_PCT": (float, 0.1, 10, "риск на сделку, %"),
    "MAX_POSITIONS": (int, 1, 20, "позиций одновременно"),
    "MAX_TOTAL_RISK_PCT": (float, 1, 100, "суммарный риск открытых позиций, %"),
    "LEVERAGE": (int, 1, 20, "максимальное плечо"),
    "TARGET_PROFIT_PCT": (float, 0, 10000, "остановка при прибыли, % (100 = x2, 0 — без цели)"),
    "MAX_DRAWDOWN_PCT": (float, 5, 90, "аварийная остановка при просадке, %"),
    "MAX_CONSECUTIVE_LOSSES": (int, 1, 50, "убытков подряд до перерыва"),
    "PAUSE_HOURS": (float, 0, 720, "длина перерыва, ч (0 — полная остановка)"),
    "MAX_FUNDING_PCT": (float, 0, 1, "фильтр funding, % за 8ч (0 — выкл.)"),
    "DONCHIAN_PERIOD": (int, 5, 200, "период канала, свечей"),
    "TAKE_PROFIT_RR": (float, 1, 10, "тейк в стопах"),
    "STOP_ATR_MULT": (float, 0.5, 10, "стоп в ATR"),
    "SYMBOLS": (str, None, None, "TOP30 или список: BTC,ETH,SOL"),
    "TIMEFRAME": (str, None, None, "таймфрейм: " + ", ".join(TIMEFRAMES)),
}
SETTINGS_ATTR = {
    "RISK_PER_TRADE_PCT": "risk_per_trade_pct", "MAX_POSITIONS": "max_positions",
    "MAX_TOTAL_RISK_PCT": "max_total_risk_pct", "LEVERAGE": "leverage",
    "TARGET_PROFIT_PCT": "target_profit_pct", "MAX_DRAWDOWN_PCT": "max_drawdown_pct",
    "MAX_CONSECUTIVE_LOSSES": "max_consecutive_losses", "PAUSE_HOURS": "pause_hours",
    "SYMBOLS": "symbols", "TIMEFRAME": "timeframe",
    "MAX_FUNDING_PCT": "strategy.max_funding_pct", "DONCHIAN_PERIOD": "strategy.donchian_period",
    "TAKE_PROFIT_RR": "strategy.take_profit_rr", "STOP_ATR_MULT": "strategy.stop_atr_mult",
}
# Наборы настроек одной командой /preset
PRESETS = {
    "razgon": ("разгон: риск 2%, цель x2, аварийная остановка -50%", {
        "SYMBOLS": "TOP30", "RISK_PER_TRADE_PCT": 2, "MAX_POSITIONS": 6, "MAX_TOTAL_RISK_PCT": 20,
        "LEVERAGE": 10, "TARGET_PROFIT_PCT": 100, "MAX_DRAWDOWN_PCT": 50,
        "MAX_CONSECUTIVE_LOSSES": 10, "PAUSE_HOURS": 24}),
    "safe": ("осторожно: риск 1%, без цели, аварийная остановка -25%", {
        "SYMBOLS": "TOP30", "RISK_PER_TRADE_PCT": 1, "MAX_POSITIONS": 6, "MAX_TOTAL_RISK_PCT": 10,
        "LEVERAGE": 5, "TARGET_PROFIT_PCT": 0, "MAX_DRAWDOWN_PCT": 25,
        "MAX_CONSECUTIVE_LOSSES": 10, "PAUSE_HOURS": 24}),
}

COMMANDS = [
    ("status", "депозит, позиции, состояние"),
    ("trades", "последние сделки и статистика"),
    ("journal", "журнал сделок файлом (Excel)"),
    ("coins", "монеты, которыми торгует бот"),
    ("pause", "не открывать новые сделки"),
    ("resume", "возобновить торговлю"),
    ("close", "закрыть позицию: /close SOL да или /close all да"),
    ("settings", "текущие настройки"),
    ("set", "изменить настройку: /set КЛЮЧ ЗНАЧЕНИЕ"),
    ("preset", "набор настроек: /preset razgon или /preset safe"),
    ("reset", "paper: начать заново со стартовым депозитом"),
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
        log.warning("%s: не удалось получить funding: %s", symbol, e)
        return None


def get_attr(cfg, path):
    obj = cfg
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def short(symbol):
    return symbol.split("/")[0]


def resolve_symbols(ex, spec):
    """TOP30 -> 30 самых ликвидных монет; 'BTC,ETH' -> список фьючерсов к USDT."""
    spec = spec.upper().replace(" ", "")
    if spec.startswith("TOP"):
        return top_symbols(ex, int(spec[3:] or 30))
    ex.load_markets()
    symbols = [t if "/" in t else f"{t}/USDT:USDT" for t in spec.split(",") if t]
    return [s for s in symbols if s in ex.markets]


def validate_symbols_spec(ex, raw):
    """Нормализованное значение SYMBOLS или текст ошибки."""
    spec = raw.upper().replace(" ", "")
    if spec.startswith("TOP"):
        n = spec[3:]
        if not n.isdigit() or not 1 <= int(n) <= 100:
            return None, "Формат: TOP30 (от TOP1 до TOP100)"
        return spec, None
    ex.load_markets()
    tickers = [t for t in spec.split(",") if t]
    missing = [t for t in tickers if f"{t}/USDT:USDT" not in ex.markets]
    if not tickers or missing:
        return None, f"Нет на Binance Futures: {', '.join(missing) or raw}"
    return ",".join(tickers), None


class Restart(Exception):
    """Перезапуск процесса (после /restart, /set, /update)."""


class TradingBot:
    def __init__(self, cfg, ex, broker, tg):
        self.cfg, self.ex, self.broker, self.tg = cfg, ex, broker, tg
        self.tf_ms = ex.parse_timeframe(cfg.timeframe) * 1000
        self.limit = min(cfg.strategy.warmup + 100, MAX_CANDLES)
        self.news_path = cfg.news_file if os.path.isabs(cfg.news_file) else os.path.join(ROOT, cfg.news_file)
        self.events = load_events(self.news_path)
        self.guard = RiskGuard(broker.equity(), cfg.target_profit_pct, cfg.max_drawdown_pct,
                               cfg.max_consecutive_losses, cfg.pause_hours)
        self.symbols = []
        self.symbols_day = None
        self.trading_enabled = True
        self.halted_reason = None  # причина остановки по лимитам риска
        self.last_candle_ts = None
        self.last_check_ms = None
        self.last_scan = ""
        self.last_report_day = datetime.now(timezone.utc).date().isoformat()
        self.day_trades = []
        self.close_requests = set()
        self.manual_closes = set()  # на бирже ручное закрытие увидит следующий check_exits
        self.restored = False

    # --- Состояние -------------------------------------------------------------

    def to_state(self):
        g = self.guard
        return {
            "version": STATE_VERSION, "mode": self.cfg.mode, "timeframe": self.cfg.timeframe,
            "guard": {"start_equity": g.start_equity, "consecutive_losses": g.consecutive_losses,
                      "paused_until_ms": g.paused_until_ms, "trades": g.trades},
            "trading_enabled": self.trading_enabled, "halted_reason": self.halted_reason,
            "last_candle_ts": self.last_candle_ts, "last_report_day": self.last_report_day,
            "day_trades": self.day_trades, "broker": self.broker.to_state(),
        }

    def load(self, st):
        """Восстановить состояние после перезапуска (только для того же режима)."""
        if not st or st.get("version") != STATE_VERSION or st.get("mode") != self.cfg.mode:
            return
        g = st["guard"]
        self.guard.start_equity = g["start_equity"]
        self.guard.consecutive_losses = g["consecutive_losses"]
        self.guard.paused_until_ms = g["paused_until_ms"]
        self.guard.trades = g["trades"]
        self.trading_enabled = st["trading_enabled"]
        self.halted_reason = st["halted_reason"]
        self.last_report_day = st.get("last_report_day", self.last_report_day)
        self.day_trades = st.get("day_trades", [])
        if st.get("timeframe") == self.cfg.timeframe:
            self.last_candle_ts = st.get("last_candle_ts")
        self.broker.load_state(st["broker"])
        self.restored = True

    def save(self):
        try:
            save_state(self.to_state(), STATE_FILE)
        except OSError as e:
            log.warning("Не удалось сохранить состояние: %s", e)

    # --- Торговля --------------------------------------------------------------

    def refresh_symbols(self):
        """Список монет; для TOP-N обновляется раз в сутки."""
        today = datetime.now(timezone.utc).date().isoformat()
        if self.symbols and self.symbols_day == today:
            return
        self.symbols = resolve_symbols(self.ex, self.cfg.symbols)
        self.symbols_day = today
        log.info("Монеты (%d): %s", len(self.symbols), ", ".join(short(s) for s in self.symbols))

    def tick(self):
        now_ms = self.ex.milliseconds()
        for symbol in list(self.close_requests):
            self.close_requests.discard(symbol)
            if symbol not in self.broker.positions:
                continue
            pos = self.broker.positions[symbol]
            price = self.ex.fetch_ticker(symbol)["last"]
            pnl = self.broker.close(symbol, price)
            if pnl is not None:
                self.on_closed(symbol, pos, pnl, now_ms, manual=True)
            else:
                self.manual_closes.add(symbol)
        for symbol, pos, pnl in self.broker.check_exits(self.ex, self.cfg.timeframe):
            manual = symbol in self.manual_closes
            self.manual_closes.discard(symbol)
            self.on_closed(symbol, pos, pnl, now_ms, manual)

        self.daily_report()
        self.refresh_symbols()

        closed_ts = now_ms // self.tf_ms * self.tf_ms - self.tf_ms  # последняя закрытая свеча
        if closed_ts == self.last_candle_ts or now_ms - (closed_ts + self.tf_ms) < SCAN_DELAY_MS:
            return
        self.last_candle_ts = closed_ts
        self.last_check_ms = now_ms
        if not self.trading_enabled:
            log.info("Торговля выключена, свеча %s пропущена", fmt_ts(closed_ts))
            return
        if self.guard.is_paused(now_ms):
            log.info("Перерыв после серии убытков, новые сделки не открываются")
            return
        event = blocking_event(self.events, datetime.now(timezone.utc),
                               self.cfg.news_hours_before, self.cfg.news_hours_after)
        if event:
            log.info("Рядом важное событие (%s), новые сделки не открываются", event)
            return
        self.scan(closed_ts, now_ms)

    def scan(self, closed_ts, now_ms):
        """Проверить все монеты на закрытой свече и открыть позиции в пределах лимитов."""
        cfg = self.cfg
        signals, checked = [], 0
        for symbol in self.symbols:  # порядок = приоритет (по ликвидности)
            if symbol in self.broker.positions:
                continue
            try:
                candles = [c for c in self.ex.fetch_ohlcv(symbol, cfg.timeframe, limit=self.limit)
                           if c[0] <= closed_ts]
            except Exception as e:
                log.warning("%s: не удалось загрузить свечи: %s", symbol, e)
                continue
            if not candles or candles[-1][0] != closed_ts:
                continue
            checked += 1
            sig = generate_signal(candles, cfg.strategy)
            if sig:
                signals.append((symbol, sig))

        opened, skipped = [], []
        max_funding = cfg.strategy.max_funding_pct / 100
        for symbol, sig in signals:
            if len(self.broker.positions) >= cfg.max_positions:
                skipped.append(f"{short(symbol)} (лимит позиций)")
                continue
            if max_funding > 0:
                f = current_funding(self.ex, symbol)
                if f is not None and (f > max_funding if sig.side == "long" else f < -max_funding):
                    skipped.append(f"{short(symbol)} (funding {f * 100:.3f}%)")
                    continue
            sized = portfolio_size(
                self.broker.equity(), sig.entry, sig.stop, cfg.risk_per_trade_pct,
                [(p["signal"].entry, p["amount"], p["risk"]) for p in self.broker.positions.values()],
                cfg.max_total_risk_pct, cfg.leverage, min_notional(self.ex, symbol))
            if not sized:
                skipped.append(f"{short(symbol)} (лимит риска или мал объём)")
                continue
            amount, risk = sized
            try:
                ok = self.broker.open(symbol, sig, amount, risk, closed_ts, now_ms)
            except Exception as e:
                log.exception("%s: не удалось открыть позицию", symbol)
                self.tg.error(f"{short(symbol)}: не удалось открыть позицию: {e}"[:500])
                continue
            if ok:
                opened.append(symbol)
                self.tg.send(f"{'🟢 Лонг' if sig.side == 'long' else '🔴 Шорт'} {short(symbol)}\n"
                             f"Вход: {sig.entry:.6g}\nСтоп: {sig.stop:.6g}\nТейк: {sig.take:.6g}\n"
                             f"Риск: {risk:.2f} USDT")
        self.last_scan = (f"{fmt_ts(closed_ts)} UTC: проверено {checked} из {len(self.symbols)}, "
                          f"сигналов {len(signals)}, открыто {len(opened)}")
        log.info("Свеча %s", self.last_scan + (f"; пропущены: {', '.join(skipped)}" if skipped else ""))

    def on_closed(self, symbol, pos, pnl, now_ms, manual=False):
        self.guard.record_trade(pnl, now_ms)
        self.day_trades.append(pnl)
        equity = self.broker.equity()
        log.info("%s: сделка %+.4f USDT, депозит %.2f (%+.1f%% от старта)",
                 symbol, pnl, equity, self.change_pct(equity))
        append_trade(JOURNAL_FILE, trade_row(symbol, pos["signal"], pos["amount"],
                                             pos["open_ms"], now_ms, pnl, equity))
        how = "вручную" if manual else ("по тейку" if pnl >= 0 else "по стопу")
        self.tg.send(f"{'✅' if pnl >= 0 else '❌'} {short(symbol)} закрыта {how}: {pnl:+.2f} USDT\n"
                     f"Депозит: {equity:.2f} USDT ({self.change_pct(equity):+.1f}% от старта)")
        if self.guard.is_paused(now_ms):
            log.warning("Серия убытков — перерыв %.0f ч", self.cfg.pause_hours)
            self.tg.send(f"⏸ {self.cfg.max_consecutive_losses} убытков подряд — "
                         f"перерыв {self.cfg.pause_hours:.0f} ч до "
                         f"{fmt_ts(self.guard.paused_until_ms)} UTC")
        reason = self.guard.stop_reason(equity)
        if reason and self.trading_enabled:
            self.trading_enabled = False
            self.halted_reason = reason
            log.warning("Торговля остановлена: %s", reason)
            extra = ""
            if self.broker.positions:
                extra = (f"\nОткрыто позиций: {len(self.broker.positions)} — они сохраняют стоп и тейк. "
                         "/close all да — закрыть все.")
            self.tg.send(f"🛑 Торговля остановлена: {reason}\nДепозит: {equity:.2f} USDT{extra}\n"
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
                     f"Открыто позиций: {len(self.broker.positions)}\n"
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
        if not parts:
            return
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
        positions = self.broker.positions
        lines = [f"🤖 {cfg.mode} | {cfg.symbols} ({len(self.symbols)} монет) {cfg.timeframe}",
                 f"Торговля: {self.trading_state()}",
                 f"Депозит: {equity:.2f} USDT ({self.change_pct(equity):+.1f}% "
                 f"от старта {self.guard.start_equity:.2f})",
                 f"Риск {cfg.risk_per_trade_pct:g}% на сделку, цель "
                 + (f"+{cfg.target_profit_pct:g}%" if cfg.target_profit_pct > 0 else "нет")
                 + f", аварийная остановка -{cfg.max_drawdown_pct:g}%",
                 f"Позиций: {len(positions)} из {cfg.max_positions}"]
        total = 0.0
        for symbol, p in positions.items():
            s = p["signal"]
            try:
                upnl = self.broker.unrealized(symbol, self.ex.fetch_ticker(symbol)["last"])
            except Exception:
                upnl = 0.0
            total += upnl
            lines.append(f"{'🟢' if s.side == 'long' else '🔴'} {short(symbol)}: вход {s.entry:.6g}, "
                         f"стоп {s.stop:.6g}, тейк {s.take:.6g}, PnL {upnl:+.2f}")
        if positions:
            lines.append(f"Нереализованный PnL: {total:+.2f} USDT")
        trades = self.guard.trades
        if trades:
            wins = sum(1 for t in trades if t > 0)
            lines.append(f"Сделок: {len(trades)}, прибыльных {wins} "
                         f"({wins / len(trades) * 100:.0f}%), итог {sum(trades):+.2f} USDT")
        if self.last_scan:
            lines.append(f"Последняя проверка: {self.last_scan}")
        self.tg.send("\n".join(lines))

    def cmd_coins(self, args):
        self.tg.send(f"Монеты ({len(self.symbols)}), по убыванию ликвидности:\n"
                     + ", ".join(short(s) for s in self.symbols))

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
                         f"{short(r['symbol'])} {r['side']} → {float(r['pnl']):+.2f}")
        self.tg.send("\n".join(lines))

    def cmd_journal(self, args):
        if os.path.exists(JOURNAL_FILE):
            self.tg.send_document(JOURNAL_FILE, "Журнал сделок")
        else:
            self.tg.send("Сделок пока не было.")

    def cmd_pause(self, args):
        self.trading_enabled = False
        self.tg.send("⏸ Новые сделки не открываются. Открытые позиции сохраняют стоп и тейк.\n"
                     "/resume — возобновить, /close all да — закрыть все.")

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
        positions = self.broker.positions
        if not positions:
            self.tg.send("Открытых позиций нет.")
            return
        if len(args) != 2 or args[1].lower() not in ("да", "yes"):
            self.tg.send("Открыты: " + ", ".join(short(s) for s in positions)
                         + "\nЗакрыть одну: /close SOL да\nЗакрыть все: /close all да")
            return
        target = args[0].upper()
        if target in ("ALL", "ВСЕ"):
            symbols = list(positions)
        else:
            symbols = [s for s in positions if short(s) == target]
            if not symbols:
                self.tg.send(f"Позиции по {target} нет. Открыты: "
                             + ", ".join(short(s) for s in positions))
                return
        self.close_requests.update(symbols)
        self.tg.send(f"Закрываю по рынку: {', '.join(short(s) for s in symbols)}…")

    def cmd_settings(self, args):
        lines = ["Настройки (менять: /set КЛЮЧ ЗНАЧЕНИЕ):"]
        for key, (typ, lo, hi, desc) in SETTINGS.items():
            value = get_attr(self.cfg, SETTINGS_ATTR[key])
            rng = f" [{lo}…{hi}]" if lo is not None else ""
            lines.append(f"{key} = {value}  — {desc}{rng}")
        lines.append(f"\nMODE = {self.cfg.mode} (меняется только на сервере)")
        lines.append("Наборы: " + "; ".join(f"/preset {k} — {d}" for k, (d, _) in PRESETS.items()))
        self.tg.send("\n".join(lines))

    def parse_setting(self, key, raw):
        """(значение, None) или (None, текст ошибки)."""
        typ, lo, hi, _ = SETTINGS[key]
        if key == "SYMBOLS":
            return validate_symbols_spec(self.ex, raw)
        if key == "TIMEFRAME":
            value = raw.lower()
            if value not in TIMEFRAMES:
                return None, f"Допустимые таймфреймы: {', '.join(TIMEFRAMES)}"
            if self.broker.positions:
                return None, "Таймфрейм можно менять только без открытых позиций."
            return value, None
        try:
            value = typ(str(raw).replace(",", "."))
        except ValueError:
            return None, f"{raw} — не подходящее число для {key}."
        if not lo <= value <= hi:
            return None, f"{key} должен быть от {lo} до {hi}."
        return value, None

    def cmd_set(self, args):
        if len(args) < 2:
            self.tg.send("Формат: /set КЛЮЧ ЗНАЧЕНИЕ, например /set RISK_PER_TRADE_PCT 1.5\n"
                         "Список ключей: /settings")
            return
        key = args[0].upper()
        if key not in SETTINGS:
            if key == "MODE" or key.startswith(("BINANCE", "TELEGRAM")):
                self.tg.send("Эту настройку можно менять только на сервере.")
            else:
                self.tg.send(f"Неизвестный ключ {key}. Список: /settings")
            return
        value, error = self.parse_setting(key, " ".join(args[1:]))
        if error:
            self.tg.send(error)
            return
        set_env_value(key, value, ENV_FILE)
        self.tg.send(f"✅ {key} = {value}. Перезапускаю бота…")
        raise Restart

    def cmd_preset(self, args):
        name = args[0].lower() if args else ""
        if name not in PRESETS:
            self.tg.send("Наборы настроек:\n" + "\n".join(f"/preset {k} — {d}" for k, (d, _) in PRESETS.items()))
            return
        desc, values = PRESETS[name]
        for key, value in values.items():
            set_env_value(key, value, ENV_FILE)
        self.tg.send(f"✅ Набор «{desc}» установлен:\n"
                     + "\n".join(f"{k} = {v}" for k, v in values.items())
                     + "\nПерезапускаю бота…")
        raise Restart

    def cmd_reset(self, args):
        if self.cfg.mode != "paper":
            self.tg.send("Сброс доступен только в режиме paper.")
            return
        if args[:1] not in (["да"], ["yes"]):
            self.tg.send(f"Начать paper-торговлю заново с {self.cfg.paper_balance:g} USDT? "
                         "Открытые бумажные позиции и счётчики риска будут сброшены, "
                         "журнал сделок сохранится.\nПодтвердите: /reset да")
            return
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        self.tg.send("🔄 Paper-торговля начнётся заново. Перезапускаю бота…")
        raise Restart

    def cmd_news(self, args):
        self.events = load_events(self.news_path)
        if not self.events:
            self.tg.send("Событий нет. Добавить: /news_add 2026-11-12 13:30 CPI (время UTC)")
            return
        lines = [f"Не открываю сделки за {self.cfg.news_hours_before:.0f} ч до и "
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
        try:
            self.refresh_symbols()
        except Exception as e:
            log.warning("Не удалось получить список монет: %s", e)
        equity = self.broker.equity()
        log.info("Старт: режим=%s, %s (%d монет) %s, депозит %.2f USDT, риск %.1f%%%s",
                 cfg.mode, cfg.symbols, len(self.symbols), cfg.timeframe, equity,
                 cfg.risk_per_trade_pct, ", состояние восстановлено" if self.restored else "")
        self.tg.send(f"🚀 Бот запущен\nРежим: {cfg.mode}\n"
                     f"Монеты: {cfg.symbols} ({len(self.symbols)} шт.), {cfg.timeframe}\n"
                     f"Депозит: {equity:.2f} USDT, риск {cfg.risk_per_trade_pct:g}% на сделку, "
                     f"до {cfg.max_positions} позиций\n"
                     f"Торговля: {self.trading_state()}"
                     + (f"\nОткрыто позиций: {len(self.broker.positions)}" if self.broker.positions else "")
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
        broker = PaperPortfolio(cfg.paper_balance)
    else:
        if not cfg.api_key or not cfg.api_secret:
            raise SystemExit("Для режимов testnet/demo/live нужны BINANCE_API_KEY и BINANCE_API_SECRET")
        if cfg.mode == "live":
            log.warning("РЕЖИМ LIVE: торговля реальными деньгами, плечо до x%d", cfg.leverage)
        broker = BinancePortfolio(ex, cfg.leverage)
    tg = Notifier(cfg.telegram_token, cfg.telegram_chat_id)
    bot = TradingBot(cfg, ex, broker, tg)
    bot.load(load_state(STATE_FILE))
    try:
        bot.run()
    except Exception as e:
        # Падение — в лог и в Telegram; systemd перезапустит бота через 5 секунд
        log.exception("Бот упал")
        tg.send(f"💥 Бот упал: {type(e).__name__}: {e}"[:500] + "\nПерезапуск через несколько секунд.")
        raise


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Остановлено пользователем. Открытые позиции на бирже НЕ закрываются автоматически.")
