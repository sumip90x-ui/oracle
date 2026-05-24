#!/usr/bin/env python3
"""
ORACLE EDGAR XBRL Financial Extractor
Fetches structured financial data from SEC EDGAR XBRL API.
No regex, no parsing — pure structured JSON facts.
Works for any stock with a CIK in the ORACLE manifest.
"""

import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import date, timedelta

# ── Config ────────────────────────────────────────────────────────────────────

FILINGS_ROOT = Path.home() / "ORACLE" / "filings"
CACHE_DIR = Path.home() / "ORACLE" / "cache"
EDGAR_USER_AGENT = "ORACLE MiroShark oracle_fetch admin@localhost.com"
XBRL_BASE = "https://data.sec.gov/api/xbrl/companyfacts/{cik}.json"
SLEEP_BETWEEN_REQUESTS = 0.12

METRIC_TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense", "InterestAndNoninterestIncome", "NetRevenues", "TotalRevenues", "NoninterestIncome", "BrokerageCommissionsRevenue", "InvestmentBankingRevenue", "Revenue", "RevenueFromContractsWithCustomers"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "rd_expense": ["ResearchAndDevelopmentExpense"],
    "sga_expense": ["SellingGeneralAndAdministrativeExpense", "SellingAndMarketingExpense"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsAndShortTermInvestments"],
    "operating_cf": ["NetCashProvidedByUsedInOperatingActivities"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity", "StockholdersEquityAttributableToParent"],
    "ltd": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding", "CommonStockSharesOutstanding"],
}

ANNUAL_FORMS = {"10-K", "20-F", "40-F"}
QUARTERLY_FORMS = {"10-Q", "6-K", "10-Q/A"}
QUARTERLY_FPS = {"Q1", "Q2", "Q3", "Q4"}

# Tags whose values are already per-share dollar amounts — never divide by 1e6
PER_SHARE_TAGS = {"EarningsPerShareDiluted", "EarningsPerShareBasic"}

# ── 1. Manifest loader ────────────────────────────────────────────────────────

def load_manifest(ticker: str) -> dict:
    """Load ~/ORACLE/filings/{ticker}/manifest.json. Return {} if not found."""
    path = FILINGS_ROOT / ticker.upper() / "manifest.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_cik_from_manifest(ticker: str) -> str | None:
    """Return the CIK string (already padded) from the manifest, or None."""
    manifest = load_manifest(ticker)
    cik = manifest.get("cik")
    if cik:
        return str(cik).strip()

    # Fallback: look up from cached company_tickers.json
    import glob as _glob
    cache_dir = Path.home() / "ORACLE" / "cache"
    ticker_files = sorted(_glob.glob(str(cache_dir / "company_tickers_*.json")), reverse=True)
    if ticker_files:
        try:
            with open(ticker_files[0]) as f:
                tickers_map = json.load(f)
            ticker_upper = ticker.upper()
            for entry in tickers_map.values():
                if entry.get("ticker", "").upper() == ticker_upper:
                    cik_num = int(entry["cik_str"])
                    return f"{cik_num:010d}"
        except Exception:
            pass

    # Hardcoded CIK for tickers that have changed (cache has new ticker, not old)
    TICKER_CIK_OVERRIDES = {
        "SQ": "0001512673",   # Block Inc (formerly Square) — now trades as XYZ
    }
    if ticker.upper() in TICKER_CIK_OVERRIDES:
        return TICKER_CIK_OVERRIDES[ticker.upper()]

    # Final fallback: fetch from EDGAR directly
    try:
        import requests as _req
        import time as _time
        HEADERS = {"User-Agent": "ORACLE MiroShark oracle_fetch admin@localhost.com"}
        url = "https://www.sec.gov/files/company_tickers.json"
        resp = _req.get(url, headers=HEADERS, timeout=15)
        _time.sleep(0.12)
        if resp.status_code == 200:
            tickers_map = resp.json()
            # Cache it
            from datetime import date
            cache_path = cache_dir / f"company_tickers_{date.today().strftime('%Y%m%d')}.json"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(resp.text)
            ticker_upper = ticker.upper()
            for entry in tickers_map.values():
                if entry.get("ticker", "").upper() == ticker_upper:
                    cik_num = int(entry["cik_str"])
                    return f"{cik_num:010d}"
    except Exception:
        pass

    return None


# ── 2. Fetch XBRL facts ───────────────────────────────────────────────────────

def _build_cik_url_id(cik: str) -> str:
    """Format CIK as CIK0001234567 for use in EDGAR URL."""
    # Strip leading zeros, then zero-pad to 10 digits, then prefix CIK
    cik_num = cik.lstrip("0") or "0"
    return f"CIK{int(cik_num):010d}"


def fetch_xbrl_facts(cik: str, ticker: str) -> dict:
    """
    Fetch XBRL company facts from SEC EDGAR.
    Cache for 24 hours at ~/ORACLE/cache/xbrl_{ticker}_{YYYYMMDD}.json
    Also checks legacy cache at xbrl_{CIK}_{YYYY-MM-DD}.json
    Returns the 'facts' sub-dict or {} on failure.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().strftime("%Y%m%d")
    today_dashed = date.today().strftime("%Y-%m-%d")
    cache_path = CACHE_DIR / f"xbrl_{ticker.upper()}_{today_str}.json"

    # Try ticker-based cache first
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("facts", {}) if isinstance(data, dict) else {}
        except Exception:
            pass

    # Try legacy CIK-based cache (YYYY-MM-DD format)
    cik_cache = CACHE_DIR / f"xbrl_{cik}_{today_dashed}.json"
    if cik_cache.exists():
        try:
            with open(cik_cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Save to ticker-based path for future use
            try:
                import shutil
                shutil.copy2(cik_cache, cache_path)
            except Exception:
                pass
            return data.get("facts", {}) if isinstance(data, dict) else {}
        except Exception:
            pass

    # Fetch from EDGAR — URL requires CIK0001234567 format
    cik_url_id = _build_cik_url_id(cik)
    url = XBRL_BASE.format(cik=cik_url_id)
    req = urllib.request.Request(url, headers={"User-Agent": EDGAR_USER_AGENT})
    try:
        time.sleep(SLEEP_BETWEEN_REQUESTS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
    except urllib.error.HTTPError as e:
        print(f"[edgar_financials] HTTP {e.code} fetching {url}")
        return {}
    except Exception as e:
        print(f"[edgar_financials] Error fetching {url}: {e}")
        return {}

    # Save to cache
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

    return data.get("facts", {}) if isinstance(data, dict) else {}


# ── 3. Extract a single metric ────────────────────────────────────────────────

def _get_entries_for_tag(facts: dict, namespace: str, tag: str, preferred_unit: str = None) -> list:
    """Return the list of unit entries for a given namespace/tag, or []."""
    ns_data = facts.get(namespace, {})
    tag_data = ns_data.get(tag, {})
    units = tag_data.get("units", {})
    # Prefer a specific unit key (e.g., USD/shares for EPS tags)
    if preferred_unit and preferred_unit in units and isinstance(units[preferred_unit], list) and units[preferred_unit]:
        return units[preferred_unit], preferred_unit
    # Return entries from first available unit key (USD, shares, pure, etc.)
    for unit_key, entries in units.items():
        if isinstance(entries, list) and entries:
            return entries, unit_key
    return [], "unknown"


def _is_single_quarter(entry: dict) -> bool:
    """
    Return True if entry represents a single quarter (not YTD cumulative).
    Uses 'frame' field (CY2025Q3 = single quarter) or calculates duration from start/end.
    """
    frame = entry.get("frame", "")
    if frame:
        # frame like CY2025Q3 or CY2025Q3I means single quarter
        import re
        if re.match(r"CY\d{4}Q\d", frame):
            return True
        # Frames like CY2025 are annual, CY2025Q3 are quarterly
        return False

    # Fallback: check duration via start/end
    start = entry.get("start", "")
    end = entry.get("end", "")
    if start and end:
        try:
            from datetime import date as _date
            s = _date.fromisoformat(start)
            e = _date.fromisoformat(end)
            days = (e - s).days
            # Single quarter is ~85-100 days
            return 60 <= days <= 110
        except Exception:
            pass

    # No frame and no start date — include by default (old XBRL)
    return True


def extract_metric(facts: dict, tags: list, period_type: str) -> dict | None:
    """
    Extract the most recent metric value for a list of fallback tags.
    period_type: "annual" or "quarterly"
    Tries all tags across all namespaces and returns the one with the most recent data.
    Returns metric dict or None.
    """
    namespaces = ["us-gaap", "ifrs-full", "dei"]
    candidates = []
    cutoff = date.today() - timedelta(days=3 * 365)

    for tag in tags:
        preferred_unit = "USD/shares" if tag in PER_SHARE_TAGS else None
        for ns in namespaces:
            entries, unit_key = _get_entries_for_tag(facts, ns, tag, preferred_unit)
            if not entries:
                continue

            if period_type == "annual":
                filtered = [
                    e for e in entries
                    if e.get("form") in ANNUAL_FORMS and e.get("fp") == "FY"
                    and e.get("val") is not None
                    and e.get("filed") and e.get("end")
                ]
            else:  # quarterly
                filtered = [
                    e for e in entries
                    if e.get("form") in QUARTERLY_FORMS and e.get("fp") in QUARTERLY_FPS
                    and e.get("val") is not None
                    and e.get("filed") and e.get("end")
                    and _is_single_quarter(e)
                    and date.fromisoformat(e.get("end", "1900-01-01")) >= cutoff
                ]

            if not filtered:
                continue

            # Sort by (filed desc, end desc) — get most recent period from most recent filing
            filtered.sort(key=lambda x: (x.get("filed", ""), x.get("end", "")), reverse=True)
            best = filtered[0]

            raw_val = best.get("val", 0)
            try:
                raw_val = float(raw_val)
            except (TypeError, ValueError):
                raw_val = 0.0

            candidates.append({
                "value": raw_val,
                "raw": raw_val,
                "unit": unit_key,
                "period_end": best.get("end", ""),
                "filed": best.get("filed", ""),
                "form": best.get("form", ""),
                "fp": best.get("fp", ""),
                "tag": tag,
                "namespace": ns,
            })

    if not candidates:
        return None

    # Return the candidate with the most recent (filed, period_end)
    candidates.sort(key=lambda x: (x.get("filed", ""), x.get("period_end", "")), reverse=True)
    return candidates[0]


# ── 4. Revenue trend ──────────────────────────────────────────────────────────

def get_revenue_trend(facts: dict) -> list:
    """
    Return last 4 distinct quarterly revenue entries sorted newest first.
    Each entry: {"period_end", "value", "filed", "form"}
    Tries all revenue tags and returns the candidate set with the most recent data.
    """
    revenue_tags = METRIC_TAGS["revenue"]
    namespaces = ["us-gaap", "ifrs-full"]
    cutoff = (date.today() - timedelta(days=3*365)).isoformat()

    best_result = []
    best_newest = ""

    def _maybe_add_derived_q4(periods: dict, entries: list) -> None:
        annual = [
            e for e in entries
            if e.get("form") in ANNUAL_FORMS and e.get("fp") == "FY"
            and e.get("val") is not None
            and e.get("start") and e.get("end") and e.get("filed")
        ]
        annual.sort(key=lambda x: (x.get("end", ""), x.get("filed", "")), reverse=True)

        for ann in annual:
            ann_start = ann.get("start", "")
            ann_end = ann.get("end", "")
            if ann_end in periods:
                continue
            fiscal_quarters = [
                e for e in periods.values()
                if ann_start <= e.get("end", "") < ann_end
                and e.get("fp") in {"Q1", "Q2", "Q3"}
            ]
            by_fp = {e.get("fp"): e for e in fiscal_quarters}
            if {"Q1", "Q2", "Q3"} - set(by_fp):
                continue
            q4_value = float(ann.get("val", 0)) - sum(float(by_fp[fp].get("val", 0)) for fp in ("Q1", "Q2", "Q3"))
            if q4_value <= 0:
                continue
            periods[ann_end] = {
                "end": ann_end,
                "val": q4_value,
                "filed": ann.get("filed", ""),
                "form": ann.get("form", ""),
                "fp": "Q4",
                "derived": True,
            }
            return

    for tag in revenue_tags:
        for ns in namespaces:
            entries, unit_key = _get_entries_for_tag(facts, ns, tag)
            if not entries:
                continue

            quarterly = [
                e for e in entries
                if e.get("form") in QUARTERLY_FORMS and e.get("fp") in QUARTERLY_FPS
                and e.get("val") is not None
                and e.get("filed") and e.get("end")
                and _is_single_quarter(e)
                and e.get("end", "") >= cutoff
            ]

            if not quarterly:
                continue

            # Deduplicate by period_end, keep newest filed for each
            by_period = {}
            for e in quarterly:
                period = e.get("end", "")
                if period not in by_period or e.get("filed", "") > by_period[period].get("filed", ""):
                    by_period[period] = e
            _maybe_add_derived_q4(by_period, entries)

            # Sort by period_end descending, take top 4
            sorted_periods = sorted(by_period.values(), key=lambda x: x.get("end", ""), reverse=True)[:4]
            if not sorted_periods:
                continue

            newest = sorted_periods[0].get("end", "")
            # Keep the tag/namespace that gives us the most recent data
            if newest > best_newest:
                best_newest = newest
                best_result = [
                    {
                        "period_end": e.get("end", ""),
                        "value": float(e.get("val", 0)),
                        "filed": e.get("filed", ""),
                        "form": e.get("form", ""),
                        "fp": e.get("fp", ""),
                        "derived": bool(e.get("derived")),
                    }
                    for e in sorted_periods
                ]

    return best_result


# ── 5. Freshness check ────────────────────────────────────────────────────────

def _check_edgar_submissions(ticker: str, cik: str, ann_filed: str, qtr_filed: str, warnings: list) -> None:
    """
    Fetch EDGAR submissions endpoint to check for newer filings.
    Cached per-ticker per-day so we only hit EDGAR once per session.
    Appends to warnings list if a newer filing is detected.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().strftime("%Y%m%d")
    cache_path = CACHE_DIR / f"submissions_{ticker.upper()}_{today_str}.json"

    data = None
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = None

    if data is None:
        # Fetch from EDGAR
        cik_padded = str(cik).lstrip("0") or "0"
        cik_padded = f"{int(cik_padded):010d}"
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        req = urllib.request.Request(url, headers={"User-Agent": EDGAR_USER_AGENT})
        try:
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            try:
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
            except Exception:
                pass
        except urllib.error.HTTPError as e:
            print(f"[freshness_check] HTTP {e.code} fetching submissions for {ticker}")
            return
        except Exception as e:
            print(f"[freshness_check] Could not fetch EDGAR submissions for {ticker}: {e}")
            return

    if not data:
        return

    # Parse recent filings
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])

    latest_10k = ("", "")   # (date, form)
    latest_10q = ("", "")

    for form, fd in zip(forms, filing_dates):
        if not fd:
            continue
        if form in ANNUAL_FORMS:
            if fd > latest_10k[0]:
                latest_10k = (fd, form)
        elif form in ("10-Q",):
            if fd > latest_10q[0]:
                latest_10q = (fd, form)

    # Compare with what we have
    newer_form = ""
    newer_date = ""
    if latest_10k[0] and latest_10k[0] > (ann_filed or ""):
        newer_form = latest_10k[1]
        newer_date = latest_10k[0]
    elif latest_10q[0] and latest_10q[0] > (qtr_filed or ""):
        newer_form = latest_10q[1]
        newer_date = latest_10q[0]

    if newer_form and newer_date:
        warnings.append(
            f"NEWER FILING ON EDGAR: {newer_form} filed {newer_date}"
            f" — run oracle_fetch.py {ticker.upper()} to update"
        )


def freshness_check(ticker: str, xbrl_data: dict) -> dict:
    """
    For each key metric, check if the filing date is stale.
    Returns a dict with:
    {
      "ticker": str,
      "checked_date": str (today ISO),
      "annual": {
        "filing_date": str,
        "period_end": str,
        "age_days": int,
        "status": "CURRENT" / "RECENT" / "STALE",
        "note": str  # explanation if non-December FY
      },
      "quarterly": {
        "filing_date": str,
        "period_end": str,
        "age_days": int,
        "status": "CURRENT" / "RECENT" / "STALE",
        "note": str
      },
      "warning": str or None  # populated if anything looks wrong
    }

    Status thresholds:
    - CURRENT: filed within last 120 days
    - RECENT: filed 120-270 days ago
    - STALE: filed more than 270 days ago

    For STALE annual filings: check if there is a quarterly filing that is more recent.
    If yes: note "10-K is from non-December FY — most recent 10-Q filed {date} is more current"
    If no: flag as WARNING "Annual data may be stale — verify on EDGAR"

    For STALE quarterly: always flag as WARNING.
    """
    today = date.today()
    today_str = today.isoformat()

    result = {
        "ticker": ticker.upper(),
        "checked_date": today_str,
        "annual": {},
        "quarterly": {},
        "warning": None,
    }

    def _age_status(filed_str):
        if not filed_str:
            return None, "UNKNOWN"
        try:
            fd = date.fromisoformat(filed_str)
            age = (today - fd).days
            if age <= 90:
                status = "CURRENT"
            elif age <= 180:
                status = "RECENT"
            else:
                status = "STALE"
            return age, status
        except Exception:
            return None, "UNKNOWN"

    # Annual: use revenue metric's filed date
    ann_rev = xbrl_data.get("annual", {}).get("revenue")
    ann_filed = ann_rev.get("filed", "") if ann_rev else ""
    ann_period = ann_rev.get("period_end", "") if ann_rev else ""
    ann_age, ann_status = _age_status(ann_filed)

    result["annual"] = {
        "filing_date": ann_filed,
        "period_end": ann_period,
        "age_days": ann_age,
        "status": ann_status,
        "note": "",
    }

    # Quarterly: use quarterly revenue metric's filed date
    qtr_rev = xbrl_data.get("quarterly", {}).get("revenue")
    qtr_filed = qtr_rev.get("filed", "") if qtr_rev else ""
    qtr_period = qtr_rev.get("period_end", "") if qtr_rev else ""
    qtr_age, qtr_status = _age_status(qtr_filed)

    result["quarterly"] = {
        "filing_date": qtr_filed,
        "period_end": qtr_period,
        "age_days": qtr_age,
        "status": qtr_status,
        "note": "",
    }

    warnings = []

    # Evaluate annual staleness
    if ann_status == "STALE":
        if qtr_status in ("CURRENT", "RECENT"):
            # Non-December FY — quarterly is more recent, no EDGAR check needed
            result["annual"]["note"] = (
                f"Non-December FY — quarterly data is more current"
            )
            # warning stays None for this case
        else:
            # Both annual and quarterly are STALE — check EDGAR for newer filings
            cik = xbrl_data.get("cik")
            if cik:
                try:
                    _check_edgar_submissions(ticker, cik, ann_filed, qtr_filed, warnings)
                except Exception as e:
                    print(f"[freshness_check] EDGAR submissions check failed: {e}")
            if not warnings:
                warnings.append("Both annual and quarterly data over 270 days old — verify on EDGAR")

    result["warning"] = " | ".join(warnings) if warnings else None
    return result


# ── 6. Extract EPS from earnings 8-K press release ───────────────────────────

def extract_eps_from_earnings_release(ticker: str) -> dict:
    """
    Extract GAAP and non-GAAP EPS from the most recent earnings 8-K press release.
    Returns {gaap_eps, non_gaap_eps, source_file, filed, [one_time_flag, note]}.
    """
    import re as _re

    result = {
        "gaap_eps": None,
        "non_gaap_eps": None,
        "source_file": None,
        "filed": None,
    }

    try:
        manifest = load_manifest(ticker)
        sections = manifest.get("sections", {})
        earnings_path = sections.get("newest_earnings_8k")
        if not earnings_path:
            return result

        earnings_file = Path(earnings_path)
        if not earnings_file.is_absolute():
            earnings_file = FILINGS_ROOT / ticker.upper() / earnings_file

        if not earnings_file.exists():
            return result

        result["source_file"] = str(earnings_file)

        # Try to get filed date from manifest
        for section_key in ("newest_earnings_8k_filed", "earnings_8k_filed", "8k_filed"):
            filed = manifest.get(section_key)
            if filed:
                result["filed"] = filed
                break

        text = earnings_file.read_text(encoding="utf-8", errors="replace")

        # GAAP EPS patterns (case-insensitive)
        gaap_patterns = [
            r"GAAP.*?diluted.*?\$?\s*([\-\d]+\.[\d]+)",
            r"diluted.*?GAAP.*?\$?\s*([\-\d]+\.[\d]+)",
            r"net income.*?per.*?diluted.*?\$?\s*([\-\d]+\.[\d]+)",
        ]
        # Non-GAAP EPS patterns (case-insensitive)
        non_gaap_patterns = [
            r"non-GAAP.*?diluted.*?\$?\s*([\-\d]+\.[\d]+)",
            r"adjusted.*?diluted.*?EPS.*?\$?\s*([\-\d]+\.[\d]+)",
            r"non-GAAP EPS.*?\$?\s*([\-\d]+\.[\d]+)",
        ]

        for pat in gaap_patterns:
            m = _re.search(pat, text, _re.IGNORECASE | _re.DOTALL)
            if m:
                try:
                    result["gaap_eps"] = float(m.group(1))
                    break
                except (ValueError, IndexError):
                    continue

        for pat in non_gaap_patterns:
            m = _re.search(pat, text, _re.IGNORECASE | _re.DOTALL)
            if m:
                try:
                    result["non_gaap_eps"] = float(m.group(1))
                    break
                except (ValueError, IndexError):
                    continue

        # Check divergence
        g = result["gaap_eps"]
        ng = result["non_gaap_eps"]
        if g is not None and ng is not None and g != 0:
            divergence = abs(ng - g) / abs(g)
            if divergence > 0.20:
                result["one_time_flag"] = True
                result["note"] = "GAAP/non-GAAP diverge >20% — one-time item likely"

    except Exception:
        pass

    return result


# ── 7. Get all financials ─────────────────────────────────────────────────────

def get_all_financials(ticker: str) -> dict:
    """
    Main function. Fetches all financial metrics for a ticker via XBRL.
    Returns structured dict with annual, quarterly, revenue_trend.
    Never crashes — all exceptions are caught.
    """
    result = {
        "ticker": ticker.upper(),
        "cik": None,
        "company_name": None,
        "annual": {},
        "quarterly": {},
        "revenue_trend": [],
        "error": None,
    }

    try:
        # Get CIK — checks manifest first, then company_tickers cache, then EDGAR
        cik = get_cik_from_manifest(ticker)
        if not cik:
            result["error"] = f"CIK not found for {ticker} — run oracle_fetch.py {ticker} first or check ticker is valid"
            return result

        result["cik"] = cik

        # Get company name from manifest if available
        manifest = load_manifest(ticker)
        result["company_name"] = manifest.get("company_name") or ticker

        # Fetch XBRL facts
        facts = fetch_xbrl_facts(cik, ticker)
        if not facts:
            result["error"] = f"No XBRL facts returned for CIK {cik}"
            return result

        # Extract all metrics — annual and quarterly
        for metric_name, tags in METRIC_TAGS.items():
            result["annual"][metric_name] = extract_metric(facts, tags, "annual")
            result["quarterly"][metric_name] = extract_metric(facts, tags, "quarterly")

        # Compute total liquidity (cash + current short-term/marketable investments).
        try:
            liquid_tags = [
                "MarketableSecuritiesCurrent",
                "ShortTermInvestments",
                "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
            ]

            def _with_current_investments(cash_metric: dict | None) -> dict | None:
                if not cash_metric or not cash_metric.get("raw"):
                    return cash_metric
                target_period = cash_metric.get("period_end", "")
                target_filed  = cash_metric.get("filed", "")
                total_liquid = float(cash_metric["raw"])
                added_tags = []
                for ltag in liquid_tags:
                    entries, _ = _get_entries_for_tag(facts, "us-gaap", ltag)
                    matches = [
                        e for e in entries
                        if e.get("filed") == target_filed
                        and e.get("end") == target_period
                        and e.get("val") is not None
                    ]
                    if not matches:
                        matches = [
                            e for e in entries
                            if e.get("end") == target_period
                            and e.get("val") is not None
                        ]
                        matches.sort(key=lambda x: x.get("filed", ""), reverse=True)
                    if matches:
                        total_liquid += float(matches[0]["val"])
                        added_tags.append(ltag)
                if added_tags:
                    return {
                        "value": total_liquid,
                        "raw": total_liquid,
                        "unit": "USD",
                        "period_end": target_period,
                        "filed": target_filed,
                        "form": cash_metric.get("form", ""),
                        "tag": f"Cash+{'+'.join(t[:20] for t in added_tags)}",
                        "namespace": "us-gaap",
                    }
                return cash_metric

            result["quarterly"]["cash"] = _with_current_investments(result["quarterly"].get("cash"))
            result["annual"]["cash"] = _with_current_investments(result["annual"].get("cash"))

            q_cash = result["quarterly"].get("cash")
            a_cash = result["annual"].get("cash")
            if q_cash and (not a_cash or (q_cash.get("period_end", ""), q_cash.get("filed", "")) > (a_cash.get("period_end", ""), a_cash.get("filed", ""))):
                result["annual"]["cash"] = q_cash
        except Exception:
            pass  # fallback to raw cash if liquidity calc fails

        # Revenue trend
        result["revenue_trend"] = get_revenue_trend(facts)

        # Freshness validation
        result["freshness"] = freshness_check(ticker, result)

        # Annualized run-rate from most recent quarter (always newer than annual 10-K)
        # If 10-Q was filed after 10-K, the quarterly revenue is more current
        try:
            qtr_rev = result["quarterly"].get("revenue")
            ann_rev = result["annual"].get("revenue")
            if qtr_rev and qtr_rev.get("raw") and qtr_rev.get("filed") and ann_rev and ann_rev.get("filed"):
                qtr_filed = qtr_rev.get("filed", "")
                ann_filed = ann_rev.get("filed", "")
                if qtr_filed > ann_filed:
                    # 10-Q is newer — compute annualized run-rate
                    qtr_val = float(qtr_rev["raw"])
                    annualized = qtr_val * 4
                    result["run_rate_revenue"] = {
                        "value": annualized,
                        "raw": annualized,
                        "unit": "USD",
                        "period_end": qtr_rev.get("period_end", ""),
                        "filed": qtr_filed,
                        "form": qtr_rev.get("form", ""),
                        "tag": f"Annualized from Q ({qtr_rev.get('tag','')}) x4",
                        "namespace": "us-gaap",
                        "note": f"Most recent quarter x4 — 10-Q filed {qtr_filed} is newer than 10-K filed {ann_filed}",
                    }
        except Exception:
            pass

        # EPS from earnings press release (8-K)
        result["eps_detail"] = extract_eps_from_earnings_release(ticker)

    except Exception as e:
        result["error"] = str(e)

    return result


# ── 8. Format helpers ─────────────────────────────────────────────────────────

def format_value(metric: dict | None, unit_override: str = None) -> str:
    """Format a metric dict for display with citation."""
    if metric is None:
        return "[NOT IN XBRL]"

    unit = unit_override or metric.get("unit", "")
    raw = metric.get("raw", 0)
    value = metric.get("value", 0)

    try:
        raw = float(raw)
        value = float(value)
    except (TypeError, ValueError):
        raw = 0.0
        value = 0.0

    # Format the number
    if unit == "USD" or unit.startswith("USD/"):
        # USD/shares = EPS, USD = regular dollar amounts
        if unit == "USD" and abs(raw) > 1e8:
            num_str = f"${value / 1e6:,.1f}M"
        elif unit == "USD" and abs(raw) >= 1e6:
            num_str = f"${value / 1e6:,.1f}M"
        elif unit == "USD" and abs(raw) >= 1e3:
            num_str = f"${value / 1e3:,.1f}K"
        else:
            # EPS or small dollar values
            num_str = f"${value:,.2f}"
    elif unit in ("shares", "pure") or "share" in unit.lower():
        if abs(raw) > 1e6:
            num_str = f"{value / 1e6:,.1f}M shares"
        else:
            num_str = f"{value:,.0f} shares"
    else:
        # Generic fallback — treat large numbers as USD millions
        if abs(raw) > 1e8:
            num_str = f"${value / 1e6:,.1f}M"
        elif abs(raw) > 1 or raw == 0:
            num_str = f"{value:,.2f}"
        else:
            num_str = f"{value:.4f}"

    citation = format_citation(metric)
    return f"{num_str} {citation}"


def format_citation(metric: dict | None) -> str:
    """Return just the citation string for a metric."""
    if metric is None:
        return "[NOT IN XBRL]"
    tag = metric.get("tag", "?")
    form = metric.get("form", "?")
    filed = metric.get("filed", "?")
    period = metric.get("period_end", "?")
    return f"[{tag}, {form} filed {filed}, period {period}]"


def show_financials_summary(ticker: str) -> None:
    """
    Print a one-line summary for a ticker showing annual + quarterly revenue,
    run-rate, EPS, cash, debt, and freshness status.
    Format:
      {TICKER} — Price: ${price} | Annual Rev: ${Xb} | Qtr Rev: ${Xb} (run-rate ${Xb}) | EPS: ${X} | Cash: ${Xb} | Debt: ${Xb}
      Annual 10-K filed: {date} ({N}d) | Latest 10-Q filed: {date} ({N}d) | [CURRENT/RECENT/STALE]
    """
    import sys
    sys.path.insert(0, str(Path.home() / "ORACLE"))
    try:
        from data.oracle_data import get_price
        p = get_price(ticker, fresh=False)
        price_str = f"${p['price']:.2f}" if p and p.get("price") else "N/A"
    except Exception:
        price_str = "N/A"

    d = get_all_financials(ticker)
    ann = d["annual"]
    qtr = d["quarterly"]
    rev   = ann.get("revenue")
    qrev  = qtr.get("revenue")
    rr    = d.get("run_rate_revenue")
    eps   = ann.get("eps_diluted")
    cash  = ann.get("cash")
    ltd   = ann.get("ltd")
    f     = d.get("freshness", {})

    def _b(m):
        if m and m.get("raw"):
            return f"${float(m['raw'])/1e9:.2f}B"
        return "N/A"

    rev_str  = _b(rev)
    qrev_str = _b(qrev)
    rr_str   = _b(rr) if rr else ""
    eps_str  = f"${float(eps['raw']):.2f}" if eps and eps.get("raw") is not None else "N/A"
    cash_str = _b(cash)
    ltd_str  = _b(ltd) if ltd and ltd.get("raw") and float(ltd["raw"]) > 1e6 else "no debt"

    rr_part = f" (run-rate {rr_str})" if rr_str else ""
    ann_filed = f.get("annual", {}).get("filing_date", "?")
    ann_age   = f.get("annual", {}).get("age_days", "?")
    qtr_filed = f.get("quarterly", {}).get("filing_date", "?")
    qtr_age   = f.get("quarterly", {}).get("age_days", "?")
    status    = f.get("quarterly", {}).get("status") or f.get("annual", {}).get("status") or "?"

    print(f"{ticker.upper()} — Price: {price_str} | Annual Rev: {rev_str} | Qtr Rev: {qrev_str}{rr_part} | EPS: {eps_str} | Cash: {cash_str} | Debt: {ltd_str}")
    print(f"Annual 10-K filed: {ann_filed} ({ann_age}d) | Latest 10-Q filed: {qtr_filed} ({qtr_age}d) | [{status}]")


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "ANET"
    data = get_all_financials(ticker)
    print(f"\n=== XBRL Financials for {ticker} ===")
    print(f"CIK: {data['cik']}")
    print(f"Company: {data['company_name']}")
    if data["error"]:
        print(f"ERROR: {data['error']}")
    print("\n-- Annual --")
    for k, v in data["annual"].items():
        print(f"  {k:<20} {format_value(v)}")
    print("\n-- Quarterly --")
    for k, v in data["quarterly"].items():
        print(f"  {k:<20} {format_value(v)}")
    print("\n-- Revenue Trend --")
    for r in data["revenue_trend"]:
        val_m = r["value"] / 1e6 if r["value"] > 1e6 else r["value"]
        print(f"  {r['period_end']}  {r['fp']}  ${val_m:,.1f}M  [{r['form']} filed {r['filed']}]")
