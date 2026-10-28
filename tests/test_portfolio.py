import random

from bot.backtest import run_backtest
from bot.config import load_config
from bot.portfolio import PortfolioParams, acceleration_odds, prepare, simulate
from bot.strategy import StrategyParams

H = 4 * 3600 * 1000


def walk(n, seed, drift=0.0003, vol=0.012, start=100.0):
    random.seed(seed)
    price, out = start, []
    for i in range(n):
        o = price
        price *= 1 + random.gauss(drift, vol)
        out.append([i * H, o, max(o, price) * 1.004, min(o, price) * 0.996, price, 1.0])
    return out


def loose(risk=1.0, **kw):
    """Без портфельных лимитов — как одиночный бэктест."""
    return PortfolioParams(risk_pct=risk, max_positions=100, max_total_risk_pct=1000,
                           leverage=3, target_pct=1e9, ruin_pct=100, **kw)


def test_single_symbol_matches_classic_backtest():
    candles = walk(3000, seed=11)
    cfg = load_config()
    cfg.strategy = StrategyParams()
    cfg.risk_per_trade_pct, cfg.leverage = 1.0, 3
    cfg.pause_hours, cfg.max_consecutive_losses = 0, 10 ** 6
    classic = run_backtest(candles, cfg, stop_on_guard=False)
    prepared = prepare({"X": (candles, None, 0)}, cfg.strategy)
    port = simulate(prepared, loose(), start_ts=candles[cfg.strategy.warmup][0], stop_at_target=False)
    assert port["trades"] == classic["trades"] > 10
    assert abs(port["end"] - classic["end"]) < 1e-6


def portfolio(n_symbols=8, n=2500):
    data = {f"S{k}": (walk(n, seed=k), None, 5.0) for k in range(n_symbols)}
    return prepare(data, StrategyParams(trend_ema=50))


def test_limits_are_respected():
    prepared = portfolio()
    pp = PortfolioParams(risk_pct=5, max_positions=3, max_total_risk_pct=12, leverage=10,
                         target_pct=1e9, ruin_pct=100)
    r = simulate(prepared, pp, stop_at_target=False)
    trades = r["trade_list"]
    assert trades
    # одновременно открытых позиций не больше 2: 3-я превысила бы 12% риска (3 * 5%)
    events = sorted([(t["open_ts"], 1) for t in trades] + [(t["close_ts"], -1) for t in trades],
                    key=lambda e: (e[0], e[1]))
    open_now = peak = 0
    for _, d in events:
        open_now += d
        peak = max(peak, open_now)
    assert peak <= 2


def test_min_notional_skips_small_positions():
    candles = walk(2000, seed=3)
    tiny = prepare({"X": (candles, None, 1e9)}, StrategyParams())
    assert simulate(tiny, loose(), stop_at_target=False)["trades"] == 0


def test_target_and_ruin_outcomes():
    prepared = portfolio()
    hit = simulate(prepared, PortfolioParams(risk_pct=5, target_pct=1, ruin_pct=99))
    assert hit["outcome"] == "target" and hit["return_pct"] >= 1
    ruin = simulate(prepared, PortfolioParams(risk_pct=5, target_pct=1e9, ruin_pct=1))
    assert ruin["outcome"] == "ruin" and ruin["return_pct"] <= -1


def test_acceleration_odds_sum_to_100():
    prepared = portfolio()
    o = acceleration_odds(prepared, PortfolioParams(risk_pct=3, target_pct=20, ruin_pct=20),
                          horizon_days=60, step_days=30, first_ts=prepared[0][0] + 60 * H)
    assert o["runs"] > 5
    assert abs(o["target_pct"] + o["ruin_pct"] + o["neither_pct"] - 100) < 1e-9
