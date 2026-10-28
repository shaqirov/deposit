"""Бот целиком: поддельная биржа с несколькими монетами и Telegram, команды, состояние."""
import pytest

import bot.main as m
from bot.broker import PaperPortfolio
from bot.config import load_config
from bot.strategy import StrategyParams

H = 4 * 3600 * 1000


def breakout_closed(base=100.0):
    closes = [base + i * 0.1 * base / 100 for i in range(80)] + [base * 1.08] * 5 + [base * 1.12]
    return [[i * H, c, c * 1.002, c * 0.998, c, 1.0] for i, c in enumerate(closes)]


def flat_closed(base=100.0):
    return [[i * H, base, base * 1.001, base * 0.999, base, 1.0] for i in range(86)]


class FakeEx:
    """Монеты A и B дают сигнал на лонг, C — без сигнала."""

    def __init__(self):
        self.closed = {"A/USDT:USDT": breakout_closed(100), "B/USDT:USDT": breakout_closed(50),
                       "C/USDT:USDT": flat_closed(10)}
        self.current = {s: [len(c) * H, c[-1][4], c[-1][4], c[-1][4], c[-1][4], 1.0]
                        for s, c in self.closed.items()}
        self.now = len(self.closed["A/USDT:USDT"]) * H + 20_000
        self.markets = {s: {"swap": True, "linear": True, "quote": "USDT", "active": True,
                            "base": s.split("/")[0], "limits": {"cost": {"min": 5}}}
                        for s in list(self.closed) + ["SOL/USDT:USDT"]}
        self.funding = 0.0001

    def parse_timeframe(self, tf):
        return 4 * 3600

    def fetch_ohlcv(self, symbol, timeframe, limit=None, **k):
        data = self.closed[symbol] + [self.current[symbol]]
        return data[-limit:] if limit else data

    def milliseconds(self):
        return self.now

    def fetch_ticker(self, symbol):
        return {"last": self.current[symbol][4]}

    def fetch_tickers(self):
        vol = {"A/USDT:USDT": 3e9, "B/USDT:USDT": 2e9, "C/USDT:USDT": 1e9, "SOL/USDT:USDT": 1}
        return {s: {"quoteVolume": v} for s, v in vol.items()}

    def fetch_funding_rate(self, symbol):
        return {"fundingRate": self.funding}

    def load_markets(self):
        return self.markets

    def next_candle(self, symbol_moves=None):
        """Сдвинуть время на свечу; symbol_moves: {symbol: (high, low)} для текущей свечи."""
        for s in self.closed:
            self.closed[s].append(self.current[s])
            last = self.closed[s][-1][4]
            self.current[s] = [len(self.closed[s]) * H, last, last, last, last, 1.0]
        for s, (high, low) in (symbol_moves or {}).items():
            self.current[s][2], self.current[s][3] = high, low
        self.now += H


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


def make_bot(files, tg=None, ex=None, **cfg_overrides):
    cfg = load_config()
    cfg.mode, cfg.timeframe, cfg.symbols = "paper", "4h", "TOP3"
    cfg.risk_per_trade_pct, cfg.max_positions, cfg.max_total_risk_pct = 2, 6, 20
    cfg.leverage, cfg.target_profit_pct, cfg.max_drawdown_pct = 10, 100, 50
    cfg.strategy = StrategyParams(trend_ema=50, donchian_period=10)
    cfg.news_file = str(files / "news.txt")
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)
    bot = m.TradingBot(cfg, ex or FakeEx(), PaperPortfolio(100), tg or FakeTg())
    bot.load(m.load_state(m.STATE_FILE))
    return bot


def test_scan_opens_signals_on_several_coins(files):
    bot = make_bot(files)
    bot.tick()
    assert sorted(bot.broker.positions) == ["A/USDT:USDT", "B/USDT:USDT"]
    assert sum(t.startswith("🟢 Лонг") for t in bot.tg.sent) == 2
    # та же свеча второй раз не сканируется
    bot.tick()
    assert len(bot.broker.positions) == 2
    bot.handle("/status")
    assert "Позиций: 2 из 6" in bot.tg.last and "A:" in bot.tg.last


def test_waits_for_candle_to_form(files):
    ex = FakeEx()
    ex.now -= 10_000  # 10 с после закрытия свечи — рано
    bot = make_bot(files, ex=ex)
    bot.tick()
    assert not bot.broker.positions
    ex.now += 10_000
    bot.tick()
    assert bot.broker.positions


def test_max_positions_and_priority(files):
    bot = make_bot(files, max_positions=1)
    bot.tick()
    assert list(bot.broker.positions) == ["A/USDT:USDT"]  # A ликвиднее B


def test_total_risk_cap(files):
    bot = make_bot(files, max_total_risk_pct=3)  # 2% + 2% > 3%
    bot.tick()
    assert len(bot.broker.positions) == 1


def test_funding_filter(files):
    ex = FakeEx()
    ex.funding = 0.01
    bot = make_bot(files, ex=ex)
    bot.tick()
    assert not bot.broker.positions


def test_exit_on_take_and_journal(files):
    bot = make_bot(files)
    bot.tick()
    take = bot.broker.positions["A/USDT:USDT"]["signal"].take
    bot.ex.next_candle({"A/USDT:USDT": (take * 1.01, take * 0.999)})
    bot.tick()
    assert "A/USDT:USDT" not in bot.broker.positions
    assert any(t.startswith("✅ A закрыта по тейку") for t in bot.tg.sent)
    bot.handle("/trades")
    assert "Сделок: 1" in bot.tg.last
    bot.handle("/journal")
    assert bot.tg.docs == [m.JOURNAL_FILE]


def test_exit_seen_on_candle_closed_between_polls(files):
    bot = make_bot(files)
    bot.tick()
    stop = bot.broker.positions["B/USDT:USDT"]["signal"].stop
    bot.ex.next_candle({"B/USDT:USDT": (60, stop * 0.99)})
    bot.ex.next_candle()  # свеча со стопом уже закрыта, текущая — спокойная
    bot.tick()
    assert "B/USDT:USDT" not in bot.broker.positions


def test_close_commands(files):
    bot = make_bot(files)
    bot.tick()
    bot.handle("/close")
    assert "/close all да" in bot.tg.last
    bot.handle("/close XRP да")
    assert "Позиции по XRP нет" in bot.tg.last
    bot.handle("/close a да")
    bot.tick()
    assert list(bot.broker.positions) == ["B/USDT:USDT"]
    assert any("A закрыта вручную" in t for t in bot.tg.sent)
    bot.handle("/close all да")
    bot.tick()
    assert not bot.broker.positions


def test_pause_resume(files):
    bot = make_bot(files)
    bot.handle("/pause")
    bot.tick()
    assert not bot.broker.positions
    bot.handle("/resume")
    bot.ex.next_candle()
    bot.tick()  # сигнальная свеча прошла, но и новая годится только с новым сигналом
    assert "включена" in bot.trading_state()


def test_set_validates_and_restarts(files):
    bot = make_bot(files)
    bot.handle("/set RISK_PER_TRADE_PCT 50")
    assert "от 0.1 до 10" in bot.tg.last
    bot.handle("/set MODE live")
    assert "только на сервере" in bot.tg.last
    bot.handle("/set SYMBOLS DOGE,A")
    assert "Нет на Binance Futures: DOGE" in bot.tg.last
    bot.handle("/set SYMBOLS TOP500")
    assert "TOP100" in bot.tg.last
    with pytest.raises(m.Restart):
        bot.handle("/set symbols sol, a")
    with pytest.raises(m.Restart):
        bot.handle("/set risk_per_trade_pct 1,5")
    env = (files / "env_file").read_text(encoding="utf-8")
    assert "SYMBOLS=SOL,A\n" in env and "RISK_PER_TRADE_PCT=1.5\n" in env and "MODE=paper" in env


def test_preset_writes_all_values(files):
    bot = make_bot(files)
    bot.handle("/preset")
    assert "/preset razgon" in bot.tg.last
    with pytest.raises(m.Restart):
        bot.handle("/preset razgon")
    env = (files / "env_file").read_text(encoding="utf-8")
    assert "RISK_PER_TRADE_PCT=2\n" in env and "SYMBOLS=TOP30\n" in env and "MAX_DRAWDOWN_PCT=50\n" in env


def test_reset_paper(files):
    bot = make_bot(files)
    bot.tick()
    bot.save()
    bot.handle("/reset")
    assert "/reset да" in bot.tg.last
    with pytest.raises(m.Restart):
        bot.handle("/reset да")
    fresh = make_bot(files)
    assert not fresh.restored and not fresh.broker.positions and fresh.broker.equity() == 100


def test_state_survives_restart(files):
    bot = make_bot(files)
    bot.tick()
    bot.handle("/pause")
    bot.save()
    restored = make_bot(files, ex=bot.ex)
    assert restored.restored
    assert sorted(restored.broker.positions) == sorted(bot.broker.positions)
    assert not restored.trading_enabled
    assert restored.broker.equity() == bot.broker.equity()
    restored.handle("/resume")
    restored.tick()  # та же свеча не открывает позиции повторно
    assert len(restored.broker.positions) == 2


def test_old_state_format_is_ignored(files):
    m.save_state({"mode": "paper", "symbol": "BTC/USDT:USDT", "broker": {"balance": 97.9}},
                 m.STATE_FILE)
    bot = make_bot(files)
    assert not bot.restored and bot.broker.equity() == 100


def test_halt_on_drawdown_keeps_listening_and_resume_resets(files):
    bot = make_bot(files, max_drawdown_pct=1)
    bot.guard.max_drawdown_pct = 1
    bot.tick()
    stop = bot.broker.positions["A/USDT:USDT"]["signal"].stop
    bot.ex.next_candle({"A/USDT:USDT": (stop, stop * 0.98)})
    bot.tick()
    assert bot.halted_reason and not bot.trading_enabled
    assert any("Торговля остановлена" in t and "/close all да" in t for t in bot.tg.sent)
    bot.handle("/resume")
    assert bot.trading_enabled and bot.halted_reason is None
    assert bot.guard.start_equity == bot.broker.equity()


def test_run_restart_exits_with_code_and_acks(files):
    bot = make_bot(files, tg=FakeTg(["/restart"]))
    with pytest.raises(SystemExit) as e:
        bot.run()
    assert e.value.code == m.RESTART_EXIT_CODE
    assert bot.tg.acked
    assert m.load_state(m.STATE_FILE)["version"] == m.STATE_VERSION


def test_news_commands(files):
    bot = make_bot(files)
    bot.handle("/news_add 2026-13-01 10:00 bad")
    assert "Неверная дата" in bot.tg.last
    bot.handle("/news_add 2026-11-12 13:30 CPI США")
    assert "1. 2026-11-12 13:30 CPI США" in bot.tg.last
    bot.handle("/news_del 1")
    assert "Удалено" in bot.tg.last
    assert bot.events == []


def test_coins_unknown_empty_and_help(files):
    bot = make_bot(files)
    bot.tick()
    bot.handle("/coins")
    assert "A, B, C" in bot.tg.last
    bot.handle("/foo")
    assert "Неизвестная команда" in bot.tg.last
    n = len(bot.tg.sent)
    bot.handle("   ")
    assert len(bot.tg.sent) == n
    bot.handle("/help@my_bot")
    assert "/status" in bot.tg.last


def test_notifier_ignores_foreign_chats(monkeypatch):
    import bot.telegram as tgmod
    updates = {"result": [
        {"update_id": 5, "message": {"chat": {"id": 999}, "text": "/close all да"}},
        {"update_id": 6, "message": {"chat": {"id": 42}, "text": "/status"}},
    ]}
    monkeypatch.setattr(tgmod, "call", lambda *a, **k: updates)
    n = tgmod.Notifier("token", 42)
    assert n.poll(0) == ["/status"]
    assert n.offset == 7


def test_notifier_poll_timeout_returns_immediately(monkeypatch):
    import bot.telegram as tgmod

    def slow(*a, **k):
        raise TimeoutError("The read operation timed out")
    monkeypatch.setattr(tgmod, "call", slow)
    monkeypatch.setattr(tgmod.time, "sleep", lambda s: pytest.fail("не должен ждать"))
    assert tgmod.Notifier("token", 42).poll(10) == []
