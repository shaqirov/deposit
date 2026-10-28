"""BinancePortfolio на поддельной бирже с интерфейсом ccxt."""
import pytest

from bot.broker import BinancePortfolio
from bot.strategy import Signal


class FakeCcxt:
    def __init__(self, fail_stop=False):
        self.orders, self.cancelled, self.leverage = [], [], {}
        self.open_positions = {}  # symbol -> contracts (со знаком)
        self.fail_stop = fail_stop
        self.trades = {}

    def load_markets(self):
        return {}

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.001}}}

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.3f}"

    def price_to_precision(self, symbol, price):
        return f"{price:.2f}"

    def set_leverage(self, lev, symbol):
        self.leverage[symbol] = lev

    def set_margin_mode(self, mode, symbol):
        pass

    def fetch_balance(self):
        return {"total": {"USDT": 100.0}}

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        params = params or {}
        self.orders.append((symbol, side, amount, params))
        if "stopLossPrice" in params and self.fail_stop:
            raise RuntimeError("stop rejected")
        if not ("stopLossPrice" in params or "takeProfitPrice" in params):
            delta = amount if side == "buy" else -amount
            self.open_positions[symbol] = self.open_positions.get(symbol, 0) + delta
            if abs(self.open_positions[symbol]) < 1e-12:
                del self.open_positions[symbol]
        return {"id": str(len(self.orders))}

    def fetch_positions(self, symbols):
        return [{"symbol": s, "contracts": abs(c), "side": "long" if c > 0 else "short",
                 "unrealizedPnl": 1.5} for s, c in self.open_positions.items() if s in symbols]

    def cancel_all_orders(self, symbol, params=None):
        self.cancelled.append((symbol, params))

    def fetch_my_trades(self, symbol, since=None):
        return self.trades.get(symbol, [])


SIG = Signal("long", 100.0, 95.0, 110.0)


def test_open_places_entry_stop_and_take():
    ex = FakeCcxt()
    b = BinancePortfolio(ex, leverage=10)
    assert b.open("SOL/USDT:USDT", SIG, 0.4, 2.0, 0, 1000)
    entry, stop, take = ex.orders
    assert entry[:3] == ("SOL/USDT:USDT", "buy", 0.4) and not entry[3]
    assert stop[1] == "sell" and stop[3] == {"stopLossPrice": 95.0, "reduceOnly": True}
    assert take[1] == "sell" and take[3] == {"takeProfitPrice": 110.0, "reduceOnly": True}
    assert ex.leverage["SOL/USDT:USDT"] == 10
    assert b.unrealized("SOL/USDT:USDT") == 1.5


def test_failed_stop_closes_position_immediately():
    ex = FakeCcxt(fail_stop=True)
    b = BinancePortfolio(ex, leverage=10)
    with pytest.raises(RuntimeError):
        b.open("SOL/USDT:USDT", SIG, 0.4, 2.0, 0, 1000)
    assert ex.open_positions == {}  # позиция без стопа закрыта по рынку
    assert ex.orders[-1][3] == {"reduceOnly": True}


def test_too_small_amount_is_skipped():
    ex = FakeCcxt()
    assert BinancePortfolio(ex, 10).open("SOL/USDT:USDT", SIG, 0.0001, 0.1, 0, 1000) is False
    assert ex.orders == []


def test_exit_detected_with_realized_pnl_and_orders_cancelled():
    ex = FakeCcxt()
    b = BinancePortfolio(ex, leverage=10)
    b.open("SOL/USDT:USDT", SIG, 0.4, 2.0, 0, 1000)
    b.open("ETH/USDT:USDT", Signal("short", 100.0, 105.0, 90.0), 0.4, 2.0, 0, 1000)
    assert b.check_exits() == []
    del ex.open_positions["SOL/USDT:USDT"]  # биржа закрыла по стопу
    ex.trades["SOL/USDT:USDT"] = [
        {"info": {"realizedPnl": "0"}, "fee": {"cost": 0.016}},
        {"info": {"realizedPnl": "-2.0"}, "fee": {"cost": 0.015}},
    ]
    [(symbol, pos, pnl)] = b.check_exits()
    assert symbol == "SOL/USDT:USDT" and pos["signal"] == SIG
    assert abs(pnl - (-2.031)) < 1e-9
    assert ("SOL/USDT:USDT", {}) in ex.cancelled and ("SOL/USDT:USDT", {"trigger": True}) in ex.cancelled
    assert list(b.positions) == ["ETH/USDT:USDT"]


def test_manual_close_and_state_roundtrip():
    ex = FakeCcxt()
    b = BinancePortfolio(ex, leverage=10)
    b.open("ETH/USDT:USDT", Signal("short", 100.0, 105.0, 90.0), 0.4, 2.0, 0, 1000)
    restored = BinancePortfolio(ex, leverage=10)
    restored.load_state(b.to_state())
    assert restored.positions["ETH/USDT:USDT"]["signal"].side == "short"
    assert restored.close("ETH/USDT:USDT") is None
    assert ex.orders[-1][:2] == ("ETH/USDT:USDT", "buy") and ex.orders[-1][3] == {"reduceOnly": True}
    assert [s for s, _, _ in restored.check_exits()] == ["ETH/USDT:USDT"]
