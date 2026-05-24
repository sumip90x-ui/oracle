#!/usr/bin/env python3
"""
oracle_regression.py — Regression test suite for ORACLE Think Tank.

Tests 5 known-answer tickers using cheap Haiku model.
Runs in ~2 minutes for under $2 after any code change.

Usage:
    cd ~/ORACLE && python3 scripts/oracle_regression.py
    cd ~/ORACLE && python3 scripts/oracle_regression.py --ticker SMCI
    cd ~/ORACLE && python3 scripts/oracle_regression.py --quick  # preflight only, no API calls
    cd ~/ORACLE && python3 scripts/oracle_regression.py --full   # include verdict tests
"""

import sys, os, json, argparse, datetime, re
sys.path.insert(0, str(__import__('pathlib').Path.home() / 'ORACLE' / 'engine'))
sys.path.insert(0, str(__import__('pathlib').Path.home() / 'ORACLE' / 'sim'))
sys.path.insert(0, str(__import__('pathlib').Path.home() / 'ORACLE'))

ORACLE_DIR = __import__('pathlib').Path.home() / 'ORACLE'


# ── Known-Answer Test Cases ──────────────────────────────────────────────────

TEST_CASES = {
    "SMCI": {
        "description": "Super Micro — active audit issues, Hindenburg, export control indictment",
        "expected_verdict": ["PASS", "WATCH"],  # acceptable verdicts
        "expected_conviction_max": 4,           # conviction must be <= this
        "expected_risks_contain": ["audit", "hindenburg", "export", "accounting"],  # at least 2 must appear
        "expected_preflight": "pass",           # EDGAR corrects yfinance EPS — passes cleanly
        "notes": "SMCI is a PASS at low conviction. EDGAR TTM=$4.39 corrects yfinance $1.90."
    },
    "ZETA": {
        "description": "Zeta Global — short seller attack, GAAP inflection thesis",
        "expected_verdict": ["INVESTIGATE", "BUY", "WATCH"],
        "expected_conviction_min": 4,
        "expected_conviction_max": 9,
        "expected_risks_contain": ["culper", "short", "gaap", "non-gaap"],
        "expected_preflight": "pass",
        "notes": "ZETA should show moderate conviction with short seller alert."
    },
    "KTOS": {
        "description": "Kratos Defense — recently profitable, defense AI thesis",
        "expected_verdict": ["INVESTIGATE", "BUY", "STRONG_BUY"],
        "expected_conviction_min": 5,
        "expected_preflight": "pass",
        "notes": "KTOS should be bullish. Recently profitable, clean data."
    },
    "CRDO": {
        "description": "Credo Technology — high-growth AI interconnect",
        "expected_verdict": ["INVESTIGATE", "BUY", "STRONG_BUY", "WATCH"],
        "expected_conviction_min": 4,
        "expected_preflight": "pass",
        "notes": "CRDO should be positive. High growth justifies elevated EPS ratio."
    },
    "FLEX": {
        "description": "Flex Ltd — phantom EPS + pending spinoff",
        "expected_verdict": None,               # doesn't matter — should halt at preflight
        "expected_preflight": "halt",           # MUST halt at preflight
        "notes": "FLEX must halt at preflight. Phantom EPS + CPI spinoff = bad data."
    },
    "ACM": {
        "description": "AECOM — infrastructure/engineering services, May 11 2026 earnings",
        "expected_verdict": ["INVESTIGATE", "BUY", "WATCH", "PASS"],
        "expected_conviction_max": 8,
        "expected_preflight": "pass",
        "expected_company_name": "AECOM",   # disambiguation check
        "notes": "ACM=AECOM not ACM Research. Q2 FY2026 revenue ~$3.8B. Must NOT show pre-divestiture $39B."
    },
    "YELP": {
        "description": "Yelp Inc — local business platform, Q1 2026 revenue $361M",
        "expected_verdict": ["INVESTIGATE", "BUY", "WATCH", "PASS", "AVOID"],
        "expected_conviction_max": 8,
        "expected_preflight": "pass",
        "expected_company_name": "Yelp",
        "revenue_mrq_min": 300e6,   # Q1 revenue should be ~$361M
        "revenue_mrq_max": 450e6,
        "notes": "YELP TTM must be ~$1.44B not $3.66B. Gate check: MRQ=$361M × 4=$1.44B. If XBRL shows $3.66B, quarterly_only fix failed."
    },
    "TTEK": {
        "description": "Tetra Tech — professional services, USAID structural break, Q2 FY2026",
        "expected_verdict": ["INVESTIGATE", "BUY", "WATCH", "PASS"],
        "expected_conviction_max": 9,
        "expected_preflight": "pass",
        "expected_company_name": "Tetra Tech",
        "revenue_mrq_min": 900e6,    # Q2 FY2026 quarterly ~$1.05B (half-year $2.43B / 2)
        "revenue_mrq_max": 1400e6,
        "revenue_ttm_max": 6000e6,   # TTM must be < $6B (not $10.45B XBRL YTD inflation)
        "notes": "TTEK USAID structural break. XBRL YTD entry $2.43B (181d span) must be rejected. TTM must use quarterly_only=True to get ~$4.2B not $10.45B."
    },
    "GAP": {
        "description": "Gap Inc -- retail apparel, January fiscal year, 4-4-5 calendar, old ticker GPS",
        "expected_verdict": ["PASS", "WATCH", "INVESTIGATE"],
        "expected_conviction_max": 7,
        "expected_preflight": "pass",
        "expected_company_name": "Gap",
        "revenue_mrq_min": 3_500_000_000,
        "revenue_mrq_max": 5_000_000_000,
        "revenue_ttm_min": 14_000_000_000,
        "revenue_ttm_max": 17_000_000_000,
        "ocf_ttm_min": 800_000_000,
        "cash_min": 2_000_000_000,
        "must_find_earnings_8k": True,
        "notes": (
            "GAP fiscal year ends January 31. 4-4-5 retail calendar produces quarters "
            "of 56-112 days -- span filter must use 55-112 day tolerance. "
            "Old ticker GPS must not confuse CIK lookup. "
            "OCF must be positive ~$1.3B. Cash ~$3B must appear in balance sheet."
        )
    },
    "AEM": {
        "description": (
            "Agnico Eagle Mines — Canadian gold miner, foreign private issuer, "
            "6-K filer, world's second largest gold producer"
        ),
        "expected_verdict": ["INVESTIGATE", "BUY", "WATCH", "PASS"],
        "expected_conviction_max": 9,
        "expected_preflight": "pass",
        "expected_company_name": "Agnico Eagle",
        "filing_type": "6-K",
        "foreign_private_issuer": True,
        "commodity": "XAUUSD",
        "commodity_price_min": 1_000,
        "notes": (
            "AEM files 6-K not 8-K. XAUUSD must be fetched in preflight. "
            "NAV methodology must be used for valuation. "
            "IFRS filer — cash/OCF XBRL tags differ from US GAAP; balance sheet checks skipped."
        ),
    },
    "AG": {
        "description": (
            "First Majestic Silver Corp — Canadian silver miner, "
            "foreign private issuer, 6-K filer, "
            "four operating mines in Mexico, Jerritt Canyon Nevada development, "
            "founder-led by Keith Neumeyer"
        ),
        "expected_verdict": ["BUY", "INVESTIGATE", "WATCH"],
        "expected_conviction_max": 8,
        "expected_preflight": "pass",
        "expected_company_name": "First Majestic",
        "filing_type": "6-K",
        "foreign_private_issuer": True,
        "commodity": "SILVER",
        "sector": "silver_mining",
        "revenue_mrq_min": 350_000_000,
        "revenue_mrq_max": 700_000_000,
        "ocf_ttm_min": 500_000_000,
        "cash_min": 800_000_000,
        "commodity_price_min": 20.0,
        "expected_sector_metrics": [
            "aisc_per_ageq_oz",
            "realized_silver_price",
            "silver_production_oz",
        ],
        "aisc_min": 15.0,
        "aisc_max": 45.0,
        "notes": (
            "AG files 6-K not 8-K. AISC is in silver equivalent ounces (AgEq), "
            "NOT straight silver oz. Q1 2026 AISC was $29.76/AgEq oz. "
            "Silver price must be fetched (XAGUSD) — NOT assumed from training data. "
            "Q1 2026 realized silver price was $86.35 — confirm press release fetched. "
            "AgEq ratio fixed at 75:1 for 2026 per management guidance. "
            "Founder-led: Keith Neumeyer. Complex jurisdiction: Mexico. "
            "Jerritt Canyon (Nevada, 7.8M oz gold reserves) restart targeted H2 2027."
        )
    },
    "BTG": {
        "description": (
            "B2Gold Corp — Canadian gold miner, foreign private issuer, "
            "6-K filer, Mali/Philippines/Namibia/Canada operations"
        ),
        "expected_verdict": ["WATCH", "INVESTIGATE", "BUY", "PASS"],
        "expected_conviction_max": 8,
        "expected_preflight": "pass",
        "expected_company_name": "B2Gold",
        "filing_type": "6-K",
        "foreign_private_issuer": True,
        "commodity": "XAUUSD",
        "sector": "gold_mining",
        "revenue_mrq_min": 900_000_000,
        "revenue_mrq_max": 1_400_000_000,
        "ocf_ttm_min": 500_000_000,
        "cash_min": 300_000_000,
        "commodity_price_min": 2_000,
        "expected_sector_metrics": ["aisc_per_oz", "gold_production_oz"],
        "notes": (
            "BTG files 6-K not 8-K. AISC $1,964/oz not cash cost $1,005/oz. "
            "CEO transition Feb 2026 — escalate HIGH. "
            "Goose Mine fire April 2026 — must appear in material events. "
            "Fekola Regional permit deadline end June 2026 — time-sensitive risk. "
            "Gold price must be current spot not historical average."
        ),
    },
}


# ── Preflight Tests ──────────────────────────────────────────────────────────

def run_preflight_tests(tickers=None) -> dict:
    """Test preflight on all 5 tickers (or specified subset). Fast, no LLM API calls."""
    try:
        from oracle_preflight import run_preflight
    except ImportError as e:
        return {"_error": f"Could not import oracle_preflight: {e}"}

    from pathlib import Path

    results = {}
    test_set = tickers or list(TEST_CASES.keys())

    for ticker in test_set:
        if ticker not in TEST_CASES:
            results[ticker] = {"test": "preflight", "passed": False, "error": f"Unknown ticker {ticker}"}
            continue

        tc = TEST_CASES[ticker]
        # Clear today's preflight cache for this ticker to force fresh run
        today = datetime.date.today().isoformat()
        cache = Path.home() / 'ORACLE' / 'cache' / f'preflight_{today}.json'
        # Don't delete shared cache — run_preflight handles per-ticker logic
        try:
            pf_cache = {}
            if cache.exists():
                try:
                    pf_cache = json.loads(cache.read_text())
                except Exception:
                    pf_cache = {}
                # Remove this ticker from cache to force fresh run
                if ticker in pf_cache:
                    del pf_cache[ticker]
                    cache.write_text(json.dumps(pf_cache, indent=2, default=str))
        except Exception:
            pass

        try:
            pf_result = run_preflight([ticker], verbose=False)
            r = pf_result.get(ticker)
            if r is None:
                results[ticker] = {
                    "test": "preflight",
                    "passed": False,
                    "error": "run_preflight returned no result for ticker"
                }
                continue

            halted = getattr(r, 'halted', False)
            score = getattr(r, 'score', 0)
            errors = getattr(r, 'errors', [])
            warnings = getattr(r, 'warnings', [])

            expected_halt = tc["expected_preflight"] == "halt"
            passed = halted == expected_halt
            fs = None  # initialized here so OCF/cash checks can reference it after revenue block

            results[ticker] = {
                "test": "preflight",
                "passed": passed,
                "score": score,
                "halted": halted,
                "expected": tc["expected_preflight"],
                "errors": errors[:1] if errors else [],
                "warnings": [w[:60] for w in warnings[:2]],
            }

            # Revenue sanity check if expected range provided
            if passed and "revenue_mrq_min" in tc:
                fs = None
                try:
                    import pathlib, json as _json, datetime as _dt
                    cache_fs = pathlib.Path.home() / "ORACLE" / "cache" / f"factsheet_{ticker}_{_dt.date.today().isoformat()}.json"
                    if cache_fs.exists():
                        fs = _json.loads(cache_fs.read_text())
                    if fs:
                        mrq = (fs.get("metrics") or {}).get("revenue_mrq", {}).get("value") or 0
                        ttm = (fs.get("metrics") or {}).get("revenue_ttm", {}).get("value") or 0
                        if mrq > 0:
                            if mrq < tc["revenue_mrq_min"] or mrq > tc["revenue_mrq_max"]:
                                results[ticker] = {"test": "revenue_sanity", "passed": False,
                                                   "error": f"MRQ=${mrq/1e6:.0f}M outside expected range ${tc['revenue_mrq_min']/1e6:.0f}M-${tc['revenue_mrq_max']/1e6:.0f}M"}
                                continue
                        if "revenue_ttm_max" in tc and ttm > 0:
                            if ttm > tc["revenue_ttm_max"]:
                                results[ticker] = {"test": "revenue_ttm_max", "passed": False,
                                                   "error": f"TTM=${ttm/1e6:.0f}M exceeds max ${tc['revenue_ttm_max']/1e6:.0f}M — YTD contamination fix failed"}
                                continue
                        if ttm > 0 and mrq > 0:
                            ratio = ttm / (mrq * 4)
                            if ratio > 2.0 or ratio < 0.5:
                                results[ticker] = {"test": "revenue_ratio", "passed": False,
                                                   "error": f"TTM=${ttm/1e6:.0f}M is {ratio:.1f}x MRQ×4 — reconciliation gate should have caught this"}
                                continue
                except Exception:
                    pass  # revenue check is best-effort

            # OCF TTM minimum check
            if passed and "ocf_ttm_min" in tc and fs:
                ocf_ttm = ((fs.get("metrics") or {}).get("operating_cashflow_ttm") or {}).get("value") or 0
                if ocf_ttm < tc["ocf_ttm_min"]:
                    results[ticker] = {
                        "test": "ocf_ttm_sanity",
                        "passed": False,
                        "error": (
                            f"OCF TTM ${ocf_ttm/1e6:.0f}M below minimum "
                            f"${tc['ocf_ttm_min']/1e6:.0f}M -- wrong period artifact"
                        )
                    }
                    passed = False

            # Cash position minimum check
            if passed and "cash_min" in tc and fs:
                _cash = ((fs.get("metrics") or {}).get("cash_and_equivalents") or {}).get("value") or 0
                _sti = ((fs.get("metrics") or {}).get("short_term_investments") or {}).get("value") or 0
                total_cash = _cash + _sti
                if total_cash < tc["cash_min"]:
                    results[ticker] = {
                        "test": "cash_sanity",
                        "passed": False,
                        "error": (
                            f"Total cash ${total_cash/1e9:.2f}B below minimum "
                            f"${tc['cash_min']/1e9:.2f}B -- balance sheet extraction failed"
                        )
                    }
                    passed = False

            # Commodity price check
            if passed and tc.get("commodity") and fs:
                pfw = (fs.get("preflight_web") or {})
                commodity_data = pfw.get("commodity", {})
                commodity_price = commodity_data.get("price", 0) or 0
                min_price = tc.get("commodity_price_min", 0)

                if min_price > 0 and commodity_price < min_price:
                    results[ticker] = {
                        "test": "commodity_price",
                        "passed": False,
                        "error": (
                            f"Commodity {tc['commodity']} price ${commodity_price:.0f} "
                            f"below minimum ${min_price:.0f} — fetch failed"
                        ),
                    }
                    continue

                if commodity_price > 0:
                    print(f"  [{ticker}] Commodity {tc['commodity']}: ${commodity_price:.2f} — OK")

            # Sector metrics validation
            if passed and "expected_sector_metrics" in tc and fs:
                sector_metrics = fs.get("sector_metrics", {})
                missing = [m for m in tc["expected_sector_metrics"] if m not in sector_metrics]
                if missing:
                    results[ticker] = {
                        "test": "sector_metrics",
                        "passed": False,
                        "error": f"Missing sector metrics: {missing}. Got: {list(sector_metrics.keys())}",
                    }
                    passed = False

            # AISC sanity check for miners
            if passed and "aisc_min" in tc and fs:
                sector_metrics = fs.get("sector_metrics", {})
                aisc_value = None
                for field in ["aisc_per_oz", "aisc_per_ageq_oz", "aisc_per_lb"]:
                    if field in sector_metrics:
                        val = sector_metrics[field]
                        if isinstance(val, dict):
                            aisc_value = val.get("value")
                        elif isinstance(val, (int, float)):
                            aisc_value = val
                        break

                if aisc_value is not None:
                    if aisc_value < tc["aisc_min"] or aisc_value > tc["aisc_max"]:
                        results[ticker] = {
                            "test": "aisc_sanity",
                            "passed": False,
                            "error": (
                                f"AISC {aisc_value:.2f} outside expected range "
                                f"[{tc['aisc_min']:.2f}, {tc['aisc_max']:.2f}]. "
                                f"May be using wrong cost metric "
                                f"(e.g., gold AISC vs silver AgEq AISC)"
                            )
                        }
                        passed = False

            # 8-K earnings filter check (warning only, does not fail the test)
            if tc.get("must_find_earnings_8k"):
                try:
                    import pathlib as _pl, json as _json2, datetime as _dt2
                    pr_cache = _pl.Path.home() / "ORACLE" / "cache" / f"press_release_{ticker}_{_dt2.date.today().isoformat()}.json"
                    if pr_cache.exists():
                        pr = _json2.loads(pr_cache.read_text())
                        item_filter = pr.get("item_filter_used", "MISSING")
                        if item_filter == "UNFILTERED_FALLBACK":
                            print(f"  [8-K WARN] {ticker}: item_filter_used=UNFILTERED_FALLBACK — no Item 2.02 found, fallback used")
                            results[ticker]["8k_filter_warning"] = f"UNFILTERED_FALLBACK (items={pr.get('items', '?')})"
                        elif item_filter == "MISSING":
                            print(f"  [8-K WARN] {ticker}: press_release cache missing item_filter_used field")
                        else:
                            print(f"  [8-K OK] {ticker}: item_filter_used={item_filter}")
                            if isinstance(results.get(ticker), dict):
                                results[ticker]["8k_filter_used"] = item_filter
                except Exception:
                    pass  # 8-K filter check is best-effort
        except Exception as e:
            results[ticker] = {
                "test": "preflight",
                "passed": False,
                "error": str(e)[:80]
            }

    return results


# ── Verdict Tests (LLM) ──────────────────────────────────────────────────────

def run_verdict_tests(tickers=None) -> dict:
    """
    Run quick TT analysis using Haiku model.
    Uses composite mode with cheap model for speed and cost.
    """
    try:
        sys.path.insert(0, str(ORACLE_DIR / 'engine'))
        from oracle_think_tank import get_fundamentals, run_composite
    except ImportError as e:
        return {"error": f"Import failed: {e}"}

    results = {}
    test_tickers = tickers or [
        t for t, tc in TEST_CASES.items() if tc["expected_preflight"] != "halt"
    ]

    for ticker in test_tickers:
        if ticker not in TEST_CASES:
            results[ticker] = {"test": "verdict", "passed": False, "error": f"Unknown ticker {ticker}"}
            continue

        tc = TEST_CASES[ticker]
        try:
            print(f"  Running verdict test for {ticker}...", end="", flush=True)
            fundamentals = get_fundamentals([ticker])
            date = datetime.date.today().strftime("%Y%m%d")

            # Use cheap Haiku model for regression tests
            results_tt = run_composite(
                stocks=[ticker],
                fundamentals=fundamentals,
                model="anthropic/claude-3.5-haiku",
                screener_context="",
                date=date,
                mode="composite"
            )

            # Extract verdict and conviction from summary
            summary = results_tt.get("summary", "")
            verdict = "UNKNOWN"
            conviction = -1

            v_match = re.search(r'OVERALL:\s*(\w+)', summary, re.IGNORECASE)
            c_match = re.search(r'Score:\s*(\d+)/10', summary, re.IGNORECASE)
            if v_match:
                verdict = v_match.group(1).upper()
            if c_match:
                conviction = int(c_match.group(1))

            # Check against expected
            expected_verdicts = tc.get("expected_verdict", [])
            conviction_min = tc.get("expected_conviction_min", 0)
            conviction_max = tc.get("expected_conviction_max", 10)

            verdict_ok = not expected_verdicts or verdict in [v.upper() for v in expected_verdicts]
            conviction_ok = conviction_min <= conviction <= conviction_max if conviction >= 0 else True

            # Check risks mentioned
            risks_contain = tc.get("expected_risks_contain", [])
            summary_lower = summary.lower()
            risks_found = [r for r in risks_contain if r in summary_lower]
            risks_ok = len(risks_found) >= min(2, len(risks_contain))

            passed = verdict_ok and conviction_ok and risks_ok

            results[ticker] = {
                "test": "verdict",
                "passed": passed,
                "verdict": verdict,
                "conviction": conviction,
                "verdict_ok": verdict_ok,
                "conviction_ok": conviction_ok,
                "risks_found": risks_found,
                "risks_ok": risks_ok,
                "expected_verdict": expected_verdicts,
            }
            print(f" verdict={verdict} conviction={conviction}/10", flush=True)
        except Exception as e:
            results[ticker] = {"test": "verdict", "passed": False, "error": str(e)[:80]}
            print(f" ERROR: {e}", flush=True)

    return results


# ── Output Formatter ─────────────────────────────────────────────────────────

def print_results(preflight_results: dict, verdict_results: dict = None) -> bool:
    """Print a clean summary table. Returns True if all tests passed."""
    print(f"\n{'='*60}")
    print(f"  ORACLE REGRESSION TEST -- {datetime.date.today()}")
    print(f"{'='*60}\n")

    pf_pass = pf_fail = 0

    if "_error" in preflight_results:
        print(f"[ Preflight Tests ] ERROR: {preflight_results['_error']}")
        pf_fail += 1
    else:
        print("[ Preflight Tests ]")
        for ticker, r in preflight_results.items():
            ok = r.get("passed", False)
            icon = "OK" if ok else "FAIL"
            if ok:
                pf_pass += 1
            else:
                pf_fail += 1
            if "error" in r:
                detail = f"ERROR: {r['error']}"
            else:
                detail = (
                    f"score={r.get('score', 0)} "
                    f"halted={r.get('halted', '?')} "
                    f"expected={r.get('expected', '?')}"
                )
            print(f"  [{icon}] {ticker}: {detail}")
            # Show first error if failed and not expected halt
            if not ok and r.get("errors"):
                print(f"        -> {r['errors'][0][:80]}")

    v_pass = v_fail = 0
    if verdict_results:
        print("\n[ Verdict Tests (Haiku fast mode) ]")
        for ticker, r in verdict_results.items():
            if "error" in r and r.get("test") != "verdict":
                print(f"  [ERR] {ticker}: {r['error']}")
                v_fail += 1
                continue
            if "error" in r:
                print(f"  [ERR] {ticker}: {r['error']}")
                v_fail += 1
                continue
            ok = r.get("passed", False)
            icon = "OK" if ok else "FAIL"
            if ok:
                v_pass += 1
            else:
                v_fail += 1
            detail = (
                f"verdict={r.get('verdict', '?')} "
                f"conviction={r.get('conviction', '?')}/10 "
                f"risks={r.get('risks_found', [])}"
            )
            print(f"  [{icon}] {ticker}: {detail}")
            if not ok:
                if not r.get("verdict_ok"):
                    print(f"        -> Verdict mismatch: got {r.get('verdict')} expected {r.get('expected_verdict')}")
                if not r.get("conviction_ok"):
                    print(f"        -> Conviction out of range: {r.get('conviction')}")
                if not r.get("risks_ok"):
                    print(f"        -> Risks not found: needed 2 of {TEST_CASES.get(ticker,{}).get('expected_risks_contain',[])} in summary")

    total_pass = pf_pass + v_pass
    total_fail = pf_fail + v_fail
    total = total_pass + total_fail

    print(f"\n{'='*60}")
    print(f"  RESULT: {total_pass}/{total} passed")
    if total_fail > 0:
        print(f"  {total_fail} FAILURES -- review before running production tickers")
    else:
        print(f"  All tests passed. System is stable.")
    print(f"{'='*60}\n")

    return total_fail == 0


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ORACLE regression test suite. Always runs preflight tests (free). Add --full for LLM verdict tests."
    )
    parser.add_argument("--ticker", help="Test only this ticker (e.g. SMCI)")
    parser.add_argument("--quick", action="store_true", help="Preflight tests only, no API calls (default behavior)")
    parser.add_argument("--full", action="store_true", help="Include verdict tests (uses API credits, ~$0.50-2.00)")
    args = parser.parse_args()

    tickers = [args.ticker.upper()] if args.ticker else None

    # Always run preflight tests (free — yfinance + web search only)
    print("Running preflight tests...")
    pf_results = run_preflight_tests(tickers)

    v_results = None
    if args.full and not args.quick:
        print("\nRunning verdict tests (uses API credits)...")
        v_results = run_verdict_tests(tickers)

    success = print_results(pf_results, v_results)
    sys.exit(0 if success else 1)
