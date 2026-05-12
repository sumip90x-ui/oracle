#!/usr/bin/env python3
"""
ORACLE Phase 2 — graph_builder.py
Neo4j graph construction and query layer.
"""

import re
import os
from datetime import date

TODAY = date.today().isoformat()  # e.g. "2026-05-12"

ROUND_WEIGHTS = {1: 1.0, 2: 1.2, 3: 1.4, 4: 1.6, 5: 1.8, 6: 2.0, 7: 2.3, 8: 2.5}

AGENT_DISPLAY_NAMES = {
    "growth_compounder":           "Lynch",
    "probabilist":                 "Thorp",
    "tail_risk_skeptic":           "Taleb",
    "quality_compounder":          "Munger",
    "momentum_trader":             "Darvas",
    "biotech_specialist":          "BioMD",
    "saas_specialist":             "SaaS",
    "data_ai_specialist":          "DataAI",
    "short_seller":                "Chanos",
    "opportunity_cost_accountant": "OpCost",
    "catalyst_skeptic":            "CatSkep",
}

NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "miroshark2026")


# ── Driver factory ─────────────────────────────────────────────────────────────

def get_driver():
    """Return connected Neo4j driver or None with a warning."""
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
        driver.verify_connectivity()
        return driver
    except Exception as e:
        print(f"WARNING: Neo4j unavailable ({e}). Graph features disabled.")
        return None


# ── Internal helpers ───────────────────────────────────────────────────────────

def _extract_catalysts(report_text, run_id):
    """Parse CATALYST: lines plus FDA / earnings / Phase-3 mentions."""
    catalysts = []
    seen = set()

    # Explicit CATALYST: tags
    for m in re.finditer(r'CATALYST:\s*(.+?)(?:\n|$)', report_text, re.IGNORECASE):
        text = m.group(1).strip()
        if text in seen:
            continue
        seen.add(text)
        date_m = re.search(r'\d{4}-\d{2}-\d{2}', text)
        date_str = date_m.group(0) if date_m else ""
        is_past = bool(date_str and date_str < TODAY)
        catalysts.append({"text": text, "date": date_str, "is_past": is_past, "run_id": run_id})

    # Inline FDA / Earnings / Phase-3 patterns
    patterns = [
        r'FDA\s+\w+(?:\s+\w+){0,4}',
        r'(?:Q[1-4]|FY)\s*20\d{2}\s+earnings',
        r'Phase\s+3\s+(?:trial|data|results?|readout)\s+(?:for\s+)?\w+',
    ]
    for pat in patterns:
        for m in re.finditer(pat, report_text, re.IGNORECASE):
            text = m.group(0).strip()
            if text in seen:
                continue
            seen.add(text)
            window = report_text[max(0, m.start() - 60): m.end() + 60]
            date_m = re.search(r'\d{4}-\d{2}-\d{2}', window)
            date_str = date_m.group(0) if date_m else ""
            is_past = bool(date_str and date_str < TODAY)
            catalysts.append({"text": text, "date": date_str, "is_past": is_past, "run_id": run_id})

    return catalysts


def _extract_theses(report_text, run_id):
    """Parse BULL:, BEAR:, SCOUT VERDICT: lines from report."""
    theses = []
    for thesis_type, pattern in [
        ("bull",          r'BULL:\s*(.+?)(?:\n|$)'),
        ("bear",          r'BEAR:\s*(.+?)(?:\n|$)'),
        ("scout_verdict", r'SCOUT\s+VERDICT:\s*(.+?)(?:\n|$)'),
    ]:
        for m in re.finditer(pattern, report_text, re.IGNORECASE):
            theses.append({"text": m.group(1).strip(), "type": thesis_type, "run_id": run_id})
    return theses


def _parse_scout_verdicts(report_text, stocks):
    """Return {ticker: verdict_string} from report."""
    verdicts = {}
    for ticker in stocks:
        # Try TICKER … SCOUT VERDICT: VALUE within 500 chars
        pattern = rf'{re.escape(ticker)}.{{0,500}}?SCOUT\s+VERDICT:\s*(INVESTIGATE\s+FURTHER|PASS)'
        m = re.search(pattern, report_text, re.IGNORECASE | re.DOTALL)
        verdicts[ticker] = m.group(1).strip() if m else "UNKNOWN"
    return verdicts


def _parse_conviction_from_report(report_text, ticker):
    """Pull a conviction score for a ticker from the Think Tank report (0-1)."""
    # Look for CONVICTION: XX% within 300 chars of the ticker
    for m in re.finditer(re.escape(ticker), report_text, re.IGNORECASE):
        window = report_text[m.start(): m.start() + 300]
        cm = re.search(r'CONVICTION:\s*(\d+)%', window, re.IGNORECASE)
        if cm:
            return float(cm.group(1)) / 100.0
    return None


# ── Public API ─────────────────────────────────────────────────────────────────

def build_graph(driver, run_id, stocks, fundamentals, report_text, agents):
    """
    Build the full simulation graph in Neo4j for this run_id.
    Clears any prior data for run_id, then creates all nodes and relationships.
    """
    if driver is None:
        return

    scout_verdicts = _parse_scout_verdicts(report_text, stocks)
    catalysts      = _extract_catalysts(report_text, run_id)
    theses         = _extract_theses(report_text, run_id)

    # Build stock data list
    stocks_data = []
    for ticker in stocks:
        f = fundamentals.get(ticker, {}) if fundamentals else {}
        price          = f.get("price") or 0.0
        analyst_target = f.get("analyst_target")
        analyst_upside = ((analyst_target - price) / price * 100) if (analyst_target and price) else 0.0
        rev_growth     = f.get("revenue_growth_yoy") or 0.0
        sector         = f.get("sector") or "Unknown"
        verdict        = scout_verdicts.get(ticker, "UNKNOWN")
        # Conviction from report or derive from verdict
        conv = _parse_conviction_from_report(report_text, ticker)
        if conv is None:
            conv = 0.7 if "INVESTIGATE" in verdict.upper() else (0.3 if verdict.upper() == "PASS" else 0.5)
        stocks_data.append({
            "ticker":          ticker,
            "price":           float(price),
            "revenue_growth":  float(rev_growth),
            "eps_ttm":         float(f.get("eps_ttm") or 0.0),
            "eps_forward":     float(f.get("eps_forward") or 0.0),
            "short_interest":  float(f.get("short_interest_pct") or 0.0),
            "analyst_upside":  float(analyst_upside),
            "sector":          sector,
            "conviction_score": float(conv),
            "scout_verdict":   verdict,
            "run_id":          run_id,
        })

    with driver.session() as s:
        # 1. Clear prior run
        s.run("MATCH (n {run_id: $run_id}) DETACH DELETE n", run_id=run_id)

        # 2. Stock nodes
        s.run("""
            UNWIND $stocks AS d
            CREATE (st:Stock {
                run_id:          d.run_id,
                ticker:          d.ticker,
                price:           d.price,
                revenue_growth:  d.revenue_growth,
                eps_ttm:         d.eps_ttm,
                eps_forward:     d.eps_forward,
                short_interest:  d.short_interest,
                analyst_upside:  d.analyst_upside,
                sector:          d.sector,
                conviction_score: d.conviction_score,
                scout_verdict:   d.scout_verdict
            })
        """, stocks=stocks_data)

        # 3. Sector nodes + BELONGS_TO
        sectors = list({d["sector"] for d in stocks_data})
        for sec in sectors:
            s.run("""
                MERGE (sec:Sector {run_id: $run_id, name: $name})
                WITH sec
                MATCH (st:Stock {run_id: $run_id, sector: $name})
                MERGE (st)-[:BELONGS_TO]->(sec)
            """, run_id=run_id, name=sec)

        # 4. COMPETES_WITH + SAME_SECTOR (both directions, same sector)
        s.run("""
            MATCH (s1:Stock {run_id: $run_id})-[:BELONGS_TO]->(sec:Sector)<-[:BELONGS_TO]-(s2:Stock {run_id: $run_id})
            WHERE s1.ticker < s2.ticker
            MERGE (s1)-[:COMPETES_WITH]->(s2)
            MERGE (s2)-[:COMPETES_WITH]->(s1)
            MERGE (s1)-[:SAME_SECTOR]->(s2)
            MERGE (s2)-[:SAME_SECTOR]->(s1)
        """, run_id=run_id)

        # 5. Catalyst nodes + HAS_CATALYST
        for cat in catalysts:
            s.run("""
                CREATE (c:Catalyst {
                    run_id:  $run_id,
                    text:    $text,
                    date:    $date,
                    is_past: $is_past
                })
                WITH c
                MATCH (st:Stock {run_id: $run_id})
                MERGE (st)-[:HAS_CATALYST]->(c)
            """, run_id=run_id, text=cat["text"], date=cat["date"], is_past=cat["is_past"])

        # 6. Thesis nodes + HAS_THESIS
        for th in theses:
            s.run("""
                CREATE (t:Thesis {run_id: $run_id, text: $text, type: $type})
                WITH t
                MATCH (st:Stock {run_id: $run_id})
                MERGE (st)-[:HAS_THESIS]->(t)
            """, run_id=run_id, text=th["text"], type=th["type"])

        # 7. Agent nodes
        agents_data = [
            {
                "name":         a.name,
                "display_name": AGENT_DISPLAY_NAMES.get(a.name, a.name),
                "layer":        a.spec.get("layer", 1),
                "lens":         a.spec.get("lens", a.name),
                "run_id":       run_id,
            }
            for a in agents
        ]
        s.run("""
            UNWIND $agents AS d
            CREATE (ag:Agent {
                run_id: d.run_id, name: d.name, display_name: d.display_name,
                layer: d.layer, lens: d.lens
            })
        """, agents=agents_data)

        # 8. FOLLOWS edges
        for agent in agents:
            for ticker in agent.followed_stocks:
                s.run("""
                    MATCH (ag:Agent {run_id: $run_id, name: $agent_name})
                    MATCH (st:Stock {run_id: $run_id, ticker: $ticker})
                    MERGE (ag)-[:FOLLOWS]->(st)
                """, run_id=run_id, agent_name=agent.name, ticker=ticker)

        # 9. DEBATES_WITH (short_seller debates quality_compounder + growth_compounder)
        s.run("""
            MATCH (ss:Agent  {run_id: $run_id, name: 'short_seller'})
            MATCH (qc:Agent  {run_id: $run_id, name: 'quality_compounder'})
            MATCH (gc:Agent  {run_id: $run_id, name: 'growth_compounder'})
            MERGE (ss)-[:DEBATES_WITH]->(qc)
            MERGE (ss)-[:DEBATES_WITH]->(gc)
        """, run_id=run_id)


def get_stock_context(driver, ticker, agent_name, agent_lens, run_id=None):
    """
    Run lens-specific Cypher and return formatted context string for an agent.
    ticker is accepted for interface compatibility but lens drives what is returned.
    """
    if driver is None or not run_id:
        return "Graph context unavailable."

    try:
        with driver.session() as s:
            return _query_by_lens(s, run_id, agent_name, agent_lens)
    except Exception as e:
        return f"Graph context error: {e}"


def _query_by_lens(session, run_id, agent_name, lens):
    """Dispatch to lens-specific query and format result."""
    lens = (lens or agent_name).lower()

    if lens == "growth_compounder":
        rows = session.run("""
            MATCH (st:Stock {run_id: $run_id})
            OPTIONAL MATCH (st)-[:BELONGS_TO]->(sec:Sector)<-[:BELONGS_TO]-(peer:Stock {run_id: $run_id})
            WHERE peer.ticker <> st.ticker
            RETURN st.ticker AS ticker, st.price AS price, st.revenue_growth AS rev_growth,
                   st.sector AS sector, collect(DISTINCT peer.ticker + ':' + toString(round(peer.revenue_growth*100)/100.0) + '%') AS peers
            ORDER BY st.revenue_growth DESC
        """, run_id=run_id).data()
        lines = ["GRAPH CONTEXT — growth_compounder | Stocks ranked by revenue growth:"]
        for r in rows:
            peers_str = ", ".join(r["peers"]) if r["peers"] else "none"
            lines.append(f"  {r['ticker']:6s} ${r['price']:.2f} | Rev Growth: {r['rev_growth']*100:.1f}% | Sector: {r['sector']} | Sector Peers: {peers_str}")
        return "\n".join(lines)

    elif lens == "probabilist":
        rows = session.run("""
            MATCH (st:Stock {run_id: $run_id})
            RETURN st.ticker AS ticker, st.price AS price, st.eps_forward AS eps_fwd, st.eps_ttm AS eps_ttm, st.revenue_growth AS rev_growth
            ORDER BY st.eps_forward DESC
        """, run_id=run_id).data()
        lines = ["GRAPH CONTEXT — probabilist | Stocks ranked by eps_forward (Kelly base):"]
        for r in rows:
            lines.append(f"  {r['ticker']:6s} ${r['price']:.2f} | EPS Fwd: {r['eps_fwd']} | EPS TTM: {r['eps_ttm']} | Rev Growth: {r['rev_growth']*100:.1f}%")
        return "\n".join(lines)

    elif lens == "tail_risk_skeptic":
        rows = session.run("""
            MATCH (st:Stock {run_id: $run_id})
            OPTIONAL MATCH (st)-[:HAS_CATALYST]->(c:Catalyst {run_id: $run_id}) WHERE c.is_past = true
            RETURN st.ticker AS ticker, st.price AS price, st.short_interest AS short_int,
                   collect(DISTINCT c.text) AS past_cats
            ORDER BY st.short_interest DESC
        """, run_id=run_id).data()
        lines = ["GRAPH CONTEXT — tail_risk_skeptic | Stocks by short interest + past catalysts:"]
        for r in rows:
            cats = "; ".join(r["past_cats"][:3]) if r["past_cats"] else "none"
            lines.append(f"  {r['ticker']:6s} ${r['price']:.2f} | Short Interest: {r['short_int']:.1f}% | Past Catalysts: {cats}")
        return "\n".join(lines)

    elif lens == "quality_compounder":
        rows = session.run("""
            MATCH (st:Stock {run_id: $run_id})
            RETURN st.ticker AS ticker, st.price AS price, st.conviction_score AS conv,
                   st.scout_verdict AS verdict, st.eps_ttm AS eps_ttm
            ORDER BY st.conviction_score DESC
        """, run_id=run_id).data()
        lines = ["GRAPH CONTEXT — quality_compounder | Stocks ordered by conviction score:"]
        for r in rows:
            lines.append(f"  {r['ticker']:6s} ${r['price']:.2f} | Conviction: {r['conv']:.2f} | Verdict: {r['verdict']} | EPS TTM: {r['eps_ttm']}")
        return "\n".join(lines)

    elif lens == "momentum_trader":
        rows = session.run("""
            MATCH (st:Stock {run_id: $run_id})
            RETURN st.ticker AS ticker, st.price AS price, st.analyst_upside AS upside, st.revenue_growth AS rev_growth
            ORDER BY st.analyst_upside DESC
        """, run_id=run_id).data()
        lines = ["GRAPH CONTEXT — momentum_trader | Stocks ranked by analyst upside:"]
        for r in rows:
            lines.append(f"  {r['ticker']:6s} ${r['price']:.2f} | Analyst Upside: {r['upside']:.1f}% | Rev Growth: {r['rev_growth']*100:.1f}%")
        return "\n".join(lines)

    elif lens in ("short_seller", "opportunity_cost_accountant"):
        rows = session.run("""
            MATCH (st:Stock {run_id: $run_id})
            RETURN st.ticker AS ticker, st.price AS price, st.eps_ttm AS eps_ttm, st.eps_forward AS eps_fwd,
                   st.short_interest AS short_int, st.revenue_growth AS rev_growth,
                   st.conviction_score AS conv
            ORDER BY st.conviction_score DESC
        """, run_id=run_id).data()
        lines = [f"GRAPH CONTEXT — {lens} | All stocks ranked by conviction:"]
        for r in rows:
            lines.append(
                f"  {r['ticker']:6s} ${r['price']:.2f} | EPS TTM: {r['eps_ttm']} | EPS Fwd: {r['eps_fwd']} "
                f"| Short: {r['short_int']:.1f}% | RevGrowth: {r['rev_growth']*100:.1f}% | Conv: {r['conv']:.2f}"
            )
        return "\n".join(lines)

    elif lens == "catalyst_skeptic":
        rows = session.run("""
            MATCH (st:Stock {run_id: $run_id})-[:HAS_CATALYST]->(c:Catalyst {run_id: $run_id})
            WHERE c.is_past = true
            RETURN st.ticker AS ticker, st.price AS price,
                   collect(DISTINCT c.text + ' [' + c.date + ']') AS past_cats
        """, run_id=run_id).data()
        lines = ["GRAPH CONTEXT — catalyst_skeptic | Stocks with past catalysts:"]
        for r in rows:
            cats = "; ".join(r["past_cats"][:4]) if r["past_cats"] else "none recorded"
            lines.append(f"  {r['ticker']:6s} ${r['price']:.2f} | Past Catalysts: {cats}")
        if not rows:
            lines.append("  No past catalysts found in graph.")
        return "\n".join(lines)

    elif lens in ("biotech_specialist", "saas_specialist", "data_ai_specialist"):
        rows = session.run("""
            MATCH (ag:Agent {run_id: $run_id, name: $agent_name})-[:FOLLOWS]->(st:Stock {run_id: $run_id})
            OPTIONAL MATCH (st)-[:HAS_CATALYST]->(c:Catalyst {run_id: $run_id})
            OPTIONAL MATCH (st)-[:HAS_THESIS]->(t:Thesis {run_id: $run_id})
            RETURN st.ticker AS ticker, st.price AS price, st.sector AS sector,
                   st.conviction_score AS conv, st.scout_verdict AS verdict,
                   collect(DISTINCT c.text + ' [past=' + toString(c.is_past) + ']') AS catalysts,
                   collect(DISTINCT t.type + ': ' + t.text) AS theses
        """, run_id=run_id, agent_name=agent_name).data()
        lines = [f"GRAPH CONTEXT — {lens} | Followed stocks with catalysts + theses:"]
        for r in rows:
            lines.append(f"  {r['ticker']:6s} ${r['price']:.2f} | {r['sector']} | Conv: {r['conv']:.2f} | Verdict: {r['verdict']}")
            for cat in r["catalysts"][:3]:
                lines.append(f"    CATALYST: {cat}")
            for th in r["theses"][:3]:
                lines.append(f"    {th}")
        if not rows:
            lines.append("  No followed stocks found — check FOLLOWS edges.")
        return "\n".join(lines)

    else:
        # Generic fallback
        rows = session.run("""
            MATCH (st:Stock {run_id: $run_id})
            RETURN st.ticker AS ticker, st.price AS price, st.revenue_growth AS rev_growth,
                   st.conviction_score AS conv, st.sector AS sector
            ORDER BY st.conviction_score DESC
        """, run_id=run_id).data()
        lines = [f"GRAPH CONTEXT — {lens} | All stocks:"]
        for r in rows:
            lines.append(f"  {r['ticker']:6s} ${r['price']:.2f} | RevGrowth: {r['rev_growth']*100:.1f}% | Conv: {r['conv']:.2f} | Sector: {r['sector']}")
        return "\n".join(lines)


def save_post_to_graph(driver, run_id, agent_name, round_num, post_text, conviction,
                       stances, director_injection_active=False):
    """
    Create POST node and BULLISH_ON / BEARISH_ON edges. Detect stance changes.
    round_weight scales linearly from 1.0 (round 1) to 2.5 (round 8).
    """
    if driver is None:
        return

    round_weight = ROUND_WEIGHTS.get(round_num, 1.0)

    with driver.session() as s:
        # Create POST node
        s.run("""
            MATCH (ag:Agent {run_id: $run_id, name: $agent_name})
            CREATE (p:Post {
                run_id:      $run_id,
                agent_name:  $agent_name,
                round_num:   $round_num,
                post_text:   $post_text,
                conviction:  $conviction
            })
            MERGE (ag)-[:WROTE]->(p)
        """, run_id=run_id, agent_name=agent_name, round_num=round_num,
             post_text=post_text[:2000], conviction=conviction)

        for ticker, stance in stances.items():
            if stance == "neutral":
                continue

            # weak_bullish: record as BULLISH_ON but cap conviction at 0.3
            effective_conviction = min(conviction, 0.3) if stance == "weak_bullish" else conviction
            effective_stance     = "bullish" if stance == "weak_bullish" else stance

            # Determine previous stance (round_num - 1)
            prev_round = round_num - 1
            prev_stance_type = None
            if prev_round >= 1:
                rec = s.run("""
                    MATCH (ag:Agent {run_id: $run_id, name: $agent_name})-[r]->(st:Stock {run_id: $run_id, ticker: $ticker})
                    WHERE (r:BULLISH_ON OR r:BEARISH_ON) AND r.round_num = $prev_round
                    RETURN type(r) AS rel_type
                    LIMIT 1
                """, run_id=run_id, agent_name=agent_name, ticker=ticker, prev_round=prev_round).single()
                if rec:
                    prev_stance_type = rec["rel_type"].lower().replace("_on", "")  # "bullish" or "bearish"

            rel_type = "BULLISH_ON" if effective_stance == "bullish" else "BEARISH_ON"
            s.run(f"""
                MATCH (ag:Agent {{run_id: $run_id, name: $agent_name}})
                MATCH (st:Stock {{run_id: $run_id, ticker: $ticker}})
                CREATE (ag)-[:{rel_type} {{
                    conviction:   $conviction,
                    round_num:    $round_num,
                    round_weight: $round_weight
                }}]->(st)
            """, run_id=run_id, agent_name=agent_name, ticker=ticker,
                 conviction=effective_conviction, round_num=round_num, round_weight=round_weight)

            # Detect stance change
            if prev_stance_type and prev_stance_type != effective_stance:
                s.run("""
                    MATCH (ag:Agent {run_id: $run_id, name: $agent_name})
                    MATCH (st:Stock  {run_id: $run_id, ticker: $ticker})
                    CREATE (ag)-[:CHANGED_STANCE {
                        from_stance:              $from_stance,
                        to_stance:                $to_stance,
                        round_num:                $round_num,
                        director_injection_active: $inj
                    }]->(st)
                """, run_id=run_id, agent_name=agent_name, ticker=ticker,
                     from_stance=prev_stance_type, to_stance=effective_stance,
                     round_num=round_num, inj=director_injection_active)


def query_final_verdict(driver, run_id, stocks):
    """
    For each stock compute:
      net_score = sum(conviction * round_weight) BULLISH - BEARISH
      converted_skeptics = count CHANGED_STANCE to_stance='bullish'
    Returns list of dicts sorted by net_score descending.
    """
    if driver is None:
        return [{"ticker": t, "net_score": 0.0, "converted_skeptics": 0} for t in stocks]

    results = []
    with driver.session() as s:
        for ticker in stocks:
            bull = s.run("""
                MATCH (ag:Agent {run_id: $run_id})-[r:BULLISH_ON]->(st:Stock {run_id: $run_id, ticker: $ticker})
                RETURN coalesce(sum(r.conviction * r.round_weight), 0.0) AS total
            """, run_id=run_id, ticker=ticker).single()["total"]

            bear = s.run("""
                MATCH (ag:Agent {run_id: $run_id})-[r:BEARISH_ON]->(st:Stock {run_id: $run_id, ticker: $ticker})
                RETURN coalesce(sum(r.conviction * r.round_weight), 0.0) AS total
            """, run_id=run_id, ticker=ticker).single()["total"]

            converted = s.run("""
                MATCH (ag:Agent {run_id: $run_id})-[r:CHANGED_STANCE {to_stance: 'bullish'}]->(st:Stock {run_id: $run_id, ticker: $ticker})
                RETURN count(r) AS cnt
            """, run_id=run_id, ticker=ticker).single()["cnt"]

            results.append({
                "ticker":             ticker,
                "net_score":          float(bull) - float(bear),
                "converted_skeptics": int(converted),
            })

    results.sort(key=lambda x: x["net_score"], reverse=True)
    return results


def parse_stances_from_post(post_text, stocks, agent_name=""):
    """
    For each ticker scan a 250-char context window around each mention.
    Matches both $TICKER and bare TICKER formats.
    Returns {ticker: 'bullish'|'bearish'|'neutral'}.

    short_seller bias: defaults to bearish when no explicit positive signal found,
    since short_seller language ("fraud risk", "overvalued", "skeptical") may fall
    outside the window even at 250 chars. Flips to bullish only on explicit positive.
    """
    POSITIVE = {"buy", "bullish", "long", "upside", "conviction", "opportunity",
                "undervalued", "strong", "own", "hold", "accumulate", "love"}
    NEGATIVE = {"sell", "bearish", "short", "avoid", "overvalued", "risk",
                "pass", "cut", "weak", "dump", "reduce", "exit", "concern"}

    is_short_seller = (agent_name == "short_seller")

    text_lower = post_text.lower()

    # Global sentiment used as weak-bullish tiebreaker for non-short_seller agents
    global_pos = sum(1 for w in POSITIVE if w in text_lower)
    global_neg = sum(1 for w in NEGATIVE if w in text_lower)

    stances = {}

    for ticker in stocks:
        t_lower = ticker.lower()
        pos, neg = 0, 0
        start = 0
        found = False

        # Match both "$TICKER" and bare "TICKER" (word-boundary aware via the loop)
        search_variants = [t_lower, f"${t_lower}"]

        for variant in search_variants:
            start = 0
            while True:
                idx = text_lower.find(variant, start)
                if idx == -1:
                    break
                found = True
                # Bug 2 fix: expanded window from 150 to 250 chars (125 each side)
                w_start = max(0, idx - 125)
                w_end   = min(len(text_lower), idx + len(variant) + 125)
                window  = text_lower[w_start:w_end]
                for word in POSITIVE:
                    if word in window:
                        pos += 1
                for word in NEGATIVE:
                    if word in window:
                        neg += 1
                start = idx + 1

        if not found:
            # short_seller defaults to bearish when ticker not mentioned
            stances[ticker] = "bearish" if is_short_seller else "neutral"
        elif pos > neg:
            # short_seller: only go bullish if explicitly very positive (pos >= 2 AND no negative)
            if is_short_seller and (pos < 2 or neg > 0):
                stances[ticker] = "bearish"
            else:
                stances[ticker] = "bullish"
        elif neg > pos:
            stances[ticker] = "bearish"
        else:
            # Tie: short_seller defaults bearish; others use global post sentiment as weak signal
            if is_short_seller:
                stances[ticker] = "bearish"
            elif global_pos > global_neg:
                print(f"  [parse_stances] {ticker}: tie resolved to weak_bullish via global sentiment (pos={global_pos}, neg={global_neg})")
                stances[ticker] = "weak_bullish"
            else:
                stances[ticker] = "neutral"

    return stances
