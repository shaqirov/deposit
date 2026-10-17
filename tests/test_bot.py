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
