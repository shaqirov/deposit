"""Бэктест той же трендовой стратегии на акциях и ETF за долгую историю (дневные свечи).

Пример: python -m bot.stocks --universe etf --years 20

Данные — дневные цены из открытых источников (Stooq, при неудаче — Yahoo Finance).
Сравнение — с простой покупкой и удержанием S&P 500 (SPY) за тот же период.

Ограничения:
* набор акций выбран сегодня — компании, которые за эти годы обанкротились или
  выпали из индексов, в него не попали, поэтому результат на акциях завышен;
  набор ETF от этого почти не страдает;
* дивиденды не учтены ни у стратегии, ни у сравнения (у SPY это ~1.5–2% в год) —
  обе считаются по одним и тем же ценам, так что сравнение между ними честное;
* шорт акций у брокера стоит денег (плата за заём), здесь не учтена.
"""
import argparse
import json
import os
import statistics
import time
import urllib.request
from datetime import datetime, timezone

from .config import load_config
from .portfolio import CACHE_DIR, DAY_MS, PortfolioParams, prepare, simulate

UNIVERSES = {
    # Индексы, отрасли, другие страны, облигации, сырьё — набор не зависит от успеха отдельных компаний
    "etf": ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU",
            "XLB", "EFA", "EEM", "EWJ", "EWZ", "FXI", "TLT", "IEF", "GLD", "SLV", "USO", "VNQ"],
    # Крупные компании — есть и выросшие, и отставшие; выбор всё равно задним числом
    "stocks": ["AAPL", "MSFT", "AMZN", "GOOGL", "NVDA", "META", "TSLA", "JPM", "JNJ", "XOM",
               "PG", "KO", "PEP", "WMT", "HD", "BAC", "INTC", "CSCO", "ORCL", "IBM", "GE", "PFE",
               "MRK", "CVX", "DIS", "MCD", "T", "VZ", "C", "WFC"],
}
USER_AGENT = "Mozilla/5.0 (compatible; deposit-backtest)"
YEAR_MS = 365.25 * DAY_MS


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _day_ms(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def parse_stooq(text):
    """CSV Stooq: Date,Open,High,Low,Close[,Volume] -> свечи [ts, o, h, l, c, v]."""
    lines = [x.strip() for x in text.strip().splitlines() if x.strip()]
    if not lines or not lines[0].lower().startswith("date,open,high,low,close"):
        raise ValueError(f"Stooq: неожиданный ответ: {text[:80]!r}")
    out = []
    for line in lines[1:]:
        parts = line.split(",")
        try:
            o, h, l, c = (float(x) for x in parts[1:5])
        except ValueError:
            continue
        v = float(parts[5]) if len(parts) > 5 and parts[5] else 0.0
        out.append([_day_ms(parts[0]), o, h, l, c, v])
    return out


def parse_yahoo(text):
    """JSON Yahoo chart -> свечи. Цены Yahoo уже учитывают сплиты; дивиденды (adjclose)
    не применяем — чтобы данные совпадали по смыслу с данными Stooq."""
    data = json.loads(text)
    result = ((data.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise ValueError(f"Yahoo: нет данных: {text[:80]!r}")
    q = result["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(result.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c) or c <= 0:
            continue
        day = ts // 86400 * 86400 * 1000  # к полуночи UTC — как у Stooq
        out.append([day, o, h, l, c, q["volume"][i] or 0.0])
    return out


def fetch_daily(ticker, years):
    """Дневные свечи за последние `years` лет: Stooq, при неудаче — Yahoo."""
    errors = []
    try:
        candles = parse_stooq(_get(f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"))
        if candles:
            since = candles[-1][0] - years * YEAR_MS
            return [c for c in candles if c[0] >= since], "stooq"
    except Exception as e:
        errors.append(f"stooq: {e}")
    try:
        candles = parse_yahoo(_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                                   f"?range={years}y&interval=1d"))
        if candles:
            return candles, "yahoo"
    except Exception as e:
        errors.append(f"yahoo: {e}")
    raise RuntimeError("; ".join(errors) or "нет данных")


def cached_daily(ticker, years):
    os.makedirs(CACHE_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = os.path.join(CACHE_DIR, f"stock_{ticker}_{years}_{day}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d["candles"], d["source"]
    candles, source = fetch_daily(ticker, years)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"candles": candles, "source": source}, f)
    time.sleep(1)  # не нагружать бесплатный источник
    return candles, source


def buy_and_hold(candles, start_ts, end_ts):
    """Итог, % в год и макс. просадка при покупке на start_ts и удержании до end_ts."""
    closes = [c[4] for c in candles if start_ts <= c[0] <= end_ts]
    if len(closes) < 2:
        return None
    peak, dd = closes[0], 0.0
    for x in closes:
        peak = max(peak, x)
        dd = max(dd, (peak - x) / peak * 100)
    total = closes[-1] / closes[0]
    years = (end_ts - start_ts) / YEAR_MS
    return {"return_pct": (total - 1) * 100, "cagr_pct": (total ** (1 / years) - 1) * 100,
            "max_drawdown_pct": dd}


def cagr(return_pct, start_ts, end_ts):
    years = max((end_ts - start_ts) / YEAR_MS, 1e-9)
    growth = 1 + return_pct / 100
    return (growth ** (1 / years) - 1) * 100 if growth > 0 else -100.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="etf", choices=sorted(UNIVERSES))
    ap.add_argument("--tickers", default="", help="свой список тикеров через запятую")
    ap.add_argument("--years", type=int, default=20)
    ap.add_argument("--risk", type=float, default=1.0, help="риск на сделку, %%")
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--max-total-risk", type=float, default=10.0)
    ap.add_argument("--leverage", type=float, default=1.0, help="1 — без плеча, как на обычном счёте")
    ap.add_argument("--slippage", type=float, default=0.05)
    args = ap.parse_args()

    cfg = load_config()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or UNIVERSES[args.universe]
    data, sources = {}, {}
    for i, t in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {t}...", flush=True)
        try:
            candles, src = cached_daily(t, args.years)
        except Exception as e:
            print(f"  пропуск: {e}")
            continue
        if len(candles) <= cfg.strategy.warmup + 50:
            print("  мало истории, пропуск")
            continue
        data[t], sources[t] = (candles, None, 0), src
    if "SPY" not in data:
        try:
            spy, _ = cached_daily("SPY", args.years)
        except Exception as e:
            raise SystemExit(f"Не удалось загрузить SPY для сравнения: {e}")
    else:
        spy = data["SPY"][0]
    print(f"Источник данных: " + ", ".join(f"{s}: {sum(1 for v in sources.values() if v == s)}"
                                         for s in sorted(set(sources.values()))))

    timestamps = sorted({c[0] for candles, _, _ in data.values() for c in candles})
    first_ts = timestamps[min(cfg.strategy.warmup, len(timestamps) - 1)]
    end_ts = timestamps[-1]
    years = (end_ts - first_ts) / YEAR_MS
    print(f"\nНабор: {args.universe if not args.tickers else 'свой'}, {len(data)} тикеров, "
          f"период {datetime.fromtimestamp(first_ts / 1000, timezone.utc):%Y-%m} — "
          f"{datetime.fromtimestamp(end_ts / 1000, timezone.utc):%Y-%m} ({years:.1f} г.)")
    print(f"Риск {args.risk:g}% на сделку, до {args.max_positions} позиций, суммарный риск до "
          f"{args.max_total_risk:g}%, плечо x{args.leverage:g}, проскальзывание {args.slippage:g}%\n")

    variants = []
    for sides, sides_name in ((("long",), "только лонг"), (("long", "short"), "лонг+шорт")):
        prepared = prepare(data, cfg.strategy, sides)
        for mode, a, t, mode_name in (("fixed", 1, 1, "тейк 2R"), ("trail_notake", 1, 2, "трейлинг 1/2")):
            pp = PortfolioParams(risk_pct=args.risk, max_positions=args.max_positions,
                                 max_total_risk_pct=args.max_total_risk, leverage=args.leverage,
                                 target_pct=1e9, ruin_pct=100, exit_mode=mode, activate_r=a,
                                 trail_r=t, slippage_pct=args.slippage)
            variants.append((f"{sides_name}, {mode_name}", prepared, pp))

    print(f"{'Вариант':<28}{'Итог %':>9}{'В год %':>9}{'Просадка':>10}{'Сделок':>8}{'Винрейт':>9}"
          f"{'1-я пол.':>10}{'2-я пол.':>10}")
    for name, prepared, pp in variants:
        r = simulate(prepared, pp, first_ts, stop_at_target=False)
        print(f"{name:<28}{r['return_pct']:>+9.1f}{cagr(r['return_pct'], first_ts, end_ts):>+9.1f}"
              f"{r['max_drawdown_pct']:>9.1f}%{r['trades']:>8}{r['winrate_pct']:>8.1f}%"
              f"{r['first_half_pct']:>+10.1f}{r['second_half_pct']:>+10.1f}")
    bh = buy_and_hold(spy, first_ts, end_ts)
    print(f"{'S&P 500 купить и держать':<28}{bh['return_pct']:>+9.1f}{bh['cagr_pct']:>+9.1f}"
          f"{bh['max_drawdown_pct']:>9.1f}%")

    # Устойчивость по периодам: % в год в каждом 5-летнем отрезке
    blocks = []
    b = first_ts
    while b < end_ts - YEAR_MS:
        blocks.append((b, min(b + 5 * YEAR_MS, end_ts)))
        b += 5 * YEAR_MS
    print("\n% в год по 5-летним отрезкам (просадка в отрезке не показана):")
    print(f"{'Вариант':<28}" + "".join(
        f"{datetime.fromtimestamp(s / 1000, timezone.utc):%Y}-{datetime.fromtimestamp(e / 1000, timezone.utc):%y}".rjust(10)
        for s, e in blocks))
    for name, prepared, pp in variants:
        row = []
        for s, e in blocks:
            r = simulate(prepared, pp, int(s), int(e), stop_at_target=False)
            row.append(cagr(r["return_pct"], s, e))
        print(f"{name:<28}" + "".join(f"{x:>+10.1f}" for x in row))
    row = [buy_and_hold(spy, s, e)["cagr_pct"] for s, e in blocks]
    print(f"{'S&P 500 купить и держать':<28}" + "".join(f"{x:>+10.1f}" for x in row))

    print("\nНе учтены дивиденды (у SPY ~1.5–2% в год) и плата за заём акций при шорте. "
          "Набор акций выбран задним числом — результат на нём завышен; на ETF — честнее.")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
