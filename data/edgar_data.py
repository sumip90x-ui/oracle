"""
edgar_data.py — SEC EDGAR data layer for MiroShark oracle-think-tank pipeline.

Replaces all LLM-guessed financial figures with verified, date-stamped facts
pulled directly from SEC EDGAR APIs. Zero LLM estimation.

Usage:
    from data.edgar_data import get_edgar_block
    block = get_edgar_block("AAPL")   # returns formatted string ready for prompt injection

CLI test:
    python3 ~/ORACLE/data/edgar_data.py LITE
    python3 ~/ORACLE/data/edgar_data.py AEM
"""

import os
import sys
import json
import time
import datetime
import requests
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

EDGAR_HEADERS = {
    "User-Agent": "MiroShark/1.0 contact@miroshark.com",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}

EDGAR_SLEEP = 0.12           # seconds between API calls (SEC rate-limit courtesy)
CACHE_DIR   = Path.home() / "ORACLE" / "cache"
CACHE_TTL   = 86400          # 24 hours in seconds
STALE_DAYS  = 548            # 18 months ≈ 548 days

# XBRL tags to pull — US GAAP (10-K/10-Q filers)
REVENUE_TAGS_USGAAP = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "RevenuesNetOfInterestExpense",
]

XBRL_TAGS_USGAAP = {
    "revenue":      REVENUE_TAGS_USGAAP,
    "net_income":   ["NetIncomeLoss"],
    "op_income":    ["OperatingIncomeLoss"],
    "eps_basic":    ["EarningsPerShareBasic"],
    "eps_diluted":  ["EarningsPerShareDiluted"],
    "shares_out":   ["CommonStockSharesOutstanding"],
    "cash":         ["CashAndCashEquivalentsAtCarryingValue"],
    "lt_debt":      ["LongTermDebt", "LongTermDebtNoncurrent"],
    "equity":       ["StockholdersEquity", "StockholdersEquityAttributableToParent"],
    "rnd":          ["ResearchAndDevelopmentExpense"],
    "gross_profit": ["GrossProfit"],
}

# XBRL tags for foreign private issuers using IFRS (40-F / 20-F / 6-K filers)
XBRL_TAGS_IFRS = {
    "revenue":      ["Revenue", "RevenueAndOtherIncome"],
    "net_income":   ["ProfitLoss", "ProfitLossAttributableToOwnersOfParent"],
    "op_income":    ["ProfitLossFromOperatingActivities", "OperatingProfitLoss"],
    "eps_basic":    ["BasicEarningsLossPerShare"],
    "eps_diluted":  ["DilutedEarningsLossPerShare"],
    "shares_out":   ["OrdinarySharesNumber", "NumberOfSharesOutstanding"],
    "cash":         ["CashAndCashEquivalents", "CashAndCashEquivalentsIfrsFullMember"],
    "lt_debt":      ["BorrowingsMaturity", "LongtermBorrowings", "NoncurrentPortionOfLongtermBorrowings"],
    "equity":       ["Equity", "EquityAttributableToOwnersOfParent"],
    "rnd":          ["ResearchAndDevelopmentExpense"],
    "gross_profit": ["GrossProfit"],
}

# Annual form types: domestic vs foreign private issuers
ANNUAL_FORMS   = {"10-K", "40-F", "20-F"}
QUARTERLY_FORMS = {"10-Q", "6-K"}


# ── CIK lookup ─────────────────────────────────────────────────────────────────

_TICKER_MAP: dict = {}
_TICKER_MAP_DATE: str = ""


def _load_ticker_map() -> dict:
    """Download or return cached full ticker->CIK map from EDGAR."""
    global _TICKER_MAP, _TICKER_MAP_DATE
    today = datetime.date.today().isoformat()
    if _TICKER_MAP and _TICKER_MAP_DATE == today:
        return _TICKER_MAP

    # Check disk cache
    cache_path = CACHE_DIR / f"edgar_ticker_map_{today}.json"
    if cache_path.exists():
        try:
            _TICKER_MAP = json.loads(cache_path.read_text())
            _TICKER_MAP_DATE = today
            return _TICKER_MAP
        except Exception:
            pass

    url = "https://www.sec.gov/files/company_tickers.json"
    resp = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    time.sleep(EDGAR_SLEEP)

    # raw is {0: {cik_str, ticker, title}, 1: ...}
    result = {}
    for entry in raw.values():
        ticker_sym = entry.get("ticker", "").upper()
        cik = str(entry.get("cik_str", "")).zfill(10)
        if ticker_sym:
            result[ticker_sym] = cik

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result))
    _TICKER_MAP = result
    _TICKER_MAP_DATE = today
    return result


def get_cik(ticker: str) -> str:
    """Return 10-digit zero-padded CIK for a ticker, or raise ValueError."""
    ticker = ticker.upper().strip()
    tmap = _load_ticker_map()
    cik = tmap.get(ticker)
    if not cik:
        raise ValueError(f"CIK not found for ticker '{ticker}' in EDGAR company_tickers.json")
    return cik.zfill(10)


# ── Submissions metadata ───────────────────────────────────────────────────────

def _get_submissions(cik: str) -> dict:
    """GET https://data.sec.gov/submissions/{CIK}.json"""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
    resp.raise_for_status()
    time.sleep(EDGAR_SLEEP)
    return resp.json()


def _parse_recent_filings(submissions: dict) -> dict:
    """
    Parse the filings.recent block to find latest annual, quarterly, and last-3 8-K/6-K events.
    Handles both domestic filers (10-K/10-Q) and foreign private issuers (40-F/20-F/6-K).
    Returns dict with keys: annual, quarterly, events (list of 3), annual_form, quarterly_form.
    """
    recent = submissions.get("filings", {}).get("recent", {})
    forms   = recent.get("form", [])
    dates   = recent.get("filingDate", [])
    accnos  = recent.get("accessionNumber", [])
    reports = recent.get("reportDate", [])

    # Build rows sorted by filingDate descending
    rows = []
    for i, form in enumerate(forms):
        fd  = dates[i]  if i < len(dates)   else ""
        an  = accnos[i] if i < len(accnos)  else ""
        rd  = reports[i] if i < len(reports) else ""
        rows.append({"form": form, "filingDate": fd, "accessionNumber": an, "reportDate": rd})

    rows.sort(key=lambda r: r["filingDate"], reverse=True)

    # Find latest annual filing (10-K preferred, then 40-F, then 20-F)
    annual = None
    annual_form = "10-K"
    for preferred in ("10-K", "40-F", "20-F"):
        hit = next((r for r in rows if r["form"] == preferred), None)
        if hit:
            annual = hit
            annual_form = preferred
            break

    # Find latest quarterly filing (10-Q preferred, then 6-K)
    quarterly = None
    quarterly_form = "10-Q"
    for preferred in ("10-Q", "6-K"):
        hit = next((r for r in rows if r["form"] == preferred), None)
        if hit:
            quarterly = hit
            quarterly_form = preferred
            break

    # Material events: 8-K for domestic, 6-K for foreign (but not as quarterly)
    event_form = "8-K" if annual_form == "10-K" else "6-K"
    events = [r for r in rows if r["form"] == event_form][:3]

    return {
        "annual": annual,
        "quarterly": quarterly,
        "events": events,
        "annual_form": annual_form,
        "quarterly_form": quarterly_form,
    }


# ── XBRL facts ─────────────────────────────────────────────────────────────────

def _get_company_facts(cik: str) -> dict:
    """GET https://data.sec.gov/api/xbrl/companyfacts/{CIK}.json"""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    resp = requests.get(url, headers=EDGAR_HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(EDGAR_SLEEP)
    return resp.json()


def _pick_best_fact(units_list: list, form_types: set) -> dict | None:
    """
    From a list of XBRL unit entries, find the most recent entry for any of the given
    form_types (e.g. {"10-K"} or {"40-F", "20-F"}).

    Each entry looks like:
        {
          "end": "2024-09-28",
          "val": 391035000000,
          "accn": "0000320193-24-000123",
          "fy": 2024,
          "fp": "FY",
          "form": "10-K",
          "filed": "2024-11-01",
          "frame": "CY2024"
        }

    Rules:
    - Must have a 'filed' date
    - Must match one of form_types exactly
    - Pick the entry with the most recent 'filed' date
    - If tie on filed date, pick the most recent 'end' date
    - For annual forms: prefer fp == "FY" entries; if none, accept any
    """
    candidates = [
        e for e in units_list
        if e.get("form") in form_types and e.get("filed")
    ]
    if not candidates:
        return None

    # For annual forms, prefer FY entries
    is_annual = bool(form_types & ANNUAL_FORMS)
    if is_annual:
        fy_only = [e for e in candidates if e.get("fp") == "FY"]
        if fy_only:
            candidates = fy_only

    # Sort: most recent filed date first, then most recent end date
    candidates.sort(
        key=lambda e: (e.get("filed", ""), e.get("end", "")),
        reverse=True
    )
    return candidates[0]


def _extract_xbrl_fact(facts_namespace: dict, tag_list: list, form_types: set) -> dict | None:
    """
    Try each tag in tag_list; return the entry with the most recent filing date.
    Returns a normalized dict: {value, end, filed, form, accn, tag}

    Note: We try ALL tags and pick the one with the most recent 'filed' date,
    not just the first tag that has data. This handles companies that switched
    XBRL tags across reporting periods (e.g. LITE switched from 'Revenues' to
    'RevenueFromContractWithCustomerExcludingAssessedTax').
    """
    best_result = None
    best_filed  = ""

    for tag in tag_list:
        tag_data = facts_namespace.get(tag)
        if not tag_data:
            continue
        units = tag_data.get("units", {})
        # Try USD first, then shares, then pure-number, then USD/shares
        for unit_key in ("USD", "shares", "pure", "USD/shares"):
            unit_list = units.get(unit_key, [])
            if not unit_list:
                continue
            best = _pick_best_fact(unit_list, form_types)
            if best and best.get("filed", "") > best_filed:
                best_filed = best.get("filed", "")
                best_result = {
                    "value":  best.get("val"),
                    "end":    best.get("end", ""),
                    "filed":  best.get("filed", ""),
                    "form":   best.get("form", ""),
                    "accn":   best.get("accn", ""),
                    "tag":    tag,
                    "unit":   unit_key,
                }
            break  # only try first available unit type per tag

    return best_result


# ── Date verification helpers ──────────────────────────────────────────────────

def _days_since(date_str: str) -> int:
    """Return integer number of days since date_str (YYYY-MM-DD)."""
    if not date_str:
        return 9999
    try:
        d = datetime.date.fromisoformat(date_str)
        return (datetime.date.today() - d).days
    except ValueError:
        return 9999


def _staleness_flag(filed_str: str) -> str:
    """Return a staleness annotation string if the filing is old, else ''."""
    days = _days_since(filed_str)
    if days > STALE_DAYS:
        return f" [STALE — filed {filed_str}, may not reflect current financials]"
    return ""


def _next_filing_flag(last_filed: str, expected_gap_days: int) -> str:
    """Flag if we're past the expected next filing date."""
    if not last_filed:
        return ""
    try:
        d = datetime.date.fromisoformat(last_filed)
        expected_next = d + datetime.timedelta(days=expected_gap_days)
        if datetime.date.today() > expected_next:
            return " [NEW FILING EXPECTED — verify on EDGAR]"
    except ValueError:
        pass
    return ""


# ── Format helpers ─────────────────────────────────────────────────────────────

def _fmt_millions(val) -> str:
    """Format a raw dollar value into millions with 1 decimal."""
    if val is None:
        return "N/A"
    try:
        v = float(val)
        return f"{v / 1_000_000:.1f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_shares_millions(val) -> str:
    """Format share count into millions."""
    if val is None:
        return "N/A"
    try:
        v = float(val)
        # EDGAR sometimes stores in actual shares, sometimes already in thousands
        # If value > 1e9, assume it's in actual shares
        if abs(v) > 1e9:
            return f"{v / 1_000_000:.1f}"
        elif abs(v) > 1e6:
            return f"{v / 1_000:.1f}"   # stored in thousands
        else:
            return f"{v:.1f}"           # stored in millions
    except (TypeError, ValueError):
        return "N/A"


def _fmt_eps(val) -> str:
    """Format EPS value."""
    if val is None:
        return "N/A"
    try:
        return f"{float(val):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _fact_citation(fact: dict | None, label: str, formatter, form_type: str) -> str:
    """Build a single line for the KEY FINANCIALS block."""
    if fact is None:
        return f"{label}: N/A [no EDGAR data found]"

    filed = fact.get("filed", "")
    end   = fact.get("end", "")
    form  = fact.get("form", form_type)
    val   = fact.get("value")

    stale = _staleness_flag(filed)
    formatted_val = formatter(val)

    if label.startswith("EPS") or label.startswith("Shares"):
        return f"{label}: {formatted_val} [{form} filed {filed}, period {end}]{stale}"
    return f"{label}: ${formatted_val}M [{form} filed {filed}, period {end}]{stale}"


# ── Main entry point ───────────────────────────────────────────────────────────

def get_edgar_block(ticker: str) -> str:
    """
    Fetch verified EDGAR financial data for `ticker` and return a formatted
    string block ready for injection into a Claude seed prompt.

    Caches the result for 24 hours in ~/ORACLE/cache/edgar_{ticker}_{YYYYMMDD}.json.
    On any failure, returns a clearly labeled error string.
    """
    ticker = ticker.upper().strip()
    today_str = datetime.date.today().strftime("%Y%m%d")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"edgar_{ticker}_{today_str}.json"

    # ── Cache hit check ──
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            cached_at = cached.get("_cached_at", "")
            if cached_at == today_str and cached.get("block"):
                return cached["block"]
        except Exception:
            pass

    # ── Fetch CIK ──
    try:
        cik = get_cik(ticker)
    except Exception as e:
        return (
            f"EDGAR DATA UNAVAILABLE for {ticker} — Claude will use training knowledge. "
            f"All figures must be labeled [UNVERIFIED — source unknown]. "
            f"(CIK lookup failed: {e})"
        )

    # ── Fetch submissions metadata ──
    try:
        submissions = _get_submissions(cik)
    except Exception as e:
        return (
            f"EDGAR DATA UNAVAILABLE for {ticker} — Claude will use training knowledge. "
            f"All figures must be labeled [UNVERIFIED — source unknown]. "
            f"(Submissions fetch failed for CIK {cik}: {e})"
        )

    # ── Fetch XBRL company facts ──
    try:
        facts_raw = _get_company_facts(cik)
    except Exception as e:
        return (
            f"EDGAR DATA UNAVAILABLE for {ticker} — Claude will use training knowledge. "
            f"All figures must be labeled [UNVERIFIED — source unknown]. "
            f"(XBRL facts fetch failed for CIK {cik}: {e})"
        )

    # ── Parse submissions for filing metadata ──
    try:
        filings = _parse_recent_filings(submissions)
    except Exception as e:
        filings = {"annual": None, "quarterly": None, "events": [], "annual_form": "10-K", "quarterly_form": "10-Q"}

    annual         = filings.get("annual")
    quarterly      = filings.get("quarterly")
    events         = filings.get("events", [])
    annual_form    = filings.get("annual_form", "10-K")
    quarterly_form = filings.get("quarterly_form", "10-Q")
    company_name   = submissions.get("name", ticker)

    # ── Detect filing regime: US GAAP vs IFRS ──
    all_facts = facts_raw.get("facts", {})
    facts_ifrs    = all_facts.get("ifrs-full", {})
    facts_usgaap  = all_facts.get("us-gaap", {})

    # Use IFRS namespace if filer uses 40-F/20-F and has IFRS facts
    is_ifrs = annual_form in ("40-F", "20-F") and bool(facts_ifrs)
    if is_ifrs:
        facts_ns   = facts_ifrs
        xbrl_tags  = XBRL_TAGS_IFRS
        # Also check us-gaap for IFRS filers that crossfile some tags
        facts_ns_secondary = facts_usgaap
    else:
        facts_ns   = facts_usgaap
        xbrl_tags  = XBRL_TAGS_USGAAP
        facts_ns_secondary = {}

    # Determine form type sets for querying
    annual_forms_set    = {annual_form}
    quarterly_forms_set = {quarterly_form}

    # ── Extract XBRL facts ──
    xbrl = {}
    for key, tags in xbrl_tags.items():
        annual_fact = _extract_xbrl_fact(facts_ns, tags, annual_forms_set)
        if annual_fact is None and facts_ns_secondary:
            # Try secondary namespace (e.g. us-gaap tags that IFRS filers also report)
            usgaap_tags = XBRL_TAGS_USGAAP.get(key, [])
            annual_fact = _extract_xbrl_fact(facts_ns_secondary, usgaap_tags, annual_forms_set)
        xbrl[f"{key}_annual"] = annual_fact

        quarterly_fact = _extract_xbrl_fact(facts_ns, tags, quarterly_forms_set)
        if quarterly_fact is None and facts_ns_secondary:
            usgaap_tags = XBRL_TAGS_USGAAP.get(key, [])
            quarterly_fact = _extract_xbrl_fact(facts_ns_secondary, usgaap_tags, quarterly_forms_set)
        xbrl[f"{key}_quarterly"] = quarterly_fact

    # ── Cross-verify: warn if XBRL filed date doesn't match submissions metadata ──
    def _verify_cross(xbrl_fact: dict | None, metadata_filing: dict | None, form: str) -> str:
        """Return a cross-verification note if dates diverge."""
        if xbrl_fact is None or metadata_filing is None:
            return ""
        xbrl_filed = xbrl_fact.get("filed", "")
        meta_filed = metadata_filing.get("filingDate", "")
        if xbrl_filed and meta_filed and xbrl_filed != meta_filed:
            return f" [NOTE: XBRL filed {xbrl_filed} vs submissions metadata filed {meta_filed}]"
        return ""

    # ── Build formatted output block ──
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = []
    lines.append(f"## VERIFIED EDGAR FINANCIAL DATA — {ticker}")
    lines.append(f"## Company: {company_name} | CIK: {cik}")
    lines.append(f"## Filing regime: {'IFRS (foreign private issuer)' if is_ifrs else 'US GAAP (domestic filer)'}")
    lines.append(f"## Data pulled: {now_str}")
    lines.append(f"## All figures sourced directly from SEC EDGAR. Zero LLM estimation.")
    lines.append("")

    # Annual filing block
    lines.append("### LATEST ANNUAL FILING")
    if annual:
        ann_stale = _staleness_flag(annual.get("filingDate", ""))
        ann_next  = _next_filing_flag(annual.get("filingDate", ""), 365)
        lines.append(f"Form: {annual_form} | Filed: {annual['filingDate']} | Period ending: {annual['reportDate']}{ann_stale}{ann_next}")
        lines.append(f"Accession: {annual['accessionNumber']}")
        lines.append(f"EDGAR link: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={annual_form}&count=1")
    else:
        lines.append("No annual filing (10-K/40-F/20-F) found in recent filings.")
    lines.append("")

    # Quarterly filing block
    lines.append("### LATEST QUARTERLY FILING")
    if quarterly:
        q_stale = _staleness_flag(quarterly.get("filingDate", ""))
        q_next  = _next_filing_flag(quarterly.get("filingDate", ""), 90)
        lines.append(f"Form: {quarterly_form} | Filed: {quarterly['filingDate']} | Period ending: {quarterly['reportDate']}{q_stale}{q_next}")
        lines.append(f"Accession: {quarterly['accessionNumber']}")
    else:
        lines.append("No quarterly filing (10-Q/6-K) found in recent filings.")
    lines.append("")

    # Key financials block
    lines.append("### KEY FINANCIALS (source: EDGAR XBRL — each figure carries its filing citation)")
    lines.append("")

    # Revenue
    rev_a = xbrl.get("revenue_annual")
    rev_q = xbrl.get("revenue_quarterly")
    rev_tag = rev_a.get("tag", "Revenues") if rev_a else "Revenues"
    lines.append(_fact_citation(rev_a, f"Revenue (annual, {rev_tag})", _fmt_millions, "10-K"))
    lines.append(_fact_citation(rev_q, f"Revenue (latest quarter, {rev_tag})", _fmt_millions, "10-Q"))

    # Net income
    lines.append(_fact_citation(xbrl.get("net_income_annual"),    "Net Income (annual)",         _fmt_millions, "10-K"))

    # Operating income
    lines.append(_fact_citation(xbrl.get("op_income_annual"),     "Operating Income (annual)",   _fmt_millions, "10-K"))

    # EPS
    eps_d = xbrl.get("eps_diluted_annual")
    lines.append(_fact_citation(eps_d, "EPS Diluted (annual)", _fmt_eps, "10-K"))

    # Gross profit
    lines.append(_fact_citation(xbrl.get("gross_profit_annual"),  "Gross Profit (annual)",       _fmt_millions, "10-K"))

    # R&D
    rnd = xbrl.get("rnd_annual")
    if rnd is not None:
        lines.append(_fact_citation(rnd, "R&D Expense (annual)", _fmt_millions, "10-K"))
    else:
        lines.append("R&D Expense (annual): N/A [not reported or not applicable]")

    # Balance sheet items — prefer most recent quarterly, fallback to annual
    cash_q  = xbrl.get("cash_quarterly")  or xbrl.get("cash_annual")
    debt_q  = xbrl.get("lt_debt_quarterly") or xbrl.get("lt_debt_annual")
    eq_q    = xbrl.get("equity_quarterly") or xbrl.get("equity_annual")
    shares_q = xbrl.get("shares_out_quarterly") or xbrl.get("shares_out_annual")

    # Last-resort shares fallback: DEI EntityCommonStockSharesOutstanding
    # This tag is in the 'dei' namespace and always has the most recent share count
    if shares_q is None or _days_since(shares_q.get("filed", "")) > STALE_DAYS:
        dei_facts = all_facts.get("dei", {})
        dei_shares_tag = dei_facts.get("EntityCommonStockSharesOutstanding", {})
        dei_shares_list = dei_shares_tag.get("units", {}).get("shares", [])
        if dei_shares_list:
            dei_shares_list_sorted = sorted(dei_shares_list, key=lambda x: x.get("filed", ""), reverse=True)
            best_dei = dei_shares_list_sorted[0]
            if best_dei.get("filed", "") > (shares_q or {}).get("filed", ""):
                shares_q = {
                    "value": best_dei.get("val"),
                    "end":   best_dei.get("end", ""),
                    "filed": best_dei.get("filed", ""),
                    "form":  best_dei.get("form", ""),
                    "accn":  best_dei.get("accn", ""),
                    "tag":   "EntityCommonStockSharesOutstanding (DEI)",
                    "unit":  "shares",
                }

    lines.append(_fact_citation(cash_q,   "Cash & Equivalents",    _fmt_millions,        "10-Q"))
    lines.append(_fact_citation(debt_q,   "Long-Term Debt",         _fmt_millions,        "10-Q"))
    lines.append(_fact_citation(eq_q,     "Stockholders Equity",    _fmt_millions,        "10-Q"))
    lines.append(_fact_citation(shares_q, "Shares Outstanding",     _fmt_shares_millions, "10-Q"))
    lines.append("")

    # 8-K events block
    event_form_label = annual_form  # "8-K" for domestic, "6-K" for foreign
    # Use actual form from events list if available
    if events:
        event_form_label = events[0].get("form", "8-K/6-K")
    lines.append(f"### RECENT {event_form_label} FILINGS (material events)")
    if events:
        for ev in events:
            lines.append(f"- {ev['form']} filed {ev['filingDate']} | Accession: {ev['accessionNumber']}")
    else:
        lines.append("No recent 8-K/6-K filings found.")
    lines.append("")

    # Date verification block
    lines.append("### DATE VERIFICATION")
    if annual:
        ann_age = _days_since(annual.get("filingDate", ""))
        ann_fresh = "STALE (>18 months)" if ann_age > STALE_DAYS else "CURRENT"
        lines.append(f"Latest 10-K age: {ann_age} days since filing ({ann_fresh})")
    else:
        lines.append("Latest 10-K age: N/A — no 10-K found")

    if quarterly:
        q_age = _days_since(quarterly.get("filingDate", ""))
        q_fresh = "STALE (>18 months)" if q_age > STALE_DAYS else "CURRENT"
        lines.append(f"Latest 10-Q age: {q_age} days since filing ({q_fresh})")
    else:
        lines.append("Latest 10-Q age: N/A — no 10-Q found")

    # Overall freshness
    worst_age = max(
        _days_since(annual.get("filingDate", "")) if annual else 0,
        _days_since(quarterly.get("filingDate", "")) if quarterly else 0,
    )
    overall = "STALE — data may be outdated (>18 months)" if worst_age > STALE_DAYS else "CURRENT"
    lines.append(f"Data freshness: {overall}")
    lines.append("")

    block = "\n".join(lines)

    # ── Write cache ──
    try:
        cache_path.write_text(json.dumps({"_cached_at": today_str, "block": block}, ensure_ascii=False))
    except Exception:
        pass  # cache write failure is non-fatal

    return block


# ── CLI entry point for testing ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["AAPL"]

    for ticker in tickers:
        print(f"\n{'='*70}")
        print(f"  EDGAR DATA PULL: {ticker}")
        print(f"{'='*70}\n")
        result = get_edgar_block(ticker)
        print(result)
        print()
