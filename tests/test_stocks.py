import json

from bot.portfolio import PortfolioParams, prepare, simulate
from bot.stocks import buy_and_hold, cagr, parse_stooq, parse_yahoo
from bot.strategy import StrategyParams

D = 86400 * 1000


def test_parse_stooq_csv():
    text = "Date,Open,High,Low,Close,Volume\n2006-01-03,124.1,125.9,123.5,125.7,73256700\n2006-01-04,125.8,126.5,125.6,126.2,51899600\n"
    c = parse_stooq(text)
    assert c[0] == [1136246400000, 124.1, 125.9, 123.5, 125.7, 73256700.0]
    assert c[1][0] - c[0][0] == D
    # индексы бывают без объёма
    assert parse_stooq("Date,Open,High,Low,Close\n2006-01-03,1,2,0.5,1.5\n")[0][5] == 0.0


def test_parse_stooq_rejects_garbage():
    import pytest
    with pytest.raises(ValueError):
        parse_stooq("Exceeded the daily hits limit")


def test_parse_yahoo_chart():
    payload = {"chart": {"result": [{
        "timestamp": [1136298600, 1136385000, 1136471400],
        "indicators": {"quote": [{"open": [124.1, None, 126.0], "high": [125.9, None, 127.0],
                                  "low": [123.5, None, 125.5], "close": [125.7, None, 126.8],
                                  "volume": [100, None, 300]}],
                       "adjclose": [{"adjclose": [90.0, None, 91.0]}]}}], "error": None}}
    c = parse_yahoo(json.dumps(payload))
    assert len(c) == 2                       # пропуск дня без цен
    assert c[0] == [1136246400000, 124.1, 125.9, 123.5, 125.7, 100]  # полночь UTC, без adjclose


def test_buy_and_hold_and_cagr():
    candles = [[i * D, 0, 0, 0, p, 0] for i, p in enumerate([100, 120, 60, 150])]
    r = buy_and_hold(candles, 0, 3 * D)
    assert abs(r["return_pct"] - 50) < 1e-9
    assert abs(r["max_drawdown_pct"] - 50) < 1e-9   # 120 -> 60
    assert abs(cagr(21, 0, 2 * 365.25 * D) - 10) < 1e-9


def test_long_only_filters_short_signals():
    import random
    random.seed(5)
    price, candles = 100.0, []
    for i in range(1500):
        o = price
        price *= 1 + random.gauss(0, 0.015)
        candles.append([i * D, o, max(o, price) * 1.004, min(o, price) * 0.996, price, 0])
    data = {"X": (candles, None, 0)}
    both = prepare(data, StrategyParams())
    longs = prepare(data, StrategyParams(), ("long",))
    sides_both = {s.side for lst in both[2].values() for _, _, s, _ in lst}
    sides_long = {s.side for lst in longs[2].values() for _, _, s, _ in lst}
    assert sides_both == {"long", "short"} and sides_long == {"long"}
    pp = PortfolioParams(risk_pct=1, leverage=1, target_pct=1e9, ruin_pct=100)
    assert all(t["side"] == "long" for t in simulate(longs, pp, stop_at_target=False)["trade_list"])
