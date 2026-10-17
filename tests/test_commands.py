"""Бот целиком: поддельная биржа и Telegram, команды, сохранение состояния."""
import pytest

import bot.main as m
from bot.broker import PaperBroker
from bot.config import load_config
from bot.strategy import StrategyParams

H = 4 * 3600 * 1000


def breakout_candles():
    closes = [100 + i * 0.1 for i in range(80)] + [108.0] * 5 + [112.0]
    return [[i * H, c, c + 0.2, c - 0.2, c, 1.0] for i, c in enumerate(closes)]


class FakeEx:
    def __init__(self):
        self.closed = breakout_candles()
        self.current = [len(self.closed) * H, 112.0, 112.1, 111.9, 112.0, 1.0]
        self.markets = {"BTC/USDT:USDT": {}, "SOL/USDT:USDT": {}}

    def fetch_ohlcv(self, *a, **k):
        return self.closed + [self.current]

    def milliseconds(self):
        return (len(self.closed) + 1) * H

    def fetch_funding_rate(self, symbol):
        return {"fundingRate": 0.0001}

    def load_markets(self):
        return self.markets


class FakeTg:
    def __init__(self, incoming=()):
        self.sent, self.docs, self.incoming, self.acked = [], [], list(incoming), False

    def send(self, text):
        self.sent.append(text)

    def send_document(self, path, caption=""):
        self.docs.append(path)

    def error(self, text):
        self.sent.append("ERR " + text)

    def set_commands(self, commands):
        pass

    def skip_pending(self):
        pass

    def ack(self):
        self.acked = True

    def poll(self, timeout):
        return [self.incoming.pop(0)] if self.incoming else []

    @property
    def last(self):
        return self.sent[-1]


@pytest.fixture
def files(tmp_path, monkeypatch):
    for name in ("JOURNAL_FILE", "LOG_FILE", "ENV_FILE", "STATE_FILE"):
        monkeypatch.setattr(m, name, str(tmp_path / name.lower()))
    (tmp_path / "env_file").write_text("MODE=paper\nRISK_PER_TRADE_PCT=1\n", encoding="utf-8")
    return tmp_path


def make_bot(files, tg=None, ex=None):
    cfg = load_config()
    cfg.mode, cfg.symbol, cfg.timeframe = "paper", "BTC/USDT:USDT", "4h"
    cfg.strategy = StrategyParams(trend_ema=50, donchian_period=10)
    cfg.news_file = str(files / "news.txt")
    bot = m.TradingBot(cfg, ex or FakeEx(), PaperBroker(100), tg or FakeTg())
    bot.load(m.load_state(m.STATE_FILE))
    return bot


def test_opens_trade_and_reports_status(files):
    bot = make_bot(files)
    bot.tick()
    assert bot.broker.in_position
    assert bot.tg.last.startswith("🟢 Лонг")
    bot.handle("/status")
    assert "лонг" in bot.tg.last and "Вход 112" in bot.tg.last


def test_pause_blocks_new_trades_and_resume(files):
    bot = make_bot(files)
    bot.handle("/pause")
    bot.tick()
    assert not bot.broker.in_position
    assert "на паузе" in bot.trading_state()
    bot.handle("/resume")
    bot.last_candle_ts = None
    bot.tick()
    assert bot.broker.in_position


def test_close_needs_confirmation(files):
    bot = make_bot(files)
    bot.tick()
    bot.handle("/close")
    assert "/close да" in bot.tg.last and bot.broker.in_position
    bot.handle("/close да")
    bot.tick()
    assert not bot.broker.in_position
    assert any("закрыта вручную" in t for t in bot.tg.sent)
    bot.handle("/trades")
    assert "Сделок: 1" in bot.tg.last
    bot.handle("/journal")
    assert bot.tg.docs == [m.JOURNAL_FILE]
    # Сигнальная свеча уже отработана — повторного входа нет
    bot.tick()
    assert not bot.broker.in_position


def test_set_validates_and_restarts(files):
    bot = make_bot(files)
    bot.handle("/set RISK_PER_TRADE_PCT 50")
    assert "от 0.1 до 5" in bot.tg.last
    bot.handle("/set MODE live")
    assert "только на сервере" in bot.tg.last
    bot.handle("/set SYMBOL DOGE")
    assert "нет на Binance" in bot.tg.last
    with pytest.raises(m.Restart):
        bot.handle("/set risk_per_trade_pct 1,5")
    env = (files / "env_file").read_text(encoding="utf-8")
    assert "RISK_PER_TRADE_PCT=1.5\n" in env and "MODE=paper" in env
    with pytest.raises(m.Restart):
        bot.handle("/set SYMBOL sol")
    assert "SYMBOL=SOL/USDT:USDT" in (files / "env_file").read_text(encoding="utf-8")


def test_set_symbol_refused_with_open_position(files):
    bot = make_bot(files)
    bot.tick()
    bot.handle("/set SYMBOL SOL")
    assert "закрытия позиции" in bot.tg.last


def test_state_survives_restart(files):
    bot = make_bot(files)
    bot.tick()
    bot.handle("/pause")
    bot.save()
    restored = make_bot(files)
    assert restored.restored
    assert restored.broker.in_position and restored.open_trade
    assert not restored.trading_enabled
    assert restored.broker.equity() == bot.broker.equity()
    restored.tick()  # та же сигнальная свеча не открывает вторую позицию
    assert restored.open_trade[2] == bot.open_trade[2]


def test_run_restart_exits_with_code_and_acks(files):
    bot = make_bot(files, tg=FakeTg(["/restart"]))
    with pytest.raises(SystemExit) as e:
        bot.run()
    assert e.value.code == m.RESTART_EXIT_CODE
    assert bot.tg.acked
    assert m.load_state(m.STATE_FILE)["mode"] == "paper"


def test_news_commands(files):
    bot = make_bot(files)
    bot.handle("/news_add 2026-13-01 10:00 bad")
    assert "Неверная дата" in bot.tg.last
    bot.handle("/news_add 2026-11-12 13:30 CPI США")
    assert "1. 2026-11-12 13:30 CPI США" in bot.tg.last
    bot.handle("/news_del 1")
    assert "Удалено" in bot.tg.last
    assert bot.events == []


def test_halt_on_drawdown_keeps_listening_and_resume_resets(files):
    bot = make_bot(files)
    bot.cfg.max_drawdown_pct = 0.5
    bot.guard.max_drawdown_pct = 0.5
    bot.tick()
    bot.ex.current = [bot.ex.current[0], 112.0, 112.1, 100.0, 101.0, 1.0]  # стоп
    bot.tick()
    assert bot.halted_reason and not bot.trading_enabled
    assert any("Торговля остановлена" in t for t in bot.tg.sent)
    bot.handle("/resume")
    assert bot.trading_enabled and bot.halted_reason is None
    assert bot.guard.start_equity == bot.broker.equity()


def test_unknown_command_and_help(files):
    bot = make_bot(files)
    bot.handle("/foo")
    assert "Неизвестная команда" in bot.tg.last
    bot.handle("/help@my_bot")
    assert "/status" in bot.tg.last


def test_notifier_ignores_foreign_chats(monkeypatch):
    import bot.telegram as tgmod
    updates = {"result": [
        {"update_id": 5, "message": {"chat": {"id": 999}, "text": "/close да"}},
        {"update_id": 6, "message": {"chat": {"id": 42}, "text": "/status"}},
    ]}
    monkeypatch.setattr(tgmod, "call", lambda *a, **k: updates)
    n = tgmod.Notifier("token", 42)
    assert n.poll(0) == ["/status"]
    assert n.offset == 7
