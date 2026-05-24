#!/usr/bin/env python3
"""
ORACLE Factsheet Generator
Reads ONLY from local EDGAR files. Zero LLM. Zero training data.
Every figure cites its source file and filing date.

Usage: python3 generate_factsheet.py ANET
"""

import sys
import os
import re
import json
from pathlib import Path
from datetime import datetime, date

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / "ORACLE" / "config.json"
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

FILINGS_ROOT = Path(CONFIG["oracle_filings"])
TODAY = date.today()
TICKER = sys.argv[1].upper() if len(sys.argv) > 1 else "ANET"
TICKER_DIR = FILINGS_ROOT / TICKER
MANIFEST_PATH = TICKER_DIR / "manifest.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

def age_label(filing_date_str: str) -> str:
    try:
        fd = date.fromisoformat(filing_date_str)
        days = (TODAY - fd).days
        if days <= 90:   return f"{days}d — CURRENT"
        if days <= 180:  return f"{days}d — RECENT"
        return f"{days}d — STALE"
    except:
        return "UNKNOWN"

def read_file(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except:
        return ""

def extract_header(text: str) -> dict:
    """Pull filing date and period from === header lines."""
    info = {}
    for line in text.split("\n")[:8]:
        if "FILING DATE:" in line:
            info["filing_date"] = line.split("FILING DATE:")[-1].strip().rstrip("=").strip()
        if "PERIOD COVERED:" in line:
            info["period"] = line.split("PERIOD COVERED:")[-1].strip().rstrip("=").strip()
        if "SOURCE:" in line:
            info["source"] = line.split("SOURCE:")[-1].strip().rstrip("=").strip()
    return info

def find_dollar_amount(text: str, patterns: list) -> tuple:
    """
    Search text for first pattern match, return (value_str, context_line).
    Patterns are regex strings. Looks for $ amounts near each match.
    """
    for pat in patterns:
        matches = list(re.finditer(pat, text, re.IGNORECASE | re.MULTILINE))
        for m in matches:
            # Search in a window around the match for a dollar amount
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 200)
            window = text[start:end]
            # Look for patterns like $2,709 or $2.709 billion or 2,709,000
            amt = re.search(r'\$\s*([\d,]+(?:\.\d+)?)\s*(billion|million|thousand)?', window, re.IGNORECASE)
            if not amt:
                amt = re.search(r'([\d,]{4,}(?:\.\d+)?)\s*(billion|million)?', window)
            if amt:
                raw = amt.group(1).replace(",", "")
                try:
                    val = float(raw)
                    unit = (amt.group(2) or "").lower()
                    if unit == "billion": val *= 1000
                    elif unit == "million": pass
                    elif val > 1e8: val /= 1e6  # convert to millions if huge
                    context = window.replace("\n", " ").strip()[:120]
                    return (f"${val:,.1f}M", context)
                except:
                    pass
    return ("[NOT FOUND IN LOCAL FILE]", "")

def find_percent(text: str, patterns: list) -> tuple:
    for pat in patterns:
        matches = list(re.finditer(pat, text, re.IGNORECASE | re.MULTILINE))
        for m in matches:
            start = max(0, m.start() - 10)
            end = min(len(text), m.end() + 100)
            window = text[start:end]
            pct = re.search(r'([\d]+\.?\d*)\s*%', window)
            if pct:
                context = window.replace("\n", " ").strip()[:100]
                return (f"{pct.group(1)}%", context)
    return ("[NOT FOUND IN LOCAL FILE]", "")

def find_eps(text: str) -> tuple:
    patterns = [
        r'diluted\s+(?:net\s+income\s+per\s+share|earnings\s+per\s+share)',
        r'net\s+income\s+per\s+share.*?diluted',
        r'earnings\s+per\s+share.*?diluted',
        r'diluted\s+EPS',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            window = text[m.start():m.start()+300]
            eps = re.search(r'\$\s*([\d]+\.[\d]+)', window)
            if eps:
                context = window.replace("\n", " ").strip()[:100]
                return (f"${eps.group(1)}", context)
    return ("[NOT FOUND IN LOCAL FILE]", "")

def extract_financials_from_text(text: str, source_label: str) -> dict:
    """Extract key financial figures from 10-K or 10-Q full text."""
    results = {}

    def r(key, patterns, finder=find_dollar_amount):
        val, ctx = finder(text, patterns)
        results[key] = {"value": val, "context": ctx, "source": source_label}

    r("revenue", [
        r"total\s+net\s+revenue",
        r"net\s+revenue",
        r"total\s+revenue",
        r"revenue[,\s]",
    ])
    r("gross_profit", [
        r"gross\s+profit",
        r"total\s+gross\s+profit",
    ])
    r("operating_income", [
        r"income\s+from\s+operations",
        r"operating\s+income",
        r"total\s+operating\s+income",
    ])
    r("net_income", [
        r"net\s+income(?!\s+per)",
        r"net\s+earnings(?!\s+per)",
    ])
    r("rd_expense", [
        r"research\s+and\s+development",
        r"R&D\s+expense",
    ])
    r("sales_marketing", [
        r"sales\s+and\s+marketing",
        r"selling.*?marketing",
    ])
    r("cash", [
        r"cash\s+and\s+cash\s+equivalents",
        r"cash\s+equivalents",
    ])
    r("operating_cashflow", [
        r"net\s+cash\s+provided\s+by\s+operating",
        r"cash\s+from\s+operations",
        r"operating\s+activities",
    ])
    r("total_assets", [
        r"total\s+assets",
    ])
    r("total_liabilities", [
        r"total\s+liabilities",
    ])
    r("stockholders_equity", [
        r"total\s+stockholders[\'\s]+equity",
        r"stockholders[\'\s]+equity",
    ])
    r("long_term_debt", [
        r"long.?term\s+debt",
        r"long.?term\s+obligations",
        r"senior\s+notes",
    ])

    # Gross margin %
    val, ctx = find_percent(text, [r"gross\s+margin", r"gross\s+profit.*?%"])
    results["gross_margin_pct"] = {"value": val, "context": ctx, "source": source_label}

    # EPS
    val, ctx = find_eps(text)
    results["eps_diluted"] = {"value": val, "context": ctx, "source": source_label}

    return results

# ── Main ──────────────────────────────────────────────────────────────────────

def generate_factsheet(ticker: str) -> str:
    if not MANIFEST_PATH.exists():
        return f"ERROR: No manifest found for {ticker}. Run oracle_fetch.py {ticker} first."

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    # ── XBRL financial data ────────────────────────────────────────────────────
    sys.path.insert(0, str(Path(__file__).parent))
    from edgar_financials import get_all_financials, format_value, format_citation
    xbrl = get_all_financials(ticker)

    lines = []
    sources_used = []

    def h(title):
        lines.append(f"\n{'═'*60}")
        lines.append(f"  {title}")
        lines.append(f"{'═'*60}")

    def sub(title):
        lines.append(f"\n{'─'*40}")
        lines.append(f"  {title}")
        lines.append(f"{'─'*40}")

    def row(label, value, source="", note=""):
        src = f"  [Source: {source}]" if source else ""
        n = f"  [{note}]" if note else ""
        lines.append(f"  {label:<35} {value}{src}{n}")

    # ── HEADER ────────────────────────────────────────────────────────────────
    lines.append(f"\n{'█'*60}")
    lines.append(f"  ORACLE FACTSHEET — {ticker}")
    lines.append(f"  Generated: {TODAY.isoformat()} | Source: EDGAR local files + Alpaca")
    lines.append(f"  ZERO training data. Every figure cites its source file.")
    lines.append(f"{'█'*60}")

    # ── SECTION 1: COMPANY IDENTITY ───────────────────────────────────────────
    h("SECTION 1 — COMPANY IDENTITY")
    row("Company Name", manifest.get("company_name", "N/A"), "manifest.json")
    row("Ticker", ticker, "manifest.json")
    row("CIK", manifest.get("cik", "N/A"), "manifest.json")
    row("SIC Code", manifest.get("sic", "N/A"), "manifest.json")
    row("Industry", manifest.get("industry", "N/A"), "manifest.json")
    row("Fiscal Year End", manifest.get("fiscal_year_end", "N/A"), "manifest.json")
    row("Last EDGAR Check", manifest.get("last_checked", "N/A"), "manifest.json")

    # ── SECTION 2: LIVE PRICE (Alpaca) ────────────────────────────────────────
    h("SECTION 2 — LIVE PRICE DATA (Alpaca)")
    try:
        sys.path.insert(0, str(Path.home() / "ORACLE"))
        from data.oracle_data import format_fundamentals_batch
        price_block = format_fundamentals_batch([ticker], fresh=True)
        for line in price_block.split("\n"):
            if line.strip():
                lines.append(f"  {line}")
        lines.append(f"  [Source: Alpaca live data, {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC]")
    except Exception as e:
        lines.append(f"  Price unavailable: {e}")

    # ── SECTION 3: ANNUAL FINANCIALS (10-K) ───────────────────────────────────
    h("SECTION 3 — ANNUAL FINANCIALS (10-K)")
    cov_10k = manifest.get("coverage", {}).get("10-K", {})
    k_path = cov_10k.get("local_path", "")
    k_filed = cov_10k.get("newest_filing_date", "N/A")
    k_period = cov_10k.get("period_covered", "N/A")

    if k_path and (TICKER_DIR / k_path).exists():
        k_text = read_file(TICKER_DIR / k_path)
        k_label = f"10-K filed {k_filed}, period {k_period}"
        sources_used.append({"file": k_path, "filing_date": k_filed, "age": age_label(k_filed)})

        # Also try item8_financials section if it exists
        sec_dir = TICKER_DIR / "sections"
        item8_files = sorted(sec_dir.glob(f"*10-K*item8_financials*"), reverse=True)
        if item8_files:
            k_text_fin = read_file(item8_files[0])
            if len(k_text_fin) > 500:
                k_text = k_text_fin
                k_label = f"10-K item8 filed {k_filed}, period {k_period}"

        lines.append(f"\n  Filing: 10-K | Filed: {k_filed} | Period: {k_period} | Age: {age_label(k_filed)}")
        financials = extract_financials_from_text(k_text, k_label)
        for key, data in financials.items():
            label = key.replace("_", " ").title()
            row(label, data["value"], data["source"])
    else:
        lines.append("  10-K not found locally.")

    # ── SECTION 4: LATEST QUARTERLY FINANCIALS (10-Q) ─────────────────────────
    h("SECTION 4 — LATEST QUARTERLY FINANCIALS (10-Q)")
    cov_10q = manifest.get("coverage", {}).get("10-Q", {})
    q_files_raw = cov_10q.get("files", [])  # list of full path strings
    # Normalize to list of dicts
    q_files = []
    for qf in q_files_raw:
        p = Path(qf)
        name = p.name  # e.g. 2026-05-06_10-Q_Q3FY2026.txt
        parts = name.replace(".txt","").split("_")
        q_files.append({
            "local_path": qf,
            "filing_date": parts[0] if parts else "?",
            "quarter": parts[2] if len(parts) > 2 else "?",
        })

    if q_files:
        newest_q = q_files[0]
        q_path = newest_q.get("local_path", "")
        q_filed = newest_q.get("filing_date", "N/A")
        q_quarter = newest_q.get("quarter", "N/A")

        q_fullpath = Path(q_path) if q_path else None
        q_text = ""
        if q_fullpath and q_fullpath.exists():
            q_text = read_file(q_fullpath)

        # Also try item8 section
        sec_dir = TICKER_DIR / "sections"
        item8_q = sorted(sec_dir.glob(f"*{q_filed}*10-Q*item8*"), reverse=True)
        if item8_q and read_file(item8_q[0]):
            q_text = read_file(item8_q[0])

        if q_text:
            q_label = f"10-Q {q_quarter} filed {q_filed}"
            sources_used.append({"file": q_path, "filing_date": q_filed, "age": age_label(q_filed)})
            lines.append(f"\n  Filing: 10-Q {q_quarter} | Filed: {q_filed} | Age: {age_label(q_filed)}")
            financials = extract_financials_from_text(q_text, q_label)
            for key, data in financials.items():
                label = key.replace("_", " ").title()
                row(label, data["value"], data["source"])
        else:
            lines.append(f"  10-Q file not found locally for {q_filed}.")
    else:
        lines.append("  No 10-Q files in manifest.")

    # ── SECTION 5: REVENUE TREND (all 4 filings) ─────────────────────────────
    h("SECTION 5 — REVENUE TREND (10-K + 3x 10-Q)")
    lines.append("")

    # 10-K revenue
    if k_path and (TICKER_DIR / k_path).exists():
        k_text2 = read_file(TICKER_DIR / k_path)
        val, _ = find_dollar_amount(k_text2, [r"total\s+net\s+revenue", r"net\s+revenue", r"total\s+revenue"])
        lines.append(f"  FY Annual  ({k_period}) [10-K filed {k_filed}]: {val}")

    # Each 10-Q — use full paths
    for qf in q_files[:3]:
        qp = Path(qf.get("local_path", ""))
        qd = qf.get("filing_date", "N/A")
        qq = qf.get("quarter", "")
        if qp.exists():
            qt = read_file(qp)
            val, _ = find_dollar_amount(qt, [r"total\s+net\s+revenue", r"net\s+revenue", r"total\s+revenue"])
            lines.append(f"  {qq} [10-Q filed {qd}]: {val}")

    # ── SECTION 6: MD&A KEY POINTS ────────────────────────────────────────────
    h("SECTION 6 — MD&A KEY POINTS (verbatim from filing)")
    sec_dir = TICKER_DIR / "sections"
    # Use manifest sections for MDA — prefer 10-Q MDA from full file, then 10-K section
    mda_text = ""
    mda_source = ""
    # Try newest 10-Q full file first (it has MD&A as Item 2 in 10-Qs)
    if q_files:
        qp = Path(q_files[0].get("local_path", ""))
        if qp.exists():
            mda_text = read_file(qp)
            mda_source = f"10-Q {q_files[0]['quarter']} filed {q_files[0]['filing_date']}"
    # Fallback to 10-K MDA section
    if not mda_text:
        mda_section = manifest.get("sections", {}).get("newest_mda", "")
        if mda_section and Path(mda_section).exists():
            mda_text = read_file(Path(mda_section))
            mda_source = f"10-K MDA section filed {k_filed}"

    if mda_text:
        lines.append(f"\n  Source: {mda_source}")
        keywords = [
            r'\d+[\.\d]*\s*%.*?(growth|increase|decrease|decline)',
            r'(guidance|outlook|expect|forecast|anticipate)',
            r'gross\s+margin',
            r'revenue.*?(grew|increased|decreased|declined)',
            r'(headcount|employees|hiring)',
            r'(customer|product|segment).*?revenue',
        ]
        found = []
        for sent in re.split(r'(?<=[.!?])\s+', mda_text):
            sent = sent.strip().replace("\n", " ")
            if len(sent) < 30 or len(sent) > 400:
                continue
            for kw in keywords:
                if re.search(kw, sent, re.IGNORECASE):
                    found.append(sent)
                    break
            if len(found) >= 10:
                break
        for i, s in enumerate(found, 1):
            lines.append(f"\n  [{i}] {s}")
            lines.append(f"      [MD&A, {mda_source}]")
    else:
        lines.append("  MD&A section not found in local files.")

    # ── SECTION 7: RISK FACTORS ───────────────────────────────────────────────
    h("SECTION 7 — RISK FACTORS SUMMARY (verbatim)")
    rf_files = sorted(sec_dir.glob("*item1A_riskfactors*"), reverse=True)
    rf_text = ""
    rf_source = ""
    if rf_files:
        rf_text = read_file(rf_files[0])
        rf_source = rf_files[0].name
        hdr = extract_header(rf_text)
        rf_source_label = f"{rf_files[0].name} (filed {hdr.get('filing_date','?')})"
        sources_used.append({"file": rf_files[0].name, "filing_date": hdr.get("filing_date","?"), "age": age_label(hdr.get("filing_date","2000-01-01"))})

    if rf_text:
        lines.append(f"\n  Source: {rf_source_label}")
        # Find the bullet-point summary section
        summary_match = re.search(r'Risk Factor Summary(.*?)(?:Risks Related to our Business|ITEM 1B|\Z)', rf_text, re.DOTALL | re.IGNORECASE)
        if summary_match:
            bullets = re.findall(r'[•\-\*]\s+(.+)', summary_match.group(1))
            for i, b in enumerate(bullets[:15], 1):
                lines.append(f"  {i:2d}. {b.strip()[:150]}")
        else:
            # Just grab first 15 bullet points from anywhere
            bullets = re.findall(r'[•\-\*]\s+(.+)', rf_text)
            for i, b in enumerate(bullets[:15], 1):
                lines.append(f"  {i:2d}. {b.strip()[:150]}")
    else:
        lines.append("  Risk factors not found in local files.")

    # ── SECTION 8: RECENT 8-K EVENTS ─────────────────────────────────────────
    h("SECTION 8 — RECENT 8-K EVENTS (newest first)")
    cov_8k = manifest.get("coverage", {}).get("8-K", {})
    eight_k_files = cov_8k.get("files", [])

    if eight_k_files:
        for ev in eight_k_files[:10]:
            fd = ev.get("filing_date", "?")
            etype = ev.get("type", "other").upper()
            # Use ex991 path if available, else primary
            read_path = Path(ev.get("ex991_path", "") or ev.get("local_path", ""))
            preview = ""
            if read_path and read_path.exists():
                raw = read_file(read_path)
                body_lines = [l for l in raw.split("\n") if l.strip() and not l.startswith("===")]
                preview = " ".join(body_lines[:5]).strip()[:200]
                sources_used.append({"file": read_path.name, "filing_date": fd, "age": age_label(fd)})

            lines.append(f"\n  {fd} | {etype}")
            if preview:
                lines.append(f"  Preview: {preview}")
    else:
        lines.append("  No 8-K files in manifest.")

    # ── SECTION 9: INSIDER TRANSACTIONS ───────────────────────────────────────
    h("SECTION 9 — INSIDER TRANSACTIONS (Form 4, last 12 months)")
    cov_f4 = manifest.get("coverage", {}).get("Form4", {})
    transactions = cov_f4.get("transactions", [])

    if transactions:
        # Group by person
        by_person = {}
        for t in transactions:
            person = t.get("person", "Unknown")
            if person not in by_person:
                by_person[person] = {"buys": 0, "sells": 0, "buy_val": 0, "sell_val": 0, "last_date": "", "last_price": 0, "title": ""}
            p = by_person[person]
            ttype = t.get("type", "OTHER")
            try:
                shares = int(float(t.get("shares", 0) or 0))
            except (ValueError, TypeError):
                shares = 0
            try:
                price = float(t.get("price", 0) or 0)
            except (ValueError, TypeError):
                price = 0
            fd = t.get("filing_date", "")
            if ttype == "BUY":
                p["buys"] += shares
                p["buy_val"] += shares * price
            elif ttype == "SELL":
                p["sells"] += shares
                p["sell_val"] += shares * price
            if fd > p["last_date"]:
                p["last_date"] = fd
                p["last_price"] = price

        lines.append(f"\n  {'Person':<30} {'Sold Shares':>12} {'Sold $':>12} {'Bought':>10} {'Last':>12} {'Pattern':<12}")
        lines.append(f"  {'-'*90}")
        for person, d in sorted(by_person.items(), key=lambda x: x[1]["sells"], reverse=True)[:15]:
            pattern = "NET SELLER" if d["sells"] > d["buys"] else ("NET BUYER" if d["buys"] > 0 else "OTHER")
            lines.append(f"  {person:<30} {d['sells']:>12,} ${d['sell_val']:>11,.0f} {d['buys']:>10,} {d['last_date']:>12} {pattern:<12}")
    else:
        lines.append("  No Form 4 transactions in manifest.")

    # ── SECTION 10: DATA INTEGRITY ────────────────────────────────────────────
    h("SECTION 10 — DATA INTEGRITY REPORT")
    lines.append(f"\n  Ticker:           {ticker}")
    lines.append(f"  Factsheet date:   {TODAY.isoformat()}")
    lines.append(f"  Manifest updated: {manifest.get('last_checked','?')}")
    lines.append(f"  Newest filing:    {manifest.get('newest_filing_date','?')}")
    lines.append(f"\n  {'File':<50} {'Filed':>12} {'Age'}")
    lines.append(f"  {'-'*80}")

    seen = set()
    for s in sources_used:
        key = s["file"]
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  {str(s['file']):<50} {s['filing_date']:>12}  {s['age']}")

    lines.append(f"\n  Total source files referenced: {len(seen)}")

    # Check for any NOT FOUND
    not_found = []
    for line in lines:
        if "NOT FOUND IN LOCAL FILE" in line:
            not_found.append(line.strip())
    if not_found:
        lines.append(f"\n  MISSING FIGURES ({len(not_found)} not extractable from local files):")
        for nf in not_found:
            lines.append(f"    {nf[:100]}")
    else:
        lines.append("\n  All key figures extracted from local files.")

    lines.append(f"\n{'█'*60}")
    lines.append(f"  END OF FACTSHEET — {ticker} — {TODAY.isoformat()}")
    lines.append(f"{'█'*60}\n")

    return "\n".join(lines)


if __name__ == "__main__":
    factsheet = generate_factsheet(TICKER)
    print(factsheet)

    # Save to disk
    out_path = TICKER_DIR / f"{TICKER}_factsheet_{TODAY.strftime('%Y%m%d')}.txt"
    out_path.write_text(factsheet, encoding="utf-8")
    print(f"\nSaved to: {out_path}", file=sys.stderr)
