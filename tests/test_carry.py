from bot.carry import CarryParams, cycle_cost_pct, notional_for, simulate

H8 = 8 * 3600 * 1000


def series(rates, start=0, step=H8):
    return [(start + i * step, r) for i, r in enumerate(rates)]


def test_static_hold_matches_hand_calculation():
    # 0.01% за 8 часов весь год: шорт на 75 USDT (капитал 100, плечо 3)
    funding = {"BTC/USDT:USDT": series([0.0001] * 1095)}
    r = simulate(funding, CarryParams(static=["BTC/USDT:USDT"]))
    notional = notional_for(100, 3)
    assert notional == 75
    costs = 2 * notional * cycle_cost_pct(CarryParams()) / 100  # вход и выход
    # первая выплата приходит раньше решения о входе — её не получаем
    expected = 100 + notional * 0.0001 * 1094 - costs
    assert abs(r["end"] - expected) < 1e-6
    assert abs(r["costs"] - costs) < 1e-9 and r["paid"] == 0


def test_rotation_enters_high_funding_and_exits_when_it_turns_negative():
    n = 300
    high_then_negative = [0.0005] * 150 + [-0.0003] * 150   # ~55% годовых, потом минус
    low = [0.00001] * n                                       # ~1% годовых — ниже порога
    funding = {"A/USDT:USDT": series(high_then_negative), "B/USDT:USDT": series(low)}
    r = simulate(funding, CarryParams(max_coins=1, enter_apr=15, exit_apr=0))
    assert r["rotations"] == 1          # вышли из A, в B не входили
    assert r["received"] > 0
    # после разворота ставки платили не дольше, чем нужно, чтобы средняя за 3 дня ушла в минус
    assert r["paid"] < 75 * 0.0003 * 10
    assert r["end"] > 100


def test_no_payment_is_collected_before_it_could_be_known():
    # ставка ноль, затем одна огромная выплата — войти «под неё» нельзя
    rates = [0.0] * 30 + [0.05] + [0.0] * 30
    r = simulate({"C/USDT:USDT": series(rates)}, CarryParams(max_coins=1, enter_apr=15))
    assert r["received"] == 0


def test_negative_funding_is_paid_when_held_statically():
    r = simulate({"BTC/USDT:USDT": series([-0.0001] * 100)}, CarryParams(static=["BTC/USDT:USDT"]))
    assert r["paid"] > 0 and r["received"] == 0 and r["end"] < 100
