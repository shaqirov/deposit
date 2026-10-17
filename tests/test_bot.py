from bot.broker import PaperBroker, TAKER_FEE
from bot.indicators import atr, ema, rsi
from bot.risk import RiskGuard, position_size
from bot.strategy import Signal, StrategyParams, generate_signal


def candle(i, close, spread=1.0):
    return [i, close, close + spread, close - spread, close, 1.0]


def test_ema_constant_series():
    assert ema([5.0] * 10, 3)[-1] == 5.0
    assert ema([1.0, 2.0], 3) == [None, None]


def test_rsi_extremes():
    assert rsi(list(range(1, 30)), 14)[-1] == 100.0
    assert rsi(list(range(30, 1, -1)), 14)[-1] == 0.0


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


def test_long_signal_on_upward_cross():
    p = StrategyParams()
    closes = [100 - i * 0.5 for i in range(40)] + [80 + i * 0.6 for i in range(1, 30)]
    candles = [candle(i, c) for i, c in enumerate(closes)]
    signals = [generate_signal(candles[:k], p) for k in range(p.warmup, len(candles) + 1)]
    longs = [s for s in signals if s and s.side == "long"]
    assert longs, "ожидался сигнал на покупку после разворота вверх"
    s = longs[0]
    assert s.stop < s.entry < s.take
    assert abs((s.take - s.entry) - 2 * (s.entry - s.stop)) < 1e-9


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


def test_breakout_signal_on_new_high_in_uptrend():
    p = StrategyParams(name="breakout", trend_ema=50, donchian_period=10)
    closes = [100 + i * 0.1 for i in range(80)] + [100 + 7.9 + 0.1] * 5 + [112.0]
    candles = [candle(i, c, spread=0.2) for i, c in enumerate(closes)]
    s = generate_signal(candles, p)
    assert s and s.side == "long" and s.stop < s.entry < s.take


def test_mean_reversion_takes_profit_at_middle_band():
    p = StrategyParams(name="mean_reversion")
    closes = [100 + (0.5 if i % 2 else -0.5) for i in range(40)] + [99, 98, 97, 96, 95, 92]
    candles = [candle(i, c, spread=0.3) for i, c in enumerate(closes)]
    s = generate_signal(candles, p)
    assert s and s.side == "long"
    assert s.stop < s.entry < s.take


def test_signals_match_between_batch_and_incremental():
    import random
    random.seed(7)
    price, candles = 100.0, []
    for i in range(400):
        price *= 1 + random.gauss(0, 0.01)
        candles.append(candle(i, price, spread=price * 0.005))
    from bot.strategy import STRATEGIES, generate_signals
    for name in STRATEGIES:
        p = StrategyParams(name=name, trend_ema=50, trend_sma_days=50)
        batch = generate_signals(candles, p)
        for k in range(p.warmup, len(candles), 37):
            assert generate_signal(candles[:k + 1], p) == batch[k]


def uptrend_with_pullback():
    """Плавный рост, затем откат к EMA и бычья свеча, отскочившая от неё."""
    candles = []
    for i in range(120):
        c = 100 + i * 0.5
        candles.append([i, c - 0.3, c + 0.4, c - 0.6, c, 1.0])
    for k, c in enumerate([158.0, 157.0, 156.0, 155.0], start=120):
        candles.append([k, c + 0.5, c + 0.8, c - 0.3, c, 1.0])
    # Свеча проколола EMA вниз и закрылась выше открытия
    candles.append([124, 154.6, 156.8, 151.0, 156.5, 1.0])
    return candles


def trend_params(**kw):
    return StrategyParams(name="trend_pullback", trend_sma_days=50, swing_lookback=10,
                          pullback_ema=20, **kw)


def test_trend_pullback_long_on_bounce_from_ema():
    from bot.indicators import ema
    candles = uptrend_with_pullback()
    closes = [c[4] for c in candles]
    assert candles[-1][3] <= ema(closes, 20)[-1] < closes[-1]
    s = generate_signal(candles, trend_params())
    assert s and s.side == "long"
    assert s.stop < min(c[3] for c in candles[-10:])
    assert abs((s.take - s.entry) - 2 * (s.entry - s.stop)) < 1e-9


def test_trend_pullback_never_trades_against_trend():
    from bot.strategy import generate_signals
    candles = uptrend_with_pullback()
    assert all(s is None or s.side == "long" for s in generate_signals(candles, trend_params()))


def test_funding_filter_blocks_overheated_longs():
    candles = uptrend_with_pullback()
    p = trend_params(max_funding_pct=0.05)
    assert generate_signal(candles, p, funding_rate=0.0001)
    assert generate_signal(candles, p, funding_rate=0.001) is None
    assert generate_signal(candles, trend_params(max_funding_pct=0), funding_rate=0.001)


def test_trend_sma_period_follows_timeframe():
    assert StrategyParams(trend_sma_days=200, candles_per_day=6).trend_sma_period == 1200
    assert StrategyParams(trend_sma_days=200, candles_per_day=1).trend_sma_period == 200


def test_risk_guard_pause_after_losses():
    hour = 3600 * 1000
    g = RiskGuard(100, 100, 50, max_consecutive_losses=2, pause_hours=48)
    g.record_trade(-1, 0)
    assert not g.is_paused(hour)
    g.record_trade(-1, hour)
    assert g.is_paused(10 * hour)
    assert not g.is_paused(50 * hour)
    assert g.stop_reason(98) is None  # при паузе серия убытков не останавливает бота


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
    import random
    from bot.backtest import run_backtest
    from bot.config import load_config
    random.seed(3)
    price, candles = 100.0, []
    for i in range(3000):
        o = price
        price *= 1 + random.gauss(0.0005, 0.01)
        candles.append([i * 4 * 3600 * 1000, o, max(o, price) * 1.003,
                        min(o, price) * 0.997, price, 1.0])
    cfg = load_config()
    cfg.strategy = trend_params()
    cfg.strategy.trend_sma_days = 200
    cfg.strategy.candles_per_day = 6
    journal = tmp_path / "trades.csv"
    r = run_backtest(candles, cfg, stop_on_guard=False, journal=str(journal))
    rows = list(csv.DictReader(open(journal, encoding="utf-8")))
    assert len(rows) == r["trades"] > 0
    total = (1 + r["first_half_pct"] / 100) * (1 + r["second_half_pct"] / 100)
    assert abs(total - (1 + r["return_pct"] / 100)) < 1e-9
