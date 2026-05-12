#!/usr/bin/env python3
"""
ORACLE Phase 2 — round_loop.py
Main simulation loop: 8 rounds, 11 agents, Neo4j graph, prediction markets.
"""

import os
import sys
import json
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Allow imports from same directory when run standalone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from graph_builder import build_graph, get_stock_context, save_post_to_graph, parse_stances_from_post
from agents        import build_agent_roster
from markets       import build_markets
from director      import Director

OBSIDIAN_BASE = os.path.expanduser(
    "~/Documents/Trading Vault/03_Stock_Analysis/ORACLE/sims"
)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _round_weight(round_num):
    """Round weight scales linearly from 1.0 (round 1) to 2.5 (round 8)."""
    return 1.0 + 1.5 * (round_num - 1) / 7.0


def _generate_agent_post(agent, round_num, stocks, graph_context,
                         prior_posts, market_probs, injection):
    """Worker function for ThreadPoolExecutor. Returns (post_text, conviction, stances)."""
    post_text  = agent.generate_post(
        round_num, stocks, graph_context, prior_posts, market_probs, injection
    )
    conviction = agent.parse_conviction(post_text)
    stances    = parse_stances_from_post(post_text, stocks, agent_name=agent.name)
    return post_text, conviction, stances


def _save_round_to_obsidian(run_id, round_num, round_posts, markets, injection):
    """Write round markdown to Trading Vault."""
    out_dir = Path(OBSIDIAN_BASE) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        "---",
        f"run_id: {run_id}",
        f"round: {round_num}",
        f"date: {date.today().isoformat()}",
        f"injection: \"{injection[:120].replace(chr(34), chr(39))}\"" if injection else "injection: \"\"",
        "---",
        "",
        f"# ORACLE Simulation — Round {round_num:02d}",
        "",
    ]

    if injection:
        lines += [
            "## Director Injection",
            f"> {injection}",
            "",
        ]

    lines += ["## Agent Posts", ""]

    for p in round_posts:
        conv_pct = int(p["conviction"] * 100)
        lines.append(f"### {p['agent']}  `CONVICTION: {conv_pct}%`")
        lines.append("")
        lines.append(p["post"])
        _stance_label = {"bullish": "B", "bearish": "B̶", "weak_bullish": "B(w)"}
        stances_str = "  ".join(
            f"{t}:{_stance_label.get(v, v[0].upper())}"
            for t, v in sorted(p.get("stances", {}).items())
            if v not in ("neutral",)
        )
        if stances_str:
            lines.append(f"\n*Stances: {stances_str}*")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Market probability table
    indiv = {m.tickers[0]: m for m in markets if m.market_type == "individual" and len(m.tickers) == 1}
    if indiv:
        lines += ["## Market Probabilities", ""]
        lines.append("| Ticker | Probability | Velocity | History |")
        lines.append("|--------|-------------|----------|---------|")
        for ticker, m in sorted(indiv.items(), key=lambda x: -x[1].probability):
            hist = " → ".join(f"{p*100:.0f}%" for p in m.prob_history[-4:])
            lines.append(
                f"| {ticker} | {m.probability*100:.1f}% | {m.velocity():+.3f} | {hist} |"
            )
        lines.append("")

    out_path = out_dir / f"round_{round_num:02d}.md"
    out_path.write_text("\n".join(lines))


def _print_prob_snapshot(markets, round_num):
    """Print mini bar-chart probability snapshot to stdout."""
    indiv = {m.tickers[0]: m for m in markets if m.market_type == "individual" and len(m.tickers) == 1}
    if not indiv:
        return

    print(f"\n  ROUND {round_num} PROBABILITIES:")
    for ticker, m in sorted(indiv.items(), key=lambda x: -x[1].probability):
        pct  = m.probability
        bar  = int(pct * 10)
        vel  = m.velocity()
        arrow = "↑" if vel > 0.01 else ("↓" if vel < -0.01 else "→")
        print(f"    {ticker:<6} {'▓'*bar}{'░'*(10-bar)}  {pct*100:4.1f}% {arrow}")


def _neo4j_node_count(driver, run_id):
    try:
        with driver.session() as s:
            return s.run("MATCH (n {run_id: $run_id}) RETURN count(n) AS cnt",
                         run_id=run_id).single()["cnt"]
    except Exception:
        return "?"


# ── Main entry point ───────────────────────────────────────────────────────────

def run_simulation(run_id, stocks, fundamentals, report_text,
                   num_rounds=8, model=None, api_key=None, base_url=None,
                   event_callback=None):
    """
    Run the full ORACLE Phase 2 simulation.

    Returns dict with keys: run_id, stocks, rounds, markets, driver_active.
    event_callback(event_dict) — if provided, emits SSE events and runs agents sequentially.
    """
    def emit(event_type, **kwargs):
        if event_callback:
            event_callback({"type": event_type, **kwargs})
    print(f"\n{'='*60}")
    print(f"  ORACLE SIMULATION — {run_id}")
    print(f"  Stocks: {' | '.join(stocks)}")
    print(f"  Rounds: {num_rounds} | Model: {model or 'default-haiku'}")
    print('='*60)

    # ── 1. Connect Neo4j ───────────────────────────────────────────────────
    driver = None
    try:
        from neo4j import GraphDatabase
        _driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "miroshark2026"))
        _driver.verify_connectivity()
        driver = _driver
        print("  Neo4j connected ✓")
    except Exception as e:
        print(f"  WARNING: Neo4j unavailable ({e}). Graph features disabled.")

    # ── 2. Build agent roster ──────────────────────────────────────────────
    agents = build_agent_roster(stocks, fundamentals, api_key=api_key, model=model, base_url=base_url)
    print(f"  Agents: {len(agents)} instantiated")

    # ── 3. Build graph ─────────────────────────────────────────────────────
    if driver:
        build_graph(driver, run_id, stocks, fundamentals, report_text, agents)
        count = _neo4j_node_count(driver, run_id)
        print(f"  Graph built: {count} nodes for {run_id}")

    # Emit initial graph topology for SSE streaming
    _gnodes = [{"id": f"stock_{t}", "label": t, "type": "stock"} for t in stocks]
    _gnodes += [{"id": f"agent_{a.name}", "label": a.name, "type": "agent"} for a in agents]
    _gedges = []
    for _a in agents:
        for _t in _a.followed_stocks:
            _gedges.append({"source": f"agent_{_a.name}", "target": f"stock_{_t}", "type": "FOLLOWS"})
    emit("graph_built", nodes=_gnodes, edges=_gedges, run_id=run_id)

    # ── 4. Build markets + Director ────────────────────────────────────────
    markets  = build_markets(stocks, fundamentals)
    director = Director(stocks)
    print(f"  Markets: {len(markets)} prediction markets initialized")

    all_rounds = []

    # ── 5. Round loop ──────────────────────────────────────────────────────
    for round_num in range(1, num_rounds + 1):
        print(f"\n{'─'*60}")
        print(f"  ROUND {round_num}/{num_rounds}")
        print(f"{'─'*60}")

        emit("round_start", round_num=round_num, total=num_rounds)

        # Market probs dict for individual stocks
        market_probs = {
            m.tickers[0]: m.probability
            for m in markets
            if m.market_type == "individual" and len(m.tickers) == 1
        }

        # Director: check divergence, get injection
        reserve_inj = director.check_divergence(market_probs)
        round_inj   = director.get_injection(round_num, market_probs)
        injection   = reserve_inj if reserve_inj else round_inj

        if injection:
            short_inj = injection[:80] + "..." if len(injection) > 80 else injection
            print(f"  INJECTION: {short_inj}")
            print(f"  INJECTION CONFIRMED IN PROMPT: {injection[:80]}...")
            emit("director_injection", text=injection, round_num=round_num)
        else:
            print("  INJECTION: (free round)")

        # Prior posts from last round
        prior_posts = all_rounds[-1]["posts"] if all_rounds else []

        round_posts = []
        injection_active = bool(injection)

        if event_callback is not None:
            # ── Sequential mode: emit agent_posting / agent_posted per agent ──
            for agent in agents:
                graph_ctx = get_stock_context(
                    driver, None, agent.name, agent.spec.get("lens", agent.name), run_id
                ) if driver else "Graph context unavailable."

                emit("agent_posting", agent=agent.name, round_num=round_num)

                try:
                    post_text, conviction, stances = _generate_agent_post(
                        agent, round_num, stocks, graph_ctx,
                        prior_posts, market_probs, injection
                    )
                except Exception as e:
                    print(f"  ERROR {agent.name}: {e}")
                    post_text  = f"[Error: {e}]\nCONVICTION: 50%"
                    conviction = 0.5
                    stances    = {t: "neutral" for t in stocks}

                print(f"  [{agent.name}] conviction={int(conviction*100)}%  "
                      f"stances={','.join(f'{t}:{v[0].upper()}' for t,v in stances.items() if v != 'neutral')}")

                new_edges = []
                for ticker, stance in stances.items():
                    if stance in ("bullish", "weak_bullish"):
                        new_edges.append({
                            "source": f"agent_{agent.name}", "target": f"stock_{ticker}",
                            "type": "BULLISH_ON", "conviction": conviction,
                        })
                    elif stance == "bearish":
                        new_edges.append({
                            "source": f"agent_{agent.name}", "target": f"stock_{ticker}",
                            "type": "BEARISH_ON", "conviction": conviction,
                        })

                emit("agent_posted",
                     agent=agent.name,
                     post=post_text,
                     conviction=int(conviction * 100),
                     stances=stances,
                     round_num=round_num,
                     new_edges=new_edges)

                round_posts.append({
                    "agent":      agent.name,
                    "post":       post_text,
                    "conviction": conviction,
                    "stances":    stances,
                })

                if driver:
                    save_post_to_graph(
                        driver, run_id, agent.name, round_num,
                        post_text, conviction, stances,
                        director_injection_active=injection_active,
                    )
        else:
            # ── Parallel mode (CLI / no SSE) ───────────────────────────────
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {}
                for agent in agents:
                    graph_ctx = get_stock_context(
                        driver, None, agent.name, agent.spec.get("lens", agent.name), run_id
                    ) if driver else "Graph context unavailable."

                    fut = executor.submit(
                        _generate_agent_post,
                        agent, round_num, stocks, graph_ctx,
                        prior_posts, market_probs, injection
                    )
                    futures[fut] = agent

                for fut in as_completed(futures):
                    agent = futures[fut]
                    try:
                        post_text, conviction, stances = fut.result()
                    except Exception as e:
                        print(f"  ERROR {agent.name}: {e}")
                        post_text  = f"[Error: {e}]\nCONVICTION: 50%"
                        conviction = 0.5
                        stances    = {t: "neutral" for t in stocks}

                    print(f"  [{agent.name}] conviction={int(conviction*100)}%  "
                          f"stances={','.join(f'{t}:{v[0].upper()}' for t,v in stances.items() if v != 'neutral')}")

                    round_posts.append({
                        "agent":      agent.name,
                        "post":       post_text,
                        "conviction": conviction,
                        "stances":    stances,
                    })

                    if driver:
                        save_post_to_graph(
                            driver, run_id, agent.name, round_num,
                            post_text, conviction, stances,
                            director_injection_active=injection_active,
                        )

        # Update prediction markets
        for market in markets:
            market.update_from_graph(driver, run_id, round_num, stocks)

        # Capture end-of-round market probabilities for chart + JSON output
        end_market_probs = {
            m.tickers[0]: m.probability
            for m in markets
            if m.market_type == "individual" and len(m.tickers) == 1
        }

        # Save round to Obsidian
        try:
            _save_round_to_obsidian(run_id, round_num, round_posts, markets, injection)
        except Exception as e:
            print(f"  WARNING: Obsidian write failed: {e}")

        # Print probability snapshot
        _print_prob_snapshot(markets, round_num)

        end_prob_deltas = {
            m.tickers[0]: round(m.velocity(), 4)
            for m in markets
            if m.market_type == "individual" and len(m.tickers) == 1
        }
        emit("round_complete",
             round_num=round_num,
             market_probs=end_market_probs,
             prob_deltas=end_prob_deltas)

        all_rounds.append({
            "round":        round_num,
            "injection":    injection,
            "posts":        round_posts,
            "market_probs": end_market_probs,
        })

    # ── 6. Close driver ────────────────────────────────────────────────────
    if driver:
        driver.close()

    print(f"\n{'='*60}")
    print(f"  Simulation complete: {len(all_rounds)}/{num_rounds} rounds")
    print('='*60)

    return {
        "run_id":        run_id,
        "stocks":        stocks,
        "rounds":        all_rounds,
        "markets":       markets,
        "driver_active": driver is not None,
    }
