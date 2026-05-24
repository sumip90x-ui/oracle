#!/usr/bin/env python3
"""
ORACLE Phase 2 — run_sim.py
CLI entry point for the simulation engine.

Usage:
  python3 run_sim.py --stocks INSM BBIO ZETA SNOW PLTR PATH
  python3 run_sim.py --stocks INSM BBIO ZETA SNOW PLTR PATH --rounds 8
  python3 run_sim.py --stocks INSM BBIO --fast --report /path/to/report.md
  python3 run_sim.py --stocks INSM BBIO --tracks 3 --seed 42
"""

import os
import sys
import json
import random
import argparse
import datetime
from collections import Counter
from pathlib import Path
from statistics import mean, stdev

# Allow imports from same sim/ directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Allow imports from ~/ORACLE
sys.path.insert(0, os.path.expanduser("~/ORACLE"))

from round_loop    import run_simulation
from scorer        import score_simulation, format_rankings
from graph_builder import get_driver
from oracle_history import record_run as _record_run

HAIKU  = "deepseek-chat"    # DeepSeek Flash — primary sim model
SONNET = "anthropic/claude-sonnet-4.5"  # OpenRouter — kept for reference

SIMS_DIR      = Path(os.path.expanduser("~/ORACLE/sims"))
ORACLE_DIR    = Path(os.path.expanduser("~/Documents/Trading Vault/03_Stock_Analysis/ORACLE"))
ENV_CANDIDATES = [
    os.path.expanduser("~/Documents/MiroShark/.env"),
    os.path.expanduser("~/ORACLE/.env"),
    os.path.expanduser("~/.hermes/.env"),
]


# ── API key loader ─────────────────────────────────────────────────────────────

def _load_api_key():
    """Load OPENROUTER_API_KEY from environment, then candidate .env files."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key

    for env_path in ENV_CANDIDATES:
        if os.path.exists(env_path):
            try:
                for line in Path(env_path).read_text().splitlines():
                    line = line.strip()
                    if line.startswith("OPENROUTER_API_KEY"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            val = parts[1].strip().strip('"').strip("'")
                            if val:
                                return val
            except Exception:
                pass

    return ""


# ── Fundamentals loader ────────────────────────────────────────────────────────

def _load_fundamentals(stocks):
    """Load fundamentals from oracle_data.py with graceful fallback."""
    try:
        from data.oracle_data import get_fundamentals_batch
        print(f"  Loading fundamentals for {stocks}...")
        data = get_fundamentals_batch(list(stocks))
        return data
    except Exception as e:
        print(f"  WARNING: Could not load fundamentals ({e}). Using empty dict.")
        return {}


# ── Report finder ──────────────────────────────────────────────────────────────

def _find_latest_report():
    """Auto-find the most recent Think Tank .md report in Trading Vault."""
    search_dirs = [
        ORACLE_DIR / "runs",
        ORACLE_DIR,
        ORACLE_DIR.parent,
    ]
    candidates = []
    for d in search_dirs:
        if d.exists():
            candidates.extend(d.glob("*.md"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def validate_stocks(stocks, fundamentals):
    """Check for pre-revenue or sub-$5 stocks and return advisory warning strings."""
    warnings = []
    for ticker in stocks:
        f = fundamentals.get(ticker, {})
        rev_growth = f.get("revenue_growth_yoy")
        price = f.get("price", 0)
        if rev_growth is None or (isinstance(rev_growth, (int, float)) and rev_growth < -50):
            warnings.append(
                f"{ticker}: No meaningful revenue — this is a speculative lottery ticket not a runner candidate"
            )
        if price and price < 5:
            warnings.append(
                f"{ticker}: Price ${price:.2f} below $5 minimum — high risk of manipulation"
            )
    return warnings


def _load_report(report_path=None):
    """Load report text from explicit path or auto-discovered latest file."""
    if report_path:
        p = Path(report_path)
        if p.exists():
            return p.read_text()
        print(f"  WARNING: Report not found at {report_path}")
        return ""

    latest = _find_latest_report()
    if latest:
        print(f"  Auto-loaded report: {latest.name}")
        return latest.read_text()

    print("  WARNING: No report found. Graph thesis/catalyst extraction will be empty.")
    return ""


# ── Run ID builder ─────────────────────────────────────────────────────────────

def _make_run_id(stocks):
    today = datetime.date.today().strftime("%Y%m%d")
    abbrev = "_".join(s[:4] for s in stocks[:3])
    return f"sim_{today}_{abbrev}"


# ── JSON serialiser helper ─────────────────────────────────────────────────────

def _serialisable(obj):
    """Make markets/results JSON-safe."""
    if hasattr(obj, "__dict__"):
        d = {k: _serialisable(v) for k, v in obj.__dict__.items()}
        return d
    if isinstance(obj, (list, tuple)):
        return [_serialisable(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _serialisable(v) for k, v in obj.items()}
    return obj


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ORACLE Phase 2 — Multi-agent simulation engine"
    )
    parser.add_argument(
        "--stocks", nargs="+", required=True,
        help="Ticker symbols to simulate (e.g. INSM BBIO ZETA SNOW PLTR PATH)"
    )
    parser.add_argument(
        "--rounds", type=int, default=8,
        help="Number of simulation rounds (default: 8)"
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="Use Haiku model (faster/cheaper). Default is also Haiku."
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Override model string (e.g. anthropic/claude-sonnet-4.5)"
    )
    parser.add_argument(
        "--report", type=str, default=None,
        help="Path to Think Tank .md report file"
    )
    parser.add_argument(
        "--tracks", type=int, default=2,
        help="Number of parallel sim tracks with different injection seeds (default: 2)"
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Base injection seed. Random if not set"
    )
    args = parser.parse_args()

    stocks = [t.upper() for t in args.stocks]
    model  = HAIKU if args.fast else (args.model or HAIKU)

    print("\n" + "=" * 60)
    print("  ORACLE PHASE 2 — SIMULATION ENGINE")
    print("=" * 60)
    print(f"  Stocks:  {' | '.join(stocks)}")
    print(f"  Rounds:  {args.rounds}")
    print(f"  Model:   {model}")

    # Load API key
    api_key = _load_api_key()
    if not api_key:
        print("\nERROR: OPENROUTER_API_KEY not found.")
        print("Set it in env or add to ~/ORACLE/.env")
        sys.exit(1)
    print(f"  API Key: {'*' * 8}{api_key[-4:]}")

    # Load fundamentals
    fundamentals = _load_fundamentals(stocks)

    # Validate stocks — advisory only, does not block simulation
    stock_warnings = validate_stocks(stocks, fundamentals)
    if stock_warnings:
        print()
        for w in stock_warnings:
            print(f"  ⚠  WARNING: {w}")
        print()

    # Load report
    report_text = _load_report(args.report)

    # Build run_id
    run_id = _make_run_id(stocks)

    # Determine base seed
    base_seed = args.seed if args.seed is not None else random.randint(0, 99999)

    print(f"  Run ID:  {run_id}")
    print(f"  Tracks:  {args.tracks} | Base seed: {base_seed}\n")

    # ── Multi-track simulation loop ────────────────────────────────────────
    track_results   = []   # list of {ticker: result_dict} per track
    first_track_sim = None  # sim results from track 0 for JSON output

    for track in range(args.tracks):
        seed         = base_seed + track
        # Use a track-specific run_id in Neo4j to avoid graph compounding
        track_run_id = f"{run_id}_t{track}" if args.tracks > 1 else run_id

        print(f"  Track {track + 1}/{args.tracks} (seed={seed})...")

        sim_results = run_simulation(
            run_id       = track_run_id,
            stocks       = stocks,
            fundamentals = fundamentals,
            report_text  = report_text,
            num_rounds   = args.rounds,
            model        = model,
            api_key      = api_key,
            seed         = seed,
        )

        driver = None
        if sim_results.get("driver_active"):
            driver = get_driver()

        rankings = score_simulation(
            driver          = driver,
            run_id          = track_run_id,
            markets         = sim_results["markets"],
            all_rounds      = sim_results["rounds"],
            stocks          = stocks,
            intended_rounds = args.rounds,
        )

        if driver:
            driver.close()

        # Record this track to oracle_history using the canonical run_id
        _record_run(run_id, rankings, injection_seed=seed)

        track_results.append({r["ticker"]: r for r in rankings})

        # v4_6 — print injection log for this track
        inj_log = sim_results.get("injection_log", [])
        if inj_log:
            print(f"\n  Injection Log (Track {track + 1}/{args.tracks}):")
            for entry in inj_log:
                print(f"    R{entry['round']} [{entry['category']:>12}] {entry['direction']:>8} | {entry['text']}")

        if first_track_sim is None:
            first_track_sim = sim_results

    # ── Compute stability + confidence interval ────────────────────────────
    final_rankings = []
    for ticker in stocks:
        signals    = [tr[ticker]["signal"]    for tr in track_results if ticker in tr]
        composites = [tr[ticker]["composite"] for tr in track_results if ticker in tr]

        if not signals:
            continue

        # Stability classification
        if len(set(signals)) == 1:
            stability = "STABLE"
        else:
            counts            = Counter(signals)
            most_common_count = counts.most_common(1)[0][1]
            stability         = "CONTESTED" if most_common_count / len(signals) > 0.5 else "FRAGILE"

        mean_composite = mean(composites)
        std_composite  = stdev(composites) if len(composites) > 1 else 0.0

        base           = dict(track_results[0][ticker])
        base["composite"] = round(mean_composite, 4)
        # Majority signal across tracks
        base["signal"]    = Counter(signals).most_common(1)[0][0]

        if args.tracks > 1:
            base["stability"]      = stability
            base["composite_mean"] = round(mean_composite, 4)
            base["composite_std"]  = round(std_composite, 4)

        # v4_9 — Consensus signal: PROVISIONAL until 3+ runs, then CONFIRMED/LEANING/CONTESTED
        try:
            from sim.oracle_history import get_ticker_history
            history = get_ticker_history(ticker)
            distinct_runs = len(set(h["run_id"].split("_t")[0] for h in history))
            if distinct_runs < 3:
                base["consensus"] = "PROVISIONAL"
            else:
                last3_signals = [h["signal"] for h in history[:3]]
                counts = Counter(last3_signals)
                top_sig, top_cnt = counts.most_common(1)[0]
                if top_cnt == 3:
                    base["consensus"] = f"CONFIRMED:{top_sig}"
                elif top_cnt == 2:
                    base["consensus"] = f"LEANING:{top_sig}"
                else:
                    base["consensus"] = "CONTESTED"
        except Exception:
            base["consensus"] = "PROVISIONAL"

        final_rankings.append(base)

    final_rankings.sort(key=lambda x: x["composite"], reverse=True)
    for i, r in enumerate(final_rankings, 1):
        r["rank"] = i

    # ── Print rankings ─────────────────────────────────────────────────────
    format_rankings(final_rankings)

    # Add 'score' alias for composite so the frontend can read either field
    for r in final_rankings:
        r["score"] = r["composite"]

    # Build prob_history for chart from first track: {TICKER: [prob_r1, prob_r2, ...]}
    prob_history = {t: [] for t in stocks}
    for rd in first_track_sim["rounds"]:
        mprobs = rd.get("market_probs", {})
        for t in stocks:
            prob_history[t].append(round(mprobs.get(t, 0.5), 4))

    # ── Save JSON ──────────────────────────────────────────────────────────
    SIMS_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "run_id":       run_id,
        "stocks":       stocks,
        "model":        model,
        "rounds":       args.rounds,
        "tracks":       args.tracks,
        "base_seed":    base_seed,
        "rankings":     final_rankings,
        "prob_history": prob_history,
        "markets":      [_serialisable(m) for m in first_track_sim["markets"]],
        "rounds_data":  [
            {
                "round":        r["round"],
                "injection":    r["injection"],
                "market_probs": r.get("market_probs", {}),
                "posts": [
                    {
                        "agent":      p["agent"],
                        "conviction": p["conviction"],
                        "stances":    p["stances"],
                        "post":       p["post"][:800],
                    }
                    for p in r["posts"]
                ],
            }
            for r in first_track_sim["rounds"]
        ],
        "timestamp": datetime.datetime.now().isoformat(),
    }
    out_path = SIMS_DIR / f"{run_id}.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"  Results saved: {out_path}")

    # ── Summary ────────────────────────────────────────────────────────────
    obsidian_path = Path(os.path.expanduser(
        f"~/Documents/Trading Vault/03_Stock_Analysis/ORACLE/sims/{run_id}"
    ))
    print(f"\n  Obsidian rounds: {obsidian_path}")
    print(f"  JSON output:     {out_path}")
    print(f"\n  Top pick: {final_rankings[0]['ticker']} ({final_rankings[0]['signal']})\n")


if __name__ == "__main__":
    main()
