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


def scripted(candles):
    """Позиция long: вход 100, стоп 95 (R = 5), тейк 110; дальше — заданные свечи (high, low)."""
    from bot.strategy import Signal
    sig = Signal("long", 100.0, 95.0, 110.0)
    timestamps = [0] + [(i + 1) * H for i in range(len(candles))]
    candles_at = {0: {"X": [0, 100, 100, 100, 100, 1]}}
    close = 100.0
    for ts, (high, low) in zip(timestamps[1:], candles):
        # открытие = закрытие предыдущей свечи, без ценовых разрывов
        open_ = min(max(close, low), high)
        close = (high + low) / 2
        candles_at[ts] = {"X": [ts, open_, high, low, close, 1]}
    return timestamps, candles_at, {0: [(0, "X", sig, 0)]}


def run_mode(mode, candles):
    pp = PortfolioParams(risk_pct=1, max_positions=6, max_total_risk_pct=100, leverage=10,
                         target_pct=1e9, ruin_pct=100, exit_mode=mode)
    r = simulate(scripted(candles), pp, stop_at_target=False)
    return [round(t["r"], 2) for t in r["trade_list"]]


def test_exit_modes_on_scripted_path():
    # +1.2R, откат к +0.1R, потом рост до +4R
    path = [(106, 101), (104, 100.5), (120, 103)]
    assert run_mode("fixed", path) == [1.98]        # тейк 2R минус комиссии
    assert run_mode("breakeven", path) == [1.98]    # безубыток не задет, тейк
    assert run_mode("trail", path) == [0.18]        # стоп подтянут до 101 и задет на откате
    assert run_mode("trail_notake", path) == [0.18]
    # без отката трейлинг без тейка забирает большее движение
    trend = [(106, 101), (112, 105), (118, 111), (124, 117), (125, 112)]
    assert run_mode("fixed", trend) == [1.98]
    # стоп 101 -> 107 -> 113 -> 119, пятая свеча задевает 119: +3.8R минус комиссии
    assert run_mode("trail_notake", trend) == [3.78]
    # разворот сразу: все режимы теряют 1R
    for mode in ("fixed", "breakeven", "trail", "trail_notake"):
        assert run_mode(mode, [(101, 94)]) == [-1.02]


def test_stop_moves_only_after_candle_close_and_only_forward():
    from bot.exits import next_stop
    # +0.8R — ещё не активировано
    assert next_stop("trail", "long", 100, 95, 95, 104) == 95
    # +2R — стоп на best - 1R
    assert next_stop("trail", "long", 100, 95, 95, 110) == 105
    # откат не опускает стоп
    assert next_stop("trail", "long", 100, 95, 105, 107) == 105
    # шорт зеркально
    assert next_stop("trail", "short", 100, 105, 105, 90) == 95
    assert next_stop("breakeven", "short", 100, 105, 105, 94) == 100 * (1 - 0.001)


def test_breakeven_frees_risk_budget():
    from bot.exits import open_risk
    assert open_risk("long", 100, 95, 2) == 10
    assert open_risk("long", 100, 100.1, 2) == 0
    assert open_risk("short", 100, 99, 2) == 0


def test_gap_through_stop_exits_at_open_and_slippage_costs():
    from bot.exits import hit_price
    assert hit_price("long", 95, 110, 99, 90, open_=92) == 92   # открылась ниже стопа
    assert hit_price("long", 95, 110, 99, 94, open_=98) == 95
    assert hit_price("short", 105, 90, 112, 101, open_=108) == 108
    path = [(101, 94)]

    def r_with(slippage):
        pp = PortfolioParams(risk_pct=1, max_positions=6, max_total_risk_pct=100, leverage=10,
                             target_pct=1e9, ruin_pct=100, slippage_pct=slippage)
        return simulate(scripted(path), pp, stop_at_target=False)["trade_list"][0]["r"]
    base, slipped = r_with(0), r_with(0.1)
    # 0.1% от 100 на входе и от 95 на выходе при объёме 0.2 = 0.039 USDT = 0.039R
    assert abs((slipped - base) - (-0.039)) < 1e-9


def fine_walk(n, seed, steps=24, vol=0.012):
    """Свечи из мелких шагов честного случайного блуждания: high/low — настоящие экстремумы."""
    rnd = random.Random(seed)
    s = vol / steps ** 0.5
    price, out = 100.0, []
    for i in range(n):
        o = hi = lo = price
        for _ in range(steps):
            price += price * rnd.gauss(0, s)
            hi, lo = max(hi, price), min(lo, price)
        out.append([i * H, o, hi, lo, price, 1.0])
    return out


def test_no_exit_mode_profits_on_a_random_walk():
    """На рынке без тренда никакой способ выхода не может зарабатывать заметно больше нуля:
    иначе бэктест заглядывает в будущее или завышает исполнение."""
    from statistics import mean
    for mode in ("fixed", "breakeven", "trail", "trail_notake"):
        rs = []
        for seed in range(6):
            data = {f"S{k}": (fine_walk(2000, seed * 10 + k), None, 0) for k in range(4)}
            pp = PortfolioParams(risk_pct=1, max_positions=6, max_total_risk_pct=20, leverage=10,
                                 target_pct=1e9, ruin_pct=100, exit_mode=mode)
            rs += [t["r"] for t in simulate(prepare(data, StrategyParams()), pp,
                                            stop_at_target=False)["trade_list"]]
        assert len(rs) > 300
        assert mean(rs) < 0.06, (mode, mean(rs))
