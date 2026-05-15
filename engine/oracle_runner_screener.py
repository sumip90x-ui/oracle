#!/usr/bin/env python3
"""
ORACLE Runner Screener
======================
Scans the Fidelity CSV for runner candidates, scores them against AMD/MU/SNDK 
pre-run DNA, ranks by conviction, and auto-generates a ready-to-run ORACLE 
seed + prompt for the top picks.

Uses LIVE yfinance data for ALL stocks — no hardcoded fundamentals.
Results cached for 24h at ~/Documents/Trading Vault/04_Bot_Rules/screener_cache.json

Run after updating portfolio.csv:
  python3 ~/oracle_runner_screener.py
  python3 ~/oracle_runner_screener.py --top 10     # show top 10
  python3 ~/oracle_runner_screener.py --no-seed     # skip seed generation, no Think Tank
  python3 ~/oracle_runner_screener.py --screen-only # table + triage line only, then exit
  python3 ~/oracle_runner_screener.py --refresh     # force re-fetch all live data

Output:
  Console: ranked table of runner candidates
  Files:   ~/Documents/Trading Vault/10_MiroFish_Simulations/runner_screen/
             ORACLE_SEED_RUNNER_SCREEN_[DATE].md
             ORACLE_PROMPT_RUNNER_SCREEN_[DATE].txt
"""

import os, sys, csv, re, json, datetime, time, requests
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.hermes/.env"), override=True)

# ── ORACLE data layer — pre-warm Think Tank cache after screener runs ───────
sys.path.insert(0, os.path.expanduser("~/ORACLE"))
try:
    from data.oracle_data import get_price, get_fundamentals, get_fundamentals_batch
    _HAS_DATA_LAYER = True
except Exception:
    _HAS_DATA_LAYER = False
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL  = "anthropic/claude-sonnet-4.5"

CSV_PATH    = os.path.expanduser("~/portfolio.csv")
CSV_FOLDER  = os.path.expanduser("~/ORACLE/portfolio_csv")

def sync_latest_csv():
    """
    Auto-detect newest Fidelity CSV in the dedicated CSV folder.
    If newer than ~/portfolio.csv, copy it over automatically.
    Just drop any new Fidelity export into:
        ~/Documents/Trading Vault/00_Portfolio_CSV/
    The screener picks it up automatically.
    """
    import glob, shutil

    os.makedirs(CSV_FOLDER, exist_ok=True)
    pattern = os.path.join(CSV_FOLDER, "*.csv")
    candidates = glob.glob(pattern)

    if not candidates:
        # fallback: check Downloads as last resort
        dl_pattern = os.path.join(os.path.expanduser("~/Downloads"), "Portfolio_Positions_*.csv")
        candidates = glob.glob(dl_pattern)
        if not candidates:
            return CSV_PATH, False

    newest = max(candidates, key=os.path.getmtime)
    newest_mtime = os.path.getmtime(newest)
    current_mtime = os.path.getmtime(CSV_PATH) if os.path.exists(CSV_PATH) else 0

    if newest_mtime > current_mtime:
        shutil.copy2(newest, CSV_PATH)
        print(f"  New CSV detected: {os.path.basename(newest)}")
        print(f"  Copied to ~/portfolio.csv automatically.")
        return CSV_PATH, True

    return CSV_PATH, False
OUTPUT_BASE = os.path.expanduser("~/ORACLE/reports")
BRAIN_PATH  = os.path.expanduser("~/Documents/Trading Vault/TRADING_BRAIN.md")
CACHE_PATH  = os.path.expanduser("~/ORACLE/cache/screener_cache.json")
CACHE_TTL_HOURS = 24

# Runner DNA thresholds (what AMD/MU/SNDK looked like before running)
DNA = {
    "max_cap_b":       10.0,   # under $10B market cap (10x math works)
    "min_rev_growth":  20.0,   # revenue growing 20%+ YoY
    "min_dip_pct":     25.0,   # at least 25% below 52wk high
    "min_analyst_up":  30.0,   # analyst target 30%+ above current price
    "min_accounts":     2,     # at least 2 Fidelity accounts = institutional signal
    "min_price":        1.0,   # hard floor — no sub-$1 stocks ever
    "preferred_price": 10.0,   # preferred floor — $10+ for serious candidates
}

# yfinance exchange codes for US-listed stocks
US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "NASDAQ", "NYSE", "AMEX", "NYSEARCA", "NYSEArca", "PCX"}
# These suffixes indicate non-US or OTC — always exclude
EXCLUDED_SUFFIXES = (".V", ".TO", ".TSX", ".OTC", ".PK", ".BB")

# Hot sectors/industries for 10x potential (matched against sector + industry text)
HOT_SECTORS = {
    # Tech
    "semiconductor", "semiconductors", "software", "technology",
    "artificial intelligence", "cloud", "cybersecurity", "data",
    # Biotech/Health
    "biotechnology", "pharmaceutical", "drug", "genomics", "gene",
    "crispr", "cell therapy", "oncology", "rare disease",
    # Defense/Energy
    "defense", "aerospace", "drone", "autonomous", "nuclear",
    "uranium", "renewable", "solar", "battery", "energy storage",
    # Other high-growth
    "robotics", "space", "photonics", "optical",
}

# Confirmed destination holds — never put in runner screen
DESTINATION_HOLDS = {
    "AMD", "GOOGL", "AMZN", "NVDA", "MU", "MSFT", "AAPL", "META",
    "AVGO", "SMH", "QQQ", "VOO", "DIA", "SGOL", "GLD", "BTC"
}


# ── Cache ───────────────────────────────────────────────────────────────────

def load_cache():
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH) as f:
                cache = json.load(f)
            ts = datetime.datetime.fromisoformat(cache.get("timestamp", "2000-01-01"))
            age = (datetime.datetime.now() - ts).total_seconds() / 3600
            if age < CACHE_TTL_HOURS:
                return cache.get("data", {})
    except Exception:
        pass
    return {}


def save_cache(data):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump({"timestamp": datetime.datetime.now().isoformat(), "data": data}, f)


# ── yfinance fetcher ─────────────────────────────────────────────────────────

def _parse_earnings_date(val):
    """Normalize yfinance earningsDate (list, timestamp, datetime) to YYYY-MM-DD string."""
    if val is None:
        return None
    if isinstance(val, list):
        val = val[0] if val else None
    if val is None:
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(val).strftime("%Y-%m-%d")
        except Exception:
            return None
    try:
        return str(val)[:10]
    except Exception:
        return None


def fetch_all_fundamentals(symbols):
    """Fetch yfinance fundamental data for all symbols with caching and progress."""
    import yfinance as yf

    cache = load_cache()

    to_fetch = [s for s in symbols if s not in cache]

    if not to_fetch:
        print(f"  Using cached data ({len(cache)} stocks, <24h old)...")
        return cache

    print(f"  Fetching live data for {len(to_fetch)} stocks (cached: {len(cache)})...")

    batch_size = 50
    new_data = {}

    for i in range(0, len(to_fetch), batch_size):
        batch = to_fetch[i:i + batch_size]
        end_idx = min(i + batch_size, len(to_fetch))
        print(f"  Fetching {i + 1}-{end_idx}/{len(to_fetch)}...", end="\r", flush=True)

        try:
            tickers = yf.Tickers(" ".join(batch))
            for sym in batch:
                try:
                    info = tickers.tickers[sym].info
                    price = (info.get("currentPrice") or info.get("regularMarketPrice") or 0)
                    if price and price > 0:
                        target = info.get("targetMeanPrice")
                        # Prefer quarterly YoY over stale annual revenueGrowth field
                        rev_growth = 0
                        try:
                            q_fin = tickers.tickers[sym].quarterly_income_stmt
                            if q_fin is not None and not q_fin.empty:
                                rev_row = None
                                for label in ("Total Revenue", "Revenue"):
                                    if label in q_fin.index:
                                        rev_row = q_fin.loc[label].dropna().sort_index(ascending=False)
                                        break
                                if rev_row is not None and len(rev_row) >= 5:
                                    r0, r4 = rev_row.iloc[0], rev_row.iloc[4]
                                    if r4 and abs(r4) > 0:
                                        rev_growth = (r0 - r4) / abs(r4)
                            if not rev_growth:
                                rev_growth = info.get("revenueGrowth") or 0
                        except Exception:
                            rev_growth = info.get("revenueGrowth") or 0
                        analyst_upside = 0
                        if price and target:
                            analyst_upside = ((target - price) / price) * 100

                        new_data[sym] = {
                            "price":              price,
                            "market_cap_b":       (info.get("marketCap") or 0) / 1e9,
                            "exchange":           info.get("exchange", ""),
                            "sector":             info.get("sector", ""),
                            "industry":           info.get("industry", ""),
                            "52wk_high":          info.get("fiftyTwoWeekHigh"),
                            "52wk_low":           info.get("fiftyTwoWeekLow"),
                            "analyst_target":     target,
                            "analyst_upside_pct": analyst_upside,
                            "forward_eps":        info.get("forwardEps"),
                            "trailing_eps":       info.get("trailingEps"),
                            "short_pct":          info.get("shortPercentOfFloat"),
                            "rev_growth_pct":     rev_growth * 100,
                            "earnings_growth":    (info.get("earningsGrowth") or 0) * 100,
                            "profit_margin":      (info.get("profitMargins") or 0) * 100,
                            "beta":               info.get("beta"),
                            "full_name":          info.get("longName", sym),
                            "next_earnings_date": _parse_earnings_date(
                                info.get("earningsDate") or info.get("nextEarningsDate")
                            ),
                        }
                    else:
                        new_data[sym] = {}  # tried but no price data
                except Exception:
                    new_data[sym] = {}
        except Exception as e:
            print(f"\n  Batch error: {e}, continuing...")
            for sym in batch:
                new_data[sym] = {}

        time.sleep(0.5)  # gentle rate limiting

    cache.update(new_data)
    save_cache(cache)
    successful = sum(1 for v in new_data.values() if v)
    print(f"  Live data fetched and cached. ({successful}/{len(to_fetch)} symbols returned data)")

    return cache


# ── Portfolio parser ─────────────────────────────────────────────────────────

def parse_fidelity_csv(path: str) -> dict:
    """Use portfolio_parser.py directly — proven parser for Fidelity CSV format."""
    sys.path.insert(0, os.path.expanduser("~"))
    try:
        import portfolio_parser
        positions = portfolio_parser.parse_portfolio(path)
        result = {}
        for sym, p in positions.items():
            if p["val"] > 0:
                cost = p["cost"] if p["cost"] > 0 else p["val"]
                result[sym] = {
                    "accounts":    p["accts"],
                    "total_value": p["val"],
                    "total_cost":  cost,
                    "pnl_pct":     (p["gl"] / cost * 100) if cost > 0 else 0,
                    "name":        sym,
                }
        return result
    except Exception as e:
        print(f"  ERROR parsing CSV: {e}")
        return {}


def get_portfolio_accounts(path: str) -> list:
    """Return sorted list of all account names in the CSV."""
    import csv as _csv
    accounts = set()
    try:
        with open(path, encoding="utf-8-sig") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                name = (row.get("Account Name") or "").strip()
                if name:
                    accounts.add(name)
    except Exception:
        pass
    return sorted(accounts)


def parse_fidelity_csv_filtered(path: str, account_name: str) -> dict:
    """Parse CSV filtered to a specific account name. Returns same dict format as parse_fidelity_csv."""
    import csv as _csv
    result = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                acct = (row.get("Account Name") or "").strip()
                if acct != account_name:
                    continue
                sym = (row.get("Symbol") or "").strip()
                if not sym or sym.startswith("FZFXX") or sym.startswith("**") or sym.startswith("USD") or len(sym) > 6:
                    continue
                try:
                    val  = float((row.get("Current Value") or "0").replace("$","").replace(",","").strip() or 0)
                    cost = float((row.get("Cost Basis Total") or "0").replace("$","").replace(",","").strip() or 0)
                    gl   = float((row.get("Total Gain/Loss Dollar") or "0").replace("$","").replace(",","").strip() or 0)
                except (ValueError, AttributeError):
                    val = cost = gl = 0
                if val <= 0:
                    continue
                if cost <= 0:
                    cost = val
                pnl_pct = (gl / cost * 100) if cost > 0 else 0
                if sym in result:
                    # Merge if same symbol appears multiple times
                    result[sym]["total_value"] += val
                    result[sym]["total_cost"]  += cost
                    result[sym]["accounts"]    += 1
                else:
                    result[sym] = {
                        "accounts":    1,
                        "total_value": val,
                        "total_cost":  cost,
                        "pnl_pct":     pnl_pct,
                        "name":        sym,
                    }
    except Exception as e:
        print(f"  ERROR parsing filtered CSV: {e}")
    return result


# ── Scoring helpers ──────────────────────────────────────────────────────────

def get_price_flag(symbol: str, live_data: dict) -> str:
    """Return EXCLUDED, CAUTION, or OK based on exchange/price."""
    if any(symbol.upper().endswith(s.upper()) for s in EXCLUDED_SUFFIXES):
        return "EXCLUDED"
    exchange = live_data.get("exchange", "")
    if exchange and exchange not in US_EXCHANGES:
        return "EXCLUDED"
    price = live_data.get("price", 0) or 0
    if price < 1.0:
        return "EXCLUDED"
    if price < 10.0:
        return "CAUTION"
    return "OK"


def sector_score(live_data: dict) -> int:
    """0-5 pts based on how many hot sector keywords match sector+industry."""
    text = (live_data.get("sector", "") + " " + live_data.get("industry", "")).lower()
    matches = sum(1 for s in HOT_SECTORS if s in text)
    return min(matches * 2, 5)


def eps_inflection_score(live_data: dict) -> int:
    """THE AMD SIGNAL: loss->profit crossing is when smart money loads."""
    fwd   = live_data.get("forward_eps") or 0
    trail = live_data.get("trailing_eps") or 0
    egrow = live_data.get("earnings_growth", 0) or 0
    if trail < 0 and fwd > 0:          return 5  # AMD signal: loss -> profit
    if fwd > 0 and trail > 0 and egrow >= 100: return 4  # profitable + accelerating
    if fwd > trail > 0 and egrow >= 25: return 3  # improving
    if fwd > trail > 0:                return 2
    if fwd > 0:                        return 1
    return 0


def dip_depth_score(live_data: dict) -> int:
    """Actual market dip from 52wk high — fixes the P&L cost basis flaw."""
    high  = live_data.get("52wk_high") or 0
    price = live_data.get("price", 0) or 0
    if not high or not price or high == 0: return 0
    dip_pct = (high - price) / high * 100
    if dip_pct >= 60: return 5
    if dip_pct >= 40: return 4
    if dip_pct >= 25: return 3
    if dip_pct >= 15: return 2
    if dip_pct >=  5: return 1
    return 0


def continuation_score(symbol: str, csv_data: dict, live_data: dict) -> int:
    """Rewards stocks already working in Fidelity — continuation plays not pre-run setups."""
    pnl_pct = csv_data.get('pnl_pct', 0)
    if pnl_pct > 50: return 5
    if pnl_pct > 25: return 4
    if pnl_pct > 10: return 3
    if pnl_pct > 0:  return 2
    return 0  # use beaten_down scoring if red


def short_fuel_score(live_data: dict) -> int:
    """Short squeeze fuel — AMD had 34% short at bottom. Only score in hot sectors."""
    if sector_score(live_data) < 2:
        return 0
    raw = live_data.get("short_pct") or 0
    # yfinance returns decimal (0.20 = 20%) sometimes, percent other times
    pct = raw if raw <= 1.0 else raw / 100
    if pct >= 0.25: return 5
    if pct >= 0.15: return 4
    if pct >= 0.10: return 3
    if pct >= 0.05: return 2
    if pct  > 0.00: return 1
    return 0


def short_squeeze_bonus(live_data: dict) -> int:
    """High short + hot sector = squeeze fuel bonus."""
    short = live_data.get('short_pct', 0) or 0
    pct = short if short <= 1.0 else short / 100
    if pct >= 0.25 and sector_score(live_data) >= 2: return 2
    if pct >= 0.20 and sector_score(live_data) >= 2: return 1
    return 0


def earnings_trajectory_score(live_data: dict) -> int:
    """Earnings growth rate — revenue growth + earnings leverage = re-rating signal."""
    egrow  = live_data.get("earnings_growth", 0) or 0
    margin = live_data.get("profit_margin", 0) or 0
    if egrow >= 200:                  return 5
    if egrow >= 100:                  return 4
    if egrow >= 50:                   return 3
    if egrow >= 20:                   return 2
    if egrow >= 0 and margin > 0:     return 1
    return 0


def score_runner(symbol: str, csv_data: dict, live_data: dict) -> tuple:
    """
    Score a stock on runner DNA 0-50.
    Returns (score, breakdown_dict, price_flag).
    All fundamental data comes from live yfinance data.
    """
    if symbol in DESTINATION_HOLDS:
        return 0, {}, "OK"

    price_flag = get_price_flag(symbol, live_data)
    if price_flag == "EXCLUDED":
        return 0, {}, "EXCLUDED"

    score = 0
    breakdown = {}

    accounts   = csv_data["accounts"]
    pnl_pct    = csv_data["pnl_pct"]
    cap_b      = live_data.get("market_cap_b", 0) or 0
    rev_growth = live_data.get("rev_growth_pct", 0) or 0
    analyst_up = live_data.get("analyst_upside_pct", 0) or 0

    # 1. Fidelity conviction (0-5): how many accounts hold this
    pts = (5 if accounts >= 8 else 4 if accounts >= 5 else
           3 if accounts >= 3 else 2 if accounts >= 2 else
           1 if accounts >= 1 else 0)
    score += pts; breakdown["conviction"] = pts

    # 2. Use continuation score if green in Fidelity, beaten_down if red
    if pnl_pct > 0:
        pts = continuation_score(symbol, csv_data, live_data)
        score += pts; breakdown['continuation'] = pts
    else:
        pts = (5 if pnl_pct <= -30 else 4 if pnl_pct <= -20 else
               3 if pnl_pct <= -10 else 2 if pnl_pct <= 0 else 0)
        score += pts; breakdown['beaten_down'] = pts

    # 3. Market cap sweet spot (0-5): small enough to 10x
    pts = (5 if 0 < cap_b <= 1 else 4 if cap_b <= 3 else
           3 if cap_b <= 5 else 2 if cap_b <= 10 else
           1 if cap_b <= 20 else 0)
    score += pts; breakdown["small_cap"] = pts

    # 4. Revenue growth (0-5)
    pts = (5 if rev_growth >= 100 else 4 if rev_growth >= 50 else
           3 if rev_growth >= 25 else 2 if rev_growth >= 10 else
           1 if rev_growth >= 0 else 0)
    score += pts; breakdown["rev_growth"] = pts

    # 5. Analyst upside (0-5)
    pts = (5 if analyst_up >= 150 else 4 if analyst_up >= 80 else
           3 if analyst_up >= 50 else 2 if analyst_up >= 30 else
           1 if analyst_up >= 10 else 0)
    score += pts; breakdown["analyst_up"] = pts

    # 6. Hot sector (0-5)
    pts = sector_score(live_data)
    score += pts; breakdown["hot_sector"] = pts

    # 7. EPS inflection (0-5) — THE AMD SIGNAL
    pts = eps_inflection_score(live_data)
    score += pts; breakdown["eps_inflection"] = pts

    # 8. 52wk dip depth (0-5) — actual market dip not portfolio P&L
    pts = dip_depth_score(live_data)
    score += pts; breakdown["dip_depth"] = pts

    # 9. Short squeeze fuel (0-5) — only in hot sectors
    pts = short_fuel_score(live_data)
    score += pts; breakdown["short_fuel"] = pts

    # 10. Earnings trajectory (0-5)
    pts = earnings_trajectory_score(live_data)
    score += pts; breakdown["earn_trajectory"] = pts

    # 11. Short squeeze bonus (0-2) — high short + hot sector
    pts = short_squeeze_bonus(live_data)
    score += pts; breakdown['squeeze_bonus'] = pts

    return score, breakdown, price_flag


# ── Screen runner ────────────────────────────────────────────────────────────

def run_screen(holdings: dict, live_data_map: dict, top_n: int = 15) -> list:
    """Score all holdings and return top_n sorted by runner score.
    CAUTION stocks ($1-$10) are included but sorted below OK stocks."""
    results = []
    excluded = []
    no_data = []

    for symbol, csv_data in holdings.items():
        if symbol in DESTINATION_HOLDS:
            continue
        if csv_data["accounts"] < 1:
            continue

        live = live_data_map.get(symbol, {})
        if not live:
            no_data.append(symbol)
            continue

        score, breakdown, price_flag = score_runner(symbol, csv_data, live)

        if price_flag == "EXCLUDED":
            excluded.append(symbol)
            continue

        if score >= 8:
            results.append({
                "symbol":          symbol,
                "score":           score,
                "breakdown":       breakdown,
                "price_flag":      price_flag,
                "accounts":        csv_data["accounts"],
                "pnl_pct":         csv_data["pnl_pct"],
                "price":           live.get("price", 0),
                "market_cap_b":    live.get("market_cap_b", 0),
                "rev_growth":      live.get("rev_growth_pct", 0),
                "analyst_up":      live.get("analyst_upside_pct", 0),
                "sector":          live.get("sector", ""),
                "industry":        live.get("industry", ""),
                "full_name":       live.get("full_name", symbol),
                "exchange":        live.get("exchange", ""),
                "has_fundamentals": True,
            })

    if excluded:
        print(f"  EXCLUDED {len(excluded)} symbols (non-US or sub-$1)")
    if no_data:
        print(f"  SKIPPED {len(no_data)} symbols (no yfinance data)")

    ok      = sorted([r for r in results if r["price_flag"] == "OK"],      key=lambda x: -x["score"])
    caution = sorted([r for r in results if r["price_flag"] == "CAUTION"], key=lambda x: -x["score"])
    return (ok + caution)[:top_n]


# ── Output ───────────────────────────────────────────────────────────────────

def print_table(results: list, live_data_map: dict = None):
    today = datetime.date.today().strftime("%Y-%m-%d")
    print()
    print("=" * 95)
    print(f"  ORACLE RUNNER SCREEN — {today} — LIVE yfinance data")
    print(f"  Ranked by AMD/MU/SNDK runner DNA | All data live from Yahoo Finance")
    print(f"  Price: OK=$10+  [!]=CAUTION $1-$10  EXCLUDED=sub-$1 or non-US")
    print("=" * 95)
    print(f"\n  {'#':<3} {'SYM':<7} {'SCORE':<7} {'ACCTS':<6} {'P&L%':<8} {'PRICE':<9} {'REV%':<8} {'EPS':<10} {'DIP52':<7} {'ANLST':<8} {'CAP':<7} INDUSTRY")
    print("  " + "-"*92)

    for i, r in enumerate(results, 1):
        pnl   = f"{r['pnl_pct']:+.1f}%"
        rev   = f"{r['rev_growth']:+.0f}%" if r["rev_growth"] else "  n/a"
        anlst = f"+{r['analyst_up']:.0f}%" if r["analyst_up"] else "  n/a"
        cap   = f"${r['market_cap_b']:.1f}B" if r["market_cap_b"] else "  n/a"
        price = f"${r['price']:.2f}" if r["price"] else "  n/a"
        flag  = "[!]" if r["price_flag"] == "CAUTION" else "   "
        marker = "★" if i <= 5 else " "
        sector_short = (r.get("industry") or r.get("sector", ""))[:24]

        fwd_eps   = live_data_map.get(r["symbol"], {}).get("forward_eps")  if live_data_map else None
        trail_eps = live_data_map.get(r["symbol"], {}).get("trailing_eps") if live_data_map else None
        high_52   = live_data_map.get(r["symbol"], {}).get("52wk_high", 0) if live_data_map else 0
        price_val = r.get("price", 0) or 0
        dip_str   = f"-{(high_52-price_val)/high_52*100:.0f}%" if high_52 > 0 else "  n/a"

        if fwd_eps and trail_eps:
            if trail_eps < 0 and fwd_eps > 0:
                eps_str = "↑TURN"
            elif fwd_eps > trail_eps:
                eps_str = f"↑{fwd_eps:.2f}"
            else:
                eps_str = f"{fwd_eps:.2f}"
        else:
            eps_str = "  n/a"

        print(f"  {marker}{i:<2} {r['symbol']:<7} {r['score']:<7} {r['accounts']:<6} {pnl:<8} {price:<7}{flag} {rev:<8} {eps_str:<10} {dip_str:<7} {anlst:<8} {cap:<7} {sector_short}")

    print()
    total = len(results)
    print(f"  All {total} candidates have live yfinance fundamentals")
    print(f"  Score (max 52): accounts(5) + continuation_or_beaten_down(5) + small_cap(5) + rev_growth(5) + analyst_up(5) + hot_sector(5) + eps_inflection(5) + dip_depth(5) + short_fuel(5) + earn_trajectory(5) + squeeze_bonus(2)")
    print()


def haiku_triage(top_candidates: list, live_data_map: dict) -> list:
    """
    Single Haiku call (~$0.05) pattern-matches top 15 against AMD/MU/SNDK DNA.
    Returns ordered list of 5-6 tickers. Falls back to score order if API fails.

    Hard pre-filters (applied before Haiku to eliminate sim-wasting lottery tickets):
      - Market cap >= $2B  (sub-$2B small caps get torn apart by quality_compounder agent)
      - Revenue growth >= 20% YoY
      - Analyst upside >= 50%  (agents need real asymmetry to argue bullish through 8 rounds)
      - EPS: forward_eps > 0 OR (trailing_eps < 0 AND forward_eps > trailing_eps)  [positive or turning]
      - Sector: must match HOT_SECTORS (boring sectors get neutral agent stances)
    Stocks that fail 1 of 5 still pass as soft_pass. Fail 2+ = dropped.
    Safety valve: if < 4 survive, falls back to score order.
    """
    if not OR_KEY:
        return [r["symbol"] for r in top_candidates[:6]]

    # -- Hard pre-filter --
    qualified = []
    soft_pass  = []
    for r in top_candidates:
        sym  = r["symbol"]
        live = live_data_map.get(sym, {})
        cap      = r.get("market_cap_b", 0) or 0
        rev      = r.get("rev_growth", 0) or 0
        anlst_up = r.get("analyst_up", 0) or 0
        fwd_eps  = live.get("forward_eps") or 0
        trail_ep = live.get("trailing_eps") or 0
        sector_text = (live.get("sector", "") + " " + live.get("industry", "")).lower()

        rev_growth_pct = live.get('rev_growth_pct', 0) or 0
        eps_ok     = (fwd_eps > 0) or (trail_ep < 0 and fwd_eps > trail_ep) or (rev_growth_pct > 100)
        sector_ok  = any(s in sector_text for s in HOT_SECTORS)
        checks     = [cap >= 2.0, rev >= 20.0, anlst_up >= 50.0, eps_ok, sector_ok]
        passed     = sum(checks)

        if passed >= 5:
            qualified.append(r)
        elif passed >= 4:
            soft_pass.append(r)
        else:
            fails = []
            if cap < 2.0:      fails.append(f"cap=${cap:.1f}B<$2B")
            if rev < 20.0:     fails.append(f"rev={rev:.0f}%<20%")
            if anlst_up < 50.0: fails.append(f"anlst={anlst_up:.0f}%<50%")
            if not eps_ok:     fails.append("eps_not_turning")
            if not sector_ok:  fails.append("cold_sector")
            print(f"  [triage-filter] {sym} dropped: {' | '.join(fails)}")

    filtered = qualified + soft_pass
    if len(filtered) < 4:
        print(f"  [triage-filter] Only {len(filtered)} stocks passed hard filter — relaxing to score order")
        filtered = top_candidates   # fallback: don't starve the sim

    print(f"  [triage-filter] {len(qualified)} qualified + {len(soft_pass)} soft-pass → {len(filtered)} into Haiku")

    rows = []
    for r in filtered:
        sym  = r["symbol"]
        live = live_data_map.get(sym, {})
        fwd   = live.get("forward_eps", "n/a")
        trail = live.get("trailing_eps", "n/a")
        egrow = live.get("earnings_growth", "n/a")
        short = live.get("short_pct", "n/a")
        margin= live.get("profit_margin", "n/a")
        high  = live.get("52wk_high", 0) or 0
        price = r.get("price", 0) or 0
        dip   = f"{(high-price)/high*100:.0f}%" if high > 0 else "n/a"
        rows.append(
            f"{sym}: score={r['score']}/52 | cap=${r.get('market_cap_b',0):.1f}B | "
            f"rev={r.get('rev_growth',0):+.0f}% | earn_growth={egrow} | "
            f"trail_eps={trail} fwd_eps={fwd} | short%={short} | "
            f"off_52wk_high={dip} | margin={margin} | "
            f"sector={r.get('sector','')} | analyst_up={r.get('analyst_up',0):.0f}%"
        )

    system_msg = """You are a quantitative pattern recognition engine trained on pre-run DNA of AMD, MU, SNDK, LXRX.

PATTERN: beaten down + revenue inflecting + EPS improving (especially loss->profit) + catalyst not yet priced + short squeeze fuel + hot sector tailwind.

AMD at $2: trailing EPS -$0.44, forward EPS +$0.83, 34% short, semiconductor, off 96% from high.
MU at trough: commodity cycle bottom, earnings inflecting, AI demand not priced.
SNDK: beaten down hardware + sector catalyst + margin recovery.
LXRX: near-bankrupt + binary catalyst (FDA) + partnership surprise.

Return ONLY valid JSON: {"picks": ["SYM1","SYM2","SYM3","SYM4","SYM5"], "reasons": {"SYM1": "one line reason", ...}}
Pick the 5-6 that MOST closely match the pre-run pattern. Ignore composite score — use your judgment on the specific signal combination."""

    user_msg = f"Rank these {len(filtered)} candidates by AMD/MU/SNDK pattern match. Pick best 5-6.\n\nCANDIDATES:\n" + "\n".join(rows) + "\n\nReturn JSON only."

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
            json={
                "model": "anthropic/claude-3.5-haiku",
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": user_msg}
                ],
                "max_tokens": 600
            },
            timeout=30
        )
        content = resp.json()["choices"][0]["message"]["content"].strip()
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            result = json.loads(match.group())
            picks   = result.get("picks", [])
            reasons = result.get("reasons", {})
            valid   = {r["symbol"] for r in top_candidates}
            picks   = [p for p in picks if p in valid]
            if len(picks) >= 4:
                print(f"\n  Haiku triage selected: {picks}")
                for sym, reason in reasons.items():
                    if sym in picks:
                        print(f"    {sym}: {reason}")
                return picks[:6]
    except Exception as e:
        print(f"  Haiku triage failed ({e}), using score order")

    return [r["symbol"] for r in top_candidates[:6]]


def get_thinktank_candidates(results: list, live_data_map: dict = None, max_stocks: int = 6) -> list:
    """Run Haiku triage on top 15, return best 5-6 for Think Tank."""
    top15 = results[:15]
    if live_data_map and OR_KEY:
        print(f"  Running Haiku triage on top {len(top15)} candidates (~$0.05)...")
        return haiku_triage(top15, live_data_map)
    return [r["symbol"] for r in top15[:max_stocks]]


def generate_seed_and_prompt(results: list, top_n: int = 5) -> tuple:
    """Use Claude to generate ORACLE seed + prompt from top candidates."""
    candidates = results[:top_n]

    candidate_block = ""
    for r in candidates:
        price_note = f"${r['price']:.2f}" if r["price"] else "n/a"
        if r["price_flag"] == "CAUTION":
            price_note += " [CAUTION <$10]"
        rev_str = f"{r['rev_growth']:+.0f}%" if r["rev_growth"] else "n/a"
        anlst_str = f"+{r['analyst_up']:.0f}%" if r["analyst_up"] else "n/a"
        cap_str = f"${r['market_cap_b']:.1f}B" if r["market_cap_b"] else "n/a"
        industry = r.get("industry") or r.get("sector", "")
        candidate_block += f"""
### {r['symbol']} — {r['full_name']} (Runner Score: {r['score']}/30)
  Fidelity: {r['accounts']} accounts | Portfolio P&L: {r['pnl_pct']:+.1f}%
  Price: {price_note} | Exchange: {r['exchange']}
  Market Cap: {cap_str} | Rev Growth: {rev_str} YoY
  Analyst Target Upside: {anlst_str} | Sector: {r['sector']}
  Industry: {industry}
  Score breakdown: {r['breakdown']}
"""

    brain_context = ""
    if os.path.exists(BRAIN_PATH):
        with open(BRAIN_PATH) as f:
            brain_context = f.read()[:3000]

    today = datetime.date.today().strftime("%Y-%m-%d")

    user_msg = f"""Build a complete ORACLE simulation seed for this runner screen.

DATE: {today}
MISSION: Rank the top runner candidates by 10x potential. Find the next AMD.

TOP CANDIDATES FROM SCREEN:
{candidate_block}

OWNER CONTEXT (from TRADING_BRAIN.md):
{brain_context[:2000]}

RUNNER DNA REFERENCE:
- AMD: $2→$455 (227x). Beaten down + new CEO + Ryzen + AI GPU pivot. Revenue turned up first.
- MU: 10x. HBM memory nobody priced. Revenue exploded when AI training demand hit.
- SNDK: 650% in 12 months. WD spin-off + NAND shortage + AI storage. Margin recovery.
- LXRX: 10x (Sumith actually made this). Near-bankrupt biopharma + surprise FDA approval.
- INTC: +559% MISSED. Beaten down + AI foundry pivot + CHIPS Act. Pattern was visible.
PATTERN: beaten down + revenue inflecting + EPS improving + catalyst not yet priced.

Build a complete 8-part seed. Follow these STRICT LENGTH RULES per section — do not exceed them:

PART I   ACTORS:          2 sentences per agent MAX. Format: [Name] — [Specialty]. Blind spot: [one phrase]. No backstory. No career history.
PART II  ENVIRONMENT:     3 bullet points ONLY. SPY direction, sector rotation signal, interest rate stance. No narrative.
PART III EVIDENCE:        FULL DETAIL. Do not compress. Include CATALYST:, BULL:, BEAR: tags. This section is parsed by code.
PART IV  CANDIDATES:      Leave compact. Pre-debate ranking table only.
PART V   RUNNER DNA:      Comparison TABLE ONLY — 5 rows. Columns: Ticker | Peak Gain | Catalyst Type | Reversal Trigger | Relevance to {top_n} candidates. No paragraph writeups.
PART VI  FAILURE SCENARIOS: FULL DETAIL. Do not compress. Include historical precedents. Agents cite these directly.
PART VII KENJI MANDATE:   3 bullet points per candidate ONLY. No duplicate table from Part IV.
PART VIII DEBATE FODDER:  Write ALL {top_n} agents' opening statements IN FULL. Each agent ranks all {top_n} candidates with 1-sentence reasoning per pick. THIS SECTION MUST COMPLETE — it is the highest priority. Do not run out of tokens here.

Agents should specifically debate the ranking: which of the {top_n} is most likely to 10x first?
Include a Devil's Advocate agent who attacks each thesis hard."""

    print(f"  Generating seed for top {top_n} candidates (30-45s)...")

    seed_resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": """You are an expert ORACLE simulation seed builder.
Build concise, data-grounded seeds that produce specific agent personas and debates.
Agents need only name, specialty, and blind spot — no backstory, no career history.
Always include: hard numbers, failure scenarios with historical precedents, explicit conflicts.
Write Parts I, II, V, and VII in compact format first to preserve token budget for Parts III, VI, and VIII which require full detail.
Structure: PART I ACTORS, PART II ENVIRONMENT, PART III EVIDENCE, PART IV CANDIDATES,
PART V RUNNER DNA, PART VI FAILURE SCENARIOS, PART VII KENJI MANDATE, PART VIII DEBATE FODDER.
Start with: # ORACLE SIMULATION SEED — RUNNER SCREEN"""},
                {"role": "user", "content": user_msg}
            ],
            "max_tokens": 8000
        },
        timeout=120
    )
    seed = seed_resp.json()["choices"][0]["message"]["content"].strip()

    syms = [r["symbol"] for r in candidates]
    prompt_msg = f"""Write a focused ORACLE simulation requirement.

Candidates: {', '.join(syms)}
Goal: Rank by 10x potential, find the best entry right now.

2-4 sentences. End with 3 forced votes: 
1) single best SGOL tap candidate (state which of the 5 gates pass/fail)
2) top 3 $1.10 starters ranked by conviction  
3) which is most likely to be a 10x in 5 years"""

    prompt_resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "Write focused 2-4 sentence simulation requirements. End with 3 forced votes. No headers. Output only the prompt text."},
                {"role": "user", "content": prompt_msg}
            ],
            "max_tokens": 300
        },
        timeout=30
    )
    prompt = prompt_resp.json()["choices"][0]["message"]["content"].strip()

    return seed, prompt


def save_outputs(seed: str, prompt: str) -> tuple:
    date = datetime.date.today().strftime("%Y%m%d")
    os.makedirs(OUTPUT_BASE, exist_ok=True)
    seed_path   = os.path.join(OUTPUT_BASE, f"ORACLE_SEED_RUNNER_SCREEN_{date}.md")
    prompt_path = os.path.join(OUTPUT_BASE, f"ORACLE_PROMPT_RUNNER_SCREEN_{date}.txt")
    with open(seed_path,   "w") as f: f.write(seed)
    with open(prompt_path, "w") as f: f.write(prompt)
    return seed_path, prompt_path


# ── Upcoming catalysts writer ────────────────────────────────────────────────

def write_upcoming_catalysts(live_data: dict, date: str) -> None:
    """Scan live_data for next_earnings_date and write 30-day calendar to Obsidian brain folder."""
    import yfinance as yf

    today = datetime.date.today()

    # Supplement any items missing next_earnings_date (old cache entries)
    missing = [sym for sym, d in live_data.items() if d and "next_earnings_date" not in d]
    if missing:
        print(f"  Fetching missing earnings dates for {len(missing)} ticker(s)...", end="", flush=True)
        for sym in missing:
            try:
                info = yf.Ticker(sym).info
                raw  = info.get("earningsDate") or info.get("nextEarningsDate")
                parsed = _parse_earnings_date(raw)
                if parsed:
                    live_data[sym]["next_earnings_date"] = parsed
            except Exception:
                pass
        print(" done.")

    upcoming = []
    for sym, data in live_data.items():
        if not data:
            continue
        raw_date = data.get("next_earnings_date")
        if not raw_date:
            continue
        try:
            earn_date = datetime.date.fromisoformat(str(raw_date)[:10])
        except Exception:
            continue
        days_away = (earn_date - today).days
        if 0 <= days_away <= 30:
            upcoming.append((days_away, sym, earn_date))

    if not upcoming:
        print("  No upcoming earnings in next 30 days found in screener data.")
        return

    upcoming.sort()

    out_dir = os.path.expanduser("~/Documents/Trading Vault/04_Bot_Rules/ORACLE/brain")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "upcoming_catalysts.md")

    lines = [
        f"# Upcoming Earnings Catalysts — updated {date}\n",
        "| Days Away | Ticker | Earnings Date | Priority |",
        "|-----------|--------|---------------|----------|",
    ]
    for days_away, sym, earn_date in upcoming:
        if days_away <= 3:
            priority = "**URGENT**"
        elif days_away <= 14:
            priority = "SOON"
        else:
            priority = ""
        lines.append(f"| {days_away} | {sym} | {earn_date} | {priority} |")

    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"  Upcoming catalysts: {len(upcoming)} stocks in next 30 days → {path}")
        urgent = [s for d, s, _ in upcoming if d <= 3]
        if urgent:
            print(f"  *** URGENT (<=3 days): {urgent}")
    except Exception as e:
        print(f"  WARNING: could not write upcoming_catalysts.md: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="ORACLE Runner Screener - Live yfinance data")
    parser.add_argument("--top",       type=int, default=15)
    parser.add_argument("--no-seed",     action="store_true", help="Skip seed generation and Think Tank launch")
    parser.add_argument("--screen-only", action="store_true", help="Print table + triage line then exit (no prompt, no seed, no Think Tank)")
    parser.add_argument("--refresh",     action="store_true", help="Force refresh cache (re-fetch all live data)")
    parser.add_argument("--csv",         type=str, default=None, help="Path to a specific Fidelity CSV file to use instead of auto-detected latest")
    parser.add_argument("--fast",      action="store_true", help="Pass --fast to Think Tank (Haiku, cheaper)")
    parser.add_argument("--no-search", action="store_true", help="Pass --no-search to Think Tank (skip live fundamentals)")
    args = parser.parse_args()

    print("\n ORACLE RUNNER SCREENER")

    # Allow explicit CSV override via --csv flag
    if args.csv:
        csv_path = os.path.expanduser(args.csv)
        if not os.path.exists(csv_path):
            print(f"  ERROR: CSV not found: {csv_path}")
            sys.exit(1)
        import shutil
        shutil.copy2(csv_path, CSV_PATH)
        print(f"  Using specified CSV: {os.path.basename(csv_path)}")
        csv_updated = True
    else:
        # Auto-detect newest CSV from Downloads — zero manual work
        _, csv_updated = sync_latest_csv()

    print(f"  Scanning: {CSV_PATH}")

    # If new CSV detected, force cache refresh so new stocks get live data
    if csv_updated or args.refresh:
        if os.path.exists(CACHE_PATH):
            os.remove(CACHE_PATH)
            print(f"  Cache cleared — will fetch fresh data for new portfolio.")

    # 1. Parse CSV
    holdings = parse_fidelity_csv(CSV_PATH)
    if not holdings:
        print("  ERROR: No holdings loaded. Check portfolio.csv path.")
        sys.exit(1)
    print(f"  Loaded {len(holdings)} symbols from Fidelity CSV")

    # 2. Force refresh if requested
    if args.refresh and os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)
        print("  Cache cleared — will re-fetch all live data.")

    # 3. Fetch live fundamentals
    symbols = [s for s in holdings.keys() if s not in DESTINATION_HOLDS]
    live_data = fetch_all_fundamentals(symbols)

    # 4. Score and rank
    results = run_screen(holdings, live_data, top_n=args.top)
    print(f"  {len(results)} runner candidates found (score >= 8)\n")

    # 5. Print table
    print_table(results, live_data)

    if not results:
        print("  No candidates found. Try --refresh to fetch fresh data.")
        return

    # 6. Triage candidates
    triage_candidates = get_thinktank_candidates(results, live_data_map=live_data, max_stocks=6)
    print(f"\n  Screener picked: {triage_candidates}")
    print(f"  Think Tank candidates (triage order): {triage_candidates}")

    # Write upcoming 30-day earnings calendar to Obsidian brain
    _today_str = datetime.date.today().strftime("%Y%m%d")
    write_upcoming_catalysts(live_data, _today_str)

    if args.screen_only:
        sys.exit(0)

    # 7. Confirmation — reserved-word check prevents "yes" from becoming a ticker
    try:
        raw = input("  Use these? [Enter = yes, or type different tickers]: ").strip()
    except EOFError:
        raw = ""
        print("  (non-interactive mode — using screener picks)")
    if raw.lower() in ("yes", "y", ""):
        final_tickers = triage_candidates
    else:
        final_tickers = [t.strip().upper() for t in raw.replace(",", " ").split() if t.strip()]
    print(f"  Think Tank will run: {final_tickers}")

    # 7b. Pre-warm fundamentals cache via data layer so Think Tank reads without re-fetching
    if _HAS_DATA_LAYER and final_tickers:
        top15_tickers = [r["symbol"] for r in results[:15]]
        print(f"\n  Pre-warming fundamentals cache for {len(top15_tickers)} stocks...")
        try:
            get_fundamentals_batch(top15_tickers)
            print(f"  Cache warmed — Think Tank will read from disk.")
        except Exception as _pw_err:
            print(f"  Cache pre-warm warning: {_pw_err}")

    # 8. Seed generation (optional)
    if not args.no_seed and OR_KEY:
        sym_to_result = {r["symbol"]: r for r in results}
        top_results = [sym_to_result[s] for s in final_tickers if s in sym_to_result]
        if top_results:
            print(f"\n  Building ORACLE seed + prompt for: {[r['symbol'] for r in top_results]}...")
            seed, prompt = generate_seed_and_prompt(top_results, top_n=min(5, len(top_results)))
            seed_path, prompt_path = save_outputs(seed, prompt)
            print()
            print("=" * 75)
            print("  ORACLE FILES READY")
            print("=" * 75)
            print(f"  Seed:   {seed_path}")
            print(f"  Prompt: {prompt_path}")
            print()
            print("  PROMPT:")
            print("  " + "-" * 60)
            for line in prompt.split("\n"):
                print(f"  {line}")
            print()
            print("  TO RUN: open http://localhost:5001, upload seed, paste prompt, 3 markets")
    elif not OR_KEY:
        print("  NOTE: OPENROUTER_API_KEY not found — skipping seed generation")

    # 9. Launch Think Tank
    if args.no_seed:
        return

    if not final_tickers:
        print("  ERROR: No tickers to analyze.")
        return
    tt_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ORACLE/engine/oracle_think_tank.py"),
        os.path.expanduser("~/ORACLE/engine/oracle_think_tank.py"),
        os.path.expanduser("~/oracle_think_tank.py"),
    ]
    tt_path = next((p for p in tt_candidates if os.path.exists(p)), None)
    if not tt_path:
        print("  ERROR: oracle_think_tank.py not found — skipping Think Tank")
        return
    cmd = [sys.executable, tt_path, "--stocks"] + final_tickers
    if args.fast:
        cmd.append("--fast")
    if args.no_search:
        cmd.append("--no-search")
    if args.refresh:
        cmd.append("--fresh")
    print(f"\n  Launching Think Tank: {' '.join(cmd)}\n")
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    main()
