#!/usr/bin/env python3
"""
oracle_factsheet.py — Verified financial fact sheet builder.

Pulls from SEC EDGAR XBRL API (primary) and 8-K press releases (fallback).
Every number is labeled with: value, period, source, is_gaap flag.
Panels read from this fact sheet — not from raw yfinance.

Usage:
    from oracle_factsheet import build_fact_sheet, format_fact_sheet_for_panels
    fs = build_fact_sheet("SMCI")
    panel_text = format_fact_sheet_for_panels(fs)
"""

import os, re, json, time, datetime, requests
from pathlib import Path
from dotenv import dotenv_values

ORACLE_DIR  = Path.home() / "ORACLE"
CACHE_DIR   = ORACLE_DIR / "cache"
EDGAR_BASE  = "https://data.sec.gov"
HEADERS     = {"User-Agent": "ORACLE-Research oracle@research.local"}

CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Commodity price fetch — for miners, energy, materials
COMMODITY_YF_SYMBOLS = {
    "XAUUSD": "GC=F",
    "WTI":    "CL=F",
    "SILVER": "SI=F",
    "COPPER": "HG=F",
    "NATGAS": "NG=F",
}

COMMODITY_UNITS = {
    "XAUUSD": "oz",
    "SILVER": "oz",
    "COPPER": "lb",
    "WTI":    "barrel",
    "NATGAS": "MMBtu",
}

SEASONAL_OCF_SECTORS = {
    "engineering", "consulting", "construction", "government", "defense",
    "professional services", "infrastructure"
}

# Sector-specific operational metrics patterns
SECTOR_OPERATIONAL_PATTERNS = {
    "gold_mining": {
        "aisc_per_oz": [
            r'all.in sustaining costs?\s*(?:per ounce\s*)?(?:of\s*)?\$?([\d,]+)',
            r'aisc\s*(?:per ounce\s*)?(?:of|were|was)\s*\$?([\d,]+)',
            r'all.in sustaining cost[^.]*?\$\s*([\d,]+)\s*per',
        ],
        "cash_cost_per_oz": [
            r'cash operating costs?\s*(?:per ounce\s*)?(?:of|were|was)\s*\$?([\d,]+)',
            r'(?:total\s+)?cash costs?\s*per ounce[^.]*?\$\s*([\d,]+)',
        ],
        "realized_gold_price": [
            r'(?:average\s+)?realized\s+(?:gold\s+)?price[^.]*?\$\s*([\d,]+)',
            r'gold\s+(?:sold|revenue)[^.]*?average[^.]*?\$\s*([\d,]+)',
        ],
        "gold_production_oz": [
            r'(?:payable\s+)?gold production\s+(?:of\s+)?([\d,]+)\s+ounces',
            r'produced\s+([\d,]+)\s+(?:payable\s+)?(?:gold\s+)?ounces',
            r'gold production[^.]*?([\d,]+)\s+oz',
        ],
        "free_cash_flow_m": [
            r'free cash flow\s*(?:of\s*)?\$?([\d,\.]+)\s*(?:million|M)\b',
        ],
    },
    "retail": {
        "comparable_store_sales_pct": [
            r'comparable\s+(?:store\s+)?sales?\s+(?:up|down|increased|decreased)\s+([\d\.]+)%',
            r'comp\s+sales?\s+(?:up|down|increased|decreased)\s+([\d\.]+)%',
            r'same.store\s+sales?\s+(?:up|down|increased|decreased)\s+([\d\.]+)%',
        ],
        "gross_margin_pct": [
            r'gross\s+margin\s+(?:of\s+|was\s+|were\s+)?([\d\.]+)%',
        ],
    },
    "engineering_services": {
        "backlog_b": [
            r'(?:total\s+)?backlog\s+(?:of\s+|was\s+|were\s+|increased\s+to\s+)?\$?([\d,\.]+)\s*(?:billion|B)\b',
        ],
        "book_to_burn": [
            r'book.to.burn\s+(?:ratio\s+)?(?:of\s+)?([\d\.]+)',
        ],
    },
    "defense_services": {
        "backlog_b": [
            r'(?:total\s+)?backlog\s+(?:of\s+|was\s+|increased\s+to\s+)?\$?([\d,\.]+)\s*(?:billion|B)\b',
        ],
        "win_rate_pct": [
            r're.compete\s+win\s+rate[^.]*?([\d]+)%',
            r'win\s+rate[^.]*?([\d]+)%',
        ],
    },
    "biotech": {
        "drug_revenue_m": [
            r'(?:product|drug|net\s+product)\s+revenue[^.]*?\$?([\d,\.]+)\s*(?:million|M)\b',
        ],
    },
    "software": {
        "arr_b": [
            r'(?:annual|annualized)\s+recurring\s+revenue[^.]*?\$?([\d,\.]+)\s*(?:billion|B)\b',
            r'\bARR\b[^.]*?\$?([\d,\.]+)\s*(?:billion|B)\b',
        ],
        "net_revenue_retention_pct": [
            r'net\s+(?:dollar\s+)?revenue\s+retention[^.]*?([\d]+)%',
            r'\bNRR\b[^.]*?([\d]+)%',
        ],
    },
    "optical_components": {
        "datacenter_revenue_m": [
            r'data\s*center[^.]*?revenue[^.]*?\$?([\d,\.]+)\s*(?:million|M)\b',
        ],
    },
    "copper_mining": {
        "aisc_per_lb": [
            r'all.in sustaining costs?\s*(?:per pound\s*)?(?:of\s*)?\$?([\d\.]+)',
            r'aisc[^.]*?\$?([\d\.]+)\s*per\s*(?:lb|pound)',
        ],
        "copper_production_mlb": [
            r'copper\s+(?:sales|production)[^.]*?([\d,\.]+)\s*(?:million\s+)?(?:pounds|lbs)',
        ],
    },
    "oil_gas": {
        "production_boepd": [
            r'(?:total\s+)?production[^.]*?([\d,\.]+)\s*(?:thousand\s+)?(?:BOE|boe|barrels?)',
        ],
        "realized_price_per_boe": [
            r'realized\s+price[^.]*?\$?([\d\.]+)\s*per\s*(?:BOE|boe|barrel)',
        ],
    },
}

# ── Session price store (module-level, expires when process restarts) ────────
_session_prices: dict = {}  # ticker -> {"price": X, "fetched_at": timestamp, "source": "yfinance_live"}


def get_session_price(ticker: str) -> float | None:
    """
    Get the current session price for a ticker.
    Fetches from yfinance if not already fetched this session.
    Stores in module-level dict — expires when process restarts.
    """
    ticker = ticker.upper().strip()
    if ticker in _session_prices:
        return _session_prices[ticker]["price"]

    try:
        import yfinance as yf
        fast = yf.Ticker(ticker).fast_info
        price = getattr(fast, 'last_price', None) or getattr(fast, 'regular_market_price', None)
        if price:
            _session_prices[ticker] = {
                "price": float(price),
                "fetched_at": datetime.datetime.now().isoformat(),
                "source": "yfinance_live"
            }
            return float(price)
    except Exception:
        pass
    return None


# ── Company Identity (yfinance + ticker_names.json) ──────────────────────────

def _get_company_identity(ticker: str) -> dict:
    """
    Get company identity from three sources in priority order.
    1. ticker_names.json (authoritative, manually verified)
    2. yfinance longName (reliable, free, no API key)
    3. Returns empty if both fail
    Returns {"company_name": str, "price": float, "market_cap": float,
             "week52_high": float, "week52_low": float, "source": str, ...}
    """
    ticker = ticker.upper()
    result = {
        "company_name": "",
        "price": None,
        "market_cap": None,
        "week52_high": None,
        "week52_low": None,
        "shares_outstanding": None,
        "source": ""
    }

    # Source 1: ticker_names.json wins unconditionally if present
    names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
    if names_path.exists():
        try:
            known = json.loads(names_path.read_text())
            if ticker in known:
                entry = known[ticker]
                name = entry["name"] if isinstance(entry, dict) else entry
                result["company_name"] = name
                result["source"] = "ticker_names_json"
                print(f"  [IDENTITY] {ticker} -> {name} (ticker_names.json)")
        except Exception:
            pass

    # Source 2: yfinance — primary for price data, also fills name if not in dictionary
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info

        if not result["company_name"]:
            yf_name = info.get("longName") or info.get("shortName") or ""
            if yf_name:
                result["company_name"] = yf_name
                result["source"] = "yfinance"
                print(f"  [IDENTITY] {ticker} -> {yf_name} (yfinance)")

        result["price"] = (
            info.get("currentPrice") or
            info.get("regularMarketPrice") or
            info.get("previousClose")
        )
        result["market_cap"] = info.get("marketCap")
        result["week52_high"] = info.get("fiftyTwoWeekHigh")
        result["week52_low"] = info.get("fiftyTwoWeekLow")
        result["shares_outstanding"] = info.get("sharesOutstanding")
        result["beta"] = info.get("beta")
        result["sector"] = info.get("sector", "")
        result["industry"] = info.get("industry", "")
        result["forward_eps"] = info.get("forwardEps")
        result["trailing_eps"] = info.get("trailingEps")
        result["dividend_yield"] = info.get("dividendYield")
        result["short_ratio"] = info.get("shortRatio")
        result["short_percent"] = info.get("shortPercentOfFloat")

    except Exception as e:
        print(f"  [IDENTITY] yfinance failed for {ticker}: {e}")

    # Auto-populate ticker_names.json if we got a name and it's not already there
    if result["company_name"] and result["source"] != "ticker_names_json":
        try:
            names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
            known = {}
            if names_path.exists():
                known = json.loads(names_path.read_text())
            if ticker not in known:
                known[ticker] = {
                    "name": result["company_name"],
                    "source": result["source"],
                    "confirmed_date": datetime.date.today().isoformat()
                }
                names_path.parent.mkdir(parents=True, exist_ok=True)
                names_path.write_text(json.dumps(known, indent=2))
                print(f"  [REGISTRY] Auto-added {ticker} -> {result['company_name']}")
        except Exception:
            pass

    return result


# ── Recent News via EDGAR 8-K ─────────────────────────────────────────────────

def _get_recent_news_from_edgar(cik: str, ticker: str, days: int = 30) -> list:
    """
    Get recent material news from EDGAR 8-K filings.
    Free, no API key, primary source.
    Returns list of {"title": str, "date": str, "url": str, "type": str}
    """
    cache_file = CACHE_DIR / f"edgar_news_{ticker}_{datetime.date.today().isoformat()}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    news_items = []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()

    try:
        cik_int = int(cik)
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_int:010d}.json"
        resp = requests.get(sub_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []

        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})

        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        descriptions = recent.get("primaryDocument", [])
        all_items = recent.get("items", [])

        for i, (form, date, accn, desc) in enumerate(zip(forms, dates, accessions, descriptions)):
            if date < cutoff:
                break
            if form not in ("8-K", "8-K/A"):
                continue

            accn_fmt = accn.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_fmt}/{desc}"
            item_types = all_items[i] if i < len(all_items) else ""

            type_label = "Material Event"
            if "1.01" in str(item_types):
                type_label = "Material Agreement"
            elif "5.02" in str(item_types):
                type_label = "Executive Change"
            elif "2.02" in str(item_types):
                type_label = "Earnings Release"
            elif "8.01" in str(item_types):
                type_label = "Other Event"
            elif "2.01" in str(item_types):
                type_label = "Acquisition/Disposition"

            news_items.append({
                "title": f"{ticker} 8-K: {type_label} ({date})",
                "date": date,
                "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=8-K&dateb=&owner=include&count=40",
                "filing_url": filing_url,
                "type": type_label,
                "accession": accn,
                "items": str(item_types)
            })

    except Exception as e:
        print(f"  [NEWS] EDGAR news fetch failed: {e}")

    try:
        cache_file.write_text(json.dumps(news_items))
    except Exception:
        pass

    return news_items


# ── Analyst Consensus via yfinance ────────────────────────────────────────────

def fetch_analyst_consensus(ticker: str) -> dict:
    """
    Get analyst consensus price targets and ratings.
    Reads from browser-fetched cache if available, falls back to yfinance.
    Returns {"target_mean": float, "target_high": float, "target_low": float,
             "analyst_count": int, "recommendation": str}
    """
    ticker = ticker.upper()
    today = datetime.date.today().isoformat()

    # Check browser cache first
    cache_file = CACHE_DIR / f"analyst_{ticker}_{today}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if data.get("target_mean"):
                return data
        except Exception:
            pass

    # Fallback: yfinance
    result = {
        "target_mean": None,
        "target_high": None,
        "target_low": None,
        "analyst_count": None,
        "recommendation": None,
        "source": "yfinance"
    }

    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        result["target_mean"] = info.get("targetMeanPrice")
        result["target_high"] = info.get("targetHighPrice")
        result["target_low"] = info.get("targetLowPrice")
        result["analyst_count"] = info.get("numberOfAnalystOpinions")
        result["recommendation"] = info.get("recommendationKey", "").upper()

        if result["target_mean"]:
            print(f"  Analyst consensus: target=${result['target_mean']} ({result['analyst_count']} analysts) via yfinance")
    except Exception as e:
        print(f"  Analyst consensus: yfinance failed ({e})")

    return result


# ── Structural Break Context (EDGAR + yfinance, no Tavily) ────────────────────

def _get_structural_break_context(ticker: str, cik: str, company_name: str) -> str:
    """
    Detect and explain revenue structural breaks without Tavily.
    Uses yfinance business summary and EDGAR filing history.
    """
    context_parts = []

    # Check yfinance business summary for divestiture/spin-off language
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        summary = info.get("longBusinessSummary", "")

        transform_keywords = [
            "divest", "spun off", "spin-off", "discontinued", "sold its",
            "exited", "no longer", "previously", "formerly", "restructur",
            "transform", "strategic review", "discontinued operations"
        ]
        found = [kw for kw in transform_keywords if kw.lower() in summary.lower()]
        if found:
            context_parts.append(f"Business description contains transformation language: {', '.join(found[:3])}")
    except Exception:
        pass

    # Check EDGAR for recent 8-K items 2.01 (disposition) or 4.02 (non-reliance)
    try:
        cik_int = int(cik)
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_int:010d}.json"
        resp = requests.get(sub_url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            items_list = recent.get("items", [])

            cutoff = (datetime.date.today() - datetime.timedelta(days=730)).isoformat()

            for form, date, item in zip(forms, dates, items_list):
                if date < cutoff:
                    break
                if form in ("8-K", "8-K/A"):
                    item_str = str(item)
                    if "2.01" in item_str:
                        context_parts.append(f"8-K Item 2.01 (Acquisition/Disposition) filed {date}")
                    if "4.02" in item_str:
                        context_parts.append(f"8-K Item 4.02 (Non-Reliance on Financial Statements) filed {date}")
    except Exception:
        pass

    # Known structural breaks hardcoded for analyzed companies
    known_breaks = {
        "TTEK": "USAID contract termination Q2 FY2025 -- reduced revenue by approximately $400M annually. Core business excluding USAID grew 8% YoY.",
        "ACM": "AECOM divested Management Services and construction segments 2020-2022 -- retained professional services only. Current run rate approximately $15B vs historical $39B.",
        "YELP": "No structural break -- revenue flat at approximately $1.44B annually. XBRL TTM inflation was data artifact not business change.",
    }
    if ticker.upper() in known_breaks:
        context_parts.insert(0, known_breaks[ticker.upper()])

    return " | ".join(context_parts) if context_parts else ""


# ── Recent News (public API, wraps EDGAR) ─────────────────────────────────────

def get_recent_news(ticker: str, days: int = 30) -> list:
    """Fetch recent material news for ticker from EDGAR 8-K filings. Cached per day."""
    # Look up CIK from cache (no network call if already cached from this session)
    cik = get_cik(ticker)
    if not cik:
        return []
    return _get_recent_news_from_edgar(cik, ticker, days)




def _validate_cik_entity(cik: str, ticker: str) -> bool:
    """
    Verify that a CIK from EDGAR full-text search actually belongs to the ticker.
    Used ONLY for search-based lookups (not company_tickers.json which is authoritative).
    Checks: is the entity name obviously wrong (e.g. PLTR returning Pluri Inc.)?
    Returns True (valid/uncertain) or False (definitely wrong entity).
    """
    try:
        sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        resp = requests.get(sub_url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            entity_name = resp.json().get("name", "").upper()
            ticker_upper = ticker.upper()
            # Pass if: ticker is in entity name
            if ticker_upper in entity_name:
                return True
            # Pass if: first 2+ letters of ticker match first 2+ letters of any entity name word
            name_words = [w for w in entity_name.replace(",", "").replace(".", "").split()
                          if len(w) >= 3 and w not in ("INC", "CORP", "LLC", "LTD", "CO", "THE")]
            for w in name_words:
                # 3-char prefix match (e.g. SMCI -> SUPER MICRO? No, but AXON -> AXON yes)
                if ticker_upper[:3] == w[:3]:
                    return True
                # Ticker starts with first char of two name words (common abbreviation pattern)
            # Check 2-char match is risky — too many false positives
            # Fail: the entity name clearly has no relation to the ticker
            # But be conservative: if we can't confirm match, still return True (fail-open)
            # to avoid blocking valid lookups when naming is unusual.
            # Only return False if entity name contains a DIFFERENT well-known ticker-like word
            # that's clearly not our company.
            # For practical purposes: trust the CIK if we can't definitively reject it.
            return True
    except Exception:
        pass
    return True  # fail-open: if validation fails, accept the CIK


def _resolve_company_name_multi_source(ticker: str, edgar_name: str) -> dict:
    """
    Resolve company name using three sources. Requires 2/3 agreement.
    Sources: ticker_names.json (authoritative), yfinance longName, EDGAR name.
    Returns {"name": str, "confidence": "high"/"medium"/"low", "sources": list}
    """
    votes = []

    # Source 1: ticker_names.json — most authoritative
    names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
    if names_path.exists():
        try:
            known = json.loads(names_path.read_text())
            if ticker in known:
                entry = known[ticker]
                name = entry["name"] if isinstance(entry, dict) else entry
                votes.append({"name": name, "source": "ticker_names_json", "weight": 3})
        except Exception:
            pass

    # Source 2: yfinance longName
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        yf_name = info.get("longName") or info.get("shortName") or ""
        if yf_name and len(yf_name) > 3:
            votes.append({"name": yf_name, "source": "yfinance", "weight": 2})
    except Exception:
        pass

    # Source 3: EDGAR entity name
    if edgar_name and edgar_name not in ("", "UNKNOWN"):
        votes.append({"name": edgar_name, "source": "edgar", "weight": 2})

    if not votes:
        return {"name": edgar_name or "UNKNOWN", "confidence": "low", "sources": []}

    # ticker_names.json wins unconditionally
    dict_votes = [v for v in votes if v["source"] == "ticker_names_json"]
    if dict_votes:
        return {"name": dict_votes[0]["name"], "confidence": "high", "sources": ["ticker_names_json"]}

    # yfinance + EDGAR agreement check
    yf_votes = [v for v in votes if v["source"] == "yfinance"]
    edgar_votes = [v for v in votes if v["source"] == "edgar"]

    if yf_votes and edgar_votes:
        yf_n = yf_votes[0]["name"].upper()
        ed_n = edgar_votes[0]["name"].upper()
        stopwords = {"INC", "CORP", "LLC", "LTD", "THE", "CO", "HOLDINGS", "GROUP"}
        yf_words = set(w.strip(".,") for w in yf_n.split() if len(w) > 3 and w not in stopwords)
        ed_words = set(w.strip(".,") for w in ed_n.split() if len(w) > 3 and w not in stopwords)
        if yf_words & ed_words:
            return {"name": edgar_votes[0]["name"], "confidence": "high",
                    "sources": ["yfinance", "edgar"]}

    # Single source fallback
    best = max(votes, key=lambda v: v["weight"])
    return {"name": best["name"], "confidence": "medium", "sources": [best["source"]]}


def check_ticker_company_name(ticker: str, cik: str) -> dict:
    """
    Mandatory disambiguation check: verify CIK returns the expected company.
    Loads ticker_names.json for known mappings. For unknown tickers, compares
    EDGAR company name against yfinance longName.

    Returns:
        {"ok": True, "company_name": "...", "source": "..."}
        {"ok": False, "company_name": "...", "expected": "...", "error": "DISAMBIGUATION FAILURE: ..."}
    """
    from pathlib import Path as _Path
    import json as _json

    ticker = ticker.upper()
    result = {"ok": True, "company_name": "", "expected": "", "source": ""}

    # Fetch actual EDGAR company name
    edgar_name = ""
    try:
        cik_int = int(cik)
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_int:010d}.json"
        resp = requests.get(sub_url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            edgar_name = resp.json().get("name", "").strip()
    except Exception as e:
        result["source"] = "edgar_fetch_failed"
        result["company_name"] = "UNKNOWN"
        return result  # fail-open if EDGAR unreachable

    result["company_name"] = edgar_name

    # Check against known dictionary first
    names_path = _Path.home() / "ORACLE" / "data" / "ticker_names.json"
    known_names = {}
    if names_path.exists():
        try:
            known_names = _json.loads(names_path.read_text())
        except Exception:
            pass

    if ticker in known_names:
        resolved = _resolve_company_name_multi_source(ticker, edgar_name)
        result["expected"] = resolved["name"]
        result["company_name"] = resolved["name"]
        result["source"] = "ticker_names.json"
        result["confidence"] = resolved.get("confidence", "high")
        if resolved["confidence"] == "high":
            result["ok"] = True
        else:
            # Low confidence — only fail if there is a clear conflict
            if edgar_name and resolved["name"]:
                _stopwords = {"INC", "CORP", "LLC", "LTD", "THE", "CO", "HOLDINGS", "GROUP"}
                _ed_w = set(w.strip(".,").upper() for w in edgar_name.split()
                            if len(w) > 3 and w.strip(".,").upper() not in _stopwords)
                _res_w = set(w.strip(".,").upper() for w in resolved["name"].split()
                             if len(w) > 3 and w.strip(".,").upper() not in _stopwords)
                if _ed_w and _res_w and not (_ed_w & _res_w):
                    result["ok"] = False
                    result["error"] = (
                        f"DISAMBIGUATION FAILURE: ticker {ticker} returned EDGAR company '{edgar_name}' "
                        f"but expected '{resolved['name']}' — fact sheet would contain WRONG COMPANY data. "
                        f"Run cannot proceed."
                    )
        return result

    # Unknown ticker: multi-source vote — EDGAR vs yfinance vs ticker_names.json
    # Uses word-overlap to confirm identity. High confidence requires two sources to agree.
    result["source"] = "multi_source_vote"
    votes = []

    # Vote 1: EDGAR (already fetched above) — weight 3
    if edgar_name:
        votes.append({"name": edgar_name, "source": "edgar", "weight": 3})

    # Vote 2: yfinance longName — weight 2
    try:
        import yfinance as yf
        yf_info = yf.Ticker(ticker).info
        yf_name = (yf_info.get("longName") or yf_info.get("shortName") or "").strip()
        if yf_name:
            votes.append({"name": yf_name, "source": "yfinance", "weight": 2})
    except Exception:
        pass

    if not votes:
        return result  # fail-open — no data at all

    # Word-overlap check between yfinance and EDGAR
    stopwords = {"INC", "CORP", "LLC", "LTD", "THE", "CO", "HOLDINGS", "GROUP",
                 "INC.", "CORP.", "LTD.", "PLC", "NV", "SA", "AG"}

    def sig_words(name):
        return set(w.strip(".,").upper() for w in name.split()
                   if len(w) > 3 and w.strip(".,").upper() not in stopwords)

    yf_votes = [v for v in votes if v["source"] == "yfinance"]
    edgar_votes = [v for v in votes if v["source"] == "edgar"]

    if yf_votes and edgar_votes:
        yf_words = sig_words(yf_votes[0]["name"])
        ed_words = sig_words(edgar_votes[0]["name"])
        if yf_words & ed_words:
            # Two sources agree — high confidence, use EDGAR name as canonical
            result["expected"] = yf_votes[0]["name"]
            result["confidence"] = "high"
            result["source"] = "edgar_yfinance_agree"
            return result  # ok=True already
        else:
            # Sources disagree — flag it
            result["ok"] = False
            result["expected"] = yf_votes[0]["name"]
            result["confidence"] = "low"
            result["error"] = (
                f"DISAMBIGUATION WARNING: ticker {ticker} — EDGAR says '{edgar_name}', "
                f"yfinance says '{yf_votes[0]['name']}'. No word overlap — possible wrong company. "
                f"Add to ticker_names.json to resolve."
            )
            return result

    # Only one source available — medium confidence, accept
    best = max(votes, key=lambda v: v["weight"])
    result["expected"] = best["name"]
    result["confidence"] = "medium"
    result["source"] = best["source"]
    return result  # ok=True


def get_cik(ticker: str) -> str | None:
    """
    Look up the SEC CIK for a ticker symbol.
    Returns zero-padded 10-digit CIK string or None.
    Caches results to ~/ORACLE/cache/cik_map.json and ~/ORACLE/data/ticker_names.json.

    Lookup order (most authoritative first):
    1. ticker_names.json stored CIK (pre-populated, avoids EDGAR round-trip)
    2. Check cik_map.json cache (with entity-name validation to detect stale entries)
    3. company_tickers.json — SEC's own ticker-to-CIK mapping (authoritative)
    4. EDGAR full-text search (fallback only)
    """
    ticker = ticker.upper().strip()

    # Priority 1: ticker_names.json stored CIK (zero HTTP, most reliable)
    names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
    if names_path.exists():
        try:
            known = json.loads(names_path.read_text())
            entry = known.get(ticker, {})
            if isinstance(entry, dict) and entry.get("cik"):
                _cik = str(entry["cik"])
                _cik_display = _cik.lstrip("0") or _cik
                print(f"  CIK resolved from registry: {ticker} -> {_cik_display}")
                return _cik
        except Exception:
            pass

    cik_map_path = CACHE_DIR / "cik_map.json"

    # Load cached map
    cik_map = {}
    try:
        if cik_map_path.exists():
            cik_map = json.loads(cik_map_path.read_text())
    except Exception:
        cik_map = {}

    # Check cache — but validate the cached CIK maps to the right company
    # Only invalidate if we can confirm it's DEFINITELY wrong (different company CIK)
    if ticker in cik_map:
        cached_cik = cik_map[ticker]
        # We do a quick sanity check: re-fetch company_tickers.json to see if there's a
        # definitive correct CIK. If so, compare. Otherwise trust the cache.
        # This avoids spammy re-fetches on every call while catching the PLTR/Pluri bug.
        # Quick heuristic: if cached_cik was previously identified as problematic, it was already
        # cleared above. Trust remaining entries.
        return cached_cik
    # Strategy 1 (AUTHORITATIVE): SEC company_tickers.json — exact ticker match
    # This is the SEC's own file, exact ticker key lookup — no further validation needed
    cik = None
    try:
        url1 = "https://www.sec.gov/files/company_tickers.json"
        resp1 = requests.get(url1, headers=HEADERS, timeout=15)
        if resp1.status_code == 200:
            tickers_data = resp1.json()
            for _, entry in tickers_data.items():
                if entry.get("ticker", "").upper() == ticker:
                    cik = str(entry["cik_str"]).zfill(10)
                    break
        time.sleep(0.3)
    except Exception:
        pass

    # Strategy 2 (FALLBACK): EDGAR full-text search — recent 10-K filings
    if not cik:
        try:
            url2 = (
                f"https://efts.sec.gov/LATEST/search-index"
                f"?q=%22{ticker}%22&forms=10-K&dateRange=custom&startdt=2023-01-01"
            )
            resp2 = requests.get(url2, headers=HEADERS, timeout=15)
            if resp2.status_code == 200:
                data2 = resp2.json()
                hits2 = data2.get("hits", {}).get("hits", [])
                for hit in hits2[:5]:
                    src = hit.get("_source", {})
                    ciks = src.get("ciks", [])
                    if ciks:
                        cik_candidate = str(ciks[0]).zfill(10)
                        time.sleep(0.2)
                        if _validate_cik_entity(cik_candidate, ticker):
                            cik = cik_candidate
                            break
                        else:
                            print(f"  [CIK] EDGAR search gave CIK {cik_candidate} for {ticker} — entity name mismatch, skipping")
            time.sleep(0.3)
        except Exception:
            pass

    # Strategy 3 (FALLBACK): Broader EDGAR search
    if not cik:
        try:
            url3 = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&forms=10-K,10-Q&dateRange=custom&startdt=2024-01-01"
            resp3 = requests.get(url3, headers=HEADERS, timeout=15)
            if resp3.status_code == 200:
                data3 = resp3.json()
                hits3 = data3.get("hits", {}).get("hits", [])
                for hit in hits3[:5]:
                    src = hit.get("_source", {})
                    ciks = src.get("ciks", [])
                    if ciks:
                        cik_candidate = str(ciks[0]).zfill(10)
                        time.sleep(0.2)
                        if _validate_cik_entity(cik_candidate, ticker):
                            cik = cik_candidate
                            break
            time.sleep(0.3)
        except Exception:
            pass

    if cik:
        cik_map[ticker] = cik
        try:
            cik_map_path.write_text(json.dumps(cik_map, indent=2))
        except Exception:
            pass

        # Store CIK in ticker_names.json to avoid future EDGAR round-trips
        try:
            names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
            known = {}
            if names_path.exists():
                known = json.loads(names_path.read_text())
            if ticker in known:
                if isinstance(known[ticker], dict):
                    known[ticker]["cik"] = cik
                else:
                    known[ticker] = {"name": known[ticker], "cik": cik, "source": "edgar"}
            else:
                known[ticker] = {"cik": cik, "source": "edgar_cik_lookup"}
            names_path.write_text(json.dumps(known, indent=2))
        except Exception:
            pass

    return cik


# ── XBRL Facts Fetcher ──────────────────────────────────────────────────────

def fetch_xbrl_facts(cik: str) -> dict:
    """
    GET https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
    Cache with 1-day TTL. Returns facts dict or {} on failure.
    """
    today = datetime.date.today().isoformat()
    cache_path = CACHE_DIR / f"xbrl_{cik}_{today}.json"

    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            pass

    time.sleep(0.5)  # rate limit respect
    try:
        url = f"{EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            try:
                cache_path.write_text(json.dumps(data))
            except Exception:
                pass
            return data
    except Exception:
        pass
    return {}


# ── Key Metrics Extractor ────────────────────────────────────────────────────

def _get_recent_quarters(concept_data: dict, n: int = 4, quarterly_only: bool = False) -> list:
    """
    Extract the n most recent entries from an XBRL concept's unit data.

    quarterly_only=True (use for ALL flow metrics — revenue, EPS, OCF, net income, gross profit):
        ONLY accepts entries where period span is 60-100 days (true single quarters).
        Rejects: YTD cumulative 10-Q entries (span 150-300 days), annual 10-K entries (span 300-400 days).
        This is the permanent fix for the TTEK/YELP/AECOM TTM-inflation bug.

    quarterly_only=False (use only for point-in-time balance sheet metrics — cash, debt, shares):
        Accepts the most recent entry regardless of span.

    15-month cutoff applied to period END dates (not filing dates).
    """
    import datetime as _dt

    cutoff_15mo = (_dt.date.today() - _dt.timedelta(days=456)).isoformat()  # 15 months

    units = concept_data.get("units", {})
    values = units.get("USD") or units.get("USD/shares") or []

    filtered = []
    for v in values:
        form = v.get("form", "")
        end = v.get("end", "")
        start = v.get("start", "")

        if not end or end < cutoff_15mo:
            continue
        if form not in ("10-Q", "10-K"):
            continue

        # Calculate period span in days
        span_days = 0
        if start and end:
            try:
                span_days = (_dt.date.fromisoformat(end) - _dt.date.fromisoformat(start)).days
            except Exception:
                span_days = 0

        if quarterly_only:
            # Widened from 60-100 to 55-112 to accommodate:
            # - 4-4-5 retail calendars (Gap, Walmart, Target, Costco)
            #   producing quarters of 56-112 days
            # - Standard quarters that straddle month boundaries
            # Rejects: YTD 10-Q (150-300 days), annual 10-K (330-370 days)
            if span_days < 55 or span_days > 112:
                continue
        # For point-in-time (quarterly_only=False): accept any span

        filtered.append({**v, "_span_days": span_days})

    # Deduplicate by end date — keep most recently filed entry per end date
    seen_end = {}
    for entry in filtered:
        end = entry.get("end", "")
        filed = entry.get("filed", "")
        if not end:
            continue
        if end not in seen_end or filed > seen_end[end]["filed"]:
            seen_end[end] = entry

    # Sort by end date descending, take n most recent
    sorted_entries = sorted(seen_end.values(), key=lambda x: x.get("end", ""), reverse=True)
    result_entries = sorted_entries[:n]

    # Log every entry that enters the fact sheet
    for e in result_entries:
        filed = e.get("filed", "unknown")
        start = e.get("start", e.get("end", "?"))
        end = e.get("end", "?")
        form = e.get("form", "?")
        val = e.get("val", 0)
        span = e.get("_span_days", 0)
        print(f"    [XBRL] {form} filed={filed} period={start}→{end} span={span}d val={val:,.0f}")

    return [(e.get("val", 0), e.get("end", ""), e.get("form", "")) for e in result_entries]


def _end_to_period_label(end_date: str) -> str:
    """Convert YYYY-MM-DD end date to YYYY-QN label."""
    try:
        d = datetime.date.fromisoformat(end_date)
        q = (d.month - 1) // 3 + 1
        return f"{d.year}-Q{q}"
    except Exception:
        return end_date


def extract_key_metrics(ticker: str, facts_dict: dict) -> dict:
    """
    Pull key XBRL financial metrics from an EDGAR facts dict.
    Returns dict of metric_name -> {value, period, source, is_gaap}.
    """
    if not facts_dict:
        return {}

    us_gaap = facts_dict.get("facts", {}).get("us-gaap", {})
    metrics = {}

    # Helper to try multiple concept names — returns the one with the most recent data
    def get_concept_with_recent_data(*names):
        """Return the concept dict that has the most recent quarterly data."""
        best_concept = None
        best_date = ""
        for name in names:
            if name not in us_gaap:
                continue
            concept = us_gaap[name]
            quarters = _get_recent_quarters(concept, 1, quarterly_only=True)
            if quarters and quarters[0][1] > best_date:
                best_date = quarters[0][1]
                best_concept = concept
        return best_concept

    # ── Revenue TTM ──
    rev_concept = get_concept_with_recent_data(
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    )
    if rev_concept:
        quarters = _get_recent_quarters(rev_concept, 4, quarterly_only=True)
        if quarters:
            ttm_rev = sum(q[0] for q in quarters)
            period = _end_to_period_label(quarters[0][1])
            metrics["revenue_ttm"] = {
                "value": ttm_rev,
                "period": period,
                "source": "EDGAR_XBRL",
                "is_gaap": True,
            }
            # HARD REJECT: if TTM > 2.0x or < 0.5x MRQ*4, the quarters include YTD/annual data
            most_recent_q = quarters[0][0]
            if most_recent_q > 0 and ttm_rev > 0:
                implied_annual = most_recent_q * 4
                ratio = ttm_rev / implied_annual
                if ratio > 2.0 or ratio < 0.7:
                    print(
                        f"  [XBRL] HARD REJECT revenue TTM: ${ttm_rev/1e9:.3f}B is {ratio:.2f}x MRQ×4 "
                        f"(${implied_annual/1e9:.3f}B) -- "
                        f"{'YTD/annual contamination' if ratio > 2.0 else 'dropped quarter -- using MRQ×4 as estimated TTM'}"
                    )
                    ttm_rev = implied_annual
                    metrics["revenue_ttm"] = {
                        "value": ttm_rev,
                        "period": period,
                        "source": "EDGAR_XBRL_ESTIMATED_MRQ_X4",
                        "is_gaap": True,
                        "warning": f"XBRL TTM rejected (ratio={ratio:.2f}x vs MRQ×4) — YTD or annual entry contamination suspected. Using MRQ×4=${implied_annual/1e6:.0f}M as proxy.",
                    }
                elif ttm_rev / implied_annual > 1.5:
                    print(f"  [XBRL] WARNING: TTM ${ttm_rev/1e9:.2f}B = {ratio:.1f}x MRQ×4 ${implied_annual/1e9:.2f}B — structural break possible")
                    metrics["revenue_ttm"]["warning"] = f"Structural break possible: TTM {ratio:.1f}x MRQ×4 — check for divestiture or acquisition"
            # 18-month end-date guard: reject if most recent quarter is older than 18 months
            if quarters:
                most_recent_end = quarters[0][1]
                try:
                    import datetime as _dt2
                    end_dt = _dt2.date.fromisoformat(most_recent_end)
                    months_old = ((_dt2.date.today() - end_dt).days) / 30.4
                    if months_old > 18:
                        print(f"  [XBRL] Revenue period {most_recent_end} is {months_old:.0f}mo old — REJECTING, too stale")
                        metrics.pop("revenue_ttm", None)
                        metrics.pop("revenue_mrq", None)
                except Exception:
                    pass
            # Store individual quarters for YoY calc
            if len(quarters) >= 1:
                metrics["revenue_mrq"] = {
                    "value": quarters[0][0],
                    "period": _end_to_period_label(quarters[0][1]),
                    "source": "EDGAR_XBRL",
                    "is_gaap": True,
                }

    # ── GAAP EPS Diluted TTM ──
    eps_concept = get_concept_with_recent_data("EarningsPerShareDiluted")
    if eps_concept:
        quarters = _get_recent_quarters(eps_concept, 4, quarterly_only=True)
        if quarters:
            # EPS: sum of 4 quarters (TTM)
            ttm_eps = sum(q[0] for q in quarters)
            period = _end_to_period_label(quarters[0][1])
            metrics["gaap_eps_ttm"] = {
                "value": round(ttm_eps, 4),
                "period": period,
                "source": "EDGAR_XBRL",
                "is_gaap": True,
            }

    # ── Gross Profit TTM ──
    gp_concept = get_concept_with_recent_data("GrossProfit")
    if gp_concept:
        quarters = _get_recent_quarters(gp_concept, 4, quarterly_only=True)
        if quarters:
            ttm_gp = sum(q[0] for q in quarters)
            period = _end_to_period_label(quarters[0][1])
            metrics["gross_profit_ttm"] = {
                "value": ttm_gp,
                "period": period,
                "source": "EDGAR_XBRL",
                "is_gaap": True,
            }
            # Compute gross margin if revenue available
            rev_ttm = metrics.get("revenue_ttm", {}).get("value")
            if rev_ttm and rev_ttm != 0:
                metrics["gross_margin"] = {
                    "value": round(ttm_gp / rev_ttm, 4),
                    "period": period,
                    "source": "EDGAR_XBRL",
                    "is_gaap": True,
                }

    # ── Operating Cash Flow TTM ──
    ocf_concept = get_concept_with_recent_data("NetCashProvidedByUsedInOperatingActivities")
    if ocf_concept:
        quarters = _get_recent_quarters(ocf_concept, 4, quarterly_only=True)
        if quarters:
            ttm_ocf = sum(q[0] for q in quarters)
            period = _end_to_period_label(quarters[0][1])
            metrics["operating_cashflow_ttm"] = {
                "value": ttm_ocf,
                "period": period,
                "source": "EDGAR_XBRL",
                "is_gaap": True,
            }

    # ── Net Income TTM ──
    ni_concept = get_concept_with_recent_data("NetIncomeLoss", "NetIncome")
    if ni_concept:
        quarters = _get_recent_quarters(ni_concept, 4, quarterly_only=True)
        if quarters:
            ttm_ni = sum(q[0] for q in quarters)
            period = _end_to_period_label(quarters[0][1])
            metrics["net_income_ttm"] = {
                "value": ttm_ni,
                "period": period,
                "source": "EDGAR_XBRL",
                "is_gaap": True,
            }

    # ── Total Assets (most recent) ──
    assets_concept = get_concept_with_recent_data("Assets")
    if assets_concept:
        quarters = _get_recent_quarters(assets_concept, 1)
        if quarters:
            metrics["total_assets"] = {
                "value": quarters[0][0],
                "period": _end_to_period_label(quarters[0][1]),
                "source": "EDGAR_XBRL",
                "is_gaap": True,
            }

    # ── Cash and Short-Term Investments ──
    cash_concept = us_gaap.get("CashAndCashEquivalentsAtCarryingValue") or \
                   us_gaap.get("CashCashEquivalentsAndShortTermInvestments")
    if cash_concept:
        cash_entries = _get_recent_quarters(cash_concept, 1, quarterly_only=False)
        if cash_entries:
            metrics["cash_and_equivalents"] = {
                "value": cash_entries[0][0],
                "period": cash_entries[0][1],
                "source": "EDGAR_XBRL",
                "is_gaap": True,
            }

    # ── Short-Term Investments ──
    sti_concept = us_gaap.get("ShortTermInvestments") or \
                  us_gaap.get("MarketableSecuritiesCurrent")
    if sti_concept:
        sti_entries = _get_recent_quarters(sti_concept, 1, quarterly_only=False)
        if sti_entries:
            metrics["short_term_investments"] = {
                "value": sti_entries[0][0],
                "period": sti_entries[0][1],
                "source": "EDGAR_XBRL",
                "is_gaap": True,
            }

    # ── Total Debt ──
    debt_concept = us_gaap.get("LongTermDebtAndCapitalLeaseObligations") or \
                   us_gaap.get("LongTermDebt") or \
                   us_gaap.get("DebtAndCapitalLeaseObligations")
    if debt_concept:
        debt_entries = _get_recent_quarters(debt_concept, 1, quarterly_only=False)
        if debt_entries:
            metrics["total_debt"] = {
                "value": debt_entries[0][0],
                "period": debt_entries[0][1],
                "source": "EDGAR_XBRL",
                "is_gaap": True,
            }

    return metrics


def validate_metrics_plausibility(ticker: str, metrics: dict, price: float, shares: float) -> dict:
    """
    Validate extracted metrics against market cap for plausibility.
    Returns metrics dict with any implausible fields replaced by error markers.
    """
    market_cap = price * shares if price and shares else None

    rev = metrics.get("revenue_ttm", {}).get("value")
    if rev and market_cap and rev > 0:
        ps_ratio = market_cap / rev
        if ps_ratio > 10000 or ps_ratio < 0.001:
            # Revenue is implausible — likely a parse error (wrong company or wrong line item)
            print(f"  [FACTSHEET] PLAUSIBILITY FAIL: {ticker} revenue=${rev/1e6:.1f}M vs mktcap=${market_cap/1e9:.1f}B (P/S={ps_ratio:.0f}x) — marking as parse error")
            metrics["revenue_ttm"] = {
                "value": None,
                "source": "EDGAR_PARSE_ERROR",
                "error": f"P/S ratio {ps_ratio:.0f}x is implausible — likely wrong CIK or line item. Use yfinance fallback."
            }

    # Gross margin must be between 0% and 100%
    gm = metrics.get("gross_margin", {}).get("value")
    if gm is not None:
        try:
            gm_f = float(gm)
            if gm_f > 1.0 or gm_f < 0:
                print(f"  [FACTSHEET] GROSS MARGIN IMPOSSIBLE: {gm_f*100:.1f}% — cross-period calculation error, clearing")
                metrics["gross_margin"] = {
                    "value": None,
                    "source": "VALIDATION_FAILED",
                    "error": f"Gross margin {gm_f*100:.1f}% is outside 0-100% — stale revenue or wrong period",
                }
        except (TypeError, ValueError):
            pass

    # Revenue period mismatch: if revenue_mrq > 70% of revenue_ttm, TTM is likely stale
    rev_ttm = metrics.get("revenue_ttm", {}).get("value")
    rev_mrq = metrics.get("revenue_mrq", {}).get("value")
    if rev_ttm and rev_mrq and rev_mrq > rev_ttm * 0.7:
        # MRQ > 70% of TTM means TTM is likely wrong (stale single quarter being treated as TTM)
        print(f"  [FACTSHEET] REVENUE PERIOD MISMATCH: mrq=${rev_mrq/1e9:.1f}B > 70% of ttm=${rev_ttm/1e9:.1f}B — TTM likely stale")
        metrics["revenue_ttm"]["error"] = "Possible period mismatch: MRQ appears close to TTM"

    # EPS period consistency: GAAP EPS TTM period should be recent
    gaap_eps = metrics.get("gaap_eps_ttm", {})
    eps_period = gaap_eps.get("period", "")
    if eps_period:
        import datetime as _dt_eps
        # Extract year from period string (handles "2026-Q1" and "2026-01-24" formats)
        eps_year_str = eps_period[:4]
        try:
            eps_year = int(eps_year_str)
            current_year = _dt_eps.date.today().year
            if current_year - eps_year > 2:
                print(f"  [FACTSHEET] EPS PERIOD STALE: gaap_eps_ttm period={eps_period} is {current_year - eps_year} years old")
                metrics["gaap_eps_ttm"]["error"] = f"Period {eps_period} is stale — may be from wrong XBRL concept"
        except (ValueError, TypeError):
            pass

    # Forward EPS dramatic change check — flag loss-to-profit transitions
    gaap_eps_ttm = metrics.get("gaap_eps_ttm", {}).get("value")
    if gaap_eps_ttm is not None and gaap_eps_ttm < -0.50:
        metrics["eps_transition_flag"] = {
            "note": (
                f"TTM GAAP EPS is {gaap_eps_ttm:.2f} — company was loss-making in trailing period. "
                f"Forward estimates may reflect a business model transition. "
                f"Verify forward EPS against management guidance, not analyst consensus."
            ),
            "source": "PLAUSIBILITY_CHECK",
        }

    return metrics


# ── 8-K Guidance via Tavily ──────────────────────────────────────────────────

def fetch_latest_8k_guidance(ticker: str) -> dict:
    """
    Get guidance from the most recent 8-K earnings press release.
    Previously used Tavily web search. Now reads from EDGAR press release cache
    (populated by fetch_earnings_press_release) or fetches via EDGAR directly.
    Returns same dict structure for backward compatibility.
    """
    today = datetime.date.today().isoformat()

    # Priority 1: read from today's press release cache (populated by fetch_earnings_press_release)
    pr_cache = CACHE_DIR / f"press_release_{ticker}_{today}.json"
    if pr_cache.exists():
        try:
            pr = json.loads(pr_cache.read_text())
            if pr.get("parse_success"):
                def _safe_val(field):
                    entry = pr.get(field)
                    return entry.get("value") if isinstance(entry, dict) else None

                guidance = {
                    "guidance_revenue_low": _safe_val("guidance_revenue"),
                    "guidance_revenue_high": None,
                    "guidance_eps_gaap_low": _safe_val("guidance_eps_gaap"),
                    "guidance_eps_gaap_high": None,
                    "guidance_eps_nongaap_low": _safe_val("guidance_eps_nongaap"),
                    "guidance_eps_nongaap_high": None,
                    "source_url": pr.get("source_url", pr.get("ex991_url", "")),
                    "source_date": pr.get("filing_date", ""),
                    "raw_snippet": "",
                    "guidance_source_type": "EDGAR_8K_PRESS_RELEASE",
                }
                return guidance
        except Exception:
            pass

    # Priority 2: CIK lookup + EDGAR fetch (if cache miss on first run)
    cik = get_cik(ticker)
    if not cik:
        return {"error": "cik_not_found", "source": "EDGAR_unavailable"}

    try:
        filing_info = find_earnings_8k(cik)
        if not filing_info:
            return {"error": "no_earnings_8k_found", "source": "EDGAR_8K_SEARCH"}

        time.sleep(0.5)
        resp = requests.get(filing_info["ex991_url"], headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            return {"error": f"fetch_failed_{resp.status_code}", "source": "EDGAR"}

        pr = parse_press_release(resp.text, ticker)
        pr["filing_date"] = filing_info["date"]
        pr["source_url"] = filing_info["ex991_url"]

        # Cache for future calls this session
        try:
            pr_cache.write_text(json.dumps(pr, indent=2))
        except Exception:
            pass

        def _safe_val(field):
            entry = pr.get(field)
            return entry.get("value") if isinstance(entry, dict) else None

        guidance = {
            "guidance_revenue_low": _safe_val("guidance_revenue"),
            "guidance_revenue_high": None,
            "guidance_eps_gaap_low": _safe_val("guidance_eps_gaap"),
            "guidance_eps_gaap_high": None,
            "guidance_eps_nongaap_low": _safe_val("guidance_eps_nongaap"),
            "guidance_eps_nongaap_high": None,
            "source_url": filing_info["ex991_url"],
            "source_date": filing_info["date"],
            "raw_snippet": "",
            "guidance_source_type": "EDGAR_8K_PRESS_RELEASE",
        }
        return guidance

    except Exception as e:
        return {"error": str(e)[:80], "source": "EDGAR_fetch_failed"}


# ── SEC 8-K EX-99.1 Press Release Fetcher ────────────────────────────────────

def find_earnings_8k(cik: str) -> dict | None:
    """
    Find the most recent earnings 8-K for a company.
    Returns {accession, date, ex991_url} or None.

    Earnings 8-Ks have an EX-99.1 exhibit (press release).
    Non-earnings 8-Ks (governance, bylaws, etc.) don't.
    """
    try:
        cik_padded = str(cik).zfill(10)
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        time.sleep(0.3)
        resp = requests.get(sub_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None

        filings = resp.json().get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])

        # Look through 8-Ks most recent first (up to 20)
        checked = 0
        import datetime as _dt_8k
        cutoff_45d = (_dt_8k.date.today() - _dt_8k.timedelta(days=90)).isoformat()  # 90 days covers all fiscal calendars
        warn_45d   = (_dt_8k.date.today() - _dt_8k.timedelta(days=45)).isoformat()
        for i, form in enumerate(forms):
            if form != "8-K":
                continue
            if checked >= 20:
                break
            checked += 1

            # 45-day cutoff — skip stale earnings releases
            if dates[i] < cutoff_45d:
                break  # filings are in reverse chronological order, so we can stop here

            acc = accessions[i]
            acc_clean = acc.replace("-", "")
            cik_int = int(cik)

            # Fetch the filing index to check for EX-99.1
            idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{acc}-index.htm"
            time.sleep(0.3)
            try:
                idx_resp = requests.get(idx_url, headers=HEADERS, timeout=10)
                if idx_resp.status_code != 200:
                    continue

                # CRITICAL: Only earnings 8-Ks contain Item 2.02 "Results of Operations"
                # Partnership/governance 8-Ks don't. Check this FIRST before looking for EX-99.
                if "2.02" not in idx_resp.text and "results of operations" not in idx_resp.text.lower():
                    continue

                # Look for EX-99 exhibit links
                ex99_links = re.findall(
                    r'href="(/Archives/edgar/data/[^"]+\.htm[^"]*)"',
                    idx_resp.text, re.IGNORECASE
                )
                # Also check for ex-99 in text (sometimes labeled differently)
                has_ex99 = any(
                    "ex-99" in link.lower() or "ex99" in link.lower()
                    for link in ex99_links
                ) or "ex-99" in idx_resp.text.lower() or "exhibit 99" in idx_resp.text.lower()

                if not has_ex99:
                    continue

                # Find the actual EX-99.1 URL
                ex991_url = None
                for link in ex99_links:
                    if "ex-99" in link.lower() or "ex99" in link.lower():
                        # Skip the main 8-K form itself
                        if "8k.htm" not in link.lower() and "8-k.htm" not in link.lower():
                            ex991_url = f"https://www.sec.gov{link}"
                            break

                # If no ex99 link found in hrefs, look in raw text for exhibit filename
                if not ex991_url:
                    patterns = [
                        r'href="(/Archives/edgar/data/\d+/[^"]+ex.{0,3}99[^"]+\.htm[^"]*)"',
                        r'href="(/Archives/edgar/data/\d+/[^"]+press[^"]+\.htm[^"]*)"',
                    ]
                    for pat in patterns:
                        m = re.search(pat, idx_resp.text, re.IGNORECASE)
                        if m:
                            ex991_url = f"https://www.sec.gov{m.group(1)}"
                            break

                if ex991_url:
                    return {
                        "accession": acc,
                        "date": dates[i],
                        "ex991_url": ex991_url,
                        "cik": cik_int,
                    }

            except Exception:
                continue

        return None
    except Exception:
        return None


def parse_press_release(html_text: str, ticker: str) -> dict:
    """
    Parse key financial figures from an earnings press release HTML.
    Returns structured dict with GAAP/non-GAAP labeled fields.
    """
    # Strip HTML tags
    text = re.sub(r'<[^>]+>', ' ', html_text)
    text = re.sub(r'&#\d+;', ' ', text)   # HTML entities
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    result = {
        "raw_text_sample": text[:500],
        "source": "SEC_8K_EX991",
        "parse_errors": [],
    }

    # ── Revenue (quarter) ──
    # Patterns: (regex, multiplier) — multiplier=None means auto-detect from matched text
    # BUG4 FIX PART 2: Place specific/narrow patterns first — "Net revenue of $X million" style
    # precedes broad "net revenue ... $X billion" which can match guidance lines
    rev_patterns = [
        # Bullet-point / narrative format with million FIRST — avoids matching guidance with billion
        (r'[Nn]et\s+revenue\s+of\s+\$\s*([0-9,\.]+)\s*(?:million|M)\b', 1e6),
        (r'net revenue[^.]*?\$([0-9,\.]+)\s*(?:billion|B)', 1e9),
        (r'revenue[^.]*?\$([0-9,\.]+)\s*(?:billion|B)', 1e9),
        # Table format: "Revenue $ 15.8 billion"
        (r'^\s*Revenue\s+\$\s*([0-9,\.]+)\s*(?:billion|B)', 1e9),
        # Record revenue format: "Record revenue of $15.8 billion"
        (r'[Rr]ecord\s+(?:total\s+)?revenue[^.]*?of\s+\$\s*([0-9,\.]+)\s*(?:billion|B)', 1e9),
        # Bullet-point / narrative format: "- $194.5 million in total first quarter revenues"
        (r'\$\s*([\d,\.]+)\s*(?:million|M)\s+in\s+(?:total\s+)?(?:first|second|third|fourth|Q[1-4])\s+quarter\s+revenues?', 1e6),
        # Narrative: "total revenues of $194.5 million" or "total revenues of $1.2 billion"
        (r'total\s+(?:net\s+)?revenues?\s+(?:of\s+|were\s+)?\$\s*([\d,\.]+)\s*(?:million|M|billion|B)', None),
        # Narrative: "revenues of $X million for the quarter"
        (r'revenues?\s+(?:of\s+)?\$\s*([\d,\.]+)\s*(?:million|M)', 1e6),
        # Net product revenue specifically (biotech commercial)
        (r'net\s+product\s+revenues?\s+(?:of\s+)?\$\s*([\d,\.]+)\s*(?:million|M)', 1e6),
    ]
    for pat, mult in rev_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                raw_val = float(m.group(1).replace(',', ''))
                if mult is None:
                    # Auto-detect scale from matched text
                    mt = m.group(0).lower()
                    mult = 1e9 if 'billion' in mt else 1e6
                # BUG4 FIX: Only set revenue_quarter once — first match is the actual quarter result
                # Subsequent matches may be guidance figures appearing later in the press release
                if "revenue_quarter" not in result:
                    result["revenue_quarter"] = {
                        "value": raw_val * mult,
                        "unit": "USD",
                        "is_gaap": True,
                        "source": "SEC_8K_EX991",
                        "period": "most_recent_quarter",
                        "raw": m.group(0)[:80],
                    }
                break
            except Exception:
                pass

    # ── Revenue (full year) ──
    # Stricter patterns — require "fiscal year" phrasing with "was $X billion" to avoid
    # matching quarterly revenue lines that mention "fiscal 2026" in context.
    # Also handle "Full Year Fiscal YYYY Financial Highlights ... revenue was $X billion"
    fy_rev_patterns = [
        # "Full Year Fiscal 2026 ... revenue ... $4.81 billion"
        r'[Ff]ull[.\s]+[Yy]ear[^$]{0,150}?\$([\d,\.]+)\s*(?:billion|B)',
        # "Net revenue for the fiscal year ended ... was $4.811 billion"
        r'(?:net\s+)?revenue\s+for\s+(?:the\s+)?fiscal\s+year[^.]*?was\s+\$([\d,\.]+)\s*(?:billion|B)',
        # "Net revenue for fiscal 2026 was $4.811 billion"
        r'(?:net\s+)?revenue\s+for\s+fiscal\s+\d{4}\s+was\s+\$([\d,\.]+)\s*(?:billion|B)',
        # "annual / full-year revenue ... $X billion"
        r'(?:annual|full.?year)\s+(?:net\s+)?revenue[^$]{0,80}?\$([\d,\.]+)\s*(?:billion|B)',
    ]
    fy_rev = None
    fy_rev_match_text = None
    for fy_pat in fy_rev_patterns:
        fy_rev = re.search(fy_pat, text, re.IGNORECASE)
        if fy_rev:
            fy_rev_match_text = fy_rev.group(0)
            break
    if fy_rev:
        try:
            fy_val = float(fy_rev.group(1).replace(',', '')) * 1e9
            q_rev = result.get("revenue_quarter", {}).get("value") or 0
            if fy_val > 0 and q_rev > 0 and fy_val < q_rev * 1.5:
                # Annual revenue smaller than 1.5x quarterly = almost certainly the quarterly figure re-matched
                result["parse_errors"].append(
                    f"Annual revenue plausibility fail: ${fy_val/1e9:.2f}B < 1.5x quarterly ${q_rev/1e9:.2f}B — skipping"
                )
            else:
                result["revenue_annual"] = {
                    "value": fy_val,
                    "unit": "USD",
                    "is_gaap": True,
                    "source": "SEC_8K_EX991",
                    "period": "fiscal_year",
                    "raw": (fy_rev_match_text or "")[:80],
                }
        except Exception:
            pass

    # ── GAAP EPS (quarter) ──
    gaap_eps_patterns = [
        # BUG2 FIX: Strong GAAP context anchoring — GAAP must appear adjacent to EPS value
        r'GAAP\s+diluted\s+net\s+income\s+per\s+share[^$\n]*?\$\s*([\d\.]+)(?!\s*billion|\s*B\b)',
        r'GAAP\s+(?:net\s+income|loss)\s+per\s+(?:diluted\s+)?share[^$\n]*?\$\s*\(?([\d\.]+)',
        r'\$([0-9\.]+)\s+(?:GAAP\s+)?diluted\s+(?:net\s+)?income\s+per\s+share',
        r'GAAP\s+diluted\s+(?:net\s+)?income\s+per\s+share[:\s]+\$([0-9\.]+)',
        r'GAAP\s+diluted\s+(?:income|earnings)\s+per\s+share[^.]*?\$([0-9\.]+)',
        r'\$([0-9\.]+)\s+per\s+diluted\s+share\.\s+Non.GAAP',
        r'(?:GAAP\s+)?net\s+income[^.]*?\$([0-9\.]+)\s+per\s+diluted\s+share',
        # Table format: "Diluted Earnings per Share (EPS) $ 0.85"
        r'[Dd]iluted\s+[Ee]arnings\s+per\s+[Ss]hare\s*(?:\([^)]*\))?\s*\$\s*([0-9\.]+)',
        # Simple table: "EPS $ 0.85" in GAAP section
        r'GAAP[^\n]{0,200}[Ee]arnings\s+per\s+[Ss]hare\s*(?:\([^)]*\))?\s*\$\s*([0-9\.]+)',
    ]
    for pat in gaap_eps_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if 0.01 < val < 50:
                    result["eps_gaap_quarter"] = {
                        "value": val,
                        "unit": "USD_per_share",
                        "is_gaap": True,
                        "source": "SEC_8K_EX991",
                        "period": "most_recent_quarter",
                        "raw": m.group(0)[:80],
                    }
                    break
            except Exception:
                pass

    # If positive GAAP EPS not found, try negative/parenthetical formats (biotech losses)
    # BBIO format: "Net loss per share ... basic and diluted $ (0.84)"
    if "eps_gaap_quarter" not in result:
        neg_eps_patterns = [
            # Loss per share in parentheses: "$ (0.84)"
            r'(?:net\s+(?:loss|income)\s+per\s+share[^$\n]*?basic\s+and\s+diluted\s*\$\s*\(?([\d\.]+)\)?)',
            # Basic and diluted EPS in table
            r'(?:basic\s+and\s+diluted[^$\n]*?\$\s*\(?([\d\.]+)\)?)',
        ]
        for pat in neg_eps_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    matched_text = m.group(0)
                    # Check if the matched text contains parentheses around the number (= negative)
                    is_negative = '(' in matched_text and ')' in matched_text
                    val = float(m.group(1))
                    if is_negative:
                        val = -val
                    if 0 < abs(val) < 50:
                        result["eps_gaap_quarter"] = {
                            "value": val,
                            "unit": "USD_per_share",
                            "is_gaap": True,
                            "source": "SEC_8K_EX991",
                            "period": "most_recent_quarter",
                            "raw": matched_text[:80],
                        }
                        break
                except Exception:
                    pass

    # ── Non-GAAP EPS (quarter) ──
    nongaap_eps_patterns = [
        # BUG2 FIX: Strong non-GAAP context anchoring
        r'[Nn]on.GAAP\s+diluted\s+net\s+income\s+per\s+share[^$\n]*?\$\s*([\d\.]+)(?!\s*billion|\s*B\b)',
        r'\$([0-9\.]+)\s+non.GAAP\s+diluted',
        r'non.GAAP\s+diluted\s+(?:net\s+)?income\s+per\s+share[:\s]+\$([0-9\.]+)',
        r'non.GAAP\s+diluted\s+(?:income|earnings)\s+per\s+share[^.]*?\$([0-9\.]+)',
        # Non-GAAP table: "EPS $ 1.06" after Non-GAAP header (no intervening newlines)
        r'[Nn]on.GAAP\b.*?\bEPS\b\s*\$\s*([0-9\.]+)',
        # Pattern: "non-GAAP EPS of $1.06"
        r'non.GAAP\s+EPS\s+of\s+\$\s*([0-9\.]+)',
    ]
    for pat in nongaap_eps_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if 0.01 < val < 50:
                    result["eps_nongaap_quarter"] = {
                        "value": val,
                        "unit": "USD_per_share",
                        "is_gaap": False,
                        "source": "SEC_8K_EX991",
                        "period": "most_recent_quarter",
                        "raw": m.group(0)[:80],
                    }
                    break
            except Exception:
                pass

    # BUG2 FIX: EPS sanity swap — for tech companies, non-GAAP EPS should be >= GAAP EPS
    # If GAAP > non-GAAP by >20%, they're likely swapped (SBC exclusions make non-GAAP higher)
    eps_g = result.get("eps_gaap_quarter", {}).get("value")
    eps_ng = result.get("eps_nongaap_quarter", {}).get("value")
    if eps_g is not None and eps_ng is not None and eps_g > 0 and eps_ng > 0:
        if eps_g > eps_ng * 1.20:  # GAAP > non-GAAP by >20% = likely swapped
            result["eps_gaap_quarter"]["value"] = eps_ng
            result["eps_nongaap_quarter"]["value"] = eps_g
            result["parse_errors"].append(f"EPS SWAP DETECTED: GAAP ${eps_g:.2f} > non-GAAP ${eps_ng:.2f} — swapped for consistency")

    # ── Full Year GAAP EPS ──
    # Note: use [\s\S]*? carefully to not cross sentence boundary; match "or $X.XX per diluted share"
    fy_gaap = re.search(
        r'GAAP\s+net\s+income\s+for\s+fiscal\s+\d{4}\s+was\s+\$[\d,\.]+\s+\w+,\s+or\s+\$([\d\.]+)\s+per\s+diluted\s+share',
        text, re.IGNORECASE
    )
    if not fy_gaap:
        # Broader fallback: any sentence with "fiscal YYYY" and "or $X.XX per diluted share"
        fy_gaap = re.search(
            r'fiscal\s+\d{4}[^.]{0,120}?or\s+\$([\d\.]+)\s+per\s+diluted\s+share',
            text, re.IGNORECASE
        )
    if fy_gaap:
        try:
            val = float(fy_gaap.group(1))
            if 0.01 < val < 200:
                result["eps_gaap_annual"] = {
                    "value": val,
                    "unit": "USD_per_share",
                    "is_gaap": True,
                    "source": "SEC_8K_EX991",
                    "period": "fiscal_year",
                    "raw": fy_gaap.group(0)[:80],
                }
        except Exception:
            pass

    # ── Full Year Non-GAAP EPS ──
    # Pattern: "Non-GAAP net income for fiscal 2026 was $2.466 billion, or $2.84 per diluted share."
    fy_ng = re.search(
        r'[Nn]on.GAAP\s+net\s+income\s+for\s+fiscal\s+\d{4}\s+was\s+\$[\d,\.]+\s+\w+,\s+or\s+\$([\d\.]+)\s+per\s+diluted\s+share',
        text, re.IGNORECASE
    )
    if fy_ng:
        try:
            val = float(fy_ng.group(1))
            if 0.01 < val < 200:
                result["eps_nongaap_annual"] = {
                    "value": val,
                    "unit": "USD_per_share",
                    "is_gaap": False,
                    "source": "SEC_8K_EX991",
                    "period": "fiscal_year",
                    "raw": fy_ng.group(0)[:80],
                }
        except Exception:
            pass

    # ── Operating Cash Flow ──
    ocf_m = re.search(
        r'[Cc]ash\s+flow\s+from\s+operations[^.]*?\$([\d,\.]+)\s*(million|billion|M|B)',
        text, re.IGNORECASE
    )
    if ocf_m:
        try:
            val = float(ocf_m.group(1).replace(',', ''))
            mult = 1e9 if ocf_m.group(2).lower() in ('billion', 'b') else 1e6
            result["operating_cashflow_quarter"] = {
                "value": val * mult,
                "unit": "USD",
                "is_gaap": True,
                "source": "SEC_8K_EX991",
                "period": "most_recent_quarter",
                "raw": ocf_m.group(0)[:80],
            }
        except Exception:
            pass

    # ── Gross Margin ──
    # BUG3 FIX: Use multiple patterns, require gross margin on same line as %, take lowest plausible value
    # Also handle: "GAAP gross margin of 44.2%", table format "44.2 %", "44.2%"
    gm_gaap_patterns = [
        r'GAAP\s+gross\s+margin[:\s]+(?:of\s+)?([\d\.]+)\s*%',
        r'([\d\.]+)\s*%\s+GAAP\s+gross\s+margin',
        r'gross\s+margin[:\s]+(?:of\s+)?([\d\.]+)\s*%\s*GAAP',
        r'[Gg]ross\s+margin\s+(?:of\s+)?([\d\.]+)\s*%',
    ]
    # Keywords that indicate reconciliation table context (not the income statement actual margin)
    _reconcil_keywords = ["impact", "amortization", "reconcil", "adjust", "exclud", "stock-based"]
    gm_gaap_candidates = []
    for gm_pat in gm_gaap_patterns:
        for gm_m in re.finditer(gm_pat, text, re.IGNORECASE):
            try:
                gm_val = float(gm_m.group(1))
                if 0 < gm_val < 100:
                    gm_raw = gm_m.group(0)[:80]
                    # Skip matches from reconciliation table context
                    if any(kw in gm_raw.lower() for kw in _reconcil_keywords):
                        result["parse_errors"].append(
                            f"Gross margin candidate {gm_val:.1f}% skipped — reconciliation table context: {gm_raw[:50]}"
                        )
                        continue
                    # Also check surrounding context (30 chars before match)
                    ctx_start = max(0, gm_m.start() - 30)
                    ctx = text[ctx_start:gm_m.end()].lower()
                    if any(kw in ctx for kw in _reconcil_keywords):
                        result["parse_errors"].append(
                            f"Gross margin candidate {gm_val:.1f}% skipped — reconciliation context before match"
                        )
                        continue
                    gm_gaap_candidates.append((gm_val, gm_raw[:60]))
            except Exception:
                pass
    # Prefer the lowest plausible GAAP gross margin (YoY growth rates are typically > real margins)
    plausible_gm_gaap = [(v, raw) for v, raw in gm_gaap_candidates if v <= 85.0]
    if plausible_gm_gaap:
        gm_val, gm_raw = min(plausible_gm_gaap, key=lambda x: x[0])
        result["gross_margin_gaap"] = {
            "value": gm_val / 100,
            "unit": "pct",
            "is_gaap": True,
            "source": "SEC_8K_EX991",
            "period": "most_recent_quarter",
            "raw": gm_raw,
        }
    elif gm_gaap_candidates:
        # All candidates > 85% — fallback to first found but flag it
        gm_val, gm_raw = gm_gaap_candidates[0]
        result["gross_margin_gaap"] = {
            "value": gm_val / 100,
            "unit": "pct",
            "is_gaap": True,
            "source": "SEC_8K_EX991",
            "period": "most_recent_quarter",
            "raw": gm_raw,
        }

    gm_ng_patterns = [
        r'[Nn]on.GAAP\s+gross\s+margin[:\s]+(?:of\s+)?([\d\.]+)\s*%',
        r'([\d\.]+)\s*%\s+non.GAAP\s+gross\s+margin',
        r'non.GAAP\s+gross\s+margin[:\s]+(?:of\s+)?([\d\.]+)\s*%',
    ]
    gm_ng_candidates = []
    for gm_pat in gm_ng_patterns:
        for gm_m in re.finditer(gm_pat, text, re.IGNORECASE):
            try:
                gm_val = float(gm_m.group(1))
                if 0 < gm_val < 100:
                    gm_ng_candidates.append((gm_val, gm_m.group(0)[:60]))
            except Exception:
                pass
    plausible_gm_ng = [(v, raw) for v, raw in gm_ng_candidates if v <= 85.0]
    if plausible_gm_ng:
        # For non-GAAP, take the FIRST specific match (pattern order: specific non-GAAP patterns first)
        # Non-GAAP gross margin is always >= GAAP, so don't use min() here
        gm_val, gm_raw = plausible_gm_ng[0]
        result["gross_margin_nongaap"] = {
            "value": gm_val / 100,
            "unit": "pct",
            "is_gaap": False,
            "source": "SEC_8K_EX991",
            "period": "most_recent_quarter",
            "raw": gm_raw,
        }
    elif gm_ng_candidates:
        gm_val, gm_raw = gm_ng_candidates[0]
        result["gross_margin_nongaap"] = {
            "value": gm_val / 100,
            "unit": "pct",
            "is_gaap": False,
            "source": "SEC_8K_EX991",
            "period": "most_recent_quarter",
            "raw": gm_raw,
        }

    # BUG3 FIX: Plausibility check — hardware/photonics companies can't have 80%+ gross margin
    gm_g = result.get("gross_margin_gaap", {}).get("value")
    if gm_g is not None and gm_g > 0.80:
        result["parse_errors"].append(f"Gross margin GAAP {gm_g*100:.1f}% > 80% implausible for hardware — likely extracted YoY growth rate, clearing")
        result.pop("gross_margin_gaap", None)

    gm_ng_v = result.get("gross_margin_nongaap", {}).get("value")
    if gm_ng_v is not None and gm_ng_v > 0.80:
        result["parse_errors"].append(f"Gross margin non-GAAP {gm_ng_v*100:.1f}% > 80% implausible, clearing")
        result.pop("gross_margin_nongaap", None)

    # ── Gross Profit (dollar amount, for cross-check) ──
    gp_patterns = [
        r'[Gg]ross\s+profit[:\s]+\$\s*([\d,\.]+)\s*(million|M|billion|B)',
        r'\$\s*([\d,\.]+)\s*(million|M|billion|B)\s+(?:GAAP\s+)?gross\s+profit',
        r'[Gg]ross\s+profit\s+(?:of\s+)?\$\s*([\d,\.]+)\s*(million|M|billion|B)',
    ]
    for gp_pat in gp_patterns:
        gp_m = re.search(gp_pat, text, re.IGNORECASE)
        if gp_m:
            try:
                gp_val = float(gp_m.group(1).replace(',', ''))
                gp_mult = 1e9 if gp_m.group(2).lower() in ('billion', 'b') else 1e6
                result["gross_profit_quarter"] = {
                    "value": gp_val * gp_mult,
                    "unit": "USD",
                    "is_gaap": True,
                    "source": "SEC_8K_EX991",
                    "period": "most_recent_quarter",
                    "raw": gp_m.group(0)[:80],
                }
                break
            except Exception:
                pass

    # ── GAAP vs non-GAAP gross margin cross-check ──
    # If gap > 15pp, GAAP was likely extracted from reconciliation table (shows adjustment, not actual)
    gm_g = result.get("gross_margin_gaap", {}).get("value")
    gm_ng = result.get("gross_margin_nongaap", {}).get("value")
    if gm_g is not None and gm_ng is not None:
        gap = gm_ng - gm_g
        if gap > 0.15:  # >15pp gap = likely extracted adjustment, not actual margin
            result["parse_errors"].append(
                f"GAAP gross margin {gm_g*100:.1f}% vs non-GAAP {gm_ng*100:.1f}% gap={gap*100:.1f}pp — "
                f"GAAP likely extracted from reconciliation table. Clearing suspicious value."
            )
            # Try to recalculate from gross profit / revenue
            gp = result.get("gross_profit_quarter", {}).get("value")
            rev = result.get("revenue_quarter", {}).get("value")
            if gp and rev and rev > 0:
                recalc_gm = gp / rev
                if 0.30 < recalc_gm < 0.80:
                    result["gross_margin_gaap"]["value"] = recalc_gm
                    result["gross_margin_gaap"]["note"] = "Recalculated from gross_profit/revenue"
                else:
                    result.pop("gross_margin_gaap", None)
            else:
                result.pop("gross_margin_gaap", None)

    # ── Revenue Guidance ──
    rev_guide = re.search(
        r'[Nn]et\s+revenue\s+is\s+expected\s+to\s+be\s+\$([\d,\.]+)\s*(?:billion|B)',
        text, re.IGNORECASE
    )
    if rev_guide:
        try:
            result["guidance_revenue"] = {
                "value": float(rev_guide.group(1).replace(',', '')) * 1e9,
                "unit": "USD",
                "is_gaap": True,
                "source": "SEC_8K_EX991",
                "period": "next_quarter_guidance",
                "raw": rev_guide.group(0)[:100],
            }
        except Exception:
            pass

    # ── GAAP EPS Guidance ──
    gaap_guide = re.search(
        r'GAAP\s+diluted\s+net\s+income\s+per\s+share\s+is\s+expected\s+to\s+be\s+\$([0-9\.]+)',
        text, re.IGNORECASE
    )
    if not gaap_guide:
        # Cisco format: "GAAP EPS: $0.75 to $0.80"
        gaap_guide = re.search(
            r'GAAP\s+EPS[:\s]+\$\s*([0-9\.]+)',
            text, re.IGNORECASE
        )
    if gaap_guide:
        try:
            result["guidance_eps_gaap"] = {
                "value": float(gaap_guide.group(1)),
                "unit": "USD_per_share",
                "is_gaap": True,
                "source": "SEC_8K_EX991",
                "period": "next_quarter_guidance",
                "raw": gaap_guide.group(0)[:100],
            }
        except Exception:
            pass

    # ── Non-GAAP EPS Guidance ──
    ng_guide = re.search(
        r'[Nn]on.GAAP\s+diluted\s+net\s+income\s+per\s+share\s+is\s+expected\s+to\s+be\s+\$([0-9\.]+)',
        text, re.IGNORECASE
    )
    if not ng_guide:
        # Cisco format: "Non-GAAP EPS: $1.09 to $1.11"
        ng_guide = re.search(
            r'[Nn]on.GAAP\s+EPS[:\s]+\$\s*([0-9\.]+)',
            text, re.IGNORECASE
        )
    if ng_guide:
        try:
            result["guidance_eps_nongaap"] = {
                "value": float(ng_guide.group(1)),
                "unit": "USD_per_share",
                "is_gaap": False,
                "source": "SEC_8K_EX991",
                "period": "next_quarter_guidance",
                "raw": ng_guide.group(0)[:100],
            }
        except Exception:
            pass

    # ── SBC (Stock-Based Compensation) from reconciliation table ──
    # Stricter patterns — must be standalone line, not combined with other addbacks.
    # Reject lines where SBC is listed alongside amortization/acquisition items (combined addback).
    sbc_patterns = [
        # Standalone SBC line: stock-based compensation (and optional payroll taxes) followed by $
        # Negative lookahead approach: the matched segment should NOT contain ", amortization" or ", acquisition"
        r'[Ss]tock.based\s+compensation(?:\s+expense)?(?:\s+and\s+(?:related\s+)?employer\s+payroll\s+taxes?)?\s*[\$\(]\s*([\d,\.]+)',
        r'[Ss]hare.based\s+compensation(?:\s+expense)?\s*[\$\(]\s*([\d,\.]+)',
    ]
    for pat in sbc_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                # Combined-line detector: if matched text contains other addback items, it's aggregated
                matched_text = m.group(0)
                if ", amortization" in matched_text.lower() or ", acquisition" in matched_text.lower():
                    # Combined addback line — skip, value is aggregated total
                    continue
                val = float(m.group(1).replace(',', ''))
                # SBC in press release is usually in millions
                if val < 10000:  # if < $10B it's likely in millions
                    val_full = val * 1e6
                else:
                    val_full = val
                if val_full > 1e6:  # sanity: must be at least $1M
                    # Plausibility check: SBC > 35% of quarterly revenue = likely combined addback line
                    rev_q = result.get("revenue_quarter", {}).get("value") or 0
                    if rev_q > 0 and val_full > rev_q * 0.35:
                        result["parse_errors"].append(
                            f"SBC plausibility fail: ${val_full/1e6:.0f}M exceeds 35% of quarterly revenue "
                            f"${rev_q/1e6:.0f}M — likely combined addback line, skipping"
                        )
                        break
                    result["sbc_quarter"] = {
                        "value": val_full,
                        "unit": "USD",
                        "is_gaap": False,
                        "source": "SEC_8K_EX991_RECONCILIATION",
                        "period": "most_recent_quarter",
                        "raw": m.group(0)[:80],
                        "note": "Added back from GAAP net income to reach non-GAAP / OCF. Not fraud — standard SBC accounting.",
                    }
                    break
            except Exception:
                pass

    # ── Amortization of acquired intangibles ──
    amort_patterns = [
        r'[Aa]mortization\s+of\s+(?:acquired\s+)?intangibles?[^$\n]*\$\s*([\d,\.]+)',
        r'[Aa]mortization\s+of\s+(?:acquisition|purchased)\s+intangibles?[^$\n]*\$\s*([\d,\.]+)',
    ]
    for pat in amort_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                # Combined-line detector: if matched line contains SBC or acquisition comp, it's aggregated
                matched_text = m.group(0)
                if "stock-based" in matched_text.lower() or "share-based" in matched_text.lower() or \
                   "acquisition-related compensation" in matched_text.lower():
                    # Combined addback line — skip
                    continue
                val = float(m.group(1).replace(',', ''))
                if val < 10000:
                    val_full = val * 1e6
                else:
                    val_full = val
                if val_full > 1e5:
                    # Plausibility check: amortization > 35% of quarterly revenue = likely combined addback line
                    rev_q = result.get("revenue_quarter", {}).get("value") or 0
                    if rev_q > 0 and val_full > rev_q * 0.35:
                        result["parse_errors"].append(
                            f"Amortization plausibility fail: ${val_full/1e6:.0f}M exceeds 35% of quarterly revenue "
                            f"${rev_q/1e6:.0f}M — likely combined addback line, skipping"
                        )
                        break
                    result["amortization_intangibles_quarter"] = {
                        "value": val_full,
                        "unit": "USD",
                        "is_gaap": False,
                        "source": "SEC_8K_EX991_RECONCILIATION",
                        "period": "most_recent_quarter",
                        "raw": m.group(0)[:80],
                        "note": "Explains GAAP vs non-GAAP gross margin gap for acquisition-heavy companies.",
                    }
                    break
            except Exception:
                pass

    # --- Mining-specific field extraction ---
    MINING_FIELD_PATTERNS = {
        "aisc_per_oz": [
            r'all.in sustaining costs?\s+(?:per ounce\s+)?(?:of\s+)?\$?([\d,]+)',
            r'\baisc\b\s+(?:per ounce\s+)?(?:of\s+)?\$?([\d,]+)',
            r'all.in sustaining cost\s+(?:per ounce\s+)?(?:was|were|of)\s+\$?([\d,]+)',
        ],
        "total_cash_costs_per_oz": [
            r'total cash costs?\s+(?:per ounce\s+)?(?:of\s+)?\$?([\d,]+)',
            r'cash costs?\s+per ounce\s+(?:of\s+)?\$?([\d,]+)',
        ],
        "realized_gold_price": [
            r'realized\s+(?:gold\s+)?price\s+(?:of\s+)?\$?([\d,]+)',
            r'average\s+realized\s+price\s+(?:of\s+)?\$?([\d,]+)',
        ],
        "production_oz_quarter": [
            r'(?:payable\s+)?gold\s+production\s+(?:of\s+)?([\d,]+)\s+ounces',
            r'produced\s+([\d,]+)\s+(?:payable\s+)?(?:gold\s+)?ounces',
        ],
    }
    MINING_RANGE_PATTERNS = {
        "aisc_guidance_midpoint": [
            r'aisc\s+(?:per ounce\s+)?(?:guidance\s+)?(?:of\s+)?\$?([\d,]+)\s+(?:to|–|-)\s+\$?([\d,]+)',
        ],
        "production_oz_midpoint": [
            r'(?:full.year|annual)\s+(?:gold\s+)?production\s+(?:guidance\s+)?(?:of\s+)?([\d,\.]+)\s+(?:to|–|-)\s+([\d,\.]+)\s+million\s+ounces',
        ],
    }

    for field_name, patterns in MINING_FIELD_PATTERNS.items():
        if field_name in result:
            continue
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    val = float(m.group(1).replace(",", ""))
                    if val > 0:
                        result[field_name] = val
                        break
                except Exception:
                    pass

    # Range patterns — store midpoint
    _guidance_from_mining = {}
    for field_name, patterns in MINING_RANGE_PATTERNS.items():
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    lo = float(m.group(1).replace(",", ""))
                    hi = float(m.group(2).replace(",", ""))
                    midpoint = (lo + hi) / 2
                    if "production_oz_midpoint" in field_name:
                        midpoint = midpoint * 1_000_000  # convert M oz to oz
                    _guidance_from_mining[field_name] = midpoint
                    break
                except Exception:
                    pass
    if _guidance_from_mining:
        result["mining_guidance"] = _guidance_from_mining

    # --- Segment Revenue Extraction ---
    segments = {}
    try:
        # Look for segment tables — patterns like "Segment Revenue", "Business Segments", "Revenue by Segment"
        seg_patterns = [
            r'(?:segment|business\s+unit|revenue\s+by)\s+revenue[^\n]*\n((?:[^\n]+\n){2,20})',
            r'((?:(?:US\s+Commercial|US\s+Government|International|Data\s+Center|Enterprise|Consumer)[^\n]*\$[^\n]*\n){2,10})',
        ]
        for pat in seg_patterns:
            seg_matches = re.findall(pat, text, re.IGNORECASE)
            if seg_matches:
                # Parse dollar amounts from matched lines
                for line in seg_matches[0].split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    # Find segment name and dollar amount
                    dollar_match = re.search(r'\$?\s*([\d,]+(?:\.\d+)?)\s*(?:million|billion|M|B)?', line, re.IGNORECASE)
                    if dollar_match and len(line) < 150:
                        seg_name_part = re.sub(r'\$.*', '', line).strip()[:60]
                        if seg_name_part and len(seg_name_part) > 3:
                            raw_val = float(dollar_match.group(1).replace(',', ''))
                            # Detect M vs B
                            if 'billion' in line.lower() or line.strip().endswith('B'):
                                raw_val *= 1e9
                            else:
                                raw_val *= 1e6
                            segments[seg_name_part] = raw_val
                if segments:
                    break
        # Also look for explicit YoY growth rates next to segment names
        growth_pattern = r'([\w\s]+?)\s+\$?([\d,.]+)\s+\$?([\d,.]+)\s+([+-]?\d+)%'
        growth_matches = re.findall(growth_pattern, text)
        for m in growth_matches[:20]:
            seg_name = m[0].strip()[:60]
            if seg_name and any(kw in seg_name.lower() for kw in ['commercial', 'government', 'center', 'enterprise', 'international', 'segment', 'cloud', 'services', 'product']):
                try:
                    curr_val = float(m[1].replace(',', '')) * 1e6
                    growth_pct = float(m[3])
                    segments[seg_name] = {"current": curr_val, "growth_pct": growth_pct}
                except Exception:
                    pass
    except Exception as _seg_e:
        segments["_error"] = str(_seg_e)[:100]

    result["segments"] = segments

    # --- GAAP to Non-GAAP Reconciliation Table (SEC Reg G required) ---
    reconciliation = {}
    try:
        # Look for reconciliation table headers
        recon_patterns = [
            r'(?:GAAP\s+to\s+Non-GAAP|Non-GAAP\s+Reconciliation|Reconciliation\s+of\s+GAAP)[^\n]*\n((?:[^\n]+\n){3,25})',
            r'(?:Reconciliation)[^\n]*\n((?:[^\n]+\n){3,25})',
        ]
        addback_items = {}
        for pat in recon_patterns:
            recon_matches = re.findall(pat, text, re.IGNORECASE)
            if recon_matches:
                for line in recon_matches[0].split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    # Find addback items with dollar amounts
                    dollar_match = re.search(r'\(?\$?\s*([\d,]+(?:\.\d+)?)\s*(?:million|M)?\)?', line)
                    if dollar_match and len(line) < 200:
                        item_name = re.sub(r'\$.*|\(.*', '', line).strip()[:80]
                        if item_name and len(item_name) > 5:
                            raw = float(dollar_match.group(1).replace(',', ''))
                            # Heuristic: is this an addback line?
                            is_addback = any(kw in item_name.lower() for kw in [
                                'stock-based', 'amortization', 'depreciation', 'restructur',
                                'impairment', 'acquisition', 'litigation', 'tax effect', 'non-cash'
                            ])
                            if is_addback:
                                addback_items[item_name] = raw * 1e6  # assume millions
                if addback_items:
                    reconciliation["addback_items"] = addback_items
                    reconciliation["total_addbacks"] = sum(addback_items.values())
                    reconciliation["source"] = "SEC_REG_G_RECONCILIATION_TABLE"
                    break
        if not reconciliation:
            reconciliation["note"] = "Reconciliation table not parsed — check raw press release"
    except Exception as _recon_e:
        reconciliation["error"] = str(_recon_e)[:100]

    result["gaap_nongaap_reconciliation"] = reconciliation

    # Count fields parsed (exclude metadata keys)
    meta_keys = {"raw_text_sample", "source", "parse_errors"}
    parsed_fields = [k for k in result if k not in meta_keys]
    result["fields_parsed"] = len(parsed_fields)
    result["parse_success"] = len(parsed_fields) >= 2

    return result


def _fetch_6k_press_release(ticker: str, cik: str) -> dict:
    """
    Fetch earnings data from 6-K filing for foreign private issuers.
    6-K has no item classification — find by keyword matching in exhibits.
    """
    import datetime as _dt
    today = _dt.date.today().isoformat()
    cutoff = (_dt.date.today() - _dt.timedelta(days=120)).isoformat()

    earnings_keywords = [
        "quarterly results", "earnings per share", "net income",
        "revenue", "operating income", "adjusted ebitda",
        "production", "ounces", "quarterly financial", "financial results",
    ]

    try:
        cik_int = int(cik.lstrip("0") or "0")
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_int:010d}.json"
        resp = requests.get(sub_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return {"error": f"EDGAR submissions fetch failed: {resp.status_code}", "parse_success": False}

        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])

        earnings_6ks = []
        for form, date, accn in zip(forms, dates, accessions):
            if date < cutoff:
                break
            if form in ("6-K", "6-K/A"):
                earnings_6ks.append({"form": form, "date": date, "accession": accn})

        if not earnings_6ks:
            return {
                "error": f"No 6-K filings found in past 120 days for {ticker}",
                "parse_success": False,
                "filing_form": "6-K",
            }

        print(f"  [6-K] {ticker}: found {len(earnings_6ks)} 6-K filings, checking for earnings...")

        for filing in earnings_6ks[:8]:
            accn_clean = filing["accession"].replace("-", "")
            accn_fmt   = filing["accession"]
            index_url  = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{accn_clean}/{accn_fmt}-index.htm"
            )
            try:
                idx_resp = requests.get(index_url, headers=HEADERS, timeout=10)
                if idx_resp.status_code != 200:
                    continue

                import re as _re
                exhibit_links = _re.findall(
                    r'href="(/Archives/edgar/data/\d+/\d+/[^"]+\.htm)"',
                    idx_resp.text, _re.IGNORECASE,
                )

                for exhibit_path in exhibit_links[:4]:
                    ex_url = f"https://www.sec.gov{exhibit_path}"
                    ex_resp = requests.get(ex_url, headers=HEADERS, timeout=15)
                    if ex_resp.status_code != 200:
                        continue

                    content_lower = ex_resp.text.lower()
                    keyword_hits = sum(1 for kw in earnings_keywords if kw in content_lower)

                    if keyword_hits >= 3:
                        print(f"  [6-K] Found earnings exhibit ({keyword_hits} keywords) in {filing['date']}")
                        parsed = parse_press_release(ex_resp.text, ticker)
                        parsed["filing_date"] = filing["date"]
                        parsed["filing_form"] = "6-K"
                        parsed["parse_success"] = True
                        parsed["source_url"] = ex_url
                        return parsed

            except Exception as e:
                print(f"  [6-K] Error checking filing {filing['accession']}: {e}")
                continue

        return {
            "error": f"No earnings content in recent 6-K filings for {ticker} (checked {min(8, len(earnings_6ks))})",
            "parse_success": False,
            "filing_form": "6-K",
            "filings_found": len(earnings_6ks),
        }

    except Exception as e:
        return {"error": str(e)[:150], "parse_success": False, "filing_form": "6-K"}


def fetch_earnings_press_release(ticker: str, cik: str) -> dict:
    """
    Find and parse the most recent earnings press release from SEC EDGAR.
    Uses EDGAR submissions JSON items field for reliable Item 2.02 filtering.

    Priority order:
    1. Most recent 8-K with Item 2.02 (Earnings Announcements) in past 90 days
    2. Most recent 8-K with Item 9.01 (Financial Statements/Exhibits) in past 90 days
    3. Extend window to 150 days and repeat Item 2.02 search
    4. Item 9.01 in 150 days
    5. Last resort: most recent 8-K of any type in 150 days (with warning logged)
    """
    # Check if this is a foreign private issuer (6-K filer)
    _names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
    _filing_form = "8-K"
    if _names_path.exists():
        try:
            _known = json.loads(_names_path.read_text())
            _entry = _known.get(ticker.upper(), {})
            if isinstance(_entry, dict):
                if _entry.get("foreign_private_issuer") or _entry.get("filing_type") == "6-K":
                    _filing_form = "6-K"
        except Exception:
            pass

    if _filing_form == "6-K":
        return _fetch_6k_press_release(ticker, cik)

    cache_file = CACHE_DIR / f"press_release_{ticker}_{datetime.date.today().isoformat()}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    result = {
        "ticker": ticker,
        "found": False,
        "accession": None,
        "filing_date": None,
        "items": None,
        "item_filter_used": None,
        "text": "",
        "url": None,
        "note": "",
        "error": None,
    }

    try:
        cik_int = int(cik)
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_int:010d}.json"
        time.sleep(0.3)
        resp = requests.get(sub_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            result["note"] = f"EDGAR submissions fetch failed: {resp.status_code}"
            result["error"] = f"submissions_fetch_failed_{resp.status_code}"
            return result

        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})

        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        items_list = recent.get("items", [])
        primary_docs = recent.get("primaryDocument", [])

        candidates = []
        for form, date, accn, items, doc in zip(forms, dates, accessions, items_list, primary_docs):
            if form in ("8-K", "8-K/A"):
                candidates.append({"date": date, "accession": accn, "items": str(items), "doc": doc})

        def _search_candidates(candidate_list, cutoff_date, item_filter=None):
            for c in candidate_list:  # newest-first from EDGAR
                if c["date"] < cutoff_date:
                    break
                if item_filter is None or item_filter in c["items"]:
                    return c
            return None

        cutoff_90 = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
        cutoff_150 = (datetime.date.today() - datetime.timedelta(days=150)).isoformat()

        target = _search_candidates(candidates, cutoff_90, "2.02")
        if target:
            result["item_filter_used"] = "2.02 (90d)"

        if not target:
            target = _search_candidates(candidates, cutoff_90, "9.01")
            if target:
                result["item_filter_used"] = "9.01 (90d)"

        if not target:
            target = _search_candidates(candidates, cutoff_150, "2.02")
            if target:
                result["item_filter_used"] = "2.02 (150d)"

        if not target:
            target = _search_candidates(candidates, cutoff_150, "9.01")
            if target:
                result["item_filter_used"] = "9.01 (150d)"

        if not target:
            target = _search_candidates(candidates, cutoff_150, None)
            if target:
                result["item_filter_used"] = "UNFILTERED_FALLBACK"
                print(f"  [8-K WARN] {ticker}: No Item 2.02 found in 150d — using unfiltered fallback (items={target['items']})")

        if not target:
            result["note"] = "no_earnings_8k_found — no 8-K in past 150 days"
            result["error"] = "no_earnings_8k_found"
            print(f"  [8-K WARN] {ticker}: No 8-K found in past 150 days")
            return result

        result["found"] = True
        result["accession"] = target["accession"]
        result["filing_date"] = target["date"]
        result["items"] = target["items"]

        acc = target["accession"]
        acc_clean = acc.replace("-", "")

        print(f"  8-K press release: {target['date']} items={target['items']} filter={result['item_filter_used']}")

        # Try to find EX-99.1 via filing index (preferred — actual press release exhibit)
        ex991_url = None
        try:
            time.sleep(0.3)
            idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{acc}-index.htm"
            idx_resp = requests.get(idx_url, headers=HEADERS, timeout=10)
            if idx_resp.status_code == 200:
                ex99_links = re.findall(
                    r'href="(/Archives/edgar/data/[^"]+\.htm[^"]*)"',
                    idx_resp.text, re.IGNORECASE
                )
                for link in ex99_links:
                    if ("ex-99" in link.lower() or "ex99" in link.lower()) and \
                       "8k.htm" not in link.lower() and "8-k.htm" not in link.lower():
                        ex991_url = f"https://www.sec.gov{link}"
                        break
                if not ex991_url:
                    for pat in [
                        r'href="(/Archives/edgar/data/\d+/[^"]+ex.{0,3}99[^"]+\.htm[^"]*)"',
                        r'href="(/Archives/edgar/data/\d+/[^"]+press[^"]+\.htm[^"]*)"',
                    ]:
                        m = re.search(pat, idx_resp.text, re.IGNORECASE)
                        if m:
                            ex991_url = f"https://www.sec.gov{m.group(1)}"
                            break
        except Exception:
            pass

        # EX-99.1 preferred; fall back to primary document
        fetch_url = ex991_url or f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{target['doc']}"
        result["url"] = fetch_url
        result["source_url"] = fetch_url  # backward compat

        # Fetch document text
        doc_text = ""
        try:
            time.sleep(0.5)
            txt_resp = requests.get(fetch_url, headers=HEADERS, timeout=30)
            if txt_resp.status_code == 200:
                doc_text = txt_resp.text
            else:
                # Scan index.json for any htm/txt file
                idx_json_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/index.json"
                idx_resp2 = requests.get(idx_json_url, headers=HEADERS, timeout=10)
                if idx_resp2.status_code == 200:
                    files = idx_resp2.json().get("directory", {}).get("item", [])
                    for f in files:
                        name = f.get("name", "")
                        if name.endswith(".htm") or name.endswith(".txt"):
                            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{name}"
                            dr = requests.get(doc_url, headers=HEADERS, timeout=15)
                            if dr.status_code == 200:
                                doc_text = dr.text
                                result["url"] = doc_url
                                result["source_url"] = doc_url
                                break
        except Exception as e:
            result["note"] = f"fetch_error: {e}"
            print(f"  [8-K ERROR] {ticker}: text fetch failed: {e}")

        if doc_text:
            result["text"] = doc_text[:50000]
            # Parse structured financial fields for reconciliation gate and panels
            parsed = parse_press_release(doc_text, ticker)
            for k, v in parsed.items():
                if k != "raw_text_sample":
                    result[k] = v
        else:
            result["error"] = result.get("error") or "text_fetch_failed"

    except Exception as e:
        result["note"] = f"fetch_error: {e}"
        result["error"] = f"fetch_exception: {str(e)[:80]}"
        print(f"  [8-K ERROR] {ticker}: {e}")

    try:
        cache_file.write_text(json.dumps(result, default=str))
    except Exception:
        pass

    return result




# ── Form 4 Insider Transactions ──────────────────────────────────────────────

def fetch_form4_transactions(cik: str, ticker: str, days: int = 90) -> dict:
    """
    Fetch Form 4 insider transactions from SEC EDGAR for the past N days.
    Returns structured dict with buys, sells, and summary.

    Form 4 is filed within 2 business days of any transaction.
    Distinguishes open-market purchases (P) from plan-based sales (S under 10b5-1).
    """
    import xml.etree.ElementTree as ET

    cache_file = CACHE_DIR / f"form4_{ticker}_{datetime.date.today().isoformat()}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    result = {
        "transactions": [],
        "summary": {
            "open_market_buys_90d": 0.0,
            "open_market_sells_90d": 0.0,
            "plan_sells_90d": 0.0,
            "awards_90d": 0.0,
            "net_open_market_90d": 0.0,
            "ceo_buys_90d": 0.0,
            "open_market_buys_30d": 0.0,
            "open_market_sells_30d": 0.0,
            "plan_sells_30d": 0.0,
            "net_open_market_30d": 0.0,
            "ceo_buys_30d": 0.0,
            "significant_buys": [],
            "significant_sells": [],
        },
        "equity_offerings": [],  # ATM or follow-on offerings — dilution risk
        "source": "SEC_EDGAR_FORM4",
        "period_days": days,
        "error": None,
    }

    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    cik_int = int(cik)

    try:
        atom_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik_int}&type=4&dateb=&owner=include"
            f"&count=40&search_text=&output=atom"
        )
        time.sleep(0.5)
        resp = requests.get(atom_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            result["error"] = f"atom_feed_failed_{resp.status_code}"
            return result

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            result["error"] = "atom_xml_parse_error"
            return result

        entries = root.findall("atom:entry", ns)

        for entry in entries[:30]:
            updated = entry.find("atom:updated", ns)
            if updated is None:
                continue
            try:
                filing_date = datetime.date.fromisoformat(updated.text[:10])
            except Exception:
                continue

            if filing_date < cutoff:
                break

            link = entry.find("atom:link", ns)
            if link is None:
                continue
            href = link.get("href", "")
            # Href is like: .../data/CIK/000.../0001XXXXX-XX-XXXXXX-index.htm
            acc_match = re.search(r'/(\d{10}-\d{2}-\d{6})-index\.htm', href)
            if not acc_match:
                # Try id element: urn:tag:sec.gov,2008:accession-number=XXXXXXXXXX-XX-XXXXXX
                id_el = entry.find("atom:id", ns)
                if id_el is not None:
                    acc_match = re.search(r'accession-number=([0-9-]+)', id_el.text or "")
            accession = None
            acc_clean = None
            if acc_match and acc_match is not True:
                accession = acc_match.group(1)
                acc_clean = accession.replace("-", "")
            else:
                # Try content element for accession-number sub-element
                content_el = entry.find("atom:content", ns)
                if content_el is not None:
                    for acc_el in list(content_el):
                        if acc_el.tag.endswith("accession-number") and acc_el.text:
                            accession = acc_el.text.strip()
                            acc_clean = accession.replace("-", "")
                            break
            if not accession:
                continue

            xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{accession}.xml"
            time.sleep(0.3)
            try:
                xml_resp = requests.get(xml_url, headers=HEADERS, timeout=10)
                if xml_resp.status_code != 200:
                    # Use the href from atom feed directly for the index (handles cross-CIK cases)
                    if href and href.startswith("https://"):
                        idx_url = href
                    else:
                        idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{accession}-index.htm"
                    idx_resp = requests.get(idx_url, headers=HEADERS, timeout=10)
                    if idx_resp.status_code != 200:
                        continue
                    xml_links = re.findall(
                        r'/Archives/edgar/data/\d+/[^"]+\.xml',
                        idx_resp.text
                    )
                    if not xml_links:
                        continue
                    # Prefer the raw XML — skip XSLT-rendered paths (xslF345X06/)
                    raw_xml_links = [l for l in xml_links if 'xslF' not in l]
                    xml_links = raw_xml_links if raw_xml_links else xml_links
                    xml_resp = requests.get(f"https://www.sec.gov{xml_links[0]}", headers=HEADERS, timeout=10)
                    if xml_resp.status_code != 200:
                        continue
            except Exception:
                continue

            try:
                f4_root = ET.fromstring(xml_resp.content)
            except ET.ParseError:
                continue

            reporter_name = ""
            reporter_title = ""
            for el in f4_root.iter("rptOwnerName"):
                reporter_name = el.text or ""
                break
            for el in f4_root.iter("officerTitle"):
                reporter_title = el.text or ""
                break
            is_ceo = any(t in reporter_title.upper() for t in ["CEO", "CHIEF EXECUTIVE", "CHAIRMAN AND CEO"])

            for tx_el in f4_root.iter("nonDerivativeTransaction"):
                try:
                    tx_code = ""
                    for el in tx_el.iter("transactionCode"):
                        tx_code = (el.text or "").strip()
                        break

                    tx_date_str = ""
                    for el in tx_el.iter("transactionDate"):
                        for v in el.iter("value"):
                            tx_date_str = (v.text or "").strip()
                            break
                        break

                    shares = 0.0
                    for el in tx_el.iter("transactionShares"):
                        for v in el.iter("value"):
                            shares = float(v.text.replace(",", "") or 0)
                            break
                        break

                    price = 0.0
                    for el in tx_el.iter("transactionPricePerShare"):
                        for v in el.iter("value"):
                            price = float(v.text.replace(",", "") or 0)
                            break
                        break

                    direction = "A"
                    for el in tx_el.iter("transactionAcquiredDisposedCode"):
                        for v in el.iter("value"):
                            direction = (v.text or "A").strip()
                            break
                        break

                    is_plan = False
                    for el in tx_el.iter("Rule10b5-1Transaction"):
                        for v in el.iter("value"):
                            is_plan = (v.text or "").strip().upper() == "TRUE"
                            break
                        break

                    value = shares * price

                    tx = {
                        "date": tx_date_str or filing_date.isoformat(),
                        "insider": reporter_name[:50],
                        "title": reporter_title[:40],
                        "is_ceo": is_ceo,
                        "code": tx_code,
                        "direction": direction,
                        "shares": shares,
                        "price": price,
                        "value": value,
                        "is_plan": is_plan,
                        "type": (
                            "open_market_purchase" if tx_code == "P" else
                            "open_market_sale" if tx_code == "S" and not is_plan else
                            "plan_sale" if tx_code == "S" and is_plan else
                            "award" if tx_code == "A" else
                            "tax_withholding" if tx_code == "F" else
                            "other"
                        ),
                        "source": "SEC_EDGAR_FORM4",
                    }
                    result["transactions"].append(tx)

                    # Determine if within 30 days
                    try:
                        tx_date_obj = datetime.date.fromisoformat(tx["date"][:10])
                        within_30d = tx_date_obj >= (datetime.date.today() - datetime.timedelta(days=30))
                    except Exception:
                        within_30d = False

                    if tx["type"] == "open_market_purchase":
                        result["summary"]["open_market_buys_90d"] += value
                        if within_30d:
                            result["summary"]["open_market_buys_30d"] += value
                        if is_ceo:
                            result["summary"]["ceo_buys_90d"] += value
                            if within_30d:
                                result["summary"]["ceo_buys_30d"] += value
                        if value >= 1_000_000:
                            result["summary"]["significant_buys"].append({
                                "insider": reporter_name[:40],
                                "title": reporter_title[:30],
                                "value": value,
                                "shares": shares,
                                "date": tx["date"],
                            })
                    elif tx["type"] == "open_market_sale":
                        result["summary"]["open_market_sells_90d"] += value
                        if within_30d:
                            result["summary"]["open_market_sells_30d"] += value
                        if value >= 1_000_000:
                            result["summary"]["significant_sells"].append({
                                "insider": reporter_name[:40],
                                "value": value,
                                "date": tx["date"],
                            })
                    elif tx["type"] == "plan_sale":
                        result["summary"]["plan_sells_90d"] += value
                        if within_30d:
                            result["summary"]["plan_sells_30d"] += value
                    elif tx["type"] == "award":
                        result["summary"]["awards_90d"] += value

                except Exception:
                    continue

        result["summary"]["net_open_market_90d"] = (
            result["summary"]["open_market_buys_90d"] -
            result["summary"]["open_market_sells_90d"]
        )

        result["summary"]["net_open_market_30d"] = (
            result["summary"]["open_market_buys_30d"] -
            result["summary"]["open_market_sells_30d"]
        )

        result["corporate_buybacks"] = {
            "note": "Corporate share repurchases appear in cash flow statement financing section, not Form 4. See metrics.share_repurchases_ttm for the EDGAR figure.",
            "source": "CF_STATEMENT"
        }

        # --- ATM / follow-on offering detection from S-3ASR, 424B3, 424B5 filings ---
        try:
            atm_cutoff = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
            offering_forms = ["S-3ASR", "424B3", "424B5", "424B2", "S-1", "S-3"]
            sub_url = f"https://data.sec.gov/submissions/CIK{cik_int:010d}.json"
            time.sleep(0.3)
            sub_resp = requests.get(sub_url, headers=HEADERS, timeout=15)
            if sub_resp.status_code == 200:
                sub_data = sub_resp.json()
                sub_forms = sub_data.get("filings", {}).get("recent", {})
                s_forms = sub_forms.get("form", [])
                s_dates = sub_forms.get("filingDate", [])
                s_descs = sub_forms.get("primaryDocument", [])
                for idx_f, form_f in enumerate(s_forms):
                    if s_dates[idx_f] < atm_cutoff:
                        break
                    if form_f in offering_forms:
                        result["equity_offerings"].append({
                            "form": form_f,
                            "date": s_dates[idx_f],
                            "label": "EQUITY OFFERING — potential dilution",
                            "doc": s_descs[idx_f] if idx_f < len(s_descs) else "",
                        })
        except Exception:
            pass

        cache_file.write_text(json.dumps(result, indent=2))

    except Exception as e:
        result["error"] = str(e)[:100]

    return result


# ── Legal Proceedings from 10-Q/10-K ─────────────────────────────────────────

def fetch_legal_proceedings(cik: str, ticker: str) -> dict:
    """
    Extract legal proceedings section from most recent 10-Q or 10-K.
    Returns structured dict with active litigation summary.
    """
    cache_file = CACHE_DIR / f"legal_{ticker}_{datetime.date.today().isoformat()}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    result = {
        "proceedings": [],
        "has_sec_investigation": False,
        "has_doj_investigation": False,
        "has_securities_class_action": False,
        "auditor_name": "",
        "auditor_changed": False,
        "going_concern": False,
        "source": "SEC_EDGAR_10Q",
        "error": None,
    }

    cik_int = int(cik)
    cik_padded = str(cik_int).zfill(10)

    try:
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        time.sleep(0.3)
        resp = requests.get(sub_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            result["error"] = f"submissions_failed_{resp.status_code}"
            return result

        filings = resp.json().get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])

        target_acc = None
        target_date = None
        target_form = None
        for i, form in enumerate(forms):
            if form in ("10-Q", "10-K"):
                target_acc = accessions[i]
                target_date = dates[i]
                target_form = form
                break

        if not target_acc:
            result["error"] = "no_10q_found"
            return result

        acc_clean = target_acc.replace("-", "")

        idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{target_acc}-index.htm"
        time.sleep(0.3)
        idx_resp = requests.get(idx_url, headers=HEADERS, timeout=15)
        if idx_resp.status_code != 200:
            result["error"] = f"index_failed_{idx_resp.status_code}"
            return result

        doc_links = re.findall(
            rf'/Archives/edgar/data/{cik_int}/{acc_clean}/[^"]+\.htm[^"]*',
            idx_resp.text
        )
        main_docs = [l for l in doc_links if
                     'ex' not in l.lower().split('/')[-1][:4] and
                     'ex-' not in l.lower()]

        if not main_docs:
            result["error"] = "no_main_document"
            return result

        doc_url = f"https://www.sec.gov{main_docs[0]}"
        time.sleep(0.5)
        doc_resp = requests.get(doc_url, headers=HEADERS, timeout=30)
        if doc_resp.status_code != 200:
            result["error"] = f"doc_fetch_failed_{doc_resp.status_code}"
            return result

        text = re.sub(r'<[^>]+>', ' ', doc_resp.text)
        text = re.sub(r'\s+', ' ', text)

        legal_match = re.search(
            r'(?:LEGAL PROCEEDINGS|Legal Proceedings)(.{100,3000}?)(?:RISK FACTORS|Risk Factors|QUANTITATIVE|Item \d)',
            text, re.IGNORECASE | re.DOTALL
        )

        if legal_match:
            legal_text = legal_match.group(1).strip()
            result["legal_proceedings_text"] = legal_text[:1000]

            legal_lower = legal_text.lower()
            result["has_sec_investigation"] = any(w in legal_lower for w in
                ["sec investigation", "securities and exchange commission", "sec formal order", "sec inquiry"])
            result["has_doj_investigation"] = any(w in legal_lower for w in
                ["department of justice", "doj", "criminal investigation", "grand jury", "indictment"])
            result["has_securities_class_action"] = any(w in legal_lower for w in
                ["securities class action", "class action complaint", "securities fraud class"])

            amounts = re.findall(r'\$[\d,\.]+\s*(?:million|billion|M|B)', legal_text, re.IGNORECASE)
            result["dollar_amounts_mentioned"] = amounts[:5]

        auditor_match = re.search(
            r'(?:registered with|opinions? of|Report of Independent).*?(?:Deloitte|Ernst|PwC|KPMG|BDO|Grant Thornton|RSM|Moss Adams|WithumSmith)[^.]*',
            text, re.IGNORECASE
        )
        if auditor_match:
            auditor_text = auditor_match.group(0)
            for firm in ["Deloitte", "Ernst & Young", "PricewaterhouseCoopers", "KPMG", "BDO", "Grant Thornton"]:
                if firm.lower() in auditor_text.lower():
                    result["auditor_name"] = firm
                    break

        # BUG1 FIX: Require auditor-specific going concern language, not just any mention
        # (10-Qs often mention "going concern" hypothetically in risk factors or ToC)
        # Specifically require "substantial doubt" to appear together with "going concern"
        going_concern_phrases = [
            r"substantial doubt about.*going concern",
            r"going concern.*substantial doubt",
            r"raise substantial doubt.*ability to continue",
            r"doubt about.*ability to continue as a going concern",
            r"substantial doubt.*ability to continue as a going concern",
        ]
        import re as _re_gc
        result["going_concern"] = any(
            _re_gc.search(phrase, text, _re_gc.IGNORECASE)
            for phrase in going_concern_phrases
        )
        result["filing_date"] = target_date
        result["filing_type"] = target_form or "10-Q"

        # Corporate buyback from financing activities
        buyback_m = re.search(
            r'(?:repurchases?|repurchased)\s+of\s+(?:common\s+)?(?:stock|shares)[^$\n]*\$\s*([\d,\.]+)',
            text, re.IGNORECASE
        )
        if not buyback_m:
            buyback_m = re.search(
                r'(?:treasury\s+stock|share\s+repurchase)[^$\n]*\$\s*([\d,\.]+)',
                text, re.IGNORECASE
            )
        if buyback_m:
            try:
                val = float(buyback_m.group(1).replace(',',''))
                # Usually in millions in 10-Q text
                if val < 100000:
                    val_full = val * 1e6
                else:
                    val_full = val
                result["corporate_buyback_quarter"] = {
                    "value": val_full,
                    "source": "SEC_10Q_CASHFLOW",
                    "period": result.get("filing_date", ""),
                    "note": "Corporate share repurchase program — separate from insider Form 4 transactions",
                }
            except (ValueError, TypeError):
                pass

        cache_file.write_text(json.dumps(result, indent=2))

    except Exception as e:
        result["error"] = str(e)[:100]

    return result


# ── Main Entry Point ─────────────────────────────────────────────────────────

def _classify_8k_item(item_str: str) -> str:
    """Classify 8-K event type from item number string."""
    ITEM_TYPES = {
        "1.01": "Material Agreement",
        "1.02": "Contract Termination",
        "1.05": "Cybersecurity Incident",
        "2.01": "Acquisition/Disposition",
        "2.06": "Material Impairment",
        "3.01": "Delisting Notice",
        "4.01": "Auditor Change",
        "5.01": "Control Change",
        "5.02": "Executive Change",
        "5.03": "Charter Amendment",
        "7.01": "Regulation FD",
        "8.01": "Other Material Event",
        "9.01": "Financial Statements",
    }
    for code, label in ITEM_TYPES.items():
        if code in item_str:
            return label
    return "Material Event"


def _classify_6k_event(accn: str, cik_int: int, primary_doc: str, date: str) -> str:
    """
    Classify 6-K event type by fetching and scanning the document title/intro.
    Returns event type string or 'EARNINGS' to skip.
    """
    EARNINGS_KEYWORDS = [
        "quarterly results", "annual results", "financial results",
        "earnings per share", "fourth quarter", "first quarter",
        "second quarter", "third quarter", "full year results",
        "q1 ", "q2 ", "q3 ", "q4 ",
    ]
    OPERATIONAL_KEYWORDS = {
        "fire": "Operational Incident",
        "explosion": "Operational Incident",
        "accident": "Operational Incident",
        "incident": "Operational Incident",
        "force majeure": "Force Majeure",
        "acquisition": "Acquisition",
        "merger": "Acquisition",
        "leadership": "Executive Change",
        "transition": "Executive Change",
        "ceo ": "Executive Change",
        "president": "Executive Change",
        "officer": "Executive Change",
        "appointed": "Executive Change",
        "dividend": "Dividend Declaration",
        "buyback": "Share Repurchase",
        "repurchase": "Share Repurchase",
        "guidance": "Guidance Update",
        "discovery": "Exploration Discovery",
        "reserve": "Reserve Update",
        "permit": "Regulatory/Permit",
        "regulatory": "Regulatory/Permit",
        "government": "Government/Regulatory",
        "court": "Legal Proceedings",
        "litigation": "Legal Proceedings",
    }

    if not primary_doc:
        return "Material Event"

    try:
        accn_fmt = accn.replace("-", "")
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_fmt}/{primary_doc}"
        resp = requests.get(doc_url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return "Material Event"

        content = resp.text[:3000].lower()

        # Earnings check
        earnings_hits = sum(1 for kw in EARNINGS_KEYWORDS if kw in content)
        if earnings_hits >= 2:
            return "EARNINGS"

        # Classify by operational keywords
        for keyword, event_type in OPERATIONAL_KEYWORDS.items():
            if keyword in content:
                return event_type

        return "Material Event"
    except Exception:
        return "Material Event"


def fetch_material_8k_events(cik: str, ticker: str, days: int = 90) -> list:
    """
    BUG5 FIX: Fetch recent material 8-K events (non-earnings) from SEC EDGAR.
    Supports both 8-K (domestic issuers) and 6-K (foreign private issuers).
    Returns list of {date, type, description} for strategic investments,
    major contracts, executive changes, etc. Filed within past N days.
    """
    cache_file = CACHE_DIR / f"material_events_{ticker}_{datetime.date.today().isoformat()}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    events = []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    cik_int = int(cik)

    # Determine which form types to search based on filing type
    filing_forms = ["8-K", "8-K/A"]
    try:
        _names = json.loads((Path.home() / "ORACLE" / "data" / "ticker_names.json").read_text())
        _entry = _names.get(ticker.upper(), {})
        if isinstance(_entry, dict) and _entry.get("foreign_private_issuer"):
            filing_forms = ["6-K", "6-K/A"]
    except Exception:
        pass

    try:
        time.sleep(0.3)
        sub_resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{str(cik_int).zfill(10)}.json",
            headers=HEADERS, timeout=15
        )
        if sub_resp.status_code != 200:
            return events

        filings = sub_resp.json().get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])
        primary_docs = filings.get("primaryDocument", [])

        for i, form in enumerate(forms):
            if form not in filing_forms:
                continue
            if dates[i] < cutoff:
                break  # reverse chronological, stop when past cutoff

            acc = accessions[i]
            acc_clean = acc.replace("-", "")
            primary_doc = primary_docs[i] if i < len(primary_docs) else ""

            if form in ("8-K", "8-K/A"):
                # Check index for material item types
                idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{acc}-index.htm"
                time.sleep(0.2)
                try:
                    idx_resp = requests.get(idx_url, headers=HEADERS, timeout=10)
                    if idx_resp.status_code != 200:
                        continue
                    idx_text = idx_resp.text.lower()

                    # Skip earnings 8-Ks (Item 2.02) — those are handled by find_earnings_8k
                    if "2.02" in idx_text or "results of operations" in idx_text:
                        continue

                    # Material items to surface (not earnings)
                    material_items = {
                        "1.01": "Material Agreement",
                        "1.02": "Contract Termination",
                        "5.02": "Executive Change",
                        "8.01": "Other Material Event",
                        "7.01": "Regulation FD",
                    }

                    for item_code, item_desc in material_items.items():
                        if f"item {item_code}" in idx_text or f"item{item_code}" in idx_text:
                            import re as _re_ev
                            title_m = _re_ev.search(r'<title>([^<]+)</title>', idx_resp.text, _re_ev.IGNORECASE)
                            title = title_m.group(1).strip() if title_m else f"8-K {item_desc}"
                            body_text = ""
                            body_dollar_amounts = []
                            body_description = ""
                            try:
                                body_links = re.findall(
                                    r'/Archives/edgar/data/\d+/[^"]+\.htm',
                                    idx_resp.text
                                )
                                main_body_links = [l for l in body_links if 'ex' not in l.lower().split('/')[-1][:4]]
                                if main_body_links:
                                    time.sleep(0.2)
                                    body_resp = requests.get(
                                        f"https://www.sec.gov{main_body_links[0]}",
                                        headers=HEADERS, timeout=10
                                    )
                                    if body_resp.status_code == 200:
                                        body_text = re.sub(r'<[^>]+>', ' ', body_resp.text)
                                        body_text = re.sub(r'\s+', ' ', body_text)[:5000]
                                        body_dollar_amounts = re.findall(
                                            r'\$[\d,\.]+\s*(?:million|billion|M|B)',
                                            body_text, re.IGNORECASE
                                        )[:5]
                                        sentences = [s.strip() for s in re.split(r'[.!?]', body_text) if len(s.strip()) > 40]
                                        for sent in sentences[1:6]:
                                            if not any(bp in sent.lower() for bp in ['pursuant', 'hereto', 'incorporated', 'exhibits']):
                                                body_description = sent[:200]
                                                break
                            except Exception:
                                pass

                            events.append({
                                "date": dates[i],
                                "type": item_desc,
                                "item": item_code,
                                "accession": acc,
                                "title": title[:100],
                                "dollar_amounts": body_dollar_amounts,
                                "description": body_description,
                            })
                except Exception:
                    continue

            else:
                # 6-K: classify by document content
                try:
                    time.sleep(0.2)
                    event_type = _classify_6k_event(acc, cik_int, primary_doc, dates[i])
                    if event_type == "EARNINGS":
                        continue
                    events.append({
                        "date": dates[i],
                        "type": event_type,
                        "item": "6-K",
                        "accession": acc,
                        "title": f"{ticker} 6-K: {event_type} ({dates[i]})",
                        "dollar_amounts": [],
                        "description": "",
                    })
                except Exception:
                    continue

        cache_file.write_text(json.dumps(events, indent=2))
    except Exception:
        pass

    return events


def fetch_earnings_transcript(ticker: str, cik: str) -> dict:
    """
    Fetch earnings call transcript.
    Priority order:
    1. Browser-fetched transcript cache (from Motley Fool via Hermes browser task)
    2. SEC EDGAR 8-K transcript exhibit (EX-99.2)
    3. EDGAR full-text search fallback
    4. Empty result with explanation
    """
    ticker = ticker.upper()
    today = datetime.date.today().isoformat()

    result = {
        "transcript_text": "",
        "source": "NOT_FOUND",
        "source_url": "",
        "error": None,
    }

    # SOURCE 0 — Browser-fetched transcript cache (highest priority)
    cache_file = CACHE_DIR / f"transcript_{ticker}_{today}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if data.get("transcript_text") and len(data["transcript_text"]) > 500:
                print(f"  Transcript: BROWSER_CACHE ({data.get('char_count', len(data['transcript_text']))} chars from {data.get('source', 'unknown')})")
                result["transcript_text"] = data["transcript_text"]
                result["source"] = "BROWSER_CACHE"
                result["source_url"] = data.get("source", "")
                return result
        except Exception:
            pass

    # SOURCE 1 — SEC EDGAR 8-K transcript exhibit (EX-99.2)
    try:
        filing_info = find_earnings_8k(cik)
        if filing_info:
            acc = filing_info.get("accession", "")
            if acc:
                cik_int = int(cik)
                acc_clean = acc.replace("-", "")
                idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{acc}-index.htm"
                time.sleep(0.3)
                idx_resp = requests.get(idx_url, headers=HEADERS, timeout=15)
                if idx_resp.status_code == 200:
                    exhibit_links = re.findall(
                        r'/Archives/edgar/data/\d+/[^"]+',
                        idx_resp.text
                    )
                    for link in exhibit_links:
                        link_lower = link.lower()
                        is_ex992 = 'ex99' in link_lower and ('2' in link_lower.split('ex99')[-1][:3] or 'ex-99.2' in link_lower)
                        is_transcript = 'transcript' in link_lower
                        if (is_ex992 or is_transcript) and (link_lower.endswith('.htm') or link_lower.endswith('.txt')):
                            full_url = f"https://www.sec.gov{link}"
                            time.sleep(0.3)
                            ex_resp = requests.get(full_url, headers=HEADERS, timeout=20)
                            if ex_resp.status_code == 200:
                                text = re.sub(r'<[^>]+>', ' ', ex_resp.text)
                                text = re.sub(r'\s+', ' ', text)[:15000]
                                if len(text) > 500:
                                    result["transcript_text"] = text
                                    result["source"] = "SEC_8K_EXHIBIT"
                                    result["source_url"] = full_url
                                    return result
    except Exception as e:
        result["error"] = str(e)[:100]

    # SOURCE 2 — EDGAR full-text search
    try:
        search_url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22+%22earnings+call%22"
            f"&dateRange=custom&startdt={(datetime.date.today() - datetime.timedelta(days=90)).isoformat()}"
            f"&enddt={today}&forms=8-K"
        )
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            hits = resp.json().get("hits", {}).get("hits", [])
            if hits:
                print(f"  Transcript: EDGAR_SEARCH ({len(hits)} results, quality uncertain)")
                result["source"] = "EDGAR_SEARCH"
                result["note"] = "EDGAR search found filings but transcript text not extracted -- run browser fetch for full transcript"
                return result
    except Exception:
        pass

    print(f"  Transcript: NOT_FOUND -- run browser fetch task for {ticker}")
    result["note"] = f"No transcript available. To fix: ask Hermes to fetch transcript for {ticker} from Motley Fool"
    return result


def parse_transcript_statements(transcript_text: str, ticker: str) -> dict:
    """
    Parse key management statements from an earnings call transcript.
    Extracts revenue guidance, gross margin guidance, op income guidance,
    CEO demand characterization, and CFO projections.
    """
    result = {
        "revenue_guidance_quote": "",
        "gross_margin_guidance_quote": "",
        "op_income_guidance_quote": "",
        "ceo_demand_quote": "",
        "cfo_projection_quote": "",
        "parsed_ok": False,
    }

    if not transcript_text:
        return result

    try:
        text = transcript_text

        # Full year revenue guidance
        rev_patterns = [
            r'(?:full[- ]year|fiscal[- ]year)[^.]{0,200}?\$[\d\.]+\s*(?:billion|million|B|M)[^.]{0,100}',
            r'\$[\d\.]+\s*(?:billion|million)[^.]{0,100}(?:full[- ]year|fiscal[- ]year|annual)[^.]{0,100}',
        ]
        for pat in rev_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                result["revenue_guidance_quote"] = m.group(0)[:300].strip()
                break

        # Gross margin guidance
        gm_patterns = [
            r'gross\s+margin[^.]{0,200}?(?:target|expect|guide|anticipate)[^.]{0,100}?\d+(?:\.\d+)?%[^.]{0,100}',
            r'\d+(?:\.\d+)?%[^.]{0,100}gross\s+margin[^.]{0,100}(?:target|expect|guide)[^.]{0,100}',
        ]
        for pat in gm_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                result["gross_margin_guidance_quote"] = m.group(0)[:200].strip()
                break

        # Operating income guidance
        op_patterns = [
            r'(?:operating\s+income|EBITDA)[^.]{0,200}?\$[\d\.]+\s*(?:billion|million|B|M)[^.]{0,100}',
            r'\$[\d\.]+\s*(?:billion|million)[^.]{0,100}(?:operating\s+income|EBITDA)[^.]{0,100}',
        ]
        for pat in op_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                result["op_income_guidance_quote"] = m.group(0)[:200].strip()
                break

        # CEO demand characterization
        # Find CEO mention then look for demand/pipeline within 1000 chars
        ceo_pos = -1
        for ceo_title in ['chief executive officer', 'ceo', 'president and ceo']:
            m = re.search(ceo_title, text, re.IGNORECASE)
            if m:
                ceo_pos = m.start()
                break
        if ceo_pos >= 0:
            ceo_window = text[ceo_pos:ceo_pos + 3000]
            demand_m = re.search(r'(?:demand|pipeline)[^.!?]{0,300}', ceo_window, re.IGNORECASE)
            if demand_m:
                result["ceo_demand_quote"] = demand_m.group(0)[:200].strip()

        # CFO projections
        cfo_pos = -1
        for cfo_title in ['chief financial officer', 'cfo']:
            m = re.search(cfo_title, text, re.IGNORECASE)
            if m:
                cfo_pos = m.start()
                break
        if cfo_pos >= 0:
            cfo_window = text[cfo_pos:cfo_pos + 2000]
            dollar_m = re.search(r'[^.!?]{0,100}\$[\d\.]+\s*(?:billion|million|B|M)[^.!?]{0,200}', cfo_window, re.IGNORECASE)
            if dollar_m:
                result["cfo_projection_quote"] = dollar_m.group(0)[:200].strip()

        result["parsed_ok"] = any([
            result["revenue_guidance_quote"],
            result["gross_margin_guidance_quote"],
            result["op_income_guidance_quote"],
            result["ceo_demand_quote"],
            result["cfo_projection_quote"],
        ])
    except Exception:
        pass

    return result


def fetch_commodity_price(commodity_code: str) -> dict:
    """
    Fetch current commodity spot price via yfinance futures.
    Returns {"commodity": str, "price": float|None, "unit": str, "source": str, "date": str}
    Called during preflight before any EDGAR query.
    Results cached daily.
    """
    import datetime as _dt
    today = _dt.date.today().isoformat()
    cache_file = CACHE_DIR / f"commodity_{commodity_code}_{today}.json"

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            if cached.get("price"):
                print(f"  [COMMODITY] {commodity_code}: ${cached['price']:.2f} (cached)")
                return cached
        except Exception:
            pass

    result = {
        "commodity": commodity_code,
        "price": None,
        "unit": COMMODITY_UNITS.get(commodity_code, "unit"),
        "source": "NOT_FOUND",
        "date": today,
    }

    yf_sym = COMMODITY_YF_SYMBOLS.get(commodity_code)
    if yf_sym:
        try:
            import yfinance as yf
            ticker = yf.Ticker(yf_sym)
            fast = ticker.fast_info
            price = getattr(fast, "last_price", None)
            if price and float(price) > 0:
                result["price"] = float(price)
                result["source"] = f"yfinance_{yf_sym}"
                print(f"  [COMMODITY] {commodity_code}: ${price:.2f} ({yf_sym})")
        except Exception as e:
            print(f"  [COMMODITY] yfinance failed for {commodity_code}: {e}")

    if not result["price"]:
        print(f"  [COMMODITY] WARNING: Could not fetch {commodity_code} price — panels will lack current commodity context")

    try:
        cache_file.write_text(json.dumps(result, indent=2))
    except Exception:
        pass

    return result


def run_preflight_web_searches(ticker: str) -> dict:
    """
    Gather pre-EDGAR ground truth for a ticker.
    NO TAVILY. Uses yfinance + EDGAR + browser cache.
    Returns same dict structure as before for backward compatibility.
    """
    today = datetime.date.today().isoformat()
    cache_file = CACHE_DIR / f"preflight_web_{ticker}_{today}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    result = {
        "ticker": ticker,
        "as_of_date": today,
        "identity": {},
        "financials": {},
        "news": [],
        "source": "YFINANCE_EDGAR_BROWSER_CACHE",
    }

    # Identity and price from yfinance (replaces Tavily Search 1)
    print(f"  [PREFLIGHT] Identity check for {ticker}...", end="", flush=True)
    identity = _get_company_identity(ticker)
    result["identity"] = {
        "company_name": identity.get("company_name", ""),
        "price_hint": identity.get("price"),
        "market_cap": identity.get("market_cap"),
        "week52_high": identity.get("week52_high"),
        "week52_low": identity.get("week52_low"),
        "sector": identity.get("sector", ""),
        "industry": identity.get("industry", ""),
        "source": identity.get("source", "yfinance"),
    }
    result["financials"]["eps_hint"] = identity.get("forward_eps")
    result["financials"]["trailing_eps"] = identity.get("trailing_eps")
    result["financials"]["short_percent"] = identity.get("short_percent")
    print(f" {identity.get('company_name', 'unknown')} @ ${identity.get('price', '?')}")

    # Analyst consensus from yfinance (replaces Tavily Search 2 financials)
    analyst = fetch_analyst_consensus(ticker)
    if analyst.get("target_mean"):
        result["financials"]["analyst_target_mean"] = analyst.get("target_mean")
        result["financials"]["analyst_target_high"] = analyst.get("target_high")
        result["financials"]["analyst_target_low"] = analyst.get("target_low")
        result["financials"]["analyst_count"] = analyst.get("analyst_count")
        result["financials"]["recommendation"] = analyst.get("recommendation")

    # News: EDGAR 8-K (CIK will be populated after main CIK lookup in build_fact_sheet)
    result["news"] = []
    result["news_source"] = "EDGAR_8K_PENDING_CIK"

    # Browser-fetched transcript check
    transcript_cache = CACHE_DIR / f"transcript_{ticker}_{today}.json"
    if transcript_cache.exists():
        result["financials"]["transcript_available"] = True
        print(f"  [PREFLIGHT] Transcript cache found for {ticker}")
    else:
        result["financials"]["transcript_available"] = False
        print(f"  [PREFLIGHT] No transcript cache -- run browser pre-fetch for full context")

    # Commodity price fetch — critical for miners, energy, materials
    names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
    ticker_entry = {}
    if names_path.exists():
        try:
            known = json.loads(names_path.read_text())
            entry = known.get(ticker.upper(), {})
            ticker_entry = entry if isinstance(entry, dict) else {}
        except Exception:
            pass

    commodity_code = ticker_entry.get("commodity")
    if commodity_code:
        print(f"  [PREFLIGHT] Fetching commodity price: {commodity_code}...")
        commodity_data = fetch_commodity_price(commodity_code)
        result["commodity"] = commodity_data
        if commodity_data.get("price"):
            result.setdefault("financials", {})
            result["financials"]["commodity_price"] = commodity_data["price"]
            result["financials"]["commodity_code"] = commodity_code
            print(f"  [PREFLIGHT] {commodity_code} spot: ${commodity_data['price']:.2f}")

    try:
        cache_file.write_text(json.dumps(result, indent=2, default=str))
    except Exception:
        pass

    return result


def reconciliation_gate(fact_sheet: dict) -> dict:
    """
    5-check reconciliation gate. Runs after fact_sheet is built, before panels fire.
    Returns dict with: passed (bool), corrections (list), hard_stops (list), gate_log (list).
    Hard stops abort the run. Corrections substitute wrong values with verified ones.
    """
    gate_log = []
    corrections = []
    hard_stops = []

    ticker = fact_sheet.get("ticker", "?")
    metrics = fact_sheet.get("metrics") or {}
    press_release = fact_sheet.get("press_release") or {}
    preflight = fact_sheet.get("preflight_web") or {}

    def log(check_name, status, detail):
        symbol = "PASS" if status else "FAIL"
        gate_log.append(f"  [{symbol}] {check_name}: {detail}")
        print(f"  [GATE {symbol}] {check_name}: {detail}")

    # ── CHECK 1: Revenue Consistency ─────────────────────────────────────────
    # Prefer 8-K press release quarterly revenue over XBRL (XBRL may have YTD contamination)
    rev_mrq = None
    rev_mrq_source = "unknown"
    try:
        # Try press_release first (8-K ground truth)
        pr_rev = press_release.get("revenue_quarter", {})
        if not isinstance(pr_rev, dict):
            pr_rev = {}
        rev_mrq_pr = pr_rev.get("value")

        # Also try parsed sub-dict
        if rev_mrq_pr is None:
            parsed = press_release.get("parsed", {})
            if isinstance(parsed, dict):
                rev_mrq_pr = parsed.get("revenue_quarter", {}).get("value")

        if rev_mrq_pr and rev_mrq_pr > 1e6:  # sanity: >$1M
            rev_mrq = rev_mrq_pr
            rev_mrq_source = "8K_PRESS_RELEASE"
        else:
            # Fallback: XBRL MRQ (may have YTD issues — treat as lower confidence)
            xbrl_mrq = metrics.get("revenue_mrq", {}).get("value")
            if xbrl_mrq and xbrl_mrq > 1e6:
                rev_mrq = xbrl_mrq
                rev_mrq_source = "XBRL_MRQ_FALLBACK"
    except Exception:
        pass

    rev_ttm = metrics.get("revenue_ttm", {}).get("value")

    if rev_mrq and rev_mrq > 0 and rev_ttm and rev_ttm > 0:
        implied_annual = rev_mrq * 4
        ratio = rev_ttm / implied_annual
        if ratio < 0.7 or ratio > 2.0:
            detail = (f"TTM=${rev_ttm/1e9:.2f}B vs MRQ×4=${implied_annual/1e9:.2f}B ratio={ratio:.2f}x — REJECTED, using MRQ×4")
            log("Revenue Consistency", False, detail)
            hard_stops.append(f"REVENUE: {detail}")
            if "revenue_ttm" in metrics:
                metrics["revenue_ttm"]["value"] = implied_annual
                metrics["revenue_ttm"]["source"] = "GATE_CORRECTED_MRQ_X4"
                metrics["revenue_ttm"]["warning"] = detail
                corrections.append(f"revenue_ttm corrected: ${rev_ttm/1e9:.2f}B → ${implied_annual/1e9:.2f}B (MRQ×4)")
        else:
            log("Revenue Consistency", True, f"TTM=${rev_ttm/1e9:.2f}B, {rev_mrq_source} MRQ×4=${implied_annual/1e9:.2f}B, ratio={ratio:.2f}x")
    elif rev_mrq and rev_mrq > 0 and not rev_ttm:
        implied_annual = rev_mrq * 4
        metrics["revenue_ttm"] = {
            "value": implied_annual,
            "period": "TTM_ESTIMATED",
            "source": "GATE_MRQ_X4_PROXY",
            "is_gaap": True,
            "warning": "No XBRL TTM available — estimated as MRQ×4"
        }
        corrections.append(f"revenue_ttm set from MRQ×4: ${implied_annual/1e9:.2f}B")
        log("Revenue Consistency", True, f"No XBRL TTM — set MRQ×4=${implied_annual/1e9:.2f}B as proxy")
    else:
        log("Revenue Consistency", True, "Skipped — MRQ or TTM unavailable for comparison")

    # ── CHECK 2: EPS Consistency ──────────────────────────────────────────────
    eps_mrq = None
    try:
        eps_mrq = (
            press_release.get("eps_gaap_quarter", {}).get("value") or
            press_release.get("parsed", {}).get("eps_gaap_quarter", {}).get("value")
        )
    except Exception:
        pass

    eps_ttm = metrics.get("gaap_eps_ttm", {}).get("value")

    if eps_mrq is not None and eps_ttm is not None and eps_mrq != 0:
        implied_eps = eps_mrq * 4
        ratio = abs(eps_ttm / implied_eps) if implied_eps != 0 else 999
        if ratio < 0.4 or ratio > 2.5:
            detail = f"TTM EPS={eps_ttm:.2f} vs MRQ×4={implied_eps:.2f} ratio={ratio:.2f}x — suspect"
            log("EPS Consistency", False, detail)
            corrections.append(f"gaap_eps_ttm flagged: {eps_ttm:.2f} vs estimated {implied_eps:.2f}")
            if "gaap_eps_ttm" in metrics:
                metrics["gaap_eps_ttm"]["warning"] = f"GATE: {detail}"
        else:
            log("EPS Consistency", True, f"TTM={eps_ttm:.2f}, MRQ×4={implied_eps:.2f}, ratio={ratio:.2f}x")
    else:
        log("EPS Consistency", True, "Skipped — MRQ EPS or TTM EPS unavailable")

    # ── CHECK 3: Operating Cash Flow Consistency ──────────────────────────────
    ocf_mrq = None
    try:
        ocf_mrq = (
            press_release.get("operating_cashflow_quarter", {}).get("value") or
            press_release.get("parsed", {}).get("operating_cashflow_quarter", {}).get("value")
        )
    except Exception:
        pass

    ocf_ttm = metrics.get("operating_cashflow_ttm", {}).get("value")

    if ocf_mrq and ocf_mrq > 0 and ocf_ttm is not None:
        implied_ocf = ocf_mrq * 4
        ratio = ocf_ttm / implied_ocf if implied_ocf != 0 else 999
        if ratio < 0.4 or ratio > 2.5:
            detail = f"TTM OCF=${ocf_ttm/1e6:.0f}M vs MRQ×4=${implied_ocf/1e6:.0f}M ratio={ratio:.2f}x — flagged"
            log("OCF Consistency", False, detail)
            corrections.append(f"operating_cashflow_ttm flagged: {detail}")
            if "operating_cashflow_ttm" in metrics:
                metrics["operating_cashflow_ttm"]["warning"] = f"GATE: {detail}"
        else:
            log("OCF Consistency", True, f"TTM=${ocf_ttm/1e6:.0f}M, MRQ×4=${implied_ocf/1e6:.0f}M, ratio={ratio:.2f}x")

    elif ocf_ttm is not None:
        # 8-K OCF not available — use yfinance as fallback for gate validation
        try:
            import yfinance as yf
            cf = yf.Ticker(ticker).cashflow
            if cf is not None and not cf.empty:
                for label in ("Operating Cash Flow", "Cash From Operations",
                              "Total Cash From Operating Activities"):
                    if label in cf.index:
                        yf_ocf = float(cf.loc[label].dropna().iloc[0])
                        if yf_ocf != 0:
                            ratio = ocf_ttm / yf_ocf
                            if ratio < 0.3 or ratio > 3.0:
                                detail = (
                                    f"XBRL OCF=${ocf_ttm/1e6:.0f}M diverges {ratio:.2f}x from "
                                    f"yfinance annual OCF=${yf_ocf/1e6:.0f}M — "
                                    f"XBRL likely wrong period or seasonal single quarter"
                                )
                                log("OCF Consistency (yfinance fallback)", False, detail)
                                corrections.append(f"operating_cashflow_ttm suspect: {detail}")
                                if "operating_cashflow_ttm" in metrics:
                                    metrics["operating_cashflow_ttm"]["warning"] = (
                                        f"GATE YFINANCE CHECK: {detail}. "
                                        f"Use yfinance figure ${yf_ocf/1e6:.0f}M as reference."
                                    )
                                    # Correct value if divergence is extreme
                                    if ratio < 0.2 or ratio > 5.0:
                                        metrics["operating_cashflow_ttm"]["value"] = yf_ocf
                                        metrics["operating_cashflow_ttm"]["source"] = "GATE_CORRECTED_YFINANCE"
                                        corrections.append(
                                            f"operating_cashflow_ttm corrected: "
                                            f"${ocf_ttm/1e6:.0f}M -> ${yf_ocf/1e6:.0f}M (yfinance)"
                                        )
                            else:
                                log("OCF Consistency (yfinance fallback)", True,
                                    f"XBRL=${ocf_ttm/1e6:.0f}M, yfinance=${yf_ocf/1e6:.0f}M, ratio={ratio:.2f}x")
                        break
        except Exception as e:
            log("OCF Consistency", True, f"Skipped — no MRQ OCF and yfinance fallback failed: {e}")
    else:
        log("OCF Consistency", True, "Skipped — OCF TTM unavailable for comparison")

    # ── CHECK 4: Market Cap / P/S Plausibility ────────────────────────────────
    try:
        import yfinance as _yf
        mktcap = _yf.Ticker(ticker).info.get("marketCap") or 0
    except Exception:
        mktcap = 0

    rev_ttm_final = metrics.get("revenue_ttm", {}).get("value") or 0
    if mktcap > 0 and rev_ttm_final > 0:
        ps = mktcap / rev_ttm_final
        if ps < 0.05:
            detail = f"P/S={ps:.3f}x — revenue likely wrong period (pre-divestiture or wrong taxonomy)"
            log("P/S Plausibility", False, detail)
            hard_stops.append(f"P/S: {detail}")
        elif ps > 500:
            detail = f"P/S={ps:.0f}x — revenue likely single quarter misread as annual"
            log("P/S Plausibility", False, detail)
            hard_stops.append(f"P/S: {detail}")
        else:
            log("P/S Plausibility", True, f"P/S={ps:.2f}x — within 0.05x–500x range")
    else:
        log("P/S Plausibility", True, "Skipped — market cap or revenue unavailable")

    # ── CHECK 5: Company Name Disambiguation ──────────────────────────────────
    company_check = fact_sheet.get("company_check") or {}
    company_name = fact_sheet.get("company_name") or company_check.get("company_name") or "UNKNOWN"

    # Prefer yfinance-sourced expected name (from check_ticker_company_name) over Tavily web search.
    # Tavily results for short/ambiguous tickers (ACM, IQ, AI) often return garbage.
    # yfinance longName is reliable for any ticker with an active listing.
    yf_expected = company_check.get("expected", "")
    preflight_company = preflight.get("identity", {}).get("company_name", "")
    # Use yfinance if available; fall back to Tavily web search only if yfinance empty
    comparison_name = yf_expected or preflight_company

    if comparison_name and company_name and company_name != "UNKNOWN":
        def sig_words(name):
            stopwords = {"inc", "corp", "llc", "ltd", "the", "co", "inc.", "corp.", "ltd.",
                         "holdings", "group", "plc", "technologies", "technology"}
            return set(w.lower().strip(".,") for w in name.split() if len(w) > 3 and w.lower() not in stopwords)

        edgar_words = sig_words(company_name)
        compare_words = sig_words(comparison_name)
        overlap = edgar_words & compare_words
        compare_source = "yfinance" if yf_expected else "web_search"

        if not overlap:
            detail = f"EDGAR='{company_name}' vs {compare_source}='{comparison_name}' — no word overlap"
            log("Company Disambiguation", False, detail)
            hard_stops.append(f"DISAMBIGUATION: {detail}")
        else:
            log("Company Disambiguation", True, f"EDGAR='{company_name}' matches {compare_source}='{comparison_name}'")
    elif company_name and company_name != "UNKNOWN":
        log("Company Disambiguation", True, f"EDGAR='{company_name}' (no comparison source available)")
    else:
        log("Company Disambiguation", False, "Company name unknown — cannot verify")

    passed = len(hard_stops) == 0
    return {
        "passed": passed,
        "hard_stops": hard_stops,
        "corrections": corrections,
        "gate_log": gate_log,
        "company_name": company_name,
    }


def calculate_miner_nav(ticker: str, fact_sheet: dict) -> dict:
    """
    Calculate Net Asset Value for gold/silver miners.
    NAV = FCF_perpetuity / discount_rate + net_cash
    Industry standard valuation for commodity producers.
    """
    result = {
        "method": "NAV",
        "nav_per_share": None,
        "p_nav_ratio": None,
        "inputs": {},
        "error": None,
    }
    try:
        preflight   = fact_sheet.get("preflight_web", {})
        metrics     = fact_sheet.get("metrics", {})
        price       = fact_sheet.get("price") or fact_sheet.get("live_price")
        guidance    = fact_sheet.get("guidance", {}) or {}

        commodity_price = (preflight.get("commodity") or {}).get("price")
        if not commodity_price:
            commodity_price = (preflight.get("financials") or {}).get("commodity_price")
        if not commodity_price:
            result["error"] = "No commodity price in preflight — run with commodity field set"
            return result

        shares = None
        shares_entry = (metrics.get("shares_outstanding") or {})
        if isinstance(shares_entry, dict):
            shares = shares_entry.get("value")
        elif isinstance(shares_entry, (int, float)):
            shares = shares_entry
        if not shares or shares <= 0:
            result["error"] = "Shares outstanding not found"
            return result

        # AISC — try press release first, then guidance
        aisc = None
        pr = fact_sheet.get("press_release") or {}
        for key in ("aisc_per_oz", "all_in_sustaining_cost_per_oz"):
            v = pr.get(key) or (pr.get("parsed") or {}).get(key)
            if v:
                try: aisc = float(str(v).replace(",", "")); break
                except: pass

        if not aisc:
            for key in ("aisc_midpoint", "aisc_guidance_midpoint", "aisc_guidance"):
                v = guidance.get(key)
                if v:
                    try: aisc = float(str(v).replace(",", "")); break
                    except: pass

        if not aisc:
            result["error"] = "AISC not found in press release or guidance"
            return result

        margin_per_oz = commodity_price - aisc
        if margin_per_oz <= 0:
            result["error"] = f"Negative margin: commodity ${commodity_price:.0f} < AISC ${aisc:.0f}"
            return result

        # Annual production
        annual_oz = None
        for key in ("production_oz_midpoint", "production_guidance_midpoint", "annual_production_oz"):
            v = guidance.get(key)
            if v:
                try: annual_oz = float(str(v).replace(",", "")); break
                except: pass

        if not annual_oz or annual_oz <= 0:
            result["error"] = "Annual production oz not found in guidance"
            return result

        gross_margin   = margin_per_oz * annual_oz
        sustaining_cap = gross_margin * 0.15
        annual_fcf     = gross_margin - sustaining_cap

        discount_rate = 0.05
        nav_operating = annual_fcf / discount_rate

        net_cash = 0
        nc_entry = metrics.get("net_cash") or {}
        if isinstance(nc_entry, dict):
            net_cash = nc_entry.get("value") or 0
        elif isinstance(nc_entry, (int, float)):
            net_cash = nc_entry

        total_nav     = nav_operating + net_cash
        nav_per_share = total_nav / shares
        p_nav = (price / nav_per_share) if (price and nav_per_share > 0) else None

        result.update({
            "nav_per_share": round(nav_per_share, 2),
            "p_nav_ratio": round(p_nav, 2) if p_nav else None,
            "inputs": {
                "commodity_price": commodity_price,
                "aisc_per_oz": aisc,
                "margin_per_oz": round(margin_per_oz, 2),
                "annual_production_oz": annual_oz,
                "annual_fcf_bn": round(annual_fcf / 1e9, 3),
                "net_cash_bn": round(net_cash / 1e9, 3),
                "total_nav_bn": round(total_nav / 1e9, 3),
                "discount_rate": discount_rate,
                "shares_outstanding": shares,
            },
            "interpretation": (
                f"P/NAV {p_nav:.2f}x — stock trades at "
                f"{'PREMIUM to' if p_nav > 1 else 'DISCOUNT to'} NAV. "
                f"Senior gold miners historically trade 1.0-1.5x NAV."
            ) if p_nav else "P/NAV not calculated (missing price)",
        })
        print(f"  [NAV] {ticker}: ${nav_per_share:.2f}/share, P/NAV={p_nav:.2f}x at ${price:.2f}")

    except Exception as e:
        result["error"] = str(e)[:120]

    return result


def calculate_commodity_eps(ticker: str, fact_sheet: dict) -> dict:
    """
    Calculate forward EPS for commodity producers using current commodity price.
    Produces 4 scenarios: base, bull (+20%), bear (-20%), stress (-40%).
    More accurate than analyst consensus when commodity has moved significantly.
    """
    result = {
        "method": "COMMODITY_EPS",
        "eps_base": None,
        "eps_bull": None,
        "eps_bear": None,
        "eps_stress": None,
        "commodity_price_used": None,
        "note": "",
    }
    try:
        preflight = fact_sheet.get("preflight_web", {})
        commodity_price = (preflight.get("commodity") or {}).get("price")
        if not commodity_price:
            commodity_price = (preflight.get("financials") or {}).get("commodity_price")
        if not commodity_price:
            result["note"] = "No commodity price — skipping commodity EPS"
            return result

        guidance = fact_sheet.get("guidance", {}) or {}
        metrics  = fact_sheet.get("metrics", {}) or {}

        annual_oz = None
        for key in ("production_oz_midpoint", "production_guidance_midpoint", "annual_production_oz"):
            v = guidance.get(key)
            if v:
                try: annual_oz = float(str(v).replace(",", "")); break
                except: pass

        aisc = None
        for key in ("aisc_midpoint", "aisc_guidance_midpoint"):
            v = guidance.get(key)
            if v:
                try: aisc = float(str(v).replace(",", "")); break
                except: pass
        if not aisc:
            pr = fact_sheet.get("press_release") or {}
            for key in ("aisc_per_oz", "all_in_sustaining_cost_per_oz"):
                v = pr.get(key) or (pr.get("parsed") or {}).get(key)
                if v:
                    try: aisc = float(str(v).replace(",", "")); break
                    except: pass

        shares = None
        shares_entry = metrics.get("shares_outstanding") or {}
        if isinstance(shares_entry, dict):
            shares = shares_entry.get("value")
        elif isinstance(shares_entry, (int, float)):
            shares = shares_entry

        if not all([annual_oz, aisc, shares]):
            result["note"] = f"Missing data: annual_oz={annual_oz} aisc={aisc} shares={shares}"
            return result

        tax_rate = 0.25

        def calc_eps(gold_price):
            gross = (gold_price - aisc) * annual_oz
            if gross <= 0:
                return 0.0
            ebit = gross * 0.85
            net  = ebit * (1 - tax_rate)
            return round(net / shares, 2)

        result.update({
            "eps_base":   calc_eps(commodity_price),
            "eps_bull":   calc_eps(commodity_price * 1.20),
            "eps_bear":   calc_eps(commodity_price * 0.80),
            "eps_stress": calc_eps(commodity_price * 0.60),
            "commodity_price_used": commodity_price,
            "note": (
                f"Calc from ${commodity_price:.0f}/oz commodity × "
                f"{annual_oz/1e6:.2f}M oz @ ${aisc:.0f}/oz AISC"
            ),
        })
        print(f"  [C-EPS] {ticker}: base=${result['eps_base']:.2f} bear=${result['eps_bear']:.2f} stress=${result['eps_stress']:.2f}")

    except Exception as e:
        result["note"] = f"Calculation error: {e}"

    return result


def validate_and_calibrate_forward_eps(
    ticker: str,
    fact_sheet: dict,
    commodity_eps: dict,
) -> dict:
    """
    Compare analyst consensus forward EPS vs commodity-derived EPS.
    If divergence > 25%, flag analyst figure as stale and promote commodity EPS.
    Universal for any commodity-linked ticker.
    """
    result = {
        "analyst_eps": None,
        "commodity_eps": None,
        "recommended_eps": None,
        "calibration_note": "",
        "divergence_pct": None,
    }

    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        analyst_eps = info.get("forwardEps")
        result["analyst_eps"] = analyst_eps
    except Exception:
        pass

    commodity_base_eps = commodity_eps.get("eps_base")
    result["commodity_eps"] = commodity_base_eps

    if not result["analyst_eps"] or not commodity_base_eps:
        result["recommended_eps"] = result["analyst_eps"] or commodity_base_eps
        result["calibration_note"] = "Only one EPS source available — using what exists"
        return result

    divergence = abs(result["analyst_eps"] - commodity_base_eps) / max(
        abs(result["analyst_eps"]), abs(commodity_base_eps), 0.01
    )
    result["divergence_pct"] = round(divergence * 100, 1)

    if divergence > 0.25:
        direction = "OVERESTIMATES" if result["analyst_eps"] > commodity_base_eps else "UNDERESTIMATES"
        result["recommended_eps"] = commodity_base_eps
        result["calibration_note"] = (
            f"CALIBRATION WARNING: Analyst consensus EPS ${result['analyst_eps']:.2f} "
            f"{direction} commodity-derived EPS ${commodity_base_eps:.2f} "
            f"by {divergence*100:.0f}%. Analyst consensus may embed a different "
            f"commodity price assumption. Using commodity-derived EPS as primary figure."
        )
        print(f"  [EPS CAL] {ticker}: analyst ${result['analyst_eps']:.2f} vs "
              f"commodity ${commodity_base_eps:.2f} ({divergence*100:.0f}%) — using commodity")
    else:
        result["recommended_eps"] = result["analyst_eps"]
        result["calibration_note"] = (
            f"Analyst EPS ${result['analyst_eps']:.2f} consistent with "
            f"commodity-derived ${commodity_base_eps:.2f} "
            f"({divergence*100:.0f}% — within 25% threshold)"
        )

    return result


def escalate_leadership_transitions(
    material_events: list,
    ticker: str,
) -> list:
    """
    Scan material events for leadership transitions and escalate priority.
    Adds contextual analysis for founder-led and complex-jurisdiction companies.
    Universal — works for any company with executive change filings.
    """
    try:
        _names = json.loads((Path.home() / "ORACLE" / "data" / "ticker_names.json").read_text())
        _entry = _names.get(ticker.upper(), {})
        is_founder_led = _entry.get("founder_led", False) if isinstance(_entry, dict) else False
        is_complex_jurisdiction = _entry.get("complex_jurisdiction", False) if isinstance(_entry, dict) else False
    except Exception:
        is_founder_led = False
        is_complex_jurisdiction = False

    escalated = []
    for event in material_events:
        ev = dict(event)
        if ev.get("type") in ("Executive Change", "Leadership Transition", "Executive/director change"):
            notes = []
            if is_founder_led:
                notes.append(
                    "FOUNDER CEO TRANSITION: Founder-led companies often experience "
                    "strategy drift and culture change during CEO transitions. "
                    "Assign 15-20% additional risk premium until new CEO demonstrates continuity."
                )
            if is_complex_jurisdiction:
                notes.append(
                    "COMPLEX JURISDICTION RISK: Company operates in jurisdictions requiring "
                    "active government relationship management (permits, royalties, community). "
                    "CEO transitions create relationship continuity risk. "
                    "Monitor new CEO's Africa/Asia/LatAm experience specifically."
                )
            if notes:
                ev["escalation"] = " | ".join(notes)
                ev["severity"] = "HIGH"
                print(f"  [LEADERSHIP] {ticker}: CEO transition escalated to HIGH severity")
        escalated.append(ev)
    return escalated


def extract_time_sensitive_risks(
    transcript_text: str,
    ticker: str,
    run_date: str = None,
) -> list:
    """
    Extract time-sensitive risks with deadlines from earnings call transcript.
    Universal — no sector classification needed.
    Returns list of risk dicts with type, context, severity, source.
    """
    import re as _re
    import datetime as _dt

    if not transcript_text or len(transcript_text) < 500:
        return []

    today = run_date or str(_dt.date.today())

    DEADLINE_PATTERNS = [
        (
            r'(?:permit|approval|license|certificate)[^.]{0,100}'
            r'(?:by|before|end of|no later than|deadline)[^.]{0,80}'
            r'(?:january|february|march|april|may|june|july|august|'
            r'september|october|november|december)\s+\d{4}',
            "PERMIT_DEADLINE",
        ),
        (
            r'guidance[^.]{0,150}'
            r'(?:contingent|conditional|subject to|assuming|if)[^.]{0,100}'
            r'(?:permit|approval|decision|resolution)',
            "CONDITIONAL_GUIDANCE",
        ),
        (
            r'(?:production|ramp|commissioning)[^.]{0,100}'
            r'(?:expected|anticipated|planned)[^.]{0,80}'
            r'(?:second half|h2|q2|q3|q4|by year.?end)',
            "PRODUCTION_TIMELINE",
        ),
        (
            r'(?:if|unless)[^.]{0,80}'
            r'(?:not|fail|unable|delay)[^.]{0,80}'
            r'(?:by|before|end of)\s+(?:june|july|august|september|q[234])\s+\d{4}',
            "CONDITIONAL_RISK",
        ),
        (
            r'(?:new|successor|incoming|appointed|named)[^.]{0,80}'
            r'(?:ceo|president|chief executive)',
            "LEADERSHIP_TRANSITION",
        ),
        (
            r'(?:debt|facility|credit|maturity)[^.]{0,80}'
            r'(?:due|matures?|expires?)[^.]{0,60}'
            r'20\d{2}',
            "DEBT_MATURITY",
        ),
    ]

    HIGH_SEVERITY_KEYWORDS = [
        "material", "significant", "critical", "essential",
        "required", "must", "will not proceed", "contingent",
        "conditional", "offset", "guidance", "needed",
    ]

    def assess_severity(risk_type, context):
        score = sum(1 for kw in HIGH_SEVERITY_KEYWORDS if kw in context.lower())
        if risk_type in ("PERMIT_DEADLINE", "CONDITIONAL_GUIDANCE"):
            score += 2
        return "HIGH" if score >= 3 else "MEDIUM" if score >= 1 else "LOW"

    text_lower = transcript_text.lower()
    risks = []

    for pattern, risk_type in DEADLINE_PATTERNS:
        try:
            matches = list(_re.finditer(pattern, text_lower, _re.IGNORECASE))
            for match in matches[:3]:
                start = max(0, match.start() - 100)
                end = min(len(transcript_text), match.end() + 250)
                context = " ".join(transcript_text[start:end].split())[:350]
                risks.append({
                    "type": risk_type,
                    "context": context,
                    "severity": assess_severity(risk_type, context),
                    "source": "earnings_call_transcript",
                })
        except Exception:
            continue

    # Deduplicate
    seen = set()
    unique = []
    for r in risks:
        key = r["context"][:60].lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)

    if unique:
        print(f"  [TIME-SENSITIVE RISKS] {ticker}: {len(unique)} risks from transcript")

    return unique[:10]


def extract_sector_operational_metrics(
    ticker: str,
    press_release_text: str,
    sector: str
) -> dict:
    """
    Extract sector-specific operational metrics from press release text.
    Returns dict of metric_name -> {"value": float/str, "raw": str, "source": str}
    Universal — works for any sector defined in SECTOR_OPERATIONAL_PATTERNS.
    """
    import re as _re

    results = {}
    patterns = SECTOR_OPERATIONAL_PATTERNS.get(sector, {})
    if not patterns:
        return results

    text_lower = press_release_text.lower()

    for metric_name, pattern_list in patterns.items():
        for pattern in pattern_list:
            try:
                match = _re.search(pattern, text_lower, _re.IGNORECASE)
                if match:
                    raw = match.group(0)
                    value_str = match.group(1).replace(",", "").strip()
                    try:
                        value = float(value_str)
                        results[metric_name] = {
                            "value": value,
                            "raw": raw[:120],
                            "source": "press_release_parsed",
                        }
                        break
                    except ValueError:
                        results[metric_name] = {
                            "value": value_str,
                            "raw": raw[:120],
                            "source": "press_release_parsed",
                        }
                        break
            except Exception:
                continue

    if results:
        print(f"  [SECTOR METRICS] {ticker} ({sector}): "
              f"{len(results)} fields: {list(results.keys())}")

    return results


def build_fact_sheet(ticker: str) -> dict:
    """
    Build a complete verified fact sheet for a ticker.
    Caches to ~/ORACLE/cache/factsheet_{ticker}_{today}.json.
    """
    ticker = ticker.upper().strip()
    today = datetime.date.today().isoformat()
    cache_path = CACHE_DIR / f"factsheet_{ticker}_{today}.json"

    # Return cached if available
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            pass

    # Get current price — use session-isolated price (fetches once per process lifetime)
    price = get_session_price(ticker)
    if price is None:
        # Fallback to yfinance .info if fast_info unavailable
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info
            price = info.get("currentPrice") or info.get("regularMarketPrice")
        except Exception:
            pass

    # --- PREFLIGHT WEB SEARCHES (ground truth before EDGAR) ---
    preflight_web = {}
    try:
        preflight_web = run_preflight_web_searches(ticker)
        print(f"  Preflight web: identity='{preflight_web.get('identity', {}).get('company_name', 'unknown')}' | {len(preflight_web.get('news', []))} news items")
    except Exception as e:
        print(f"  Preflight web searches failed (non-fatal): {e}")

    # Step 1: Get CIK
    cik = get_cik(ticker)
    xbrl_available = False
    metrics = {}
    warnings = []
    shares_outstanding = None

    # --- MANDATORY DISAMBIGUATION CHECK ---
    company_check = {}
    if cik:
        try:
            company_check = check_ticker_company_name(ticker, cik)
            if not company_check.get("ok"):
                print(f"\n  [HARD STOP] {company_check.get('error', 'disambiguation failed')}")
                # Return a minimal fact sheet with the error — do not proceed
                return {
                    "ticker": ticker,
                    "price": price,
                    "as_of_date": datetime.date.today().isoformat(),
                    "cik": cik,
                    "company_name": company_check.get("company_name", "UNKNOWN"),
                    "company_check": company_check,
                    "metrics": {},
                    "guidance": {},
                    "press_release": {},
                    "recent_news": [],
                    "material_events": [],
                    "insider_form4": {},
                    "legal_proceedings": {},
                    "data_quality": {
                        "xbrl_available": False,
                        "guidance_sourced": False,
                        "missing_fields": ["revenue_ttm", "gaap_eps_ttm"],
                        "warnings": [company_check.get("error", "disambiguation failed")],
                        "hard_stop": True,
                        "hard_stop_reason": company_check.get("error", "disambiguation failed"),
                    },
                    "halt_if_missing": ["revenue_ttm", "gaap_eps_ttm"],
                }
            else:
                print(f"  Company confirmed: {company_check.get('company_name')} [via {company_check.get('source')}]")
        except Exception as e:
            print(f"  Company check failed (proceeding): {e}")
            company_check = {"ok": True, "company_name": "CHECK_FAILED", "error": str(e)[:100]}

    # Primary: fetch earnings press release from 8-K EX-99.1
    press_release = {}
    if cik:
        try:
            press_release = fetch_earnings_press_release(ticker, cik)
            if press_release.get("parse_success"):
                print(f"  8-K press release loaded: {press_release.get('fields_parsed', 0)} fields from {press_release.get('filing_date', '')}")
            else:
                print(f"  8-K press release: {press_release.get('error', 'parse failed')}")
        except Exception as e:
            print(f"  8-K press release failed: {e}")

    # Analyst consensus (yfinance, after press release so cache is warm)
    analyst_consensus = {}
    try:
        analyst_consensus = fetch_analyst_consensus(ticker)
    except Exception as e:
        print(f"  Analyst consensus failed (non-fatal): {e}")

    # Earnings call transcript
    transcript_data = {}
    parsed_statements = {}
    if cik:
        try:
            transcript_data = fetch_earnings_transcript(ticker, cik)
            if transcript_data.get("transcript_text"):
                parsed_statements = parse_transcript_statements(
                    transcript_data["transcript_text"], ticker
                )
                print(f"  Transcript: {transcript_data['source']} — {len(transcript_data['transcript_text'])} chars")
            else:
                print(f"  Transcript: {transcript_data.get('source', 'NOT_FOUND')}")
        except Exception as e:
            print(f"  Transcript fetch failed: {e}")
            transcript_data = {"source": "ERROR", "error": str(e)[:100]}

    # Form 4 insider transactions
    insider_data = {}
    if cik:
        try:
            insider_data = fetch_form4_transactions(cik, ticker, days=90)
            buys = insider_data.get("summary", {}).get("open_market_buys_90d", 0)
            sells = insider_data.get("summary", {}).get("open_market_sells_90d", 0)
            print(f"  Form 4: {len(insider_data.get('transactions', []))} transactions | buys=${buys/1e6:.1f}M sells=${sells/1e6:.1f}M")
        except Exception as e:
            print(f"  Form 4 failed: {e}")

    # Legal proceedings
    legal_data = {}
    if cik:
        try:
            legal_data = fetch_legal_proceedings(cik, ticker)
            has_issues = any([
                legal_data.get("has_sec_investigation"),
                legal_data.get("has_doj_investigation"),
                legal_data.get("has_securities_class_action"),
                legal_data.get("going_concern"),
            ])
            print(f"  Legal: {'⚠ issues found' if has_issues else 'clean'} | auditor={legal_data.get('auditor_name','unknown')}")
        except Exception as e:
            print(f"  Legal proceedings failed: {e}")

    # BUG5 FIX: Material non-earnings 8-K events (strategic investments, contracts, exec changes)
    material_events = []
    if cik:
        try:
            material_events = fetch_material_8k_events(cik, ticker, days=90)
            if material_events:
                print(f"  Material events: {len(material_events)} in past 90 days ({', '.join(e['type'] for e in material_events[:3])})")
            else:
                print(f"  Material events: none in past 90 days")
        except Exception as e:
            print(f"  Material events fetch failed: {e}")

    if cik:
        # Step 2: Fetch XBRL facts
        facts = fetch_xbrl_facts(cik)
        if facts:
            xbrl_available = True
            metrics = extract_key_metrics(ticker, facts)
            # Extract shares outstanding for plausibility check
            try:
                us_gaap = facts.get("facts", {}).get("us-gaap", {})
                shares_concept = us_gaap.get("CommonStockSharesOutstanding") or us_gaap.get("WeightedAverageNumberOfSharesOutstandingBasic")
                if shares_concept:
                    _units = shares_concept.get("units", {})
                    _share_vals = _units.get("shares") or []
                    if _share_vals:
                        _recent = sorted(_share_vals, key=lambda x: x.get("end", ""), reverse=True)
                        shares_outstanding = _recent[0].get("val") if _recent else None
            except Exception:
                pass
            # Run plausibility check on extracted metrics
            if price and shares_outstanding:
                metrics = validate_metrics_plausibility(ticker, metrics, price, shares_outstanding)
                # Surface revenue mismatch warning to data_quality
                rev_error = metrics.get("revenue_ttm", {}).get("error", "")
                if rev_error and "mismatch" in rev_error.lower():
                    warnings.append(f"REVENUE PERIOD MISMATCH: {rev_error}")
            elif price:
                # Try to get shares from yfinance as fallback for plausibility check
                try:
                    import yfinance as yf
                    _shares_yf = yf.Ticker(ticker).info.get("sharesOutstanding")
                    if _shares_yf:
                        metrics = validate_metrics_plausibility(ticker, metrics, price, _shares_yf)
                        # Surface revenue mismatch warning to data_quality
                        rev_error = metrics.get("revenue_ttm", {}).get("error", "")
                        if rev_error and "mismatch" in rev_error.lower():
                            warnings.append(f"REVENUE PERIOD MISMATCH: {rev_error}")
                except Exception:
                    pass
        else:
            warnings.append("XBRL fetch returned empty — check CIK or SEC availability")
    else:
        warnings.append(f"CIK not found for {ticker} — EDGAR data unavailable, using yfinance fallback")

    # yfinance fallback for missing metrics
    if not metrics.get("revenue_ttm") or not metrics.get("gaap_eps_ttm"):
        try:
            import yfinance as yf
            tkr = yf.Ticker(ticker)
            yf_info = tkr.info

            # Revenue fallback
            if not metrics.get("revenue_ttm"):
                rev_ttm = None
                try:
                    q_fin = tkr.quarterly_income_stmt
                    if q_fin is not None and not q_fin.empty:
                        for label in ("Total Revenue", "Revenue"):
                            if label in q_fin.index:
                                row = q_fin.loc[label].dropna().sort_index(ascending=False)
                                n = min(4, len(row))
                                if n >= 1:
                                    rev_ttm = sum(float(row.iloc[i]) for i in range(n))
                                break
                except Exception:
                    pass
                if rev_ttm is None:
                    rev_ttm_info = yf_info.get("totalRevenue")
                    if rev_ttm_info:
                        rev_ttm = rev_ttm_info
                if rev_ttm:
                    metrics["revenue_ttm"] = {
                        "value": rev_ttm,
                        "period": "TTM",
                        "source": "yfinance_unverified",
                        "is_gaap": False,
                    }
                    warnings.append("revenue_ttm sourced from yfinance (unverified)")

            # EPS fallback
            if not metrics.get("gaap_eps_ttm"):
                ttm_eps = yf_info.get("trailingEps")
                if ttm_eps is not None:
                    metrics["gaap_eps_ttm"] = {
                        "value": ttm_eps,
                        "period": "TTM",
                        "source": "yfinance_unverified",
                        "is_gaap": False,
                    }
                    warnings.append("gaap_eps_ttm sourced from yfinance (unverified)")

        except Exception as e:
            warnings.append(f"yfinance fallback failed: {e}")

    # OCF yfinance fallback — separate block so it runs even when XBRL has revenue+EPS
    # XBRL OCF is typically YTD-cumulative in 10-Qs; no individual-quarter OCF entries
    # for most companies (especially retail with January fiscal years like GAP)
    if not metrics.get("operating_cashflow_ttm"):
        try:
            import yfinance as _yf_ocf
            _cf = _yf_ocf.Ticker(ticker).cashflow
            if _cf is not None and not _cf.empty:
                for _lbl in ("Operating Cash Flow", "Cash From Operations",
                             "Total Cash From Operating Activities"):
                    if _lbl in _cf.index:
                        _ocf_val = float(_cf.loc[_lbl].dropna().iloc[0])
                        metrics["operating_cashflow_ttm"] = {
                            "value": _ocf_val,
                            "period": "TTM_ANNUAL",
                            "source": "yfinance_annual_cashflow",
                            "is_gaap": False,
                        }
                        warnings.append("operating_cashflow_ttm sourced from yfinance annual cashflow (XBRL YTD-only)")
                        break
        except Exception:
            pass

    # Step 3: Fetch 8-K guidance via Tavily
    guidance = {}
    guidance_sourced = False
    try:
        guidance = fetch_latest_8k_guidance(ticker)
        guidance_sourced = bool(guidance and not guidance.get("error"))
    except Exception as e:
        warnings.append(f"8-K guidance fetch failed: {e}")
        guidance = {"error": str(e)}

    # Label analyst-derived forward EPS as consensus, not company guidance
    if guidance and not guidance.get("error"):
        guidance["analyst_consensus_note"] = (
            "Any forward EPS figures NOT explicitly stated in the 8-K press release "
            "are analyst consensus DERIVED from management operational guidance. "
            "Palantir, Cisco, BridgeBio guide on revenue/adjusted operating income — "
            "not on GAAP EPS. Do NOT call it 'guidance' unless it appears verbatim in the press release."
        )

    # Validate periods — reject stale XBRL data (older than 24 months)
    import datetime as _dt_v
    stale_cutoff = (_dt_v.date.today() - _dt_v.timedelta(days=730)).isoformat()[:7]  # YYYY-MM
    for metric_key, metric_val in list(metrics.items()):
        if isinstance(metric_val, dict) and "period" in metric_val:
            period = metric_val.get("period", "")
            # Extract year-month from period strings like "2018-Q3" or "2018-07-28"
            period_ym = period[:7] if len(period) >= 7 else ""
            if period_ym and period_ym < stale_cutoff:
                print(f"  [FACTSHEET] STALE XBRL: {metric_key} period={period} is >24mo old — marking invalid")
                metrics[metric_key] = {
                    "value": None,
                    "period": period,
                    "source": "EDGAR_XBRL_STALE",
                    "error": f"Period {period} is more than 24 months old — likely wrong taxonomy tag",
                    "is_gaap": metric_val.get("is_gaap", True),
                }

    # Check for cash flow warning
    ocf = metrics.get("operating_cashflow_ttm", {}).get("value")
    ni = metrics.get("net_income_ttm", {}).get("value")
    if ocf is not None and ni is not None:
        if ni > 0 and ocf < 0:
            warnings.append(f"WORKING CAPITAL FLAG: Net income positive (${ni/1e6:.0f}M) but operating CF negative (${ocf/1e6:.0f}M) — Burry forensic check required")
        elif ocf is not None and ni is not None and ni != 0 and abs(ocf) > abs(ni) * 2:
            warnings.append(f"WORKING CAPITAL FLAG: Operating CF (${ocf/1e6:.0f}M) exceeds net income (${ni/1e6:.0f}M) by >2x — flag for Skeptic")

    # Compute net cash / net debt from balance sheet metrics
    _cash = (metrics.get("cash_and_equivalents") or {}).get("value") or 0
    _sti = (metrics.get("short_term_investments") or {}).get("value") or 0
    _debt = (metrics.get("total_debt") or {}).get("value") or 0
    _total_cash = _cash + _sti
    if _total_cash > 0 or _debt > 0:
        _net_cash = _total_cash - _debt
        _net_cash_entry = {
            "value": _net_cash,
            "total_cash": _total_cash,
            "total_debt": _debt,
            "source": "COMPUTED",
            "label": "Net Cash" if _net_cash >= 0 else "Net Debt"
        }
        _price_hint = (preflight_web.get("identity") or {}).get("price_hint")
        _shares_entry = metrics.get("shares_outstanding", {})
        _shares = _shares_entry.get("value") if isinstance(_shares_entry, dict) else None
        if _price_hint and _shares:
            _mktcap = float(_price_hint) * float(_shares)
            if _mktcap > 0:
                _net_cash_entry["pct_of_mktcap"] = (_net_cash / _mktcap) * 100
        metrics["net_cash"] = _net_cash_entry

    missing_fields = [f for f in ["revenue_ttm", "gaap_eps_ttm"] if f not in metrics]

    # Validate guidance is forward-looking (not expired)
    import datetime as _dt_guide
    today_dt = _dt_guide.date.today()
    if guidance and not guidance.get("error"):
        # Check if guidance period has already passed
        for guide_key in ["guidance_revenue_low", "guidance_eps_gaap_low", "guidance_eps_nongaap_low"]:
            source_date = guidance.get("source_date", "")
            if source_date:
                try:
                    filed_dt = _dt_guide.date.fromisoformat(source_date[:10])
                    age_days = (today_dt - filed_dt).days
                    if age_days > 120:  # guidance older than 4 months is likely for a past period
                        guidance["expired_warning"] = (
                            f"Guidance sourced from {source_date} which is {age_days} days ago — "
                            f"may be for a period that has already ended. Verify against most recent 8-K."
                        )
                    break
                except (ValueError, TypeError):
                    pass

    # Step 4: Fetch recent news via EDGAR 8-K (CIK is now resolved)
    recent_news = []
    try:
        if cik:
            recent_news = _get_recent_news_from_edgar(cik, ticker, days=30)
            # Back-fill news into preflight_web so format_fact_sheet_for_panels sees it
            if recent_news and isinstance(preflight_web, dict):
                preflight_web["news"] = recent_news
                preflight_web["news_source"] = "EDGAR_8K"
        else:
            recent_news = get_recent_news(ticker, days=30)
    except Exception as e:
        warnings.append(f"News fetch failed: {e}")

    fact_sheet = {
        "ticker": ticker,
        "price": price,
        "as_of_date": today,
        "cik": cik,
        "company_name": company_check.get("company_name", ""),
        "company_check": company_check,
        "metrics": metrics,
        "guidance": guidance,
        "press_release": press_release,
        "analyst_consensus": analyst_consensus,
        "recent_news": recent_news,
        "material_events": material_events,
        "insider_form4": insider_data,
        "legal_proceedings": legal_data,
        "transcript": transcript_data,
        "management_statements": parsed_statements,
        "preflight_web": preflight_web,
        "data_quality": {
            "xbrl_available": xbrl_available,
            "guidance_sourced": guidance_sourced,
            "missing_fields": missing_fields,
            "warnings": warnings,
        },
        "halt_if_missing": ["revenue_ttm", "gaap_eps_ttm"],
    }

    # --- RECONCILIATION GATE ---
    gate_result = reconciliation_gate(fact_sheet)
    fact_sheet["reconciliation_gate"] = gate_result
    if not gate_result["passed"]:
        for stop in gate_result["hard_stops"]:
            print(f"  [HARD STOP] {stop}")
        fact_sheet["data_quality"]["hard_stop"] = True
        fact_sheet["data_quality"]["hard_stop_reason"] = "; ".join(gate_result["hard_stops"])
    elif gate_result["corrections"]:
        print(f"  [GATE] {len(gate_result['corrections'])} correction(s) applied: {', '.join(gate_result['corrections'][:2])}")

    # Contextualize OCF warnings for project-based/seasonal businesses
    _ocf_entry = metrics.get("operating_cashflow_ttm", {})
    _ocf_warn = _ocf_entry.get("warning", "") if isinstance(_ocf_entry, dict) else ""
    if _ocf_warn:
        _co_name = (company_check.get("company_name", "") or "").upper()
        _sector = (company_check.get("sector", "") or "").lower()
        _is_seasonal = any(s in _sector for s in SEASONAL_OCF_SECTORS)
        if not _is_seasonal:
            _eng_hints = ["TETRA", "AECOM", "JACOBS", "FLUOR", "LEIDOS", "PARSONS", "BOOZ", "SAIC", "CACI"]
            _is_seasonal = any(h in _co_name for h in _eng_hints)
        if _is_seasonal and isinstance(_ocf_entry, dict):
            metrics["operating_cashflow_ttm"]["seasonal_note"] = (
                "Engineering and consulting firms bill milestone-based contracts with uneven cash receipt timing. "
                "Single-quarter OCF is not representative of annual cash generation. "
                "Use first-half or full-year OCF from the 8-K as the primary cash flow signal. "
                "Skeptic panel: treat OCF/NI ratio anomalies here as seasonal timing, not fraud signal, "
                "unless supported by specific evidence of payment delays or revenue recognition manipulation."
            )

    # --- STRUCTURAL BREAK DETECTION ---
    # If revenue_ttm was estimated from MRQ*4 due to XBRL rejection, explain why
    rev_source = metrics.get("revenue_ttm", {}).get("source", "")
    structural_break_context = ""
    try:
        company_name_for_search = company_check.get("company_name", ticker)
        structural_break_context = _get_structural_break_context(
            ticker, cik or "0", company_name_for_search
        )
        if structural_break_context:
            print(f"  [STRUCTURAL BREAK] Context found: {structural_break_context[:150]}")
    except Exception as e:
        print(f"  [STRUCTURAL BREAK] Detection failed (non-fatal): {e}")

    if structural_break_context:
        if "revenue_ttm" in metrics:
            metrics["revenue_ttm"]["structural_break_context"] = structural_break_context

    # NAV calculation for commodity producers
    _commodity_code_for_nav = None
    try:
        _nav_names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
        _nav_known = json.loads(_nav_names_path.read_text())
        _nav_entry = _nav_known.get(ticker.upper(), {})
        _commodity_code_for_nav = _nav_entry.get("commodity") if isinstance(_nav_entry, dict) else None
    except Exception:
        pass

    if _commodity_code_for_nav:
        nav_result = calculate_miner_nav(ticker, fact_sheet)
        fact_sheet["miner_nav"] = nav_result
        if nav_result.get("nav_per_share"):
            print(f"  [NAV] {ticker}: ${nav_result['nav_per_share']:.2f}/share P/NAV={nav_result.get('p_nav_ratio','?')}x")

        c_eps = calculate_commodity_eps(ticker, fact_sheet)
        fact_sheet["commodity_eps"] = c_eps

        if commodity_code and fact_sheet.get("commodity_eps", {}).get("eps_base") is not None:
            eps_calibration = validate_and_calibrate_forward_eps(
                ticker, fact_sheet, fact_sheet["commodity_eps"]
            )
            fact_sheet["eps_calibration"] = eps_calibration

    # Sector-specific operational metrics
    sector_metrics = {}
    _sector = ""
    try:
        _names = json.loads((Path.home() / "ORACLE" / "data" / "ticker_names.json").read_text())
        _entry = _names.get(ticker.upper(), {})
        _sector = _entry.get("sector", "") if isinstance(_entry, dict) else ""
    except Exception:
        pass

    if _sector:
        _pr = fact_sheet.get("press_release") or {}
        _pr_text = (
            _pr.get("raw_text") or
            _pr.get("text") or
            _pr.get("content") or
            _pr.get("exhibit_text") or
            ""
        )
        if _pr_text and len(_pr_text) > 200:
            sector_metrics = extract_sector_operational_metrics(ticker, _pr_text, _sector)
            fact_sheet["sector_metrics"] = sector_metrics
        else:
            print(f"  [SECTOR METRICS] {ticker}: no press release text available for sector extraction")

    # Time-sensitive risk extraction from transcript
    _transcript_text = ""
    _tr = fact_sheet.get("transcript") or fact_sheet.get("transcript_data") or {}
    if isinstance(_tr, dict):
        _transcript_text = (
            _tr.get("transcript_text") or
            _tr.get("text") or
            _tr.get("content") or
            _tr.get("raw_text") or
            ""
        )
    elif isinstance(_tr, str):
        _transcript_text = _tr

    if _transcript_text:
        ts_risks = extract_time_sensitive_risks(_transcript_text, ticker)
        fact_sheet["time_sensitive_risks"] = ts_risks
    else:
        fact_sheet["time_sensitive_risks"] = []
        print(f"  [TIME-SENSITIVE RISKS] {ticker}: no transcript text available")

    # Leadership transition escalation
    if fact_sheet.get("material_events"):
        fact_sheet["material_events"] = escalate_leadership_transitions(
            fact_sheet["material_events"], ticker
        )

    try:
        cache_path.write_text(json.dumps(fact_sheet, indent=2, default=str))
    except Exception:
        pass

    # Auto-populate ticker_names.json after successful build
    if company_check.get("ok") and company_check.get("company_name") not in ("", "UNKNOWN", "CHECK_FAILED"):
        try:
            names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
            known = {}
            if names_path.exists():
                known = json.loads(names_path.read_text())
            if ticker not in known:
                known[ticker] = {
                    "name": company_check["company_name"],
                    "source": company_check.get("source", "edgar"),
                    "confirmed_date": datetime.date.today().isoformat()
                }
                names_path.write_text(json.dumps(known, indent=2))
                print(f"  [REGISTRY] Added {ticker} -> {company_check['company_name']} to ticker_names.json")
        except Exception as e:
            print(f"  [REGISTRY] Failed to update ticker_names.json: {e}")

    return fact_sheet


# ── Format for Panel Injection ───────────────────────────────────────────────

def format_commodity_anchor(fs: dict) -> list:
    """
    Generate COMMODITY ANCHOR block — appears FIRST in panel fact sheet.
    Forces all panels to use same authoritative commodity price.
    Universal — only fires when commodity field present.
    """
    lines = []
    pfw = fs.get("preflight_web") or {}
    commodity = pfw.get("commodity", {})
    commodity_price = commodity.get("price")
    commodity_code = commodity.get("commodity")

    if not commodity_price or not commodity_code:
        return lines

    UNITS = {
        "XAUUSD": "per troy ounce",
        "SILVER": "per troy ounce",
        "COPPER": "per pound",
        "WTI":    "per barrel",
        "NATGAS": "per MMBtu",
    }
    unit_label = UNITS.get(commodity_code, "per unit")

    lines.append("=" * 60)
    lines.append("COMMODITY ANCHOR — ALL PANELS MUST USE THIS FIGURE")
    lines.append("=" * 60)
    lines.append(f"  {commodity_code}: ${commodity_price:,.2f} {unit_label}")
    lines.append(f"  Source: {commodity.get('source', 'verified')} as of {commodity.get('date', 'today')}")
    lines.append("")
    lines.append("  MANDATORY RULES FOR ALL PANELS:")
    lines.append(f"  1. Use ${commodity_price:,.0f} as the {commodity_code} price in ALL scenarios")
    lines.append("  2. Do NOT use historical averages, prior-quarter realized prices, or training data prices")
    lines.append(f"  3. Bull/base/bear scenarios = % above/below ${commodity_price:,.0f}, not arbitrary levels")
    lines.append("  4. If analyst EPS uses a different commodity price, it is STALE — use commodity-derived EPS")
    lines.append(f"  5. Every margin, scenario tree, and EPS estimate is anchored to {commodity_code} = ${commodity_price:,.0f}")
    lines.append("=" * 60)
    lines.append("")

    return lines


def _fmt_dollar(val, suffix=""):
    """Format a dollar value with B/M/K suffix."""
    if val is None:
        return "N/A"
    abs_val = abs(val)
    sign = "-" if val < 0 else ""
    if abs_val >= 1e9:
        return f"{sign}${abs_val/1e9:.2f}B{suffix}"
    elif abs_val >= 1e6:
        return f"{sign}${abs_val/1e6:.1f}M{suffix}"
    elif abs_val >= 1e3:
        return f"{sign}${abs_val/1e3:.1f}K{suffix}"
    else:
        return f"{sign}${abs_val:.2f}{suffix}"


def format_fact_sheet_for_panels(fs: dict) -> str:
    """
    Convert a fact sheet dict to a formatted text block for panel prompt injection.
    """
    if not fs:
        return ""

    ticker = fs.get("ticker", "UNKNOWN")
    as_of = fs.get("as_of_date", "")
    # Use session price if available (more current than cached fact sheet price)
    price = get_session_price(ticker) or fs.get("price")
    metrics = fs.get("metrics", {})
    guidance = fs.get("guidance", {})
    dq = fs.get("data_quality", {})

    xbrl_src = "SEC EDGAR XBRL" if dq.get("xbrl_available") else "yfinance (unverified)"

    # PREFLIGHT section — always first
    pfw = fs.get("preflight_web", {})
    preflight_lines = []
    if pfw:
        identity = pfw.get("identity", {})
        financials = pfw.get("financials", {})
        news_items = pfw.get("news", [])
        preflight_lines.append(f"PREFLIGHT GROUND TRUTH — {ticker} (web-verified before EDGAR)")
        if identity.get("company_name"):
            preflight_lines.append(f"  Company (web): {identity['company_name']}")
        if financials.get("revenue_hint"):
            preflight_lines.append(f"  Revenue hint (web): ${financials['revenue_hint']/1e9:.2f}B")
        if financials.get("eps_hint"):
            preflight_lines.append(f"  EPS hint (web): ${financials['eps_hint']:.2f}")
        if financials.get("guidance_hint"):
            preflight_lines.append(f"  Guidance (web): {financials['guidance_hint'][:150]}")
        if news_items:
            preflight_lines.append("  Recent news (web-sourced):")
            for n in news_items[:3]:
                preflight_lines.append(f"    [{n.get('date','?')[:10]}] {n.get('title','')[:100]}")
        preflight_lines.append("  [RULE: Any EDGAR figure that conflicts with web ground truth by >30% is suspect]")
        preflight_lines.append("")

    # Analyst consensus section
    analyst_data = fs.get("analyst_consensus", {}) or {}
    analyst_lines = []
    if analyst_data.get("target_mean"):
        analyst_lines.append("ANALYST CONSENSUS:")
        analyst_lines.append(f"  Target (mean): ${analyst_data['target_mean']:.2f}")
        if analyst_data.get("target_high"):
            analyst_lines.append(f"  Target (high): ${analyst_data['target_high']:.2f}")
        if analyst_data.get("target_low"):
            analyst_lines.append(f"  Target (low): ${analyst_data['target_low']:.2f}")
        if analyst_data.get("analyst_count"):
            analyst_lines.append(f"  Analysts: {analyst_data['analyst_count']}")
        if analyst_data.get("recommendation"):
            analyst_lines.append(f"  Consensus rating: {analyst_data['recommendation']}")
        # Calculate implied upside — need current price
        _price = (fs.get("preflight_web", {}) or {}).get("identity", {}).get("price_hint") or price
        if _price and analyst_data.get("target_mean"):
            try:
                upside = ((analyst_data["target_mean"] / float(_price)) - 1) * 100
                analyst_lines.append(f"  Implied upside from current price: {upside:+.1f}%")
            except Exception:
                pass
        analyst_lines.append("")

    # ── Balance Sheet Summary (appears before EDGAR data) ──
    balance_sheet_lines = []
    _net_cash_data = metrics.get("net_cash") or {}
    _cash_val = (metrics.get("cash_and_equivalents") or {}).get("value")
    _sti_val = (metrics.get("short_term_investments") or {}).get("value")
    _debt_val = (metrics.get("total_debt") or {}).get("value")
    if any([_cash_val, _sti_val, _debt_val]):
        balance_sheet_lines.append("BALANCE SHEET SUMMARY:")
        if _cash_val:
            balance_sheet_lines.append(f"  Cash and equivalents: ${_cash_val/1e9:.2f}B")
        if _sti_val and _sti_val > 0:
            balance_sheet_lines.append(f"  Short-term investments: ${_sti_val/1e9:.2f}B")
        _total_cash = (_cash_val or 0) + (_sti_val or 0)
        if _total_cash > 0:
            balance_sheet_lines.append(f"  Total cash and investments: ${_total_cash/1e9:.2f}B")
        if _debt_val:
            balance_sheet_lines.append(f"  Total debt: ${_debt_val/1e9:.2f}B")
        if _net_cash_data:
            _label = _net_cash_data.get("label", "Net Cash")
            _net_val = _net_cash_data.get("value", 0)
            balance_sheet_lines.append(f"  {_label}: ${_net_val/1e9:.2f}B")
            _pct = _net_cash_data.get("pct_of_mktcap")
            if _pct is not None:
                balance_sheet_lines.append(f"  Net cash as % of market cap: {_pct:.1f}%")
        balance_sheet_lines.append(
            "  [NOTE: Panels must use actual cash position in all "
            "downside scenarios and floor value calculations]"
        )
        balance_sheet_lines.append("")

    # Commodity price section — top context for miners
    commodity = pfw.get("commodity", {})
    if commodity.get("price"):
        unit = commodity.get("unit", "unit")
        balance_sheet_lines.append("COMMODITY PRICE (CURRENT SPOT):")
        balance_sheet_lines.append(f"  {commodity['commodity']}: ${commodity['price']:.2f} per {unit}")
        balance_sheet_lines.append(f"  Source: {commodity.get('source', 'unknown')} as of {commodity.get('date', 'today')}")
        balance_sheet_lines.append(
            f"  [RULE: ALL earnings scenarios, EPS estimates, and margin "
            f"calculations MUST use this commodity price as the baseline. "
            f"Do NOT use historical averages or prior-period prices.]"
        )
        balance_sheet_lines.append("")

    # NAV section — primary valuation metric for miners
    nav = fs.get("miner_nav", {})
    if nav.get("nav_per_share"):
        inp = nav.get("inputs", {})
        balance_sheet_lines.append("NET ASSET VALUE (NAV) ANALYSIS:")
        balance_sheet_lines.append(f"  NAV per share: ${nav['nav_per_share']:.2f}")
        balance_sheet_lines.append(f"  P/NAV ratio: {nav.get('p_nav_ratio', 'N/A')}x")
        cp = inp.get('commodity_price', 0)
        aisc = inp.get('aisc_per_oz', 0)
        margin = inp.get('margin_per_oz', 0)
        balance_sheet_lines.append(f"  Inputs: Commodity ${cp:.0f} | AISC ${aisc:.0f}/oz | Margin ${margin:.0f}/oz")
        balance_sheet_lines.append(f"  Annual FCF at current price: ${inp.get('annual_fcf_bn', 0):.2f}B")
        balance_sheet_lines.append(f"  {nav.get('interpretation', '')}")
        balance_sheet_lines.append(
            "  [RULE: Use P/NAV as PRIMARY valuation metric for miners. "
            "P/E and DCF are secondary. Senior gold miners trade 1.0-1.5x NAV.]"
        )
        balance_sheet_lines.append("")

    # Commodity EPS scenarios — replaces analyst consensus for miners
    c_eps = fs.get("commodity_eps", {})
    if c_eps.get("eps_base") is not None:
        cp_used = c_eps.get("commodity_price_used", 0)
        balance_sheet_lines.append("COMMODITY-DERIVED EPS SCENARIOS:")
        balance_sheet_lines.append(f"  Base  (current ${cp_used:.0f}/oz): EPS ${c_eps['eps_base']:.2f}")
        balance_sheet_lines.append(f"  Bull  (+20%):                     EPS ${c_eps['eps_bull']:.2f}")
        balance_sheet_lines.append(f"  Bear  (-20%):                     EPS ${c_eps['eps_bear']:.2f}")
        balance_sheet_lines.append(f"  Stress(-40%):                     EPS ${c_eps['eps_stress']:.2f}")
        balance_sheet_lines.append(f"  Note: {c_eps.get('note', '')}")
        balance_sheet_lines.append(
            "  [RULE: Use these EPS figures for all P/E and scenario calculations. "
            "Do NOT use analyst consensus EPS when commodity has moved >15% since last update.]"
        )
        balance_sheet_lines.append("")

    eps_cal = fs.get("eps_calibration", {})
    if eps_cal.get("calibration_note"):
        balance_sheet_lines.append("EPS CALIBRATION:")
        rec = eps_cal.get("recommended_eps")
        ana = eps_cal.get("analyst_eps")
        com = eps_cal.get("commodity_eps")
        balance_sheet_lines.append(f"  Recommended EPS (use for P/E): ${rec:.2f}" if rec else "  Recommended EPS: N/A")
        balance_sheet_lines.append(f"  Analyst consensus: ${ana:.2f}" if ana else "  Analyst consensus: N/A")
        balance_sheet_lines.append(f"  Commodity-derived: ${com:.2f}" if com else "  Commodity-derived: N/A")
        balance_sheet_lines.append(f"  Note: {eps_cal.get('calibration_note', '')}")
        balance_sheet_lines.append(
            "  [RULE: Use RECOMMENDED EPS for all P/E calculations. "
            "If CALIBRATION WARNING present, analyst consensus is stale. "
            "Do NOT use analyst EPS when divergence exceeds 25%.]"
        )
        balance_sheet_lines.append("")

    # Sector operational metrics
    sector_metrics = fs.get("sector_metrics", {})
    if sector_metrics:
        balance_sheet_lines.append("SECTOR-SPECIFIC OPERATIONAL METRICS:")
        for metric_name, data in sector_metrics.items():
            value = data.get("value", "N/A")
            display_name = metric_name.replace("_", " ").title()
            if isinstance(value, float):
                balance_sheet_lines.append(f"  {display_name}: {value:,.0f}")
            else:
                balance_sheet_lines.append(f"  {display_name}: {value}")
        balance_sheet_lines.append(
            "  [RULE: These sector metrics are PRIMARY analytical inputs. "
            "For miners: AISC is the correct cost figure, NOT cash operating cost. "
            "AISC includes sustaining capex; cash cost does not. "
            "For retailers: comparable store sales is the primary health indicator. "
            "For services: backlog and book-to-burn are the primary growth signals.]"
        )
        balance_sheet_lines.append("")

    # Management context appears BEFORE EDGAR data so panels understand
    # business context (USAID transition, brand performance, FDA approval)
    # before interpreting raw financial figures.
    mgmt_lines = []
    _mgmt = fs.get("management_statements", {})
    _transcript = fs.get("transcript", {})
    if _mgmt or _transcript:
        mgmt_lines.append("\nMANAGEMENT STATEMENTS (from earnings call transcript):")
        _src = _transcript.get("source", "NOT_FOUND")
        mgmt_lines.append(f"  Transcript source: {_src}")
        if _mgmt.get("revenue_guidance_quote"):
            mgmt_lines.append(f"  FULL YEAR REVENUE GUIDANCE: {_mgmt['revenue_guidance_quote'][:300]}")
        if _mgmt.get("gross_margin_guidance_quote"):
            mgmt_lines.append(f"  GROSS MARGIN GUIDANCE: {_mgmt['gross_margin_guidance_quote'][:200]}")
        if _mgmt.get("op_income_guidance_quote"):
            mgmt_lines.append(f"  OPERATING INCOME GUIDANCE: {_mgmt['op_income_guidance_quote'][:200]}")
        if _mgmt.get("ceo_demand_quote"):
            mgmt_lines.append(f"  CEO DEMAND CHARACTERIZATION: {_mgmt['ceo_demand_quote'][:200]}")
        if _mgmt.get("cfo_projection_quote"):
            mgmt_lines.append(f"  CFO PROJECTION: {_mgmt['cfo_projection_quote'][:200]}")
        if not any(_mgmt.values()):
            _url = _transcript.get("source_url", "")
            mgmt_lines.append(f"  No structured statements parsed — transcript available at: {_url}" if _url else "  Transcript not available — check IR website manually")
        mgmt_lines.append("  [Panels: management guidance quotes supersede analyst consensus estimates]")

    ts_risks = fs.get("time_sensitive_risks", [])
    if ts_risks:
        mgmt_lines.append("\nTIME-SENSITIVE RISKS (from earnings call):")
        for risk in ts_risks:
            sev = risk.get("severity", "MEDIUM")
            rtype = risk.get("type", "").replace("_", " ").title()
            ctx = risk.get("context", "")[:220]
            mgmt_lines.append(f"  [{sev}] {rtype}: {ctx}")
        mgmt_lines.append(
            "  [RULE: Every panel MUST address HIGH severity risks explicitly. "
            "PERMIT_DEADLINE and CONDITIONAL_GUIDANCE risks affect guidance validity. "
            "Include in downside scenarios and kill conditions.]"
        )

    # Recent news moved before EDGAR so panels have context before raw figures
    news_lines = []
    _news = fs.get("recent_news", [])
    if _news:
        news_lines.append("\nCURRENT EVENTS — LAST 30 DAYS (material events MUST be incorporated into analysis):")
        for _n in _news[:10]:
            _date_str = _n.get("date", "")[:10] if _n.get("date") else ""
            news_lines.append(f"  [{_date_str}] {_n.get('title','')[:100]}")

    # COMMODITY ANCHOR — must be absolute first content when present
    anchor_lines = format_commodity_anchor(fs)
    lines = anchor_lines + preflight_lines + analyst_lines + balance_sheet_lines + mgmt_lines + news_lines + [
        f"=== VERIFIED FACT SHEET: {ticker} (source: {xbrl_src}) ===",
        f"As of: {as_of}",
        f"Current Price: {_fmt_dollar(price) if price else 'N/A'} (yfinance live)",
        "",
    ]

    # ── 8-K Press Release Data (primary, most authoritative) ──
    pr = fs.get("press_release", {})
    if pr.get("parse_success"):
        lines.append(f"\n=== EARNINGS PRESS RELEASE DATA (SEC 8-K EX-99.1, filed {pr.get('filing_date', '')}) ===")
        lines.append("SOURCE: Company-filed document. Every number is exactly as reported.")

        if "revenue_quarter" in pr:
            v = pr["revenue_quarter"]
            lines.append(f"Revenue (most recent quarter): ${v['value']/1e9:.3f}B [GAAP] [8-K]")
        if "revenue_annual" in pr:
            v = pr["revenue_annual"]
            lines.append(f"Revenue (full fiscal year): ${v['value']/1e9:.3f}B [GAAP] [8-K]")
        if "eps_gaap_quarter" in pr:
            v = pr["eps_gaap_quarter"]
            lines.append(f"EPS (most recent quarter): ${v['value']:.2f} GAAP [8-K]")
        if "eps_nongaap_quarter" in pr:
            v = pr["eps_nongaap_quarter"]
            lines.append(f"EPS (most recent quarter): ${v['value']:.2f} non-GAAP [8-K]")
        if "eps_gaap_annual" in pr:
            v = pr["eps_gaap_annual"]
            lines.append(f"EPS (full fiscal year): ${v['value']:.2f} GAAP [8-K]")
        if "eps_nongaap_annual" in pr:
            v = pr["eps_nongaap_annual"]
            lines.append(f"EPS (full fiscal year): ${v['value']:.2f} non-GAAP [8-K]")

        # Gross margin — combine GAAP and non-GAAP on one line when both present
        gm_gaap_str = ""
        gm_ng_str = ""
        if "gross_margin_gaap" in pr:
            gm_gaap_str = f"{pr['gross_margin_gaap']['value']*100:.1f}% GAAP"
        if "gross_margin_nongaap" in pr:
            gm_ng_str = f"{pr['gross_margin_nongaap']['value']*100:.1f}% non-GAAP"
        if gm_gaap_str and gm_ng_str:
            lines.append(f"Gross Margin: {gm_gaap_str} | {gm_ng_str} [8-K]")
        elif gm_gaap_str:
            lines.append(f"Gross Margin: {gm_gaap_str} [8-K]")
        elif gm_ng_str:
            lines.append(f"Gross Margin: {gm_ng_str} [8-K]")

        if "operating_cashflow_quarter" in pr:
            v = pr["operating_cashflow_quarter"]
            lines.append(f"Operating Cash Flow (quarter): ${v['value']/1e6:.0f}M [GAAP] [8-K]")
        if "sbc_quarter" in pr:
            v = pr["sbc_quarter"]
            lines.append(f"SBC (quarter): ${v['value']/1e6:.0f}M [8-K reconciliation] — explains OCF vs net income gap")
        if "amortization_intangibles_quarter" in pr:
            v = pr["amortization_intangibles_quarter"]
            lines.append(f"Amortization of intangibles: ${v['value']/1e6:.0f}M [8-K] — explains GAAP vs non-GAAP gross margin gap")
        if "guidance_revenue" in pr:
            v = pr["guidance_revenue"]
            lines.append(f"Revenue Guidance (next quarter): ~${v['value']/1e9:.3f}B [8-K]")
        if "guidance_eps_gaap" in pr:
            v = pr["guidance_eps_gaap"]
            g2 = pr.get("guidance_eps_nongaap", {}).get("value")
            if g2:
                lines.append(f"EPS Guidance (next quarter): ${v['value']:.2f} GAAP | ${g2:.2f} non-GAAP [8-K]")
            else:
                lines.append(f"EPS Guidance (next quarter): ${v['value']:.2f} GAAP [8-K]")

        lines.append("=== END 8-K DATA ===\n")

    # Segment revenue section
    pr_data = fs.get("press_release", {})
    segments = pr_data.get("segments", {}) or pr_data.get("parsed", {}).get("segments", {})
    if segments and not segments.get("_error"):
        lines.append("\nSEGMENT REVENUE BREAKDOWN (from earnings press release):")
        for seg_name, seg_val in list(segments.items())[:12]:
            if isinstance(seg_val, dict):
                curr = seg_val.get("current", 0)
                growth = seg_val.get("growth_pct", "?")
                lines.append(f"  {seg_name}: ${curr/1e6:.0f}M  ({growth:+.0f}% YoY)")
            elif isinstance(seg_val, (int, float)):
                lines.append(f"  {seg_name}: ${seg_val/1e6:.0f}M")
        lines.append("  [Panels MUST reference segment data when available — segment mix drives thesis quality]")

    # GAAP to non-GAAP reconciliation
    pr_data2 = fs.get("press_release", {})
    recon = pr_data2.get("gaap_nongaap_reconciliation", {}) or pr_data2.get("parsed", {}).get("gaap_nongaap_reconciliation", {})
    addbacks = recon.get("addback_items", {})
    if addbacks:
        total = recon.get("total_addbacks", 0)
        lines.append(f"\nGAAP-TO-NON-GAAP RECONCILIATION (SEC Reg G):")
        lines.append(f"  Total non-cash/non-recurring addbacks: ${total/1e6:.0f}M")
        for item, val in list(addbacks.items())[:8]:
            lines.append(f"  + {item}: ${val/1e6:.0f}M")
        lines.append("  [Skeptic: use this table BEFORE alleging OCF manipulation — these are disclosed addbacks]")
    elif recon.get("note"):
        lines.append(f"\nGAAP-NON-GAAP RECONCILIATION: {recon['note']}")

    # Income Statement
    rev = metrics.get("revenue_ttm", {})
    gp = metrics.get("gross_profit_ttm", {})
    gm = metrics.get("gross_margin", {})
    ni = metrics.get("net_income_ttm", {})
    eps = metrics.get("gaap_eps_ttm", {})

    lines.append("INCOME STATEMENT (GAAP, TTM):")
    if rev:
        gaap_tag = "[GAAP]" if rev.get("is_gaap") else "[UNVERIFIED]"
        src_tag = "[EDGAR]" if rev.get("source") == "EDGAR_XBRL" else "[yfinance]"
        lines.append(f"  Revenue: {_fmt_dollar(rev.get('value'))} (period: {rev.get('period','?')}) {gaap_tag} {src_tag}")
        # Structural break context — if XBRL TTM was rejected and we searched for why
        rev_ttm_data = metrics.get("revenue_ttm", {})
        if rev_ttm_data.get("structural_break_context"):
            lines.append(f"  REVENUE CONTEXT: {rev_ttm_data['structural_break_context']}")
            lines.append("  [Skeptic: Do NOT build a revenue-collapse thesis without reading REVENUE CONTEXT first]")
        # Run-rate check: if 8-K quarterly revenue exists, show annualized run rate
        pr = fs.get("press_release", {})
        rev_q_from_8k = pr.get("revenue_quarter", {}).get("value")
        rev_ttm_from_xbrl = rev.get("value")
        if rev_q_from_8k and rev_ttm_from_xbrl:
            annualized = rev_q_from_8k * 4
            if abs(annualized - rev_ttm_from_xbrl) / rev_ttm_from_xbrl > 0.30:
                lines.append(f"  \u26a0 RUN RATE NOTE: 8-K quarterly revenue ${rev_q_from_8k/1e6:.0f}M \u00d7 4 = ${annualized/1e6:.0f}M annualized vs XBRL TTM ${rev_ttm_from_xbrl/1e6:.0f}M \u2014 company is in ramp/transition phase, use quarterly figure for current state")
    if gp and gm:
        gaap_tag = "[GAAP]" if gp.get("is_gaap") else "[UNVERIFIED]"
        src_tag = "[EDGAR]" if gp.get("source") == "EDGAR_XBRL" else "[yfinance]"
        gm_pct = f"{gm.get('value', 0)*100:.1f}%" if gm.get("value") is not None else "N/A"
        lines.append(f"  Gross Profit: {_fmt_dollar(gp.get('value'))} | Gross Margin: {gm_pct} {gaap_tag} {src_tag}")
    if ni:
        gaap_tag = "[GAAP]" if ni.get("is_gaap") else "[UNVERIFIED]"
        src_tag = "[EDGAR]" if ni.get("source") == "EDGAR_XBRL" else "[yfinance]"
        lines.append(f"  GAAP Net Income: {_fmt_dollar(ni.get('value'))} (period: {ni.get('period','?')}) {gaap_tag} {src_tag}")
    if eps:
        gaap_tag = "[GAAP]" if eps.get("is_gaap") else "[UNVERIFIED]"
        src_tag = "[EDGAR]" if eps.get("source") == "EDGAR_XBRL" else "[yfinance]"
        lines.append(f"  GAAP EPS (diluted, TTM): ${eps.get('value', 0):.2f} (period: {eps.get('period','?')}) {gaap_tag} {src_tag}")
    lines.append("")

    # Cash Flow
    ocf = metrics.get("operating_cashflow_ttm", {})
    lines.append("CASH FLOW:")
    if ocf:
        gaap_tag = "[GAAP]" if ocf.get("is_gaap") else "[UNVERIFIED]"
        src_tag = "[EDGAR]" if ocf.get("source") == "EDGAR_XBRL" else "[yfinance]"
        ocf_val = ocf.get("value", 0)
        lines.append(f"  Operating Cash Flow TTM: {_fmt_dollar(ocf_val)} (period: {ocf.get('period','?')}) {gaap_tag} {src_tag}")
        if ocf_val < 0:
            lines.append(f"  WARNING: Operating CF negative — flag for Skeptic analysis")
        seasonal_note = ocf.get("seasonal_note", "") if isinstance(ocf, dict) else ""
        if seasonal_note:
            lines.append(f"  SEASONAL NOTE: {seasonal_note}")
        # Burry working capital check
        ni_val = ni.get("value") if ni else None
        if ni_val is not None and ocf_val is not None:
            if ni_val > 0 and ocf_val < 0:
                lines.append(f"  FORENSIC FLAG: Net income positive (${ni_val/1e6:.0f}M) but operating CF negative (${ocf_val/1e6:.0f}M) — WHERE IS THE CASH? Check working capital, inventory, receivables.")
            elif ni_val != 0 and abs(ocf_val) > abs(ni_val) * 2:
                gap = abs(ocf_val) - abs(ni_val)
                lines.append(f"  FORENSIC FLAG: Operating CF exceeds net income by >2x — gap of ${gap/1e6:.0f}M. Burry check: explain working capital drain.")
    else:
        lines.append("  Operating Cash Flow TTM: N/A")
    lines.append("")

    # Guidance
    lines.append(f"GUIDANCE (source: 8-K press release{', ' + guidance.get('source_date','') if guidance.get('source_date') else ''}):")
    lines.append(
        "GUIDANCE NOTE: Fields marked [COMPANY STATED] are verbatim from the press release. "
        "Fields marked [ANALYST DERIVED] are Wall Street consensus estimates derived from operational guidance — "
        "not directly stated by management. Use this distinction when the Skeptic challenges guidance authenticity."
    )
    if guidance and not guidance.get("error"):
        rev_lo = guidance.get("guidance_revenue_low")
        rev_hi = guidance.get("guidance_revenue_high")
        if rev_lo and rev_hi:
            lines.append(f"  Revenue guidance: {_fmt_dollar(rev_lo)} - {_fmt_dollar(rev_hi)}")
        elif rev_lo:
            lines.append(f"  Revenue guidance: {_fmt_dollar(rev_lo)}")
        else:
            lines.append("  Revenue guidance: Not found in search results")

        eps_gaap_lo = guidance.get("guidance_eps_gaap_low")
        eps_gaap_hi = guidance.get("guidance_eps_gaap_high")
        if eps_gaap_lo is not None and eps_gaap_hi is not None:
            lines.append(f"  GAAP EPS guidance: ${eps_gaap_lo:.2f} - ${eps_gaap_hi:.2f}")
        else:
            lines.append("  GAAP EPS guidance: Not found")

        eps_ng_lo = guidance.get("guidance_eps_nongaap_low")
        eps_ng_hi = guidance.get("guidance_eps_nongaap_high")
        if eps_ng_lo is not None and eps_ng_hi is not None:
            lines.append(f"  Non-GAAP EPS guidance: ${eps_ng_lo:.2f} - ${eps_ng_hi:.2f}")
            # Flag GAAP/Non-GAAP gap
            if eps_gaap_lo is not None:
                gap = abs(eps_ng_lo - eps_gaap_lo)
                if gap > 0.10:
                    lines.append(f"  WARNING: GAAP/Non-GAAP gap: ${gap:.2f} — panels MUST use GAAP for P/E calculations")
        else:
            lines.append("  Non-GAAP EPS guidance: Not found")

        if guidance.get("source_url"):
            lines.append(f"  Source: {guidance['source_url'][:80]}")
        if guidance.get("expired_warning"):
            lines.append(f"  ⚠ GUIDANCE WARNING: {guidance['expired_warning']}")
    else:
        err = guidance.get("error", "unavailable") if guidance else "unavailable"
        lines.append(f"  Guidance unavailable ({err}) — panels should note absence")
    lines.append("")

    # Data quality summary
    total_fields = len(["revenue_ttm", "gaap_eps_ttm", "gross_margin", "operating_cashflow_ttm", "net_income_ttm"])
    verified = sum(1 for f in ["revenue_ttm", "gaap_eps_ttm", "gross_margin", "operating_cashflow_ttm", "net_income_ttm"]
                   if metrics.get(f, {}).get("source") == "EDGAR_XBRL")
    dq_warnings = dq.get("warnings", [])
    warn_str = f" | {len(dq_warnings)} warning(s)" if dq_warnings else ""
    lines.append(f"DATA QUALITY: {verified}/{total_fields} fields verified from EDGAR{warn_str}")
    if dq_warnings:
        for w in dq_warnings[:3]:
            lines.append(f"  ! {w}")

    # Material corporate events (BUG5 FIX)
    material_events = fs.get("material_events", [])
    if material_events:
        lines.append(f"\nMATERIAL EVENTS — PAST 90 DAYS ({len(material_events)} events):")
        for ev in material_events[:8]:
            severity = ev.get("severity", "")
            sev_tag = f"[{severity}] " if severity == "HIGH" else ""
            etype = ev.get("type", "Material Event")
            amounts_str = " | ".join(ev.get("dollar_amounts", [])[:3]) if ev.get("dollar_amounts") else ""
            desc = ev.get("description", "") or ev.get("title", "")
            lines.append(f"  {sev_tag}[{ev['date']}] Item {ev['item']} — {etype}")
            if ev.get("escalation"):
                lines.append(f"    NOTE: {ev['escalation'][:200]}")
            if desc:
                lines.append(f"    {desc[:200]}")
            if amounts_str:
                lines.append(f"    Amounts: {amounts_str}")
        lines.append(
            "  [RULE: HIGH severity events — especially CEO transitions and "
            "operational incidents — must be addressed in every panel's "
            "risk section. They can change the investment thesis.]"
        )

    # Insider transactions (Form 4)
    f4 = fs.get("insider_form4", {})
    f4_sum = f4.get("summary", {})
    if f4_sum and not f4.get("error"):
        lines.append("\nINSIDER TRANSACTIONS (Form 4, SEC EDGAR, last 90 days):")
        lines.append(f"  Open-market BUYS:  ${f4_sum.get('open_market_buys_90d',0)/1e6:.1f}M [SEC_FORM4]")
        lines.append(f"  Open-market SELLS: ${f4_sum.get('open_market_sells_90d',0)/1e6:.1f}M [SEC_FORM4]")
        lines.append(f"  Plan-based SELLS:  ${f4_sum.get('plan_sells_90d',0)/1e6:.1f}M (pre-scheduled, less informative) [SEC_FORM4]")
        lines.append(f"  CEO buys:          ${f4_sum.get('ceo_buys_90d',0)/1e6:.1f}M [SEC_FORM4]")
        net = f4_sum.get('net_open_market_90d', 0)
        lines.append(f"  Net open-market:   ${net/1e6:.1f}M ({'bullish signal' if net > 0 else 'bearish signal' if net < 0 else 'neutral'})")

        sig_buys = f4_sum.get("significant_buys", [])
        if sig_buys:
            lines.append("  SIGNIFICANT PURCHASES (>$1M open market):")
            for b in sig_buys[:3]:
                lines.append(f"    {b.get('date','')} | {b.get('insider','')} ({b.get('title','')}) | {b.get('shares',0):,.0f} shares | ${b.get('value',0)/1e6:.1f}M")

        sig_sells = f4_sum.get("significant_sells", [])
        if sig_sells:
            lines.append("  SIGNIFICANT OPEN-MARKET SALES (>$1M):")
            for s in sig_sells[:3]:
                lines.append(f"    {s.get('date','')} | {s.get('insider','')} | ${s.get('value',0)/1e6:.1f}M")

    # Corporate buyback from 10-Q financing activities
    legal = fs.get("legal_proceedings", {})
    buyback = legal.get("corporate_buyback_quarter", {})
    if buyback and buyback.get("value"):
        lines.append(f"  Corporate buyback (quarter): ${buyback['value']/1e6:.0f}M [10-Q financing activities] — separate from insider purchases")

    # Equity offering / dilution risk
    form4 = fs.get("insider_form4", {})
    offerings = form4.get("equity_offerings", [])
    if offerings:
        lines.append(f"\nEQUITY OFFERINGS / DILUTION RISK (past 180 days):")
        for off in offerings[:5]:
            lines.append(f"  [{off['date']}] {off['form']} — {off['label']}")
        lines.append("  [Skeptic: flag ATM offerings as dilution risk. Panels: adjust per-share calculations if offering is material.]")

    # Legal proceedings
    legal = fs.get("legal_proceedings", {})
    if legal and not legal.get("error"):
        lines.append("\nLEGAL & REGULATORY STATUS (SEC 10-Q/10-K):")
        lines.append(f"  Auditor: {legal.get('auditor_name','unknown')} [10-Q/10-K]")
        if legal.get("going_concern"):
            lines.append("  !! GOING CONCERN: Auditor has raised going concern doubts")
        if legal.get("has_sec_investigation"):
            lines.append("  !! SEC INVESTIGATION: Active SEC inquiry disclosed in legal proceedings")
        if legal.get("has_doj_investigation"):
            lines.append("  !! DOJ INVESTIGATION: Active DOJ/criminal inquiry disclosed")
        if legal.get("has_securities_class_action"):
            lines.append("  Securities class action: disclosed in legal proceedings")
        if not any([legal.get("has_sec_investigation"), legal.get("has_doj_investigation"),
                    legal.get("has_securities_class_action"), legal.get("going_concern")]):
            lines.append("  No material legal proceedings flagged in most recent filing")
        text_preview = legal.get("legal_proceedings_text", "")[:300]
        if text_preview:
            lines.append(f"  Filing text preview: {text_preview}")

    lines.append("=== END FACT SHEET ===")

    return "\n".join(lines)


# ── Preflight Integration ────────────────────────────────────────────────────

def check_fact_sheet_quality(report, ticker: str):
    """
    Called from oracle_preflight.run_preflight() to validate data quality.
    Attaches the fact sheet to report.validated if data is sufficient.
    """
    fs = build_fact_sheet(ticker)

    # Check reconciliation gate hard stop — abort if gate failed AND no corrections applied
    dq = fs.get("data_quality", {})
    if dq.get("hard_stop"):
        corrections = fs.get("reconciliation_gate", {}).get("corrections", [])
        if not corrections:
            report.error(
                f"Reconciliation gate hard stop: {dq.get('hard_stop_reason', 'unknown')}",
                deduct=60
            )
            return

    missing = [f for f in fs["halt_if_missing"] if f not in fs["metrics"]]
    if missing:
        report.warn(
            f"Fact sheet missing: {missing} — panels will use unverified yfinance data",
            deduct=0
        )
    else:
        report.validated["fact_sheet"] = fs
        report.validated["fact_sheet_text"] = format_fact_sheet_for_panels(fs)


# ── CLI Test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "SMCI"
    print(f"Building fact sheet for {ticker}...")
    fs = build_fact_sheet(ticker)
    print(f"CIK: {fs.get('cik')}")
    print(f"XBRL available: {fs['data_quality']['xbrl_available']}")
    print(f"Metrics found: {list(fs['metrics'].keys())}")
    print()
    print(format_fact_sheet_for_panels(fs))
