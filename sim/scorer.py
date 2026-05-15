#!/usr/bin/env python3
"""
ORACLE Phase 2 — scorer.py
Composite scoring: 60% graph-derived conviction + 40% prediction market signals.

Scoring fixes:
  Fix A: Penalty only fires on CRASHED runs (rounds < intended_rounds), not intentional short runs
  Fix B: Single-stock graph normalization uses probability-scaled score, not forced 0.5 neutral
  Fix C: Single-stock weight shift: graph 40% / market 60% (market signal more informative with 1 stock)
"""


def score_simulation(driver, run_id, markets, all_rounds, stocks, intended_rounds=8):
    """
    Blend graph verdict and market signals into a composite score.
    intended_rounds: the rounds the sim was SUPPOSED to run (not a crash threshold).
    Penalty only fires if rounds_completed < intended_rounds (crashed/interrupted).
    """
    from graph_builder import query_final_verdict

    rounds_completed = len(all_rounds)
    single_stock     = len(stocks) == 1

    # ── 1. Graph scores ──────────────────────────────────────────────────────
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
        # Fix B: single-stock or all-equal — use the market probability as graph proxy
        # instead of forcing 0.5 neutral. This lets a 95% bullish market signal
        # propagate through the graph component instead of being zeroed out.
        indiv_markets_temp = {
            m.tickers[0]: m
            for m in markets
            if m.market_type == "individual" and len(m.tickers) == 1
        }
        graph_norms = {}
        for t in stocks:
            m = indiv_markets_temp.get(t)
            if m:
                # Use market probability as graph score proxy — agents voted with their posts
                # which directly drove the market probability. This is not circular;
                # it's using the integrated signal the agents already produced.
                graph_norms[t] = m.probability
            else:
                graph_norms[t] = 0.5

    # ── 2. Market scores ─────────────────────────────────────────────────────
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

    # ── 3. Composite blend ───────────────────────────────────────────────────
    # Fix C: single-stock runs shift weight to market (more informative with 1 stock)
    # Multi-stock: graph 60% / market 40% (comparative graph works well)
    # Single-stock: graph 40% / market 60% (market prob is the real conviction signal)
    if single_stock:
        graph_weight  = 0.40
        market_weight = 0.60
    else:
        graph_weight  = 0.60
        market_weight = 0.40

    results = []
    for ticker in stocks:
        graph_norm   = graph_norms.get(ticker, 0.5)
        market_score = market_scores.get(ticker, 0.5)
        composite    = graph_norm * graph_weight + market_score * market_weight

        # Fix A: only penalize CRASHED runs — not intentional short runs.
        # A 5-round run that completed all 5 rounds is not a failure.
        crashed = rounds_completed < intended_rounds
        if crashed:
            composite *= 0.70  # incomplete simulation penalty
            penalty_note = f"⚠ CRASHED at {rounds_completed}/{intended_rounds} rounds — penalty applied"
        else:
            penalty_note = ""

        # Skeptic-weighted composite equals composite exactly (no additional penalty)
        skeptic_weighted = round(composite, 4)

        indiv_m = indiv_markets.get(ticker)
        prob    = indiv_m.probability if indiv_m else 0.5
        vel     = indiv_m.velocity()  if indiv_m else 0.0

        # Signal thresholds
        # velocity >= -0.005: plateaued consensus at high prob is BUY not HOLD
        if composite >= 0.70 and vel >= -0.005:
            signal = "STRONG_BUY"
        elif composite >= 0.52 and vel >= -0.005:
            signal = "BUY"
        elif composite >= 0.52:
            signal = "HOLD"
        elif composite >= 0.40:
            signal = "WATCH"
        else:
            signal = "PASS"

        # EV floor override — negative EV overrides any buy signal
        result_dict = {
            "ticker":                    ticker,
            "rank":                      0,
            "graph_score":               round(graph_norm,   4),
            "market_score":              round(market_score, 4),
            "composite":                 round(composite,    4),
            "skeptic_weighted_composite": skeptic_weighted,
            "probability":               round(prob,         4),
            "velocity":                  round(vel,          4),
            "converted_skeptics":        skeptics.get(ticker, 0),
            "net_graph_raw":             round(raw_scores.get(ticker, 0.0), 4),
            "signal":                    signal,
            "rounds_completed":          rounds_completed,
            "intended_rounds":           intended_rounds,
            "crashed":                   crashed,
            "penalty_note":              penalty_note,
            "single_stock_mode":         single_stock,
            # v4_11 — Regression-to-mean flag
            "rtm_flag":                  abs(composite - 0.50) > 0.20,
            "rtm_deviation":             round(abs(composite - 0.50), 3),
        }
        ev = result_dict.get("ev", None)
        if ev is not None and ev < -0.30:
            if result_dict.get("signal") in ("BUY", "STRONG_BUY", "INVESTIGATE"):
                result_dict["signal"] = "AVOID"
                result_dict["ev_override"] = True
                result_dict["ev_override_reason"] = f"EV={ev:.1%} < -30% floor — buy signal overridden"
        results.append(result_dict)

    results.sort(key=lambda x: x["composite"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i

    # ── Conviction stability: did it hold or bend under Round 6 pressure? ────
    # Measures: (1) drop after Round 6 injection, (2) recovery in Round 7
    # Shape: HELD (dropped <5pts and recovered), BENT (dropped 5-15pts),
    #        BROKE (dropped >15pts and didn't recover), UNTESTED (no round 6)
    round_probs = {rd["round"]: rd.get("market_probs", {}) for rd in all_rounds}
    r5_probs = round_probs.get(5, {})
    r6_probs = round_probs.get(6, {})
    r7_probs = round_probs.get(7, {})

    for result in results:
        ticker = result["ticker"]
        p5 = r5_probs.get(ticker)
        p6 = r6_probs.get(ticker)
        p7 = r7_probs.get(ticker)

        if p5 is not None and p6 is not None:
            drop = (p5 - p6) * 100  # percentage point drop after Round 6
            recovery = ((p7 - p6) * 100) if p7 is not None else 0

            if drop <= 5:
                shape = "HELD"       # barely moved — bulletproof conviction
            elif drop <= 15:
                if recovery >= drop * 0.5:
                    shape = "BENT_RECOVERED"   # dropped but came back
                else:
                    shape = "BENT"             # dropped and stayed down
            else:
                if recovery >= drop * 0.5:
                    shape = "BROKE_RECOVERED"  # big drop but rallied
                else:
                    shape = "BROKE"            # thesis cracked under pressure

            result["conviction_shape"] = shape
            result["r6_drop_pts"]      = round(drop, 1)
            result["r7_recovery_pts"]  = round(recovery, 1)
        else:
            result["conviction_shape"] = "UNTESTED"  # run didn't reach Round 6
            result["r6_drop_pts"]      = 0.0
            result["r7_recovery_pts"]  = 0.0
    # A stock is resilient if probability INCREASED during round 3 (macro shock)
    # while the average of all other stocks decreased.
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

    rc       = results[0]["rounds_completed"]
    intended = results[0].get("intended_rounds", 8)
    crashed  = results[0].get("crashed", False)
    single   = results[0].get("single_stock_mode", False)
    has_stability = any(r.get("stability") for r in results)
    has_ci        = any(r.get("composite_std", 0.0) > 0 for r in results)

    sep_width = 74 + (11 if has_stability else 0) + (5 if has_ci else 0)

    print()
    print("=" * sep_width)
    print("  ORACLE SIMULATION — FINAL RANKINGS")
    status_line = f"  Rounds completed: {rc}/{intended}"
    if crashed:
        status_line += f" ⚠ CRASHED — penalty -30% applied"
    elif single:
        status_line += f"  [single-stock mode: graph 40% / market 60%]"
    print(status_line)
    print("=" * sep_width)

    comp_hdr = f"{'COMPOSITE':>15}" if has_ci else f"{'COMPOSITE':>10}"
    stab_hdr = f"  {'STABILITY':<9}" if has_stability else ""
    print(f"  {'#':>2}  {'TICKER':<7} {'GRAPH':>6} {'MARKET':>7} {comp_hdr} "
          f"{'PROB':>6} {'VEL':>6} {'CONV↑':>6}{stab_hdr}  SIGNAL")
    print("-" * sep_width)

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
        high_prob_note = ""
        if r["probability"] > 0.65 and r["signal"] in ("WATCH", "PASS", "HOLD"):
            high_prob_note = " (high market prob)"
        resilient_note = " ⚡ RESILIENT" if r.get("resilience_signal") else ""
        rtm_note       = " ⚡RTM"        if r.get("rtm_flag")          else ""

        if has_ci:
            std      = r.get("composite_std", 0.0)
            comp_str = f"{r['composite']:.3f} ± {std:.3f}"
            comp_col = f"{comp_str:>15}"
        else:
            comp_col = f"{r['composite']:>10.3f}"

        stab_col = f"  {r.get('stability', ''):.<9}" if has_stability else ""

        print(
            f"  {r['rank']:>2}  {r['ticker']:<7} "
            f"{r['graph_score']:>6.3f} "
            f"{r['market_score']:>7.3f} "
            f"{comp_col} "
            f"{r['probability']*100:>5.1f}% "
            f"{r['velocity']:>+6.3f} "
            f"{conv_str:>6}{stab_col}  {sig}{high_prob_note}{resilient_note}{rtm_note}"
        )

    print("=" * sep_width)

    top = results[0]
    print(f"\n  TOP PICK: {top['ticker']}  composite={top['composite']:.3f}  velocity={top['velocity']:+.4f}  {top['signal']}")
    if top["converted_skeptics"]:
        print(f"  {top['converted_skeptics']} skeptic(s) converted to bullish on {top['ticker']} (3× bonus applied)")
    if top.get("resilience_signal"):
        print(f"  ⚡ {top['ticker']} showed resilience through macro shock")
    if has_stability and top.get("stability"):
        print(f"  Signal stability: {top['stability']}")
    # Conviction shape — how thesis held under Round 6 adversarial pressure
    shape = top.get("conviction_shape", "UNTESTED")
    if shape != "UNTESTED":
        drop = top.get("r6_drop_pts", 0)
        rec  = top.get("r7_recovery_pts", 0)
        shape_labels = {
            "HELD":           "⬛ HELD — conviction bulletproof under adversarial pressure",
            "BENT_RECOVERED": "🟨 BENT+RECOVERED — dropped under pressure, then rallied",
            "BENT":           "🟧 BENT — thesis weakened under adversarial injection",
            "BROKE_RECOVERED":"🟥 BROKE+RECOVERED — major drop but partial recovery",
            "BROKE":          "🔴 BROKE — thesis cracked, conviction did not recover",
        }
        print(f"  Conviction shape: {shape_labels.get(shape, shape)} (R6 drop={drop:+.1f}pts, R7 recovery={rec:+.1f}pts)")
    else:
        print(f"  Conviction shape: UNTESTED (run ended before Round 6 adversarial injection)")
    print(f"  Consensus: {top.get('consensus', 'PROVISIONAL')}")
    if top.get("fair_value"):
        print(f"  Fair Value: ${top['fair_value']:.2f}  |  Floor: ${top.get('floor_price', 0):.2f}")
    if top.get("crashed"):
        print(f"  ⚠ {top['penalty_note']}")
    print()
