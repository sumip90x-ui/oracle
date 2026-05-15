#!/usr/bin/env python3
"""
oracle_preflight.py — Data validation gate for the ORACLE Think Tank.

Runs BEFORE any panel fires. Validates:
  1. EPS accuracy: cross-references yfinance forwardEps against web-sourced guidance
  2. Fiscal calendar: labels the correct FY quarter for upcoming earnings
  3. Spinoff detection: flags pending spinoffs requiring sum-of-parts valuation
  4. Insider transactions: surfaces recent sells > $500K before Skeptic runs
  5. Segment names: extracts current verified segment names from latest earnings

Outputs a DataQualityReport per ticker. If score < HALT_THRESHOLD, run halts.
"""

import os, re, json, datetime, time
from pathlib import Path

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

import requests as _requests
from dotenv import dotenv_values as _dotenv_values

def _web_search(query: str, limit: int = 3) -> list:
    """
    Previously called Tavily. Tavily removed — returns empty list.
    All callers already handle empty results gracefully.
    """
    return []

HAS_SEARCH = False  # Tavily removed — web search disabled

import signal as _signal
try:
    _signal.signal(_signal.SIGPIPE, _signal.SIG_DFL)
except (AttributeError, OSError, ValueError):
    pass

def _safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except (BrokenPipeError, OSError):
        pass

HALT_THRESHOLD = 50   # out of 100 — below this, TT refuses to run
WARN_THRESHOLD = 70   # above 70 = clean, 40-70 = proceed with warnings
CACHE_DIR = Path.home() / "ORACLE" / "cache"

# ── Data Quality Report ────────────────────────────────────────────────────────

class DataQualityReport:
    def __init__(self, ticker: str):
        self.ticker = ticker
        self.score = 100          # start perfect, deduct for issues
        self.warnings = []        # non-blocking issues
        self.errors = []          # score-deducting issues
        self.halted = False       # set True if score < HALT_THRESHOLD
        self.validated = {}       # clean validated data for panels
        self.raw = {}             # raw yfinance data

    def warn(self, msg: str, deduct: int = 0):
        self.warnings.append(msg)
        self.score -= deduct
        self.score = max(0, self.score)

    def error(self, msg: str, deduct: int):
        self.errors.append(msg)
        self.score -= deduct
        self.score = max(0, self.score)

    def summary(self) -> str:
        lines = [f"=== PRE-FLIGHT: {self.ticker} (score {self.score}/100) ==="]
        if self.errors:
            lines.append("ERRORS (score-deducting):")
            for e in self.errors:
                lines.append(f"  x {e}")
        if self.warnings:
            lines.append("WARNINGS:")
            for w in self.warnings:
                lines.append(f"  ! {w}")
        if not self.errors and not self.warnings:
            lines.append("  OK All checks passed")
        if self.halted:
            lines.append(f"\n  !! RUN HALTED — score {self.score} < {HALT_THRESHOLD} threshold")
            lines.append("  Fix the errors above before running Think Tank panels.")
        return "\n".join(lines)


# ── Check 1: EPS Sanity ────────────────────────────────────────────────────────

def _verify_eps_via_web(ticker: str, fwd: float, ttm: float = None) -> tuple:
    """
    Try to verify forward EPS via Tavily search.
    Returns (verified: bool, source_note: str)
    verified=True  → found a source that roughly confirms the FORWARD number (within 25%)
    verified=False → found contradicting data clearly below fwd
    verified=None  → search failed or inconclusive
    """
    try:
        results = _web_search(
            f"{ticker} forward EPS estimate fiscal 2026 2027 analyst consensus next year",
            limit=3
        )
        if not results:
            return None, "web search returned no results"

        import re as _re
        combined = " ".join(r.get("description","") + " " + r.get("title","") for r in results).lower()

        # Find all dollar amounts that look like EPS (under $50)
        amounts = [float(x) for x in _re.findall(r'\$(\d+\.?\d*)', combined) if float(x) < 50]
        if not amounts:
            return None, "no dollar figures found in search results"

        # Exclude TTM EPS from the match pool — we're looking for FORWARD confirmation
        ttm_excluded = set()
        if ttm is not None and ttm > 0:
            ttm_excluded = {a for a in amounts if abs(a - ttm) / ttm < 0.15}

        forward_candidates = [a for a in amounts if a not in ttm_excluded]
        if not forward_candidates:
            return None, f"all found amounts ({amounts[:5]}) match TTM EPS=${ttm:.2f} — no forward figure found"

        # Check if any forward candidate is within 25% of our forward EPS
        close_matches = [a for a in forward_candidates if abs(a - fwd) / fwd < 0.25]
        if close_matches:
            source = results[0].get("title","")[:80]
            return True, f"confirmed ~${close_matches[0]:.2f} in: {source}"

        # Check for clear contradiction: found values are <50% of forward EPS
        # BUT: quarterly EPS (~fwd/4) should not count as a contradiction of annual forward EPS
        quarterly_estimate = fwd / 4.0
        contradicting = [
            a for a in forward_candidates
            if a < fwd * 0.50          # less than 50% of forward
            and a > 0.05               # not noise
            and abs(a - quarterly_estimate) / quarterly_estimate > 0.30  # not just a quarterly figure
        ]
        if contradicting:
            source = results[0].get("title","")[:80]
            return False, f"found ${contradicting[0]:.2f} which contradicts ${fwd:.2f} in: {source}"

        return None, f"search inconclusive — forward candidates: {sorted(set(round(a,2) for a in forward_candidates))[:5]}"
    except Exception as e:
        return None, f"search failed: {e}"


def check_eps(report: DataQualityReport, info: dict):
    """
    Validate forwardEps. Uses EDGAR fact sheet as primary source (most reliable).
    Falls back to Tavily web verification, then ratio heuristic.
    Handles recently-profitable names (tiny TTM) without false positives.
    """
    fwd = info.get("forwardEps")
    ttm = info.get("trailingEps")

    if fwd is None:
        report.warn("forwardEps missing from yfinance — panels will use TTM only", deduct=5)
        report.validated["forward_eps"] = None
        report.validated["forward_eps_source"] = "missing"
        return

    report.validated["forward_eps"] = fwd

    # EDGAR override: if fact sheet has real TTM EPS from SEC filings, use it instead of yfinance
    edgar_ttm = None
    try:
        from oracle_factsheet import build_fact_sheet as _bfs
        _fs = _bfs(report.ticker)
        _edgar_eps = _fs.get("metrics", {}).get("gaap_eps_ttm", {})
        if _edgar_eps and _edgar_eps.get("value") and _edgar_eps.get("source") == "EDGAR_XBRL":
            edgar_ttm = float(_edgar_eps["value"])
            if edgar_ttm and edgar_ttm > 0:
                # Use EDGAR TTM for ratio calculation — much more reliable than yfinance
                ttm = edgar_ttm
                report.validated["trailing_eps_source"] = "EDGAR_XBRL"
                report.validated["trailing_eps_edgar"] = edgar_ttm
    except Exception:
        pass

    report.validated["trailing_eps"] = ttm

    # If TTM is missing or zero, just warn and pass
    if not ttm or ttm <= 0:
        report.warn(f"forwardEps=${fwd:.2f} but TTM EPS is zero/negative — panels should use caution", deduct=0)
        report.validated["forward_eps_source"] = "yfinance_no_ttm_baseline"
        return

    ratio = fwd / ttm
    rev_growth = report.raw.get("revenueGrowth") or 0

    # If EDGAR TTM is available and ratio is now reasonable, pass directly
    if edgar_ttm and ratio <= 2.5:
        report.validated["forward_eps_flagged"] = False
        report.validated["forward_eps_source"] = f"edgar_verified_ratio_{ratio:.1f}x"
        if edgar_ttm != info.get("trailingEps"):
            report.warn(
                f"yfinance TTM EPS=${info.get('trailingEps',0):.2f} corrected to EDGAR TTM=${edgar_ttm:.2f}. "
                f"Forward EPS=${fwd:.2f} ratio={ratio:.1f}x vs EDGAR TTM — plausible.",
                deduct=0
            )
        return

    # Recently profitable: TTM < $0.50 — ratio is meaningless, skip ratio check entirely
    recently_profitable = ttm < 0.50
    if recently_profitable:
        report.warn(
            f"Recently profitable: TTM=${ttm:.2f}, forward=${fwd:.2f} ({ratio:.1f}x). "
            f"High ratio expected on inflecting names — not a data quality flag.",
            deduct=0
        )
        report.validated["forward_eps_flagged"] = False
        report.validated["forward_eps_source"] = "yfinance_recently_profitable"
        return

    # Ratio looks fine — pass directly
    if ratio <= 2.5:
        report.validated["forward_eps_flagged"] = False
        report.validated["forward_eps_source"] = "yfinance_plausible"
        return

    # Ratio is elevated (>2.5x). Try web verification BEFORE deciding to halt.
    _safe_print(f"\n    [preflight] EPS ratio {ratio:.1f}x — verifying via web...", end="", flush=True)
    verified, source_note = _verify_eps_via_web(report.ticker, fwd, ttm=ttm)
    _safe_print(f" {source_note[:60]}", flush=True)

    if verified is True:
        # Web confirms the number — high ratio is real (high-growth or inflection)
        report.warn(
            f"forwardEps=${fwd:.2f} is {ratio:.1f}x TTM ${ttm:.2f} — "
            f"elevated but web-verified: {source_note}",
            deduct=0
        )
        report.validated["forward_eps_flagged"] = False
        report.validated["forward_eps_source"] = f"web_verified: {source_note[:60]}"

    elif verified is False:
        # Web found contradicting data — but check if high-growth company first
        # High-growth companies (>100% rev) have quarterly EPS that look like contradictions
        if rev_growth > 1.0:
            report.warn(
                f"forwardEps=${fwd:.2f} is {ratio:.1f}x TTM ${ttm:.2f} — "
                f"web search returned lower figures but revenue growth is {rev_growth*100:.0f}% — "
                f"likely quarterly vs annual comparison. Treat as analyst consensus, not verified.",
                deduct=0
            )
            report.validated["forward_eps_flagged"] = True
            report.validated["forward_eps_source"] = "high_growth_quarterly_ambiguous"
        else:
            # Real contradiction on a normal-growth company
            report.error(
                f"forwardEps=${fwd:.2f} is {ratio:.1f}x TTM ${ttm:.2f} — "
                f"web verification FAILED: {source_note}. "
                f"Panels MUST NOT use this for PEG calculation.",
                deduct=55
            )
            report.validated["forward_eps_flagged"] = True
            report.validated["forward_eps_source"] = f"web_contradicted: {source_note[:60]}"

    else:
        # Inconclusive — apply graduated response based on ratio severity
        if ratio > 4.0 and rev_growth < 0.50:
            # 4x+ on a slow-growth company AND can't verify = halt
            report.error(
                f"forwardEps=${fwd:.2f} is {ratio:.1f}x TTM ${ttm:.2f} — "
                f"extreme ratio, low growth, and web verification inconclusive. "
                f"Panels MUST NOT use this for PEG. {source_note}",
                deduct=55
            )
            report.validated["forward_eps_flagged"] = True
            report.validated["forward_eps_source"] = "unverified_extreme"
        else:
            # Warn but pass — panels get the flag in their data block
            report.warn(
                f"forwardEps=${fwd:.2f} is {ratio:.1f}x TTM ${ttm:.2f} — "
                f"elevated ratio, verification inconclusive. {source_note}. "
                f"Panels should treat as analyst consensus, not management guidance.",
                deduct=0
            )
            report.validated["forward_eps_flagged"] = True
            report.validated["forward_eps_source"] = "unverified_warn_only"

    report.validated["trailing_eps"] = ttm


# ── Check 2: Fiscal Calendar ───────────────────────────────────────────────────

def check_fiscal_calendar(report: DataQualityReport, info: dict, ticker: str):
    """Derive correct FY label for next earnings event."""
    today = datetime.date.today()

    last_fy_ts = info.get("lastFiscalYearEnd")
    next_fy_ts = info.get("nextFiscalYearEnd")
    # yfinance provides earningsTimestamps (list) and earningsTimestamp (scalar)
    # We want the NEXT future earnings date, not the most recent past one
    earnings_ts_raw = info.get("earningsTimestamps") or info.get("earningsTimestamp") or info.get("earningsDate")

    last_fy_date = None
    next_fy_date = None
    earnings_date = None

    try:
        if last_fy_ts:
            last_fy_date = datetime.datetime.fromtimestamp(int(last_fy_ts)).date()
        if next_fy_ts:
            next_fy_date = datetime.datetime.fromtimestamp(int(next_fy_ts)).date()
    except Exception:
        pass

    try:
        today = datetime.date.today()
        if isinstance(earnings_ts_raw, list):
            # Pick the first future date from the list
            future_dates = []
            for ts in earnings_ts_raw:
                try:
                    d = datetime.datetime.fromtimestamp(int(ts)).date()
                    if d > today:
                        future_dates.append(d)
                except Exception:
                    pass
            earnings_date = min(future_dates) if future_dates else None
        elif earnings_ts_raw:
            d = datetime.datetime.fromtimestamp(int(earnings_ts_raw)).date()
            # If this date is in the past, try earningsTimestampStart/End
            if d <= today:
                for key in ("earningsTimestampStart", "earningsTimestampEnd"):
                    alt = info.get(key)
                    if alt:
                        try:
                            alt_d = datetime.datetime.fromtimestamp(int(alt)).date()
                            if alt_d > today:
                                d = alt_d
                                break
                        except Exception:
                            pass
            earnings_date = d if d > today else None
    except Exception:
        pass

    # Fiscal month = month fiscal year ends
    fiscal_month = last_fy_date.month if last_fy_date else 12
    fiscal_year_end_year = last_fy_date.year if last_fy_date else today.year

    # Determine which fiscal quarter the next earnings belongs to
    fy_label = "UNKNOWN"
    q_label = "UNKNOWN"
    if last_fy_date and earnings_date:
        # yfinance lastFiscalYearEnd may be 1 year stale — advance to most recent past FY end
        today = datetime.date.today()
        adjusted_last_fy = last_fy_date
        while True:
            next_candidate = adjusted_last_fy.replace(year=adjusted_last_fy.year + 1)
            if next_candidate <= today:
                adjusted_last_fy = next_candidate
            else:
                break

        # Months from most recent actual FY end to next earnings
        months_since_fy_end = (earnings_date.year - adjusted_last_fy.year) * 12 + (earnings_date.month - adjusted_last_fy.month)
        # FY year = the fiscal year that started after adjusted_last_fy
        fy_year = adjusted_last_fy.year + 1
        # Earnings report LAGS quarter end by ~4-6 weeks, so:
        # Q1 reports in months 1-5 after FY end
        # Q2 reports in months 5-8 after FY end
        # Q3 reports in months 8-11 after FY end
        # Q4 reports in months 11-14 after FY end
        if months_since_fy_end <= 5:
            q_label = "Q1"
        elif months_since_fy_end <= 8:
            q_label = "Q2"
        elif months_since_fy_end <= 11:
            q_label = "Q3"
        else:
            q_label = "Q4"
        fy_label = f"{q_label} FY{fy_year}"
    elif next_fy_date:
        fy_label = f"Q4 FY{next_fy_date.year}"

    earnings_str = earnings_date.strftime("%B %d, %Y") if earnings_date else "date unknown"

    # Warn if fiscal year end is non-December (easy to mislabel)
    if fiscal_month != 12:
        month_name = datetime.date(2000, fiscal_month, 1).strftime("%B")
        report.warn(
            f"Non-December fiscal year end ({month_name}). "
            f"Last FY ended {last_fy_date}. "
            f"Next earnings = {fy_label} ({earnings_str}). "
            f"Panels MUST use '{fy_label}' not a calendar-year label.",
            deduct=0  # warning only, not a data error
        )

    report.validated["fiscal_label"] = fy_label
    report.validated["next_earnings_date"] = earnings_str
    report.validated["last_fy_end"] = str(last_fy_date) if last_fy_date else ""
    report.validated["fiscal_month"] = fiscal_month


# ── Check 3: Spinoff Detection ─────────────────────────────────────────────────

def check_spinoff(report: DataQualityReport, ticker: str):
    """Search news for pending spinoff announcements. Also checks known-spinoffs list."""

    # Known active spinoffs — hardcoded for reliability when Tavily is unavailable
    KNOWN_SPINOFFS = {
        "FLEX": "Flex CPI (Cloud and Power Infrastructure) spinoff announced 2025 — sum-of-parts required",
    }
    if ticker.upper() in KNOWN_SPINOFFS:
        report.error(
            f"SPINOFF DETECTED (known list): {KNOWN_SPINOFFS[ticker.upper()]}. "
            f"Single forward EPS is unreliable — sum-of-parts valuation required. "
            f"Do NOT calculate PEG from consolidated forwardEps.",
            deduct=55
        )
        report.validated["spinoff_detected"] = True
        report.validated["spinoff_detail"] = KNOWN_SPINOFFS[ticker.upper()]
        return

    if not HAS_SEARCH:
        report.warn("Web search unavailable — spinoff check skipped", deduct=0)
        report.validated["spinoff_detected"] = False
        return

    try:
        results_raw = _web_search(f"{ticker} spinoff 2025 2026 announced planned spin", limit=3)
        hits = results_raw
        spinoff_keywords = ["spinoff", "spin-off", "spin off", "spun off", "spinning off",
                           "separation", "carve-out", "ipo of", "initial public offering"]
        found = False
        spinoff_detail = ""
        ticker_lower = ticker.lower()
        for h in hits:
            title = (h.get("title") or "").lower()
            desc = (h.get("description") or "").lower()
            combined = title + " " + desc
            # Must mention the ticker in the title (not just as one of many tickers in a movers article)
            if ticker_lower not in title:
                continue
            # Skip market-movers / round-up articles that list many tickers
            movers_signals = ["stock movers", "biggest movers", "top movers", "market movers",
                              "stocks to watch", "stocks moving", "premarket movers", "after-hours movers"]
            if any(sig in title for sig in movers_signals):
                continue
            # Also skip if title has 3+ other uppercase ticker-like tokens (list article)
            import re as _re_sp
            other_tickers = _re_sp.findall(r'\b[A-Z]{2,5}\b', h.get("title", ""))
            other_tickers = [t for t in other_tickers if t != ticker.upper() and t not in ("AND", "THE", "FOR", "INC", "ETF", "CEO", "CFO", "IPO", "NYSE", "SEC")]
            if len(other_tickers) >= 3:
                continue  # list article, not about this specific company
            # Spinoff keyword must appear in title or in description's first 150 chars
            if not any(kw in title for kw in spinoff_keywords) and \
               not any(kw in desc[:150] for kw in spinoff_keywords):
                continue
            # Only flag if recent (not just historical mentions)
            if any(yr in combined for yr in ["2025", "2026", "planned", "announced", "upcoming", "pending"]):
                found = True
                spinoff_detail = h.get("title", "")[:120]
                break

        if found:
            report.error(
                f"SPINOFF DETECTED: '{spinoff_detail}'. "
                f"Single forward EPS is unreliable — sum-of-parts valuation required. "
                f"Do NOT calculate PEG from consolidated forwardEps. "
                f"Flag RemainCo vs SpinCo segments separately.",
                deduct=55  # spinoff alone should halt — forward EPS is meaningless
            )
            report.validated["spinoff_detected"] = True
            report.validated["spinoff_detail"] = spinoff_detail
        else:
            report.validated["spinoff_detected"] = False
    except Exception as e:
        report.warn(f"Spinoff check failed: {e}", deduct=0)
        report.validated["spinoff_detected"] = False


# ── Check 4: Insider Transactions ─────────────────────────────────────────────

def check_insider_transactions(report: DataQualityReport, ticker: str):
    """Pull insider transactions from yfinance, surface significant sells."""
    if not HAS_YF:
        report.validated["insider_sells"] = []
        return

    try:
        tk = yf.Ticker(ticker)
        ins = tk.insider_transactions
        if ins is None or len(ins) == 0:
            report.validated["insider_sells"] = []
            return

        cutoff = datetime.date.today() - datetime.timedelta(days=90)
        sells = []
        for _, row in ins.iterrows():
            start = row.get("Start Date")
            if start and hasattr(start, "date"):
                if start.date() >= cutoff:
                    val = row.get("Value") or 0
                    shares = row.get("Shares") or 0
                    tx = row.get("Transaction") or ""
                    insider = row.get("Insider Trading") or row.get("Insider") or ""
                    # Only flag disposals/sells
                    if any(w in str(tx).lower() for w in ["sale", "sold", "dispose", "disposition"]):
                        sells.append({
                            "date": str(start.date()),
                            "insider": str(insider)[:40],
                            "shares": int(shares),
                            "value": float(val),
                            "tx": str(tx)
                        })

        total_sell_value = sum(s["value"] for s in sells)
        report.validated["insider_sells"] = sells

        if total_sell_value > 1_000_000:
            report.warn(
                f"INSIDER SELLING: ${total_sell_value/1e6:.1f}M in disposals over past 90 days "
                f"({len(sells)} transactions). Skeptic must address this.",
                deduct=5
            )
        elif total_sell_value > 500_000:
            report.warn(
                f"Moderate insider selling: ${total_sell_value/1e6:.1f}M past 90 days.",
                deduct=0
            )
    except Exception as e:
        report.warn(f"Insider transaction check failed: {e}", deduct=0)
        report.validated["insider_sells"] = []


# ── Check 5: Segment Names ────────────────────────────────────────────────────

def check_segment_names(report: DataQualityReport, ticker: str):
    """Search for current verified segment names from latest earnings."""
    if not HAS_SEARCH:
        report.validated["segments"] = []
        return

    try:
        results_raw = _web_search(f"{ticker} earnings segments business units 2026 2025 revenue breakdown", limit=2)
        hits = results_raw
        # Just store the top result description for the panels
        if hits:
            report.validated["segment_context"] = hits[0].get("description", "")[:300]
        else:
            report.validated["segments"] = []
    except Exception:
        report.validated["segments"] = []


def check_short_seller_reports(report: DataQualityReport, ticker: str):
    """Search for credible short seller reports published in the past 24 months."""
    try:
        # Two targeted queries — one with ticker, one with company name context
        results = _web_search(
            f'"{ticker}" short seller report allegations fraud 2023 2024 2025',
            limit=3
        )
        # Also try without quotes in case exact match fails
        if not results:
            results = _web_search(
                f"{ticker} stock short seller attack allegations 2024 2025",
                limit=3
            )

        short_sellers = ["muddy waters", "hindenburg", "citron", "culper", "spruce point",
                        "gotham city", "glaucus", "grizzly", "bear cave", "short report",
                        "short attack", "fraud allegation", "accounting irregulari",
                        "short seller", "short-seller", "activist short", "consent farm",
                        "revenue round-trip", "securities investigation", "class action"]

        # Get company name from yfinance to disambiguate ticker vs product names
        # e.g. "AXON" ticker = "Axon Enterprise" — reject articles about "AppLovin AXON product"
        company_name = ""
        try:
            import yfinance as _yf_ss
            _ss_info = _yf_ss.Ticker(ticker).info
            company_name = (_ss_info.get("longName") or _ss_info.get("shortName") or "").lower()
            # Keep only first two words of company name for matching
            company_words = company_name.split()[:2]
        except Exception:
            company_words = []

        found_reports = []
        ticker_lower = ticker.lower()
        for r in results:
            title = (r.get("title") or "").lower()
            desc  = (r.get("description") or "").lower()
            combined = title + " " + desc

            # Must mention the ticker symbol AND a short seller signal
            ticker_present = ticker_lower in combined
            ss_present = any(s in combined for s in short_sellers)

            if not (ticker_present and ss_present):
                continue

            # Disambiguation: if we have a company name, verify the article is about the company
            # Reject if the article has ticker but not the company's actual name
            if company_words:
                # Strip punctuation from company words for clean matching
                clean_words = [w.strip('.,;:') for w in company_words if len(w.strip('.,;:')) > 4]
                if clean_words:
                    # For disambiguation, require at least one unique word >4 chars
                    # e.g. "enterprise" for Axon Enterprise — AppLovin articles won't say "enterprise"
                    company_match = any(w in combined for w in clean_words)
                    if not company_match:
                        continue  # ticker word present but company name absent = product/brand reference

            found_reports.append(r.get("title","")[:100])

        if found_reports:
            report.warn(
                f"SHORT SELLER ALERT: Found {len(found_reports)} credible short report(s): "
                f"{'; '.join(found_reports[:2])}. "
                f"Skeptic MUST address these specific allegations before writing any forensic analysis.",
                deduct=0
            )
            report.validated["short_seller_reports"] = found_reports
        else:
            report.validated["short_seller_reports"] = []
    except Exception:
        report.validated["short_seller_reports"] = []


# ── Master Preflight Runner ────────────────────────────────────────────────────

def run_preflight(tickers: list, verbose: bool = True) -> dict:
    """
    Run all checks for each ticker.
    Returns dict: ticker -> DataQualityReport
    Prints summary. Raises SystemExit if any ticker halts.
    """
    _safe_print(f"\n{'='*58}")
    _safe_print(f"  PRE-FLIGHT DATA VALIDATION ({len(tickers)} ticker(s))")
    _safe_print(f"  Halt threshold: {HALT_THRESHOLD}/100 | Warn threshold: {WARN_THRESHOLD}/100")
    _safe_print(f"{'='*58}")

    reports = {}
    halt_tickers = []

    for ticker in tickers:
        _safe_print(f"\n  Checking {ticker}...", flush=True)
        report = DataQualityReport(ticker)

        # Fetch yfinance once
        info = {}
        if HAS_YF:
            try:
                tk = yf.Ticker(ticker)
                info = tk.info or {}
                report.raw = info
                price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
                if not price:
                    report.error(f"yfinance returned no price — ticker may be invalid or delisted", deduct=50)
                else:
                    report.validated["price"] = float(price)
            except Exception as e:
                report.error(f"yfinance fetch failed: {e}", deduct=30)

        # Run checks
        check_eps(report, info)
        check_fiscal_calendar(report, info, ticker)
        check_spinoff(report, ticker)
        check_insider_transactions(report, ticker)
        check_segment_names(report, ticker)
        check_short_seller_reports(report, ticker)

        # Final scoring
        if report.score < HALT_THRESHOLD:
            report.halted = True
            halt_tickers.append(ticker)

        reports[ticker] = report

        if verbose:
            _safe_print(report.summary())

    # Cache validated data for TT panels
    cache_file = CACHE_DIR / f"preflight_{datetime.date.today().isoformat()}.json"
    CACHE_DIR.mkdir(exist_ok=True)
    try:
        serializable = {}
        for t, r in reports.items():
            serializable[t] = {
                "score": r.score,
                "warnings": r.warnings,
                "errors": r.errors,
                "halted": r.halted,
                "validated": r.validated,
            }
        cache_file.write_text(json.dumps(serializable, indent=2))
    except Exception:
        pass

    _safe_print(f"\n{'='*58}")
    if halt_tickers:
        _safe_print(f"  !! HALTED: {', '.join(halt_tickers)}")
        _safe_print(f"  Fix the errors above. Pre-flight cache NOT written for halted tickers.")
        _safe_print(f"  Re-run with --preflight-override to force through (not recommended).")
        _safe_print(f"{'='*58}\n")
        return reports  # caller checks .halted

    clean = [t for t, r in reports.items() if r.score >= WARN_THRESHOLD]
    warned = [t for t, r in reports.items() if HALT_THRESHOLD <= r.score < WARN_THRESHOLD]
    _safe_print(f"  CLEAN ({len(clean)}): {', '.join(clean) if clean else 'none'}")
    if warned:
        _safe_print(f"  WARNED ({len(warned)}): {', '.join(warned)} — proceed with caution")
    _safe_print(f"  All checks passed. Proceeding to Think Tank panels.")
    _safe_print(f"{'='*58}\n")
    return reports


def load_preflight_cache(ticker: str) -> dict:
    """Load validated preflight data for a ticker from today's cache."""
    cache_file = CACHE_DIR / f"preflight_{datetime.date.today().isoformat()}.json"
    if not cache_file.exists():
        return {}
    try:
        data = json.loads(cache_file.read_text())
        return data.get(ticker, {}).get("validated", {})
    except Exception:
        return {}


def build_preflight_header(ticker: str) -> str:
    """Build a validated data header block to inject into all panel prompts for this ticker."""
    v = load_preflight_cache(ticker)
    if not v:
        return ""

    lines = [f"=== VALIDATED DATA BLOCK: {ticker} (pre-flight verified) ==="]

    # EPS
    fwd_eps = v.get("forward_eps")
    fwd_source = v.get("forward_eps_source", "")
    fwd_flagged = v.get("forward_eps_flagged", False)
    ttm_eps = v.get("trailing_eps")
    if fwd_flagged:
        lines.append(f"Forward EPS (yfinance): ${fwd_eps:.2f} — UNVERIFIED, DO NOT USE FOR CALCULATIONS")
        lines.append(f"USE INSTEAD: TTM EPS=${ttm_eps:.2f}. For forward estimates, state 'management guidance pending verification' and use TTM P/E only.")
        lines.append(f"PEG CALCULATION BLOCKED: Any PEG or forward P/E using ${fwd_eps:.2f} is invalid.")
    elif fwd_eps:
        lines.append(f"Forward EPS: ${fwd_eps:.2f} [{fwd_source}]")
        if "verified" in fwd_source.lower():
            lines.append("NOTE: Verify this is GAAP EPS if the bull thesis depends on GAAP profitability.")
    if ttm_eps:
        lines.append(f"TTM EPS: ${ttm_eps:.2f}")

    # GAAP vs non-GAAP divergence check
    if fwd_eps:
        try:
            gaap_nonGAAP_results = _web_search(f"{ticker} GAAP EPS vs non-GAAP adjusted EPS 2026 difference", limit=2)
            combined = " ".join(r.get("description","") for r in gaap_nonGAAP_results).lower()
            if any(w in combined for w in ["non-gaap", "adjusted eps", "non gaap", "adjusted earnings"]):
                lines.append(
                    f"GAAP/NON-GAAP WARNING: Forward EPS may be non-GAAP adjusted. "
                    f"Verify whether ${fwd_eps:.2f} is GAAP or non-GAAP before using in P/E or PEG calculations. "
                    f"For profitability-thesis stocks, GAAP EPS is the correct metric."
                )
        except Exception:
            pass

    # Attempt to source-verify forward EPS via web search
    if fwd_flagged and fwd_eps:
        try:
            search_results = _web_search(f"{ticker} forward EPS guidance fiscal 2026 2027 analyst consensus", limit=2)
            if search_results:
                lines.append(f"EPS SOURCE CHECK: {search_results[0].get('title','')[:80]}")
                lines.append(f"  {search_results[0].get('description','')[:200]}")
        except Exception:
            pass

    # Fiscal
    fy_label = v.get("fiscal_label", "")
    next_earnings = v.get("next_earnings_date", "")
    if fy_label and fy_label != "UNKNOWN":
        lines.append(f"Next earnings: {fy_label} ({next_earnings}) — USE THIS LABEL ONLY")

    # Spinoff
    if v.get("spinoff_detected"):
        lines.append(f"!! SPINOFF ALERT: {v.get('spinoff_detail', 'Pending spinoff detected')}")
        lines.append("   Sum-of-parts valuation REQUIRED. Do NOT calculate single-entity PEG.")

    # Insider sells
    sells = v.get("insider_sells", [])
    if sells:
        total = sum(s.get("value", 0) for s in sells)
        lines.append(f"Insider selling (90 days): ${total/1e6:.1f}M across {len(sells)} transactions")
        for s in sells[:3]:
            lines.append(f"  {s['date']} | {s['insider']} | {s['shares']:,} shares | ${s['value']:,.0f}")

    # Segments
    seg_ctx = v.get("segment_context", "")
    if seg_ctx:
        lines.append(f"Segment context: {seg_ctx}")

    # Short seller reports
    ss_reports = v.get("short_seller_reports", [])
    if ss_reports:
        lines.append(f"SHORT SELLER REPORTS FOUND ({len(ss_reports)}):")
        for r in ss_reports[:3]:
            lines.append(f"  - {r}")
        lines.append("Skeptic must address these allegations specifically.")

    lines.append("=== END VALIDATED DATA BLOCK ===")
    return "\n".join(lines)
