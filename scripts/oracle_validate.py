#!/usr/bin/env python3
"""
oracle_validate.py -- Fact sheet validation harness for ORACLE.

Validates the data layer for any ticker WITHOUT running panels.
Cost: ~$0 (no LLM calls -- pure data retrieval and formatting).
Time: 15-30 seconds per ticker.

Usage:
    python3 ~/ORACLE/scripts/oracle_validate.py LITE
    python3 ~/ORACLE/scripts/oracle_validate.py LITE CSCO BBIO MRVL CRWD
    python3 ~/ORACLE/scripts/oracle_validate.py --batch  # runs all 5 known-answer tickers
    python3 ~/ORACLE/scripts/oracle_validate.py LITE --save  # saves to ~/ORACLE/validation/
    python3 ~/ORACLE/scripts/oracle_validate.py LITE --no-clear-cache  # skip cache clear (faster)
"""

import sys
import os
import argparse
import datetime
import json
import io
from pathlib import Path

ORACLE_DIR = Path.home() / "ORACLE"
sys.path.insert(0, str(ORACLE_DIR / "engine"))

from oracle_factsheet import (
    build_fact_sheet,
    get_session_price,
    CACHE_DIR,
)

# ---------------------------------------------------------------------------
# Known-answer test cases for --batch mode
# ---------------------------------------------------------------------------
KNOWN_ANSWERS = {
    "LITE": {
        "desc": "Lumentum Q3 FY2026 -- record revenue, NVIDIA investment",
        "8k_date_max_days": 45,
        "revenue_quarter_min": 750e6,
        "revenue_quarter_max": 870e6,
        "gross_margin_gaap_min": 0.38,
        "gross_margin_gaap_max": 0.55,
        "going_concern": False,
        "eps_gaap_approx": 1.50,
        "eps_nongaap_approx": 2.37,
        "expect_material_events": True,
    },
    "CSCO": {
        "desc": "Cisco Q3 FY2026 -- record AI orders",
        "8k_date_max_days": 45,
        "revenue_quarter_min": 14e9,
        "revenue_quarter_max": 17e9,
        "gross_margin_gaap_min": 0.55,
        "gross_margin_gaap_max": 0.72,
        "going_concern": False,
        "eps_gaap_approx": 0.85,
        "eps_nongaap_approx": 1.06,
    },
    "BBIO": {
        "desc": "BridgeBio Q1 2026 -- Attruby commercial ramp",
        "8k_date_max_days": 45,
        "revenue_quarter_min": 170e6,
        "revenue_quarter_max": 220e6,
        "going_concern": False,
        "eps_gaap_approx": -0.84,
    },
    "MRVL": {
        "desc": "Marvell FY2026 Q4 -- AI datacenter growth",
        "8k_date_max_days": 90,
        "revenue_quarter_min": 2.0e9,
        "revenue_quarter_max": 2.5e9,
        "going_concern": False,
        "eps_gaap_approx": 0.46,
        "eps_nongaap_approx": 0.80,
    },
    "CRWD": {
        "desc": "CrowdStrike Q4 FY2026 -- cybersecurity AI",
        "8k_date_max_days": 90,
        "revenue_quarter_min": 1.1e9,
        "revenue_quarter_max": 1.5e9,
        "going_concern": False,
    },
}


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def clear_ticker_cache(ticker: str):
    """Clear all cached data for a ticker to force fresh fetch."""
    ticker_up = ticker.upper()
    for pattern in [
        f"*{ticker_up}*",
        f"factsheet_{ticker_up}_*",
        f"press_release_{ticker_up}_*",
        f"form4_{ticker_up}_*",
        f"legal_{ticker_up}_*",
        f"news_{ticker_up}_*",
        f"material_events_{ticker_up}_*",
    ]:
        for f in CACHE_DIR.glob(pattern):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


def fmt_dollars(val, scale="auto"):
    """Format dollar value with appropriate scale."""
    if val is None:
        return "N/A"
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "N/A"
    if abs(val) >= 1e9:
        return f"${val/1e9:.2f}B"
    if abs(val) >= 1e6:
        return f"${val/1e6:.1f}M"
    if abs(val) >= 1e3:
        return f"${val/1e3:.1f}K"
    return f"${val:.2f}"


def safe_get(d, *keys, default=None):
    """Safely navigate nested dicts."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


# ---------------------------------------------------------------------------
# Core validation runner
# ---------------------------------------------------------------------------

def run_validation(ticker: str, clear_cache: bool = True, known: dict = None) -> dict:
    """Run fact sheet validation for a ticker. Returns results dict."""
    if clear_cache:
        clear_ticker_cache(ticker)

    print(f"  Fetching data for {ticker}...", end="", flush=True)
    fs = build_fact_sheet(ticker)
    print(" done.")

    pr = fs.get("press_release") or {}
    metrics = fs.get("metrics") or {}
    legal = fs.get("legal_proceedings") or {}
    f4 = fs.get("insider_form4") or {}
    f4_sum = f4.get("summary") or {}
    f4_txns = f4.get("transactions") or []
    material = fs.get("material_events") or []
    news = fs.get("recent_news") or []
    guidance = fs.get("guidance") or {}
    dq = fs.get("data_quality") or {}
    gate_data = fs.get("reconciliation_gate") or {}
    price = fs.get("price") or 0
    if not price:
        price = get_session_price(ticker) or 0
    mktcap = 0
    try:
        import yfinance as yf
        mktcap = yf.Ticker(ticker).info.get("marketCap") or 0
    except Exception:
        pass

    today = datetime.date.today()

    # 8-K age
    filing_date_str = pr.get("filing_date", "")
    filing_date = None
    filing_age_days = 999
    if filing_date_str:
        try:
            filing_date = datetime.date.fromisoformat(filing_date_str[:10])
            filing_age_days = (today - filing_date).days
        except Exception:
            pass

    # --- 8-K RECENCY HARD STOP ---
    # If company reported earnings within 14 days but fact sheet data predates that filing, hard stop
    recent_8k_hard_stop = False
    recent_8k_message = ""
    try:
        import yfinance as yf
        # Find the most recent 8-K date from EDGAR submissions
        cik = fs.get("cik")
        if cik:
            cik_int = int(cik)
            sub_url = f"https://data.sec.gov/submissions/CIK{cik_int:010d}.json"
            import requests as _req
            sub_resp = _req.get(sub_url, headers={"User-Agent": "ORACLE oracle@example.com"}, timeout=15)
            if sub_resp.status_code == 200:
                sub_data = sub_resp.json()
                forms = sub_data.get("filings", {}).get("recent", {}).get("form", [])
                fdates = sub_data.get("filings", {}).get("recent", {}).get("filingDate", [])
                # Find most recent 8-K
                most_recent_8k = None
                for i, f in enumerate(forms):
                    if f in ("8-K", "8-K/A"):
                        most_recent_8k = fdates[i]
                        break
                if most_recent_8k:
                    try:
                        mk8_dt = datetime.date.fromisoformat(most_recent_8k)
                        days_since_8k = (today - mk8_dt).days
                        if days_since_8k <= 14:
                            # Recent 8-K exists — check if fact sheet data reflects it
                            # The press release filing_date must be within 7 days of this 8-K
                            pr_date_str = fs.get("press_release", {}).get("filing_date", "")
                            pr_date = datetime.date.fromisoformat(pr_date_str[:10]) if pr_date_str and len(pr_date_str) >= 10 else None
                            if pr_date is None or abs((pr_date - mk8_dt).days) > 7:
                                recent_8k_hard_stop = True
                                recent_8k_message = (
                                    f"STALE EARNINGS DATA — most recent 8-K filed {most_recent_8k} "
                                    f"({days_since_8k}d ago) but fact sheet press release date is "
                                    f"'{pr_date_str or 'MISSING'}' — data may not reflect latest earnings. "
                                    f"Clear cache and re-run."
                                )
                    except Exception:
                        pass
    except Exception:
        pass

    # XBRL period freshness -- find the most informative metric period
    xbrl_period = ""
    xbrl_age_months = 999
    for mk in ("revenue_ttm", "gaap_eps_ttm", "gross_margin"):
        p = safe_get(metrics, mk, "period", default="")
        if p and len(p) >= 4:
            xbrl_period = p
            break

    if xbrl_period and len(xbrl_period) >= 4:
        try:
            xbrl_year = int(xbrl_period[:4])
            xbrl_age_months = (today.year - xbrl_year) * 12 + today.month
            if len(xbrl_period) >= 6 and "Q" in xbrl_period.upper():
                idx = xbrl_period.upper().index("Q")
                q = int(xbrl_period[idx + 1])
                xbrl_age_months = (today.year - xbrl_year) * 12 + (today.month - q * 3)
        except Exception:
            pass

    # Grab the main financial values from press release (primary) or metrics (fallback)
    rev_q = safe_get(pr, "revenue_quarter", "value")
    gm_gaap = safe_get(pr, "gross_margin_gaap", "value") or safe_get(metrics, "gross_margin", "value")
    gm_ng = safe_get(pr, "gross_margin_nongaap", "value")
    eps_g = safe_get(pr, "eps_gaap_quarter", "value")
    eps_ng = safe_get(pr, "eps_nongaap_quarter", "value")
    ocf = safe_get(pr, "operating_cashflow_quarter", "value") or safe_get(metrics, "operating_cashflow_ttm", "value")

    # Balance sheet from metrics
    cash = safe_get(metrics, "cash_and_investments", "value")
    debt = safe_get(metrics, "total_debt", "value")

    # Quarterly P/S (annualised revenue vs market cap)
    rev_ttm = safe_get(metrics, "revenue_ttm", "value")
    ps_ratio = None
    if mktcap and rev_ttm and rev_ttm > 0:
        ps_ratio = mktcap / rev_ttm

    # P/S sanity check — catches wrong-period revenue data
    ps_sanity_ok = True
    ps_sanity_message = ""
    if ps_ratio is not None:
        if ps_ratio < 0.10:
            ps_sanity_ok = False
            ps_sanity_message = (
                f"P/S RATIO {ps_ratio:.2f}x IS BELOW 0.10x FLOOR — "
                f"TTM revenue ${rev_ttm/1e9:.1f}B against market cap ${mktcap/1e9:.1f}B. "
                f"Likely wrong fiscal period — pre-divestiture or wrong taxonomy tag. "
                f"Revenue figure REJECTED."
            )
        elif ps_ratio > 100:
            ps_sanity_ok = False
            ps_sanity_message = (
                f"P/S RATIO {ps_ratio:.0f}x IS ABOVE 100x CEILING — "
                f"TTM revenue ${rev_ttm/1e6:.0f}M against market cap ${mktcap/1e9:.1f}B. "
                f"Likely single-quarter figure misread as annual, or wrong taxonomy tag. "
                f"Revenue figure REJECTED."
            )

    # ----- Plausibility checks -----
    failures = []
    warnings = []
    passes = []

    # Check 1: 8-K parse success + date
    max_days = (known or {}).get("8k_date_max_days", 45)
    if not pr.get("parse_success"):
        failures.append(
            f"8-K parse FAILED: {pr.get('parse_errors', ['unknown'])[:1]}"
        )
    elif filing_age_days <= max_days:
        passes.append(f"8-K date recent: {filing_date_str} ({filing_age_days} days ago)")
    else:
        failures.append(
            f"8-K date {filing_date_str} is {filing_age_days} days old (> {max_days} day limit)"
        )

    # Check 2: Revenue range plausibility
    if rev_q is not None and mktcap:
        ann_rev = rev_q * 4
        ps_q = mktcap / ann_rev if ann_rev > 0 else 0
        if ps_q < 200:
            passes.append(
                f"Revenue range plausible: {fmt_dollars(rev_q)} quarterly, P/S = {ps_q:.1f}x"
            )
        else:
            failures.append(
                f"Revenue range implausible: {fmt_dollars(rev_q)} quarterly gives P/S = {ps_q:.0f}x (>200x)"
            )
    elif rev_q is not None:
        passes.append(f"Revenue found: {fmt_dollars(rev_q)} (P/S check skipped, no mktcap)")
    else:
        warnings.append("Revenue: not found in 8-K press release")

    # Check 3: Gross margin 0-100%
    if gm_gaap is not None:
        if 0.0 < gm_gaap < 1.0:
            passes.append(f"Gross margin in range: {gm_gaap*100:.1f}%")
        else:
            failures.append(
                f"Gross margin OUT OF RANGE: {gm_gaap*100:.1f}% (must be 0-100%)"
            )
    else:
        warnings.append("Gross margin: not found in 8-K or XBRL")

    # Check 4: Going concern
    gc = legal.get("going_concern", False)
    expected_gc = (known or {}).get("going_concern", False)
    if gc == expected_gc:
        passes.append(f"Going concern: {'present' if gc else 'absent'} (expected)")
    elif gc and not expected_gc:
        failures.append(
            "Going concern: FLAGGED but not expected for this company -- check for false positive"
        )
    else:
        warnings.append("Going concern: absent but expected")

    # Check 5: XBRL period freshness
    if xbrl_period:
        if xbrl_age_months <= 18:
            passes.append(f"XBRL period fresh: {xbrl_period} ({xbrl_age_months} months old)")
        else:
            failures.append(
                f"XBRL period STALE: {xbrl_period} ({xbrl_age_months} months old)"
            )
    else:
        warnings.append("XBRL period: could not determine (no metrics loaded)")

    # Check 6: EPS GAAP vs non-GAAP direction (both positive only)
    if eps_g is not None and eps_ng is not None and eps_g > 0 and eps_ng > 0:
        if eps_ng >= eps_g * 0.90:
            passes.append(
                f"EPS direction normal: GAAP ${eps_g:.2f} <= non-GAAP ${eps_ng:.2f}"
            )
        else:
            warnings.append(
                f"EPS direction INVERTED: GAAP ${eps_g:.2f} > non-GAAP ${eps_ng:.2f} -- check for swap"
            )

    # Check 7: Revenue vs known answer bounds
    if known and rev_q is not None:
        rev_min = known.get("revenue_quarter_min", 0)
        rev_max = known.get("revenue_quarter_max", float("inf"))
        if rev_min <= rev_q <= rev_max:
            passes.append(f"Revenue in expected range: {fmt_dollars(rev_q)}")
        else:
            failures.append(
                f"Revenue OUT OF EXPECTED RANGE: {fmt_dollars(rev_q)} "
                f"(expected {fmt_dollars(rev_min)}-{fmt_dollars(rev_max)})"
            )

    # Check 8: GAAP gross margin vs known bounds
    if known and gm_gaap is not None:
        gm_min = known.get("gross_margin_gaap_min")
        gm_max = known.get("gross_margin_gaap_max")
        if gm_min is not None and gm_max is not None:
            if gm_min <= gm_gaap <= gm_max:
                passes.append(
                    f"GAAP gross margin in expected range: {gm_gaap*100:.1f}%"
                )
            else:
                failures.append(
                    f"GAAP gross margin OUT OF EXPECTED RANGE: {gm_gaap*100:.1f}% "
                    f"(expected {gm_min*100:.0f}%-{gm_max*100:.0f}%)"
                )

    # Check 9: Material events if expected
    if known and known.get("expect_material_events"):
        if material:
            passes.append(f"Material events found: {len(material)}")
        else:
            warnings.append(
                "Material events: none found -- expected strategic events for this ticker"
            )

    return {
        "ticker": ticker,
        "price": price,
        "mktcap": mktcap,
        "cik": fs.get("cik", ""),
        "company_name": fs.get("company_name", ""),
        "filing_date": filing_date_str,
        "filing_age_days": filing_age_days,
        "pr": pr,
        "metrics": metrics,
        "legal": legal,
        "f4_sum": f4_sum,
        "f4_txns": f4_txns,
        "material": material,
        "news": news,
        "guidance": guidance,
        "dq": dq,
        "gate": gate_data,
        "passes": passes,
        "warnings": warnings,
        "failures": failures,
        "xbrl_period": xbrl_period,
        "xbrl_age_months": xbrl_age_months,
        "eps_g": eps_g,
        "eps_ng": eps_ng,
        "rev_q": rev_q,
        "gm_gaap": gm_gaap,
        "gm_ng": gm_ng,
        "ocf": ocf,
        "cash": cash,
        "debt": debt,
        "ps_ratio": ps_ratio,
        "recent_8k_hard_stop": recent_8k_hard_stop,
        "recent_8k_message": recent_8k_message,
        "ps_sanity_ok": ps_sanity_ok,
        "ps_sanity_message": ps_sanity_message,
    }


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def field_line(label, field_dict, scale="M"):
    """Format a single income-statement line."""
    if not field_dict or field_dict.get("value") is None:
        return f"  {label:<28} N/A"
    val = field_dict["value"]
    src = field_dict.get("source", "?")
    gaap_label = "GAAP" if field_dict.get("is_gaap", True) else "non-GAAP"
    period = field_dict.get("period", "")
    period_str = f" | {period}" if period else ""

    if "margin" in label.lower() or label.endswith("%"):
        val_str = f"{val*100:.1f}%"
    elif scale == "EPS":
        val_str = f"${val:.2f}"
    else:
        val_str = fmt_dollars(val)

    return f"  {label:<28} {val_str:<14} [{gaap_label} | {src}{period_str}]"


def print_validation_report(r: dict, output=None):
    """Print the structured validation report. If output is a file-like object, write there too."""

    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    ticker = r["ticker"]
    today = datetime.date.today()
    pr = r["pr"]
    f4s = r["f4_sum"]
    f4_txns = r["f4_txns"]
    g = r["guidance"] or {}
    metrics = r["metrics"]

    # Company name — first line, always
    company_name = r.get("company_name", "")
    emit(f"FACT SHEET VALIDATION -- {ticker}")
    emit(f"Company confirmed: {company_name or 'UNKNOWN (check ticker_names.json)'}")
    emit(f"CIK: {r.get('cik', 'UNKNOWN')}")
    emit("=" * 60)

    # Reconciliation gate log
    gate_data = r.get("gate") or {}
    gate_log = gate_data.get("gate_log", []) if isinstance(gate_data, dict) else []
    if gate_log:
        emit("\nRECONCILIATION GATE LOG:")
        for entry in gate_log:
            emit(entry)
        corrections = gate_data.get("corrections", [])
        if corrections:
            emit(f"  Corrections applied: {len(corrections)}")
            for c in corrections[:5]:
                emit(f"    - {c}")
        emit("")

    # Hard stop checks first
    if r.get("recent_8k_hard_stop"):
        emit(f"[HARD STOP] {r['recent_8k_message']}")
        emit("")
    if not r.get("ps_sanity_ok", True):
        emit(f"[DATA REJECTED] {r.get('ps_sanity_message', '')}")
        emit("")

    emit()
    emit("=" * 60)
    price_str = fmt_dollars(r["price"]) if r["price"] else "N/A"
    emit(f"Run date: {today} | Price: {price_str}")
    emit("=" * 60)

    # ---- 8-K ----
    emit()
    emit("MOST RECENT 8-K:")
    if pr.get("parse_success"):
        emit(f"  Filing date: {r['filing_date']}  ({r['filing_age_days']} days ago)")
        url = pr.get("source_url", "") or pr.get("accession_url", "")
        if url:
            emit(f"  Source:      {url[:80]}")
        items = pr.get("items", [])
        if items:
            emit(f"  Items:       {', '.join(str(i) for i in items[:4])}")
        ex = pr.get("exhibit_description", "")
        if ex:
            emit(f"  Exhibit 99.1: {ex[:70]}")
    else:
        emit("  Status: PARSE FAILED or not found")
        errs = pr.get("parse_errors") or []
        if errs:
            for e in errs[:2]:
                emit(f"  Error: {str(e)[:100]}")
        else:
            emit(f"  Error: {pr.get('error','unknown')[:100]}")

    # ---- Income Statement ----
    filing_period = r["filing_date"] or r["xbrl_period"] or "?"
    emit()
    emit(f"INCOME STATEMENT ({filing_period}):")
    emit(field_line("Revenue:", pr.get("revenue_quarter")))
    # Revenue YoY from metrics if available
    rev_yoy = safe_get(metrics, "revenue_yoy_pct", "value")
    if rev_yoy is not None:
        emit(f"  {'Revenue YoY:':<28} {rev_yoy:+.1f}%  [calculated]")
    emit(field_line("GAAP gross margin:", pr.get("gross_margin_gaap")))
    emit(field_line("Non-GAAP gross margin:", pr.get("gross_margin_nongaap")))
    emit(field_line("GAAP diluted EPS:", pr.get("eps_gaap_quarter"), "EPS"))
    emit(field_line("Non-GAAP diluted EPS:", pr.get("eps_nongaap_quarter"), "EPS"))
    emit(field_line("Operating CF:", pr.get("operating_cashflow_quarter")))

    # ---- Guidance ----
    emit()
    emit("GUIDANCE:")
    if g and not g.get("error"):
        rev_lo = g.get("guidance_revenue_low")
        rev_hi = g.get("guidance_revenue_high")
        if rev_lo is not None and rev_hi is not None:
            emit(f"  {'Revenue:':<28} {fmt_dollars(rev_lo)} - {fmt_dollars(rev_hi)}  [8-K]")
        elif rev_lo is not None:
            emit(f"  {'Revenue:':<28} >= {fmt_dollars(rev_lo)}  [8-K]")
        else:
            emit(f"  Revenue:                     Not provided")

        eps_g_lo = g.get("guidance_eps_gaap_low")
        eps_g_hi = g.get("guidance_eps_gaap_high")
        if eps_g_lo is not None:
            hi_str = f" - ${eps_g_hi:.2f}" if eps_g_hi is not None else ""
            emit(f"  {'GAAP EPS:':<28} ${eps_g_lo:.2f}{hi_str}  [8-K]")
        else:
            emit(f"  GAAP EPS:                    Not provided")

        eps_ng_lo = g.get("guidance_eps_nongaap_low")
        eps_ng_hi = g.get("guidance_eps_nongaap_high")
        if eps_ng_lo is not None:
            hi_str = f" - ${eps_ng_hi:.2f}" if eps_ng_hi is not None else ""
            emit(f"  {'Non-GAAP EPS:':<28} ${eps_ng_lo:.2f}{hi_str}  [8-K]")
        else:
            emit(f"  Non-GAAP EPS:                Not provided")

        src_date = g.get("source_date", "")
        if src_date:
            emit(f"  (Guidance from {src_date})")
        expired = g.get("expired_warning", "")
        if expired:
            emit(f"  ** WARNING: {expired[:100]}")
    else:
        emit("  Not found in 8-K")
        if g.get("error"):
            emit(f"  Error: {g['error'][:80]}")

    # ---- Balance Sheet ----
    emit()
    emit("BALANCE SHEET:")
    cash_val = r["cash"]
    debt_val = r["debt"]
    if cash_val is not None:
        cash_src = safe_get(metrics, "cash_and_investments", "source") or "XBRL"
        cash_period = safe_get(metrics, "cash_and_investments", "period") or ""
        period_tag = f" | {cash_period}" if cash_period else ""
        emit(f"  {'Cash & investments:':<28} {fmt_dollars(cash_val):<14} [GAAP | {cash_src}{period_tag}]")
    else:
        emit(f"  {'Cash & investments:':<28} Not found")

    if debt_val is not None:
        debt_src = safe_get(metrics, "total_debt", "source") or "XBRL"
        debt_period = safe_get(metrics, "total_debt", "period") or ""
        period_tag = f" | {debt_period}" if debt_period else ""
        emit(f"  {'Total debt:':<28} {fmt_dollars(debt_val):<14} [GAAP | {debt_src}{period_tag}]")
    else:
        emit(f"  {'Total debt:':<28} Not found")

    # ---- Material Events ----
    emit()
    emit("MATERIAL EVENTS (past 90 days):")
    if r["material"]:
        for ev in r["material"][:8]:
            date_ = ev.get("date", "")
            type_ = ev.get("type", "")
            title_ = ev.get("title", "")[:60]
            emit(f"  {date_} | {type_} -- {title_}")
    else:
        emit("  None found")

    # ---- Recent News ----
    if r["news"]:
        emit()
        emit("RECENT NEWS (past 7 days):")
        for n in r["news"][:5]:
            date_str = n.get("date", "")[:10] if n.get("date") else ""
            title_ = n.get("title", "")[:70]
            emit(f"  [{date_str}] {title_}")

    # ---- Insider Transactions ----
    emit()
    emit("INSIDER TRANSACTIONS (past 90 days -- Form 4):")
    buys_val = f4s.get("open_market_buys_90d") or 0
    sells_val = f4s.get("open_market_sells_90d") or 0
    plan_val = f4s.get("plan_sells_90d") or 0
    ceo_buys = f4s.get("ceo_buys_90d") or 0

    n_buys = sum(
        1 for t in f4_txns if t.get("type") == "open_market_purchase"
    )
    n_sells = sum(
        1 for t in f4_txns if t.get("type") in ("open_market_sale", "open_market_sell")
    )

    emit(f"  Open-market buys:  {fmt_dollars(buys_val)}  [{n_buys} transactions]")
    emit(f"  Open-market sells: {fmt_dollars(sells_val)}  [{n_sells} transactions]")
    emit(f"  Plan-based sells:  {fmt_dollars(plan_val)}  (pre-scheduled, less informative)")
    emit(f"  CEO buys:          {fmt_dollars(ceo_buys)}")

    sig_buys = f4s.get("significant_buys") or []
    if sig_buys:
        emit("  Significant purchases (>$1M):")
        for b in sig_buys[:5]:
            shares_ = b.get("shares", 0)
            val_ = b.get("value", 0)
            emit(
                f"    {b.get('date','')} | {b.get('insider','')} ({b.get('title','')}) "
                f"| {shares_:,} shares | {fmt_dollars(val_)}"
            )

    # ---- Plausibility Checks ----
    emit()
    emit("PLAUSIBILITY CHECKS:")
    for p in r["passes"]:
        emit(f"  [PASS] {p}")
    for w in r["warnings"]:
        emit(f"  [WARN] {w}")
    for f in r["failures"]:
        emit(f"  [FAIL] {f}")

    # Data quality warnings
    dq_warns = (r["dq"].get("warnings") or [])
    for w in dq_warns[:5]:
        emit(f"  [DATA] {str(w)[:100]}")

    # Parse errors from press release
    pe = pr.get("parse_errors") or []
    for e in pe[:3]:
        emit(f"  [PARSE] {str(e)[:100]}")

    # Missing XBRL fields
    missing = r["dq"].get("missing_fields") or []
    if missing:
        emit(f"  [MISSING] XBRL fields not found: {', '.join(missing)}")

    # Overall
    n_fail = len(r["failures"])
    n_warn = len(r["warnings"])
    n_pass = len(r["passes"])
    if n_fail > 0:
        status = f"FAIL ({n_fail} failures, {n_warn} warnings, {n_pass} pass)"
    elif n_warn > 0:
        status = f"WARN ({n_warn} warnings, {n_pass} pass)"
    else:
        status = f"PASS ({n_pass} checks)"

    emit()
    emit(f"OVERALL: {status}")
    emit("=" * 60)

    if output is not None:
        try:
            output.write("\n".join(lines) + "\n")
        except Exception as e:
            print(f"  Warning: could not write to file: {e}")

    return n_fail == 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Oracle fact sheet validation harness -- validates data layer without panels"
    )
    parser.add_argument("tickers", nargs="*", help="Tickers to validate (e.g. LITE CSCO)")
    parser.add_argument(
        "--batch", action="store_true", help="Run all 5 known-answer tickers"
    )
    parser.add_argument(
        "--no-clear-cache", action="store_true", help="Skip cache clearing (faster, uses cached data)"
    )
    parser.add_argument(
        "--save", action="store_true", help="Save report to ~/ORACLE/validation/"
    )
    args = parser.parse_args()

    tickers = list(args.tickers)
    if args.batch:
        tickers = list(KNOWN_ANSWERS.keys())
    if not tickers:
        parser.print_help()
        sys.exit(1)

    save_dir = None
    if args.save:
        save_dir = ORACLE_DIR / "validation"
        save_dir.mkdir(parents=True, exist_ok=True)

    all_pass = True
    results = []

    for ticker in tickers:
        ticker = ticker.upper().strip()
        known = KNOWN_ANSWERS.get(ticker)
        desc = (known or {}).get("desc", "")
        if desc:
            print(f"\n[{ticker}] {desc}")

        result = run_validation(ticker, clear_cache=not args.no_clear_cache, known=known)

        fout = None
        if save_dir:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fout_path = save_dir / f"validate_{ticker}_{ts}.txt"
            fout = open(fout_path, "w", encoding="utf-8")

        ok = print_validation_report(result, output=fout)

        if fout:
            fout.close()
            print(f"  Saved to {fout_path}")

        if not ok:
            all_pass = False
        results.append((ticker, ok))

    if len(tickers) > 1:
        print()
        print("BATCH SUMMARY:")
        for t, ok in results:
            status = "OK  " if ok else "FAIL"
            known_ = KNOWN_ANSWERS.get(t, {})
            desc_ = known_.get("desc", "")
            print(f"  [{status}] {t}  {desc_}")
        print(f"\n  Result: {'ALL PASS' if all_pass else 'FAILURES FOUND'}")

    sys.exit(0 if all_pass else 1)
