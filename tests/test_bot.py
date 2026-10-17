import random

from bot.broker import PaperBroker, TAKER_FEE
from bot.indicators import atr, ema
from bot.risk import RiskGuard, position_size
from bot.strategy import Signal, StrategyParams, generate_signal, generate_signals


def candle(i, close, spread=1.0):
    return [i, close, close + spread, close - spread, close, 1.0]


def random_candles(n, seed, drift=0.0, vol=0.01, step_ms=1):
    random.seed(seed)
    price, out = 100.0, []
    for i in range(n):
        o = price
        price *= 1 + random.gauss(drift, vol)
        out.append([i * step_ms, o, max(o, price) * 1.003, min(o, price) * 0.997, price, 1.0])
    return out


def test_ema_constant_series():
    assert ema([5.0] * 10, 3)[-1] == 5.0
    assert ema([1.0, 2.0], 3) == [None, None]


def test_atr_constant_range():
    n = 30
    assert abs(atr([11.0] * n, [9.0] * n, [10.0] * n, 14)[-1] - 2.0) < 1e-9


def test_position_size_by_risk_and_margin_cap():
    # риск 2% от 100 = 2 USDT, стоп 10 -> 0.2 единицы
    assert abs(position_size(100, 1000, 990, 2, 100) - 0.2) < 1e-9
    # при плече 1 объём ограничен маржой: 100*0.95/1000
    assert abs(position_size(100, 1000, 999, 2, 1) - 0.095) < 1e-9
    assert position_size(100, 1000, 1000, 2, 10) == 0


def test_risk_guard():
    g = RiskGuard(100, target_profit_pct=50, max_drawdown_pct=20, max_consecutive_losses=2)
    assert g.stop_reason(120) is None
    assert "цель" in g.stop_reason(150)
    assert "просадка" in g.stop_reason(80)
    g.record_trade(-1)
    g.record_trade(-1)
    assert "подряд" in g.stop_reason(100)
    g.record_trade(5)
    assert g.stop_reason(100) is None


def test_risk_guard_pause_after_losses():
    hour = 3600 * 1000
    g = RiskGuard(100, 100, 50, max_consecutive_losses=2, pause_hours=48)
    g.record_trade(-1, 0)
    assert not g.is_paused(hour)
    g.record_trade(-1, hour)
    assert g.is_paused(10 * hour)
    assert not g.is_paused(50 * hour)
    assert g.stop_reason(98) is None  # при паузе серия убытков не останавливает бота


def test_paper_broker_take_and_stop():
    b = PaperBroker(100)
    b.open(Signal("long", 100, 95, 110), 1)
    assert b.check_exit(105, 96) is None
    pnl = b.check_exit(111, 99)
    fees = (100 + 110) * TAKER_FEE
    assert abs(pnl - (10 - fees)) < 1e-9
    assert abs(b.equity() - (100 + 10 - fees)) < 1e-9

    b.open(Signal("short", 100, 105, 90), 1)
    # оба уровня в одной свече -> консервативно считаем стоп
    assert b.check_exit(106, 89) < 0


def uptrend_breakout():
    closes = [100 + i * 0.1 for i in range(80)] + [100 + 7.9 + 0.1] * 5 + [112.0]
    return [candle(i, c, spread=0.2) for i, c in enumerate(closes)]


def downtrend_breakdown():
    closes = [100 - i * 0.1 for i in range(80)] + [100 - 7.9 - 0.1] * 5 + [88.0]
    return [candle(i, c, spread=0.2) for i, c in enumerate(closes)]


def test_long_on_breakout_in_uptrend():
    p = StrategyParams(trend_ema=50, donchian_period=10)
    s = generate_signal(uptrend_breakout(), p)
    assert s and s.side == "long" and s.stop < s.entry < s.take
    assert abs((s.take - s.entry) - 2 * (s.entry - s.stop)) < 1e-9


def test_short_on_breakdown_in_downtrend():
    p = StrategyParams(trend_ema=50, donchian_period=10)
    s = generate_signal(downtrend_breakdown(), p)
    assert s and s.side == "short" and s.take < s.entry < s.stop


def test_no_trade_against_trend():
    # Пробой вверх, но цена ниже EMA тренда — лонг не открывается
    closes = [130 - i * 0.3 for i in range(80)] + [106.0] * 5 + [107.0]
    candles = [candle(i, c, spread=0.2) for i, c in enumerate(closes)]
    p = StrategyParams(trend_ema=50, donchian_period=5)
    assert all(s is None or s.side == "short" for s in generate_signals(candles, p))


def test_funding_filter():
    p = StrategyParams(trend_ema=50, donchian_period=10, max_funding_pct=0.05)
    assert generate_signal(uptrend_breakout(), p, funding_rate=0.0001)
    assert generate_signal(uptrend_breakout(), p, funding_rate=0.001) is None
    assert generate_signal(downtrend_breakdown(), p, funding_rate=-0.001) is None
    p.max_funding_pct = 0
    assert generate_signal(uptrend_breakout(), p, funding_rate=0.001)


def test_signals_match_between_batch_and_incremental():
    candles = random_candles(400, seed=7)
    p = StrategyParams(trend_ema=50)
    batch = generate_signals(candles, p)
    assert any(batch)
    for k in range(p.warmup, len(candles), 7):
        assert generate_signal(candles[:k + 1], p) == batch[k]


def test_news_blackout(tmp_path):
    from datetime import datetime, timezone
    from bot.news import blocking_event, load_events
    f = tmp_path / "news.txt"
    f.write_text("# comment\n2026-10-28 18:00  FOMC\nbad line\n", encoding="utf-8")
    events = load_events(str(f))
    assert len(events) == 1
    at = lambda h: datetime(2026, 10, 28, h, tzinfo=timezone.utc)
    assert blocking_event(events, at(8), 12, 4) == "FOMC"
    assert blocking_event(events, at(21), 12, 4) == "FOMC"
    assert blocking_event(events, at(5), 12, 4) is None
    assert load_events(str(tmp_path / "missing.txt")) == []


def test_backtest_journal_and_halves(tmp_path):
    import csv
    from bot.backtest import run_backtest
    from bot.config import load_config
    candles = random_candles(3000, seed=3, drift=0.0005, step_ms=4 * 3600 * 1000)
    cfg = load_config()
    cfg.strategy = StrategyParams()
    journal = tmp_path / "trades.csv"
    r = run_backtest(candles, cfg, stop_on_guard=False, journal=str(journal))
    rows = list(csv.DictReader(open(journal, encoding="utf-8")))
    assert len(rows) == r["trades"] > 0
    total = (1 + r["first_half_pct"] / 100) * (1 + r["second_half_pct"] / 100)
    assert abs(total - (1 + r["return_pct"] / 100)) < 1e-9
