import os
from dataclasses import dataclass

import ccxt
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
    pause_hours: float
    paper_balance: float
    news_file: str
    news_hours_before: float
    news_hours_after: float
    strategy: StrategyParams


def load_config():
    load_dotenv()
    env = os.getenv
    mode = env("MODE", "paper").lower()
    if mode not in ("paper", "testnet", "demo", "live"):
        raise ValueError(f"Неизвестный MODE={mode}")
    timeframe = env("TIMEFRAME", "4h")
    return Config(
        api_key=env("BINANCE_API_KEY", ""),
        api_secret=env("BINANCE_API_SECRET", ""),
        mode=mode,
        symbol=env("SYMBOL", "BTC/USDT:USDT"),
        timeframe=timeframe,
        leverage=int(env("LEVERAGE", "3")),
        risk_per_trade_pct=float(env("RISK_PER_TRADE_PCT", "1")),
        target_profit_pct=float(env("TARGET_PROFIT_PCT", "100")),
        max_drawdown_pct=float(env("MAX_DRAWDOWN_PCT", "25")),
        max_consecutive_losses=int(env("MAX_CONSECUTIVE_LOSSES", "3")),
        pause_hours=float(env("PAUSE_HOURS", "48")),
        paper_balance=float(env("PAPER_BALANCE", "100")),
        news_file=env("NEWS_FILE", "news.txt"),
        news_hours_before=float(env("NEWS_HOURS_BEFORE", "12")),
        news_hours_after=float(env("NEWS_HOURS_AFTER", "4")),
        strategy=StrategyParams(
            name=env("STRATEGY", "trend_pullback").lower(),
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
            trend_sma_days=int(env("TREND_SMA_DAYS", "200")),
            swing_lookback=int(env("SWING_LOOKBACK", "10")),
            pullback_ema=int(env("PULLBACK_EMA", "20")),
            allow_shorts=env("ALLOW_SHORTS", "true").lower() in ("1", "true", "yes"),
            max_funding_pct=float(env("MAX_FUNDING_PCT", "0.05")),
            candles_per_day=86400 / ccxt.Exchange.parse_timeframe(timeframe),
        ),
    )
