#!/usr/bin/env python3
"""
ORACLE Phase 2 — scorer.py
Composite scoring: 60% graph-derived conviction + 40% prediction market signals.
"""


def score_simulation(driver, run_id, markets, all_rounds, stocks):
    """
    Blend graph verdict (60%) and market signals (40%) into a composite score.
    Apply 30% penalty if fewer than 6 rounds completed.
    Returns sorted list of result dicts.
    """
    from graph_builder import query_final_verdict

    rounds_completed = len(all_rounds)

    # ── 1. Graph scores (60 %) ──────────────────────────────────────────────
    graph_results = query_final_verdict(driver, run_id, stocks)
    raw_scores = {r["ticker"]: r["net_score"] for r in graph_results}
    skeptics   = {r["ticker"]: r["converted_skeptics"] for r in graph_results}

    # Converted-skeptic bonus: each convert adds 3 × a base unit
    BASE_BONUS = 0.10
    for ticker in stocks:
        raw_scores[ticker] = raw_scores.get(ticker, 0.0) + skeptics.get(ticker, 0) * 3.0 * BASE_BONUS

    vals = list(raw_scores.values())
    score_min, score_max = min(vals), max(vals)
    score_range = score_max - score_min

    if score_range > 0:
        graph_norms = {t: (raw_scores[t] - score_min) / score_range for t in stocks}
    else:
        graph_norms = {t: 0.5 for t in stocks}

    # ── 2. Market scores (40 %) ─────────────────────────────────────────────
    indiv_markets = {
        m.tickers[0]: m
        for m in markets
        if m.market_type == "individual" and len(m.tickers) == 1
    }
    hth_markets = [m for m in markets if m.market_type == "head_to_head"]

    # Head-to-head cluster wins
    cluster_wins = {t: 0.0 for t in stocks}
    for hth in hth_markets:
        if len(hth.tickers) == 2:
            t0, t1 = hth.tickers
            if hth.probability >= 0.5:
                cluster_wins[t0] = 1.0
            else:
                cluster_wins[t1] = 1.0

    market_scores = {}
    for ticker in stocks:
        m = indiv_markets.get(ticker)
        if m:
            prob    = m.probability
            vel     = max(-1.0, min(1.0, m.velocity()))
            cluster = cluster_wins.get(ticker, 0.0)
            market_scores[ticker] = prob * 0.5 + vel * 0.3 + cluster * 0.2
        else:
            market_scores[ticker] = 0.5

    # ── 3. Composite blend ──────────────────────────────────────────────────
    results = []
    for ticker in stocks:
        graph_norm   = graph_norms.get(ticker, 0.5)
        market_score = market_scores.get(ticker, 0.5)
        composite    = graph_norm * 0.60 + market_score * 0.40

        if rounds_completed < 6:
            composite *= 0.70  # incomplete simulation penalty

        indiv_m = indiv_markets.get(ticker)
        prob    = indiv_m.probability if indiv_m else 0.5
        vel     = indiv_m.velocity()  if indiv_m else 0.0

        # Signal thresholds — calibrated for 8-round sims where full consensus builds
        # BUY at 0.52 (was 0.60): with 3-round penalty sims were compressing everything to PASS
        if composite >= 0.70 and vel > 0:
            signal = "STRONG_BUY"
        elif composite >= 0.52 and vel > 0:
            signal = "BUY"
        elif composite >= 0.52:
            signal = "HOLD"
        elif composite >= 0.40:
            signal = "WATCH"
        else:
            signal = "PASS"

        results.append({
            "ticker":             ticker,
            "rank":               0,  # filled after sort
            "graph_score":        round(graph_norm,   4),
            "market_score":       round(market_score, 4),
            "composite":          round(composite,    4),
            "probability":        round(prob,         4),
            "velocity":           round(vel,          4),
            "converted_skeptics": skeptics.get(ticker, 0),
            "net_graph_raw":      round(raw_scores.get(ticker, 0.0), 4),
            "signal":             signal,
            "rounds_completed":   rounds_completed,
        })

    results.sort(key=lambda x: x["composite"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i

    # ── BUG 6: Resilience detection ─────────────────────────────────────────────
    # A stock is resilient if its market probability INCREASED during round 3 (macro shock)
    # while the average probability of all other stocks decreased.
    round_probs = {rd["round"]: rd.get("market_probs", {}) for rd in all_rounds}
    r2_probs = round_probs.get(2, {})
    r3_probs = round_probs.get(3, {})

    if r2_probs and r3_probs:
        for result in results:
            ticker = result["ticker"]
            p2 = r2_probs.get(ticker, 0.5)
            p3 = r3_probs.get(ticker, 0.5)
            others_r2 = [r2_probs.get(t, 0.5) for t in stocks if t != ticker]
            others_r3 = [r3_probs.get(t, 0.5) for t in stocks if t != ticker]
            avg_others_r2 = sum(others_r2) / len(others_r2) if others_r2 else 0.5
            avg_others_r3 = sum(others_r3) / len(others_r3) if others_r3 else 0.5
            result["resilience_signal"] = bool(p3 > p2 and avg_others_r3 < avg_others_r2)
    else:
        for result in results:
            result["resilience_signal"] = False

    return results


def format_rankings(results):
    """Print formatted ranking table to stdout."""
    if not results:
        print("No results to display.")
        return

    rc = results[0]["rounds_completed"]
    penalty = rc < 6

    print()
    print("=" * 74)
    print("  ORACLE SIMULATION — FINAL RANKINGS")
    print(f"  Rounds completed: {rc}/8" + (" ⚠ PENALTY -30%" if penalty else ""))
    print("=" * 74)
    print(f"  {'#':>2}  {'TICKER':<7} {'GRAPH':>6} {'MARKET':>7} {'COMPOSITE':>10} "
          f"{'PROB':>6} {'VEL':>6} {'CONV↑':>6}  SIGNAL")
    print("-" * 74)

    SIGNAL_FMT = {
        "STRONG_BUY": "★ STRONG BUY",
        "BUY":        "▲ BUY",
        "HOLD":       "◆ HOLD",
        "WATCH":      "◉ WATCH",
        "PASS":       "✗ PASS",
    }

    for r in results:
        conv_str = f"{r['converted_skeptics']}x" if r["converted_skeptics"] else " -"
        sig      = SIGNAL_FMT.get(r["signal"], r["signal"])
        # BUG 5: flag high market probability when composite score maps to WATCH/PASS/HOLD
        high_prob_note = ""
        if r["probability"] > 0.65 and r["signal"] in ("WATCH", "PASS", "HOLD"):
            high_prob_note = " (high market prob)"
        # BUG 6: flag resilience through macro shock
        resilient_note = " ⚡ RESILIENT" if r.get("resilience_signal") else ""
        print(
            f"  {r['rank']:>2}  {r['ticker']:<7} "
            f"{r['graph_score']:>6.3f} "
            f"{r['market_score']:>7.3f} "
            f"{r['composite']:>10.3f} "
            f"{r['probability']*100:>5.1f}% "
            f"{r['velocity']:>+6.3f} "
            f"{conv_str:>6}  {sig}{high_prob_note}{resilient_note}"
        )

    print("=" * 74)

    # Top pick callout
    top = results[0]
    print(f"\n  TOP PICK: {top['ticker']}  composite={top['composite']:.3f}  {top['signal']}")
    if top["converted_skeptics"]:
        print(f"  {top['converted_skeptics']} skeptic(s) converted to bullish on {top['ticker']} (3× bonus applied)")
    if top.get("resilience_signal"):
        print(f"  ⚡ {top['ticker']} showed resilience through macro shock")
    print()
