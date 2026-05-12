#!/usr/bin/env python3
"""
ORACLE Data Layer — oracle_data.py
===================================
Single interface for all market data in the ORACLE system.
All other modules call this. Nothing calls yfinance directly.

Cache layout (~/ORACLE/cache/):
  prices_YYYYMMDD.json        — intraday price snapshots, keyed by today's date
  fundamentals_YYYYMMDD.json  — 24hr TTL fundamentals, expires at midnight by filename
"""

import os, json, datetime, time
from pathlib import Path

CACHE_DIR = Path(os.path.expanduser("~/ORACLE/cache"))


def _today_str() -> str:
    return datetime.date.today().strftime("%Y%m%d")


def _today_iso() -> str:
    return datetime.date.today().isoformat()


# ── Price cache ───────────────────────────────────────────────────────────────

def _price_cache_path() -> Path:
    return CACHE_DIR / f"prices_{_today_str()}.json"


def _load_price_cache() -> dict:
    path = _price_cache_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _save_price_cache(data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _price_cache_path().write_text(json.dumps(data))


def get_price(ticker: str, fresh: bool = False) -> dict:
    """
    Return {"ticker", "price", "timestamp", "source"} or
           {"ticker", "price": None, "error": "fetch_failed"}.
    Checks today's prices_YYYYMMDD.json cache first.
    """
    ticker = ticker.upper()
    cache = _load_price_cache()

    if not fresh and ticker in cache and cache[ticker].get("price"):
        return cache[ticker]

    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            fi = yf.Ticker(ticker).fast_info
            price = getattr(fi, "last_price", None)
        if not price:
            return {"ticker": ticker, "price": None, "error": "fetch_failed"}

        result = {
            "ticker":    ticker,
            "price":     float(price),
            "timestamp": datetime.datetime.now().isoformat(),
            "source":    "yfinance_info",
        }
        cache[ticker] = result
        _save_price_cache(cache)
        return result
    except Exception:
        return {"ticker": ticker, "price": None, "error": "fetch_failed"}


# ── Fundamentals cache ────────────────────────────────────────────────────────

def _fund_cache_path() -> Path:
    return CACHE_DIR / f"fundamentals_{_today_str()}.json"


def _load_fund_cache() -> dict:
    path = _fund_cache_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            # New format: {"generated": ..., "ttl_hours": 24, "data": {...}}
            if isinstance(raw, dict) and "data" in raw:
                return raw["data"]
            # Old flat format — migrate transparently
            return raw
        except Exception:
            pass
    return {}


def _save_fund_cache(data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wrapper = {"generated": _today_iso(), "ttl_hours": 24, "data": data}
    _fund_cache_path().write_text(json.dumps(wrapper))


def get_fundamentals(ticker: str, fresh: bool = False) -> dict:
    """
    Return dict with: ticker, price, market_cap, revenue_growth_yoy, eps_ttm,
    eps_forward, week52_high, week52_low, analyst_target, short_interest_pct,
    sector, industry.
    Graceful None for missing fields. Never crashes.
    Cache: fundamentals_YYYYMMDD.json (24hr TTL by date in filename).
    """
    ticker = ticker.upper()
    cache = _load_fund_cache()

    if not fresh and ticker in cache and not cache[ticker].get("error"):
        return cache[ticker]

    try:
        import yfinance as yf
        tkr = yf.Ticker(ticker)
        info = tkr.info

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            fi = tkr.fast_info
            price = getattr(fi, "last_price", None)
        if not price:
            cache[ticker] = {"ticker": ticker, "error": True}
            _save_fund_cache(cache)
            return {"ticker": ticker, "price": None, "error": "fetch_failed"}

        price = float(price)

        # Revenue growth: quarterly YoY (iloc[0] vs iloc[4]).
        # NEVER use info["revenueGrowth"] — it uses stale annual data.
        rev_growth = None
        try:
            q_fin = tkr.quarterly_income_stmt
            if q_fin is not None and not q_fin.empty:
                for label in ("Total Revenue", "Revenue"):
                    if label in q_fin.index:
                        rev_row = q_fin.loc[label].dropna().sort_index(ascending=False)
                        if len(rev_row) >= 5:
                            r0 = float(rev_row.iloc[0])
                            r4 = float(rev_row.iloc[4])
                            if r4 and abs(r4) > 0:
                                rev_growth = (r0 - r4) / abs(r4) * 100
                        break
        except Exception:
            pass

        short_raw = info.get("shortPercentOfFloat")
        if short_raw is not None:
            short_pct = short_raw * 100 if short_raw <= 1.0 else float(short_raw)
        else:
            short_pct = None

        result = {
            "ticker":             ticker,
            "price":              price,
            "market_cap":         info.get("marketCap"),
            "revenue_growth_yoy": rev_growth,
            "eps_ttm":            info.get("trailingEps"),
            "eps_forward":        info.get("forwardEps"),
            "week52_high":        info.get("fiftyTwoWeekHigh"),
            "week52_low":         info.get("fiftyTwoWeekLow"),
            "analyst_target":     info.get("targetMeanPrice"),
            "short_interest_pct": short_pct,
            "sector":             info.get("sector"),
            "industry":           info.get("industry"),
        }
        cache[ticker] = result
        _save_fund_cache(cache)
        return result

    except Exception:
        return {"ticker": ticker, "price": None, "error": "fetch_failed"}


def get_fundamentals_batch(tickers: list, fresh: bool = False) -> dict:
    """
    Fetch fundamentals for each ticker. Prints progress.
    Returns {ticker: fundamentals_dict}.
    """
    results = {}
    for ticker in tickers:
        print(f"  Fetching {ticker}...")
        results[ticker] = get_fundamentals(ticker, fresh=fresh)
        time.sleep(0.3)
    return results


# ── News ──────────────────────────────────────────────────────────────────────

def get_news(ticker: str, max_headlines: int = 3) -> list:
    """
    Fetch recent news headlines (last 30 days). No caching — always live.
    Returns [] gracefully on any failure.
    """
    try:
        import yfinance as yf
        cutoff = datetime.datetime.now() - datetime.timedelta(days=30)
        news = yf.Ticker(ticker.upper()).news or []
        headlines = []
        for item in news:
            ts = item.get("providerPublishTime", 0)
            if ts and datetime.datetime.fromtimestamp(ts) < cutoff:
                continue
            title = item.get("title", "").strip()
            if title:
                headlines.append(title)
            if len(headlines) >= max_headlines:
                break
        return headlines
    except Exception:
        return []


# ── Validation ────────────────────────────────────────────────────────────────

def validate_price_vs_screener(ticker: str, screener_price: float, fundamentals: dict) -> bool:
    """
    Returns False if |screener_price - fundamentals["price"]| / screener_price > 10%.
    Returns True if within 10% or no data available to compare.
    """
    if not fundamentals or not isinstance(fundamentals, dict):
        return True
    fund_price = fundamentals.get("price")
    if not fund_price or not screener_price or screener_price == 0:
        return True
    return abs(screener_price - fund_price) / screener_price <= 0.10


# ── Formatting ────────────────────────────────────────────────────────────────

def format_fundamentals_block(ticker: str, data: dict) -> str:
    """Return a formatted text block for one stock (drop-in for Think Tank context)."""
    if not data or data.get("error") or data.get("price") is None:
        return f"{ticker} - yfinance unavailable for {ticker} — using limited data"

    price = data.get("price", 0)

    mkt_cap = data.get("market_cap")
    cap_str = f"${mkt_cap / 1e9:.1f}B" if mkt_cap else "N/A"

    rev_g = data.get("revenue_growth_yoy")
    rev_str = f"{rev_g:+.1f}%" if rev_g is not None else "N/A"

    eps_ttm = data.get("eps_ttm")
    eps_fwd = data.get("eps_forward")
    eps_str = f"${eps_ttm:.2f}" if eps_ttm is not None else "N/A"
    fwd_str = f"${eps_fwd:.2f}" if eps_fwd is not None else "N/A"

    hi = data.get("week52_high")
    lo = data.get("week52_low")
    rng_str = f"${lo:.2f} - ${hi:.2f}" if (hi and lo) else "N/A"

    target = data.get("analyst_target")
    if target and price:
        upside = (target - price) / price * 100
        tgt_str = f"${target:.2f} ({upside:+.0f}% upside)"
    else:
        tgt_str = "N/A"

    short = data.get("short_interest_pct")
    short_str = f"{short:.1f}%" if short is not None else "N/A"

    sector = data.get("sector") or "N/A"

    return (
        f"{ticker} - ${price:.2f} ({cap_str})\n"
        f"Revenue Growth (YoY MRQ): {rev_str}\n"
        f"EPS TTM: {eps_str} | Forward EPS: {fwd_str}\n"
        f"52-week range: {rng_str}\n"
        f"Analyst target: {tgt_str}\n"
        f"Short interest: {short_str}\n"
        f"Sector: {sector}"
    )


def format_fundamentals_batch(tickers: list, fresh: bool = False) -> str:
    """
    Drop-in replacement for get_fundamentals() in oracle_think_tank.py.
    Fetches all tickers, returns joined text blocks.
    """
    batch = get_fundamentals_batch(tickers, fresh=fresh)
    blocks = [format_fundamentals_block(t, batch.get(t, {})) for t in tickers]
    return "\n\n".join(blocks)


# ── Problem stock news ────────────────────────────────────────────────────────

def check_problem_stock_news(ticker: str, fundamentals: dict) -> str:
    """
    For stocks with short_interest > 20% or analyst_target < price * 0.5,
    fetch recent headlines and return a formatted string, or "" if clean.
    Fixes BUG #4: stale qualitative thesis for problem stocks.
    """
    if not fundamentals or fundamentals.get("error") or fundamentals.get("price") is None:
        return ""

    price = fundamentals.get("price") or 0
    short = fundamentals.get("short_interest_pct") or 0
    target = fundamentals.get("analyst_target")

    is_problem = short > 20 or (target and price and target < price * 0.5)
    if not is_problem:
        return ""

    headlines = get_news(ticker)
    if not headlines:
        return ""

    lines = "\n".join(f"- {h}" for h in headlines)
    return f"RECENT NEWS for {ticker}:\n{lines}"
