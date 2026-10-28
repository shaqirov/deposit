"""Исполнение сделок: бумажная торговля и Binance USDⓈ-M Futures через ccxt."""
import logging
from dataclasses import asdict

import ccxt

from .strategy import Signal

log = logging.getLogger(__name__)

TAKER_FEE = 0.0004  # 0.04% — комиссия тейкера на Binance Futures


def make_exchange(cfg):
    ex = ccxt.binanceusdm({
        "apiKey": cfg.api_key,
        "secret": cfg.api_secret,
        "enableRateLimit": True,
    })
    if cfg.mode == "testnet":
        ex.set_sandbox_mode(True)
    elif cfg.mode == "demo":
        ex.enable_demo_trading(True)
    return ex


def exit_price(signal, high, low):
    """Цена выхода, если свеча задела стоп или тейк; если оба — консервативно стоп."""
    if signal.side == "long":
        return signal.stop if low <= signal.stop else signal.take if high >= signal.take else None
    return signal.stop if high >= signal.stop else signal.take if low <= signal.take else None


class PaperBroker:
    """Одна позиция, симуляция по high/low свечей — для бэктеста."""

    def __init__(self, balance):
        self.balance = balance
        self.position = None

    def equity(self):
        return self.balance

    def open(self, signal, amount):
        self.balance -= signal.entry * amount * TAKER_FEE
        self.position = {"signal": signal, "amount": amount}
        log.info("[paper] открыта %s %.6f @ %.2f, стоп %.2f, тейк %.2f",
                 signal.side, amount, signal.entry, signal.stop, signal.take)

    def check_exit(self, high, low):
        """Возвращает PnL закрытой сделки или None, если позиция ещё открыта."""
        if not self.position:
            return None
        price = exit_price(self.position["signal"], high, low)
        return None if price is None else self._close(price)

    def _close(self, exit_price):
        s, amount = self.position["signal"], self.position["amount"]
        direction = 1 if s.side == "long" else -1
        gross = (exit_price - s.entry) * amount * direction
        exit_fee = exit_price * amount * TAKER_FEE
        self.balance += gross - exit_fee  # комиссия за вход списана при открытии
        self.position = None
        total = gross - exit_fee - s.entry * amount * TAKER_FEE
        log.info("[paper] закрыта по %.2f, PnL %.4f, баланс %.4f", exit_price, total, self.balance)
        return total


class PaperPortfolio:
    """Бумажная торговля несколькими монетами с общим депозитом."""

    def __init__(self, balance):
        self.balance = balance
        self.positions = {}  # symbol -> {signal, amount, risk, candle_ts, open_ms}

    def equity(self):
        return self.balance

    def open(self, symbol, signal, amount, risk, candle_ts, now_ms):
        self.balance -= signal.entry * amount * TAKER_FEE
        self.positions[symbol] = {"signal": signal, "amount": amount, "risk": risk,
                                  "candle_ts": candle_ts, "open_ms": now_ms}
        log.info("[paper] %s: открыта %s %.6g @ %.6g, стоп %.6g, тейк %.6g", symbol,
                 signal.side, amount, signal.entry, signal.stop, signal.take)
        return True

    def check_exits(self, ex, timeframe):
        """Закрыть позиции, у которых свечи после входа задели стоп или тейк.

        Возвращает [(symbol, позиция, pnl)]. Смотрим несколько последних свечей,
        чтобы не пропустить экстремум свечи, закрывшейся между опросами.
        """
        closed = []
        for symbol, pos in list(self.positions.items()):
            for c in ex.fetch_ohlcv(symbol, timeframe, limit=3):
                if c[0] <= pos["candle_ts"]:
                    continue
                price = exit_price(pos["signal"], c[2], c[3])
                if price is not None:
                    closed.append((symbol, pos, self._close(symbol, price)))
                    break
        return closed

    def close(self, symbol, price):
        """Закрыть по рынку. Возвращает PnL."""
        return self._close(symbol, price)

    def unrealized(self, symbol, price):
        pos = self.positions[symbol]
        s = pos["signal"]
        return (price - s.entry) * pos["amount"] * (1 if s.side == "long" else -1)

    def _close(self, symbol, price):
        pos = self.positions.pop(symbol)
        s, amount = pos["signal"], pos["amount"]
        gross = (price - s.entry) * amount * (1 if s.side == "long" else -1)
        exit_fee = price * amount * TAKER_FEE
        self.balance += gross - exit_fee  # комиссия за вход списана при открытии
        pnl = gross - exit_fee - s.entry * amount * TAKER_FEE
        log.info("[paper] %s: закрыта по %.6g, PnL %.4f, баланс %.4f", symbol, price, pnl, self.balance)
        return pnl

    def to_state(self):
        return {"balance": self.balance, "positions": _positions_to_state(self.positions)}

    def load_state(self, state):
        self.balance = state["balance"]
        self.positions = _positions_from_state(state.get("positions", {}))


class BinancePortfolio:
    """Реальные ордера на нескольких монетах: рыночный вход + стоп и тейк с reduceOnly."""

    def __init__(self, exchange, leverage):
        self.ex = exchange
        self.leverage = leverage
        self.ex.load_markets()
        self.positions = {}  # symbol -> {signal, amount, risk, candle_ts, open_ms}
        self.prepared = set()

    def equity(self):
        return float(self.ex.fetch_balance()["total"].get("USDT", 0))

    def _prepare(self, symbol):
        if symbol in self.prepared:
            return
        self.ex.set_leverage(self.leverage, symbol)
        try:
            self.ex.set_margin_mode("isolated", symbol)
        except ccxt.ExchangeError as e:  # уже isolated
            log.debug("set_margin_mode %s: %s", symbol, e)
        self.prepared.add(symbol)

    def open(self, symbol, signal, amount, risk, candle_ts, now_ms):
        amount = float(self.ex.amount_to_precision(symbol, amount))
        min_amount = (self.ex.market(symbol).get("limits", {}).get("amount", {}) or {}).get("min") or 0
        if amount <= 0 or amount < min_amount:
            log.warning("%s: объём %.8f меньше минимального %s — пропуск", symbol, amount, min_amount)
            return False
        self._prepare(symbol)
        side = "buy" if signal.side == "long" else "sell"
        close_side = "sell" if side == "buy" else "buy"
        order = self.ex.create_order(symbol, "market", side, amount)
        log.info("%s: открыта %s %s, ордер %s", symbol, signal.side, amount, order.get("id"))
        self.positions[symbol] = {"signal": signal, "amount": amount, "risk": risk,
                                  "candle_ts": candle_ts, "open_ms": now_ms}
        stop = float(self.ex.price_to_precision(symbol, signal.stop))
        take = float(self.ex.price_to_precision(symbol, signal.take))
        try:
            self.ex.create_order(symbol, "market", close_side, amount, None,
                                 {"stopLossPrice": stop, "reduceOnly": True})
            self.ex.create_order(symbol, "market", close_side, amount, None,
                                 {"takeProfitPrice": take, "reduceOnly": True})
        except Exception:
            # Без защитного стопа позицию держать нельзя — закрываем сразу
            log.exception("%s: не удалось выставить стоп/тейк, закрываю позицию", symbol)
            self.ex.create_order(symbol, "market", close_side, amount, None, {"reduceOnly": True})
            raise
        return True

    def _live(self):
        """Открытые на бирже позиции по нашим монетам: symbol -> позиция ccxt."""
        if not self.positions:
            return {}
        return {p["symbol"]: p for p in self.ex.fetch_positions(list(self.positions))
                if abs(float(p.get("contracts") or 0)) > 0}

    def check_exits(self, ex=None, timeframe=None):
        """Позиции, которые биржа закрыла (стоп, тейк или вручную): [(symbol, позиция, pnl)]."""
        live = self._live()
        closed = []
        for symbol in [s for s in self.positions if s not in live]:
            pos = self.positions.pop(symbol)
            self.cancel_all(symbol)
            pnl = self._realized_pnl(symbol, pos["open_ms"])
            log.info("%s: позиция закрыта, PnL %.4f USDT", symbol, pnl)
            closed.append((symbol, pos, pnl))
        return closed

    def _realized_pnl(self, symbol, since_ms):
        """PnL по сделкам биржи с момента входа: реализованный результат минус комиссии."""
        trades = self.ex.fetch_my_trades(symbol, since=since_ms - 60_000)
        pnl = sum(float((t.get("info") or {}).get("realizedPnl") or 0) for t in trades)
        fees = sum(float((t.get("fee") or {}).get("cost") or 0) for t in trades)
        return pnl - fees

    def close(self, symbol, price=None):
        """Закрыть по рынку. PnL посчитает следующий check_exits."""
        p = self._live().get(symbol)
        if p:
            side = "sell" if p.get("side") == "long" else "buy"
            self.ex.create_order(symbol, "market", side, abs(float(p["contracts"])), None,
                                 {"reduceOnly": True})
            log.info("%s: позиция закрыта вручную по рынку", symbol)
        return None

    def unrealized(self, symbol, price=None):
        p = self._live().get(symbol)
        return float(p.get("unrealizedPnl") or 0) if p else 0.0

    def cancel_all(self, symbol):
        for params in ({}, {"trigger": True}):
            try:
                self.ex.cancel_all_orders(symbol, params)
            except ccxt.ExchangeError as e:
                log.debug("cancel_all_orders %s %s: %s", symbol, params, e)

    def to_state(self):
        return {"positions": _positions_to_state(self.positions)}

    def load_state(self, state):
        self.positions = _positions_from_state(state.get("positions", {}))


def _positions_to_state(positions):
    return {s: {**p, "signal": asdict(p["signal"])} for s, p in positions.items()}


def _positions_from_state(data):
    return {s: {**p, "signal": Signal(**p["signal"])} for s, p in data.items()}
