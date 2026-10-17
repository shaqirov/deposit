import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .strategy import StrategyParams


@dataclass
class Config:
    api_key: str
    api_secret: str
    mode: str
    symbol: str
    timeframe: str
    leverage: int
    risk_per_trade_pct: float
    target_profit_pct: float
    max_drawdown_pct: float
    max_consecutive_losses: int
    paper_balance: float
    strategy: StrategyParams


def load_config():
    load_dotenv()
    env = os.getenv
    mode = env("MODE", "paper").lower()
    if mode not in ("paper", "testnet", "demo", "live"):
        raise ValueError(f"Неизвестный MODE={mode}")
    return Config(
        api_key=env("BINANCE_API_KEY", ""),
        api_secret=env("BINANCE_API_SECRET", ""),
        mode=mode,
        symbol=env("SYMBOL", "BTC/USDT:USDT"),
        timeframe=env("TIMEFRAME", "5m"),
        leverage=int(env("LEVERAGE", "10")),
        risk_per_trade_pct=float(env("RISK_PER_TRADE_PCT", "2")),
        target_profit_pct=float(env("TARGET_PROFIT_PCT", "100")),
        max_drawdown_pct=float(env("MAX_DRAWDOWN_PCT", "30")),
        max_consecutive_losses=int(env("MAX_CONSECUTIVE_LOSSES", "4")),
        paper_balance=float(env("PAPER_BALANCE", "100")),
        strategy=StrategyParams(
            name=env("STRATEGY", "ema_cross").lower(),
            ema_fast=int(env("EMA_FAST", "9")),
            ema_slow=int(env("EMA_SLOW", "21")),
            rsi_period=int(env("RSI_PERIOD", "14")),
            atr_period=int(env("ATR_PERIOD", "14")),
            stop_atr_mult=float(env("STOP_ATR_MULT", "1.5")),
            take_profit_rr=float(env("TAKE_PROFIT_RR", "2.0")),
            donchian_period=int(env("DONCHIAN_PERIOD", "20")),
            trend_ema=int(env("TREND_EMA", "200")),
            bb_period=int(env("BB_PERIOD", "20")),
            bb_std=float(env("BB_STD", "2.0")),
        ),
    )
