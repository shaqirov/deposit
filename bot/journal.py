"""Журнал сделок в CSV."""
import csv
import os
from datetime import datetime, timezone

FIELDS = ["open_time", "close_time", "symbol", "side", "entry", "stop", "take",
          "amount", "pnl", "equity"]


def fmt_ts(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def append_trade(path, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def trade_row(symbol, signal, amount, open_ms, close_ms, pnl, equity):
    return {
        "open_time": fmt_ts(open_ms), "close_time": fmt_ts(close_ms), "symbol": symbol,
        "side": signal.side, "entry": f"{signal.entry:.6g}", "stop": f"{signal.stop:.6g}",
        "take": f"{signal.take:.6g}", "amount": f"{amount:.6g}", "pnl": f"{pnl:.4f}",
        "equity": f"{equity:.4f}",
    }
