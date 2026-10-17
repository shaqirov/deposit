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


class PaperBroker:
    """Симуляция на реальных котировках. Стоп/тейк проверяются по high/low свечи."""

    def __init__(self, balance):
        self.balance = balance
        self.position = None

    def equity(self):
        return self.balance

    @property
    def in_position(self):
        return self.position is not None

    def open(self, signal, amount):
        self.balance -= signal.entry * amount * TAKER_FEE
        self.position = {"signal": signal, "amount": amount}
        log.info("[paper] открыта %s %.6f @ %.2f, стоп %.2f, тейк %.2f",
                 signal.side, amount, signal.entry, signal.stop, signal.take)

    def check_exit(self, high, low):
        """Возвращает PnL закрытой сделки или None, если позиция ещё открыта."""
        if not self.position:
            return None
        s, amount = self.position["signal"], self.position["amount"]
        if s.side == "long":
            # Если за свечу задеты оба уровня — считаем, что сработал стоп
            exit_price = s.stop if low <= s.stop else s.take if high >= s.take else None
        else:
            exit_price = s.stop if high >= s.stop else s.take if low <= s.take else None
        if exit_price is None:
            return None
        return self._close(exit_price)

    def close_now(self, price):
        """Закрыть позицию по рынку. Возвращает PnL."""
        return self._close(price) if self.position else None

    def unrealized_pnl(self, price):
        if not self.position:
            return 0.0
        s, amount = self.position["signal"], self.position["amount"]
        return (price - s.entry) * amount * (1 if s.side == "long" else -1)

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

    def to_state(self):
        pos = self.position
        return {"balance": self.balance,
                "position": {"signal": asdict(pos["signal"]), "amount": pos["amount"]} if pos else None}

    def load_state(self, state, with_position=True):
        self.balance = state["balance"]
        pos = state.get("position")
        if pos and with_position:
            self.position = {"signal": Signal(**pos["signal"]), "amount": pos["amount"]}


class BinanceBroker:
    """Реальные ордера: рыночный вход + стоп-лосс и тейк-профит с reduceOnly."""

    def __init__(self, exchange, symbol, leverage):
        self.ex = exchange
        self.symbol = symbol
        self.ex.load_markets()
        self.ex.set_leverage(leverage, symbol)
        try:
            self.ex.set_margin_mode("isolated", symbol)
        except ccxt.ExchangeError as e:  # уже isolated или есть открытые позиции
            log.debug("set_margin_mode: %s", e)
        self.in_position = self._has_position()
        self.equity_before = self.equity()

    def equity(self):
        bal = self.ex.fetch_balance()
        return float(bal["total"].get("USDT", 0))

    def _position(self):
        for p in self.ex.fetch_positions([self.symbol]):
            if abs(float(p.get("contracts") or 0)) > 0:
                return p
        return None

    def _has_position(self):
        return self._position() is not None

    def unrealized_pnl(self, price=None):
        p = self._position()
        return float(p.get("unrealizedPnl") or 0) if p else 0.0

    def close_now(self, price=None):
        """Закрыть позицию по рынку. PnL посчитает следующий check_exit."""
        p = self._position()
        if p:
            side = "sell" if p.get("side") == "long" else "buy"
            self.ex.create_order(self.symbol, "market", side, abs(float(p["contracts"])), None,
                                 {"reduceOnly": True})
            log.info("Позиция закрыта вручную по рынку")
        self.cancel_all()
        return None

    def to_state(self):
        return {"equity_before": self.equity_before}

    def load_state(self, state, with_position=True):
        if with_position and "equity_before" in state:
            self.equity_before = state["equity_before"]

    def open(self, signal, amount):
        amount = float(self.ex.amount_to_precision(self.symbol, amount))
        market = self.ex.market(self.symbol)
        min_amount = (market.get("limits", {}).get("amount", {}) or {}).get("min") or 0
        if amount <= 0 or amount < min_amount:
            log.warning("Объём %.8f меньше минимального %s — пропуск сигнала", amount, min_amount)
            return False
        side = "buy" if signal.side == "long" else "sell"
        close_side = "sell" if side == "buy" else "buy"
        self.equity_before = self.equity()
        order = self.ex.create_order(self.symbol, "market", side, amount)
        log.info("Открыта %s %s: %s", signal.side, amount, order.get("id"))
        stop = float(self.ex.price_to_precision(self.symbol, signal.stop))
        take = float(self.ex.price_to_precision(self.symbol, signal.take))
        try:
            self.ex.create_order(self.symbol, "market", close_side, amount, None,
                                 {"stopLossPrice": stop, "reduceOnly": True})
            self.ex.create_order(self.symbol, "market", close_side, amount, None,
                                 {"takeProfitPrice": take, "reduceOnly": True})
        except Exception:
            # Без защитного стопа позицию держать нельзя — закрываем сразу
            log.exception("Не удалось выставить стоп/тейк, закрываю позицию")
            self.ex.create_order(self.symbol, "market", close_side, amount, None,
                                 {"reduceOnly": True})
            raise
        self.in_position = True
        return True

    def check_exit(self, high=None, low=None):
        """Возвращает PnL, если позиция закрылась (сработал стоп или тейк)."""
        if not self.in_position or self._has_position():
            return None
        self.in_position = False
        self.cancel_all()
        pnl = self.equity() - self.equity_before
        log.info("Позиция закрыта, PnL %.4f USDT", pnl)
        return pnl

    def cancel_all(self):
        for params in ({}, {"trigger": True}):
            try:
                self.ex.cancel_all_orders(self.symbol, params)
            except ccxt.ExchangeError as e:
                log.debug("cancel_all_orders %s: %s", params, e)
