#!/usr/bin/env python3
"""
ORACLE Data Layer — oracle_data.py
===================================
Single interface for all market data in the ORACLE system.
Uses Alpaca live market data API. NO Yahoo Finance / yfinance.

Cache layout (~/ORACLE/cache/):
  prices_YYYYMMDD.json        — intraday price snapshots, keyed by today's date
  fundamentals_YYYYMMDD.json  — 24hr TTL fundamentals, expires at midnight by filename
"""

import os, json, datetime, time
from pathlib import Path
from dotenv import load_dotenv

_HOME_ENV = Path(os.path.expanduser("~/.env"))
load_dotenv(dotenv_path=_HOME_ENV)

CACHE_DIR = Path(os.path.expanduser("~/ORACLE/cache"))

def _read_home_env_value(name: str) -> str:
    """Fallback parser for ~/.env when python-dotenv was not able to hydrate os.environ."""
    try:
        for line in _HOME_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


# Alpaca live keys
_ALPACA_KEY = (
    os.getenv("ALPACA_LIVE_KEY")
    or os.getenv("ALPACA_API_KEY")
    or _read_home_env_value("ALPACA_LIVE_KEY")
    or _read_home_env_value("ALPACA_API_KEY")
)
_ALPACA_SECRET = (
    os.getenv("ALPACA_LIVE_SECRET")
    or os.getenv("ALPACA_SECRET_KEY")
    or _read_home_env_value("ALPACA_LIVE_SECRET")
    or _read_home_env_value("ALPACA_SECRET_KEY")
)


def _today_str() -> str:
    return datetime.date.today().strftime("%Y%m%d")


def _today_iso() -> str:
    return datetime.date.today().isoformat()


# ── Alpaca client (lazy singleton) ────────────────────────────────────────────

_alpaca_client = None

def _get_alpaca_client():
    global _alpaca_client
    if _alpaca_client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        _alpaca_client = StockHistoricalDataClient(_ALPACA_KEY, _ALPACA_SECRET)
    return _alpaca_client


def _fetch_price_alpaca(ticker: str) -> float | None:
    """Fetch latest trade price from Alpaca. Returns float or None."""
    try:
        from alpaca.data.requests import StockLatestTradeRequest
        client = _get_alpaca_client()
        req = StockLatestTradeRequest(symbol_or_symbols=ticker.upper())
        trade = client.get_stock_latest_trade(req)
        t = trade.get(ticker.upper())
        if t and hasattr(t, "price"):
            return float(t.price)
        return None
    except Exception as e:
        print(f"  [Alpaca price] {ticker}: {e}")
        return None


def _fetch_snapshot_alpaca(ticker: str) -> dict | None:
    """
    Fetch snapshot (quote + trade + daily bar) from Alpaca.
    Returns dict with price, open, high, low, prev_close, volume or None.
    """
    try:
        from alpaca.data.requests import StockSnapshotRequest
        client = _get_alpaca_client()
        req = StockSnapshotRequest(symbol_or_symbols=ticker.upper())
        snaps = client.get_stock_snapshot(req)
        snap = snaps.get(ticker.upper())
        if snap is None:
            return None
        result = {}
        # Latest trade
        if hasattr(snap, "latest_trade") and snap.latest_trade:
            result["price"] = float(snap.latest_trade.price)
        # Daily bar
        if hasattr(snap, "daily_bar") and snap.daily_bar:
            bar = snap.daily_bar
            result.setdefault("price", float(bar.close))
            result["open"]   = float(bar.open)
            result["high"]   = float(bar.high)
            result["low"]    = float(bar.low)
            result["volume"] = float(bar.volume)
        # Prev daily bar
        if hasattr(snap, "prev_daily_bar") and snap.prev_daily_bar:
            result["prev_close"] = float(snap.prev_daily_bar.close)
        return result if result.get("price") else None
    except Exception as e:
        print(f"  [Alpaca snapshot] {ticker}: {e}")
        return None


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
    Uses Alpaca live data. Caches in today's prices_YYYYMMDD.json.
    """
    ticker = ticker.upper()
    cache = _load_price_cache()

    if not fresh and ticker in cache and cache[ticker].get("price"):
        return cache[ticker]

    price = _fetch_price_alpaca(ticker)
    if price is None:
        return {"ticker": ticker, "price": None, "error": "fetch_failed"}

    result = {
        "ticker":    ticker,
        "price":     price,
        "timestamp": datetime.datetime.now().isoformat(),
        "source":    "alpaca_live",
    }
    cache[ticker] = result
    _save_price_cache(cache)
    return result


# ── Fundamentals cache ────────────────────────────────────────────────────────

def _fund_cache_path() -> Path:
    return CACHE_DIR / f"fundamentals_{_today_str()}.json"


def _load_fund_cache() -> dict:
    path = _fund_cache_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict) and "data" in raw:
                return raw["data"]
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
    Return dict with: ticker, price, open, high, low, prev_close, volume,
    market_cap (None — not available from Alpaca basic), sector (None),
    week52_high, week52_low from historical bars, analyst_target (None).
    Uses Alpaca snapshot for price/OHLCV. No Yahoo Finance.
    """
    ticker = ticker.upper()
    cache = _load_fund_cache()

    if not fresh and ticker in cache and not cache[ticker].get("error"):
        return cache[ticker]

    snap = _fetch_snapshot_alpaca(ticker)
    if snap is None or not snap.get("price"):
        result = {"ticker": ticker, "price": None, "error": "fetch_failed"}
        cache[ticker] = result
        _save_fund_cache(cache)
        return result

    # Get 52-week high/low from historical bars
    week52_high = None
    week52_low = None
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        end   = datetime.datetime.now()
        start = end - datetime.timedelta(days=365)
        client = _get_alpaca_client()
        bar_req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars_resp = client.get_stock_bars(bar_req)
        try:
            bars = bars_resp[ticker]
        except (KeyError, TypeError):
            bars = list(getattr(bars_resp, 'data', {}).get(ticker, []))
        if bars:
            highs = [b.high for b in bars]
            lows  = [b.low  for b in bars]
            week52_high = float(max(highs))
            week52_low  = float(min(lows))
    except Exception as e:
        print(f"  [Alpaca bars 52w] {ticker}: {e}")

    result = {
        "ticker":             ticker,
        "price":              snap.get("price"),
        "open":               snap.get("open"),
        "high":               snap.get("high"),
        "low":                snap.get("low"),
        "prev_close":         snap.get("prev_close"),
        "volume":             snap.get("volume"),
        "market_cap":         None,   # not in Alpaca basic
        "revenue_growth_yoy": None,   # not in Alpaca basic
        "eps_ttm":            None,   # not in Alpaca basic
        "eps_forward":        None,   # not in Alpaca basic
        "week52_high":        week52_high,
        "week52_low":         week52_low,
        "analyst_target":     None,   # not in Alpaca basic
        "short_interest_pct": None,   # not in Alpaca basic
        "sector":             None,
        "industry":           None,
        "source":             "alpaca_live",
        "timestamp":          datetime.datetime.now().isoformat(),
    }
    cache[ticker] = result
    _save_fund_cache(cache)
    return result


def get_fundamentals_batch(tickers: list, fresh: bool = False) -> dict:
    """
    Fetch fundamentals for each ticker. Prints progress.
    Returns {ticker: fundamentals_dict}.
    """
    results = {}
    for ticker in tickers:
        print(f"Fetching {ticker}...")
        results[ticker] = get_fundamentals(ticker, fresh=fresh)
        time.sleep(0.1)
    return results


# ── Formatting ────────────────────────────────────────────────────────────────

def format_fundamentals_block(ticker: str, data: dict) -> str:
    """Return a formatted text block for one stock."""
    if not data or data.get("error") or data.get("price") is None:
        return f"{ticker} - price unavailable — proceed with training knowledge, flag figures as unverified"

    price = data["price"]

    hi52  = data.get("week52_high")
    lo52  = data.get("week52_low")
    rng_str = f"${lo52:.2f} - ${hi52:.2f}" if (hi52 and lo52) else "N/A"

    prev  = data.get("prev_close")
    chg_str = ""
    if prev and price:
        chg = (price - prev) / prev * 100
        chg_str = f" ({chg:+.2f}% vs prev close)"

    vol   = data.get("volume")
    vol_str = f"{vol:,.0f}" if vol else "N/A"

    open_ = data.get("open")
    high_ = data.get("high")
    low_  = data.get("low")
    day_str = f"O${open_:.2f} H${high_:.2f} L${low_:.2f}" if (open_ and high_ and low_) else "N/A"

    return (
        f"{ticker} - ${price:.2f}{chg_str}\n"
        f"Today: {day_str} | Volume: {vol_str}\n"
        f"52-week range: {rng_str}\n"
        f"Source: Alpaca live data"
    )


def format_fundamentals_batch(tickers: list, fresh: bool = False) -> str:
    """
    Fetches all tickers via Alpaca, returns joined text blocks.
    Drop-in for the oracle-think-tank seed generation.
    """
    batch = get_fundamentals_batch(tickers, fresh=fresh)
    blocks = [format_fundamentals_block(t, batch.get(t, {})) for t in tickers]
    return "\n\n".join(blocks)


# ── News ──────────────────────────────────────────────────────────────────────

def get_news(ticker: str, max_headlines: int = 3) -> list:
    """
    No news source wired yet without Yahoo Finance.
    Returns empty list — agents use EDGAR links from seed doc instead.
    """
    return []


# ── Validation ────────────────────────────────────────────────────────────────

def validate_price_vs_screener(ticker: str, screener_price: float, fundamentals: dict) -> bool:
    if not fundamentals or not isinstance(fundamentals, dict):
        return True
    fund_price = fundamentals.get("price")
    if not fund_price or not screener_price or screener_price == 0:
        return True
    return abs(screener_price - fund_price) / screener_price <= 0.10


def check_problem_stock_news(ticker: str, fundamentals: dict) -> str:
    return ""
