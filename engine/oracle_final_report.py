#!/usr/bin/env python3
"""
oracle_final_report.py — Generate investment letter memos from Think Tank + Simulation data.
Written in the style of Einhorn/Ackman hedge fund letters — prose, narrative, conviction.
NOT tables. NOT spreadsheets. Professional investment communications.

v2 improvements:
  Fix #1/#20 : Signal Stability (PROVISIONAL / FRAGILE / STABLE) from run history DB
  Fix #5     : Divergence Warning Block at top of report
  Fix #6     : Signal Conflict Resolution section
  Fix #9     : Injection context logged in rerun seed
  Fix #10    : Run History Database (oracle_history.db)
  Fix #11    : Recommended Action column per ticker
  Fix #16    : Regression-to-mean flag
  Fix #17    : Retrospective contrarian flagging
  Fix #18    : Risk Category Tags
  PASS TABLE : PASS Classification Table as Section I
"""

import json, re, os, sys, time, sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

ORACLE_DIR   = Path.home() / "ORACLE"
REPORTS_DIR  = ORACLE_DIR / "reports"
FINAL_DIR    = REPORTS_DIR / "final"
CACHE_DIR    = ORACLE_DIR / "cache"
SIMS_DIR     = ORACLE_DIR / "sims"
DB_PATH      = ORACLE_DIR / "oracle_history.db"

SIGNAL_EMOJI = {
    "STRONG_BUY": "**",
    "BUY":        "*",
    "HOLD":       "~",
    "WATCH":      "~",
    "PASS":       "-",
}

AGENT_NAMES = {
    "growth_compounder":           "Lynch/Fisher",
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
    "technical_analyst":           "SM-Technical",
    "fidelity_mirror":             "Fidelity-Mirror",
}

DIVIDER = "─" * 69


# ── Run History DB (Fix #10) ───────────────────────────────────────────────────

def init_history_db():
    """Create oracle_history.db and runs table if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id              TEXT,
            ticker              TEXT,
            date_analyzed       TEXT,
            signal              TEXT,
            probability         REAL,
            composite           REAL,
            tt_overall          TEXT,
            tt_score            TEXT,
            catalyst            TEXT,
            velocity            REAL,
            converted_skeptics  INTEGER,
            injections_fired    TEXT,
            created_at          TEXT
        )
    """)
    conn.commit()
    conn.close()


def write_to_history(run_id: str, ticker: str, data: dict):
    """Insert one ticker result row into oracle_history.db."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            INSERT INTO runs
                (run_id, ticker, date_analyzed, signal, probability, composite,
                 tt_overall, tt_score, catalyst, velocity, converted_skeptics,
                 injections_fired, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            run_id,
            ticker,
            data.get("date_analyzed", date.today().isoformat()),
            data.get("signal", ""),
            data.get("probability", 0.0),
            data.get("composite", 0.0),
            data.get("tt_overall", ""),
            data.get("tt_score", ""),
            data.get("catalyst", ""),
            data.get("velocity", 0.0),
            data.get("converted_skeptics", 0),
            json.dumps(data.get("injections_fired", [])),
            datetime.now().isoformat(),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [DB write failed for {ticker}: {e}]", file=sys.stderr)


def query_history(ticker: str) -> list:
    """Return all prior run rows for a ticker, oldest first."""
    try:
        init_history_db()
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            "SELECT run_id, date_analyzed, signal, probability, composite, tt_overall "
            "FROM runs WHERE ticker=? ORDER BY created_at ASC",
            (ticker,)
        ).fetchall()
        conn.close()
        return [
            {
                "run_id":       r[0],
                "date_analyzed": r[1],
                "signal":       r[2],
                "probability":  r[3],
                "composite":    r[4],
                "tt_overall":   r[5],
            }
            for r in rows
        ]
    except Exception:
        return []


# ── Data loaders ───────────────────────────────────────────────────────────────

def load_sim(sim_path: str) -> dict:
    return json.loads(Path(sim_path).read_text())


def load_screener_cache() -> dict:
    for day_offset in range(3):
        d    = (date.today() - timedelta(day_offset)).strftime("%Y%m%d")
        path = CACHE_DIR / f"fundamentals_{d}.json"
        if path.exists():
            raw = json.loads(path.read_text())
            return raw.get("data", raw)
    return {}


def parse_layer5_blocks(report_text: str) -> dict:
    import datetime as _dt
    current_year = _dt.date.today().year
    blocks = {}
    for m in re.finditer(r"---STOCK:\s*([A-Z]+)---(.*?)---END---", report_text, re.DOTALL):
        ticker  = m.group(1).strip()
        content = m.group(2).strip()
        parsed  = {}
        fields  = ["SCOUT", "SKEPTIC", "FUNDAMENTALS", "TECH.MACRO",
                   "PANEL_CONSENSUS", "OVERALL", "CATALYST", "KILL CONDITION"]
        for field in fields:
            fm = re.search(rf"(?m)^{field}:\s*(.+)", content)
            if fm:
                key = re.sub(r"[^a-z0-9]", "_", field.lower()).strip("_")
                val = fm.group(1).strip()
                # Catalyst date validation: flag and scrub past-year dates
                if key == "catalyst":
                    past_years = re.findall(r'\b(20\d\d)\b', val)
                    stale = [y for y in past_years if int(y) < current_year]
                    if stale:
                        val = f"[DATE MAY BE STALE — verify] {val}"
                parsed[key] = val
        blocks[ticker] = parsed
    return blocks


def parse_layer6_blocks(report_text: str) -> dict:
    """Extract Layer 6 synthesis blocks per ticker."""
    blocks = {}
    layer6_start = report_text.find("## LAYER 6")
    if layer6_start == -1:
        layer6_start = report_text.find("LAYER 6")
    if layer6_start == -1:
        return blocks
    layer6_text = report_text[layer6_start:]
    for m in re.finditer(r"### TICKER:\s*([A-Z]+)\n(.*?)(?=### TICKER:|$)", layer6_text, re.DOTALL):
        ticker  = m.group(1).strip()
        content = m.group(2).strip()
        parsed  = {}
        for field in ["VERDICT", "CONVICTION", "MUNGER INVERSION", "KELLY SIZE",
                      "FLYWHEEL", "TAIL RISK", "DECISION QUALITY",
                      "TOP BULL ARGUMENT", "TOP BEAR ARGUMENT",
                      "CATALYST", "SELL TRIGGER"]:
            pattern = rf"\*\*{re.escape(field)}:\*\*\s*(.*?)(?=\n\*\*[A-Z]|\n---|\Z)"
            fm = re.search(pattern, content, re.DOTALL)
            if fm:
                key = re.sub(r"[^a-z0-9]", "_", field.lower()).strip("_")
                parsed[key] = fm.group(1).strip().replace("\n", " ")
        parsed["_raw"] = content
        blocks[ticker] = parsed
    return blocks


def get_top_posts(rounds_data: list, ticker: str, n: int = 4) -> list:
    posts = []
    for rd in rounds_data:
        for p in rd["posts"]:
            if ticker in p.get("post", ""):
                posts.append({
                    "round":      rd["round"],
                    "agent":      AGENT_NAMES.get(p["agent"], p["agent"]),
                    "conviction": p.get("conviction", 0.5),
                    "post":       p["post"][:500],
                })
    posts.sort(key=lambda x: -x["conviction"])
    return posts[:n]


def prob_bar(prob: float, width: int = 12) -> str:
    filled = int(prob * width)
    return "█" * filled + "░" * (width - filled)


def agent_display(agent_key: str) -> str:
    return AGENT_NAMES.get(agent_key, agent_key)


def _load_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    for env_path in ["~/ORACLE/.env", "~/.hermes/.env", "~/Documents/MiroShark/.env"]:
        p = Path(env_path).expanduser()
        if p.exists():
            for line in p.read_text().splitlines():
                if "OPENROUTER_API_KEY" in line and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    return ""


def _call_api(system_prompt: str, user_prompt: str, api_key: str, max_tokens: int = 1500) -> str:
    """Make a single OpenRouter API call. Returns empty string on failure."""
    if not api_key:
        return ""
    try:
        import requests
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://oracle.local",
                "X-Title":       "ORACLE Investment Memo",
            },
            json={
                "model":      "anthropic/claude-opus-4.7",
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]},
                    {"role": "user",   "content": user_prompt},
                ],
            },
            timeout=120,
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  [API call failed: {e}]", file=sys.stderr)
        return ""


# ── TT parsing helpers ─────────────────────────────────────────────────────────

def _parse_tt_info(l5: dict) -> tuple:
    """
    Extract TT score (0-10 normalized) and skeptic verdict from layer5.
    Returns (tt_score_int, skeptic_verdict_str, tt_overall_str).
    """
    overall  = l5.get("overall", "")
    panel_c  = l5.get("panel_consensus", "")
    skeptic  = l5.get("skeptic", "")

    # Score: try fraction first (e.g. "3/4"), then keyword
    tt_score = 5
    text = overall + " " + panel_c
    m = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if m:
        num   = int(m.group(1))
        denom = int(m.group(2))
        tt_score = round(num / denom * 10) if denom else 5
    else:
        upper = overall.upper()
        if "STRONG_BUY" in upper or "STRONG BUY" in upper:
            tt_score = 9
        elif "BUY" in upper:
            tt_score = 7
        elif "HOLD" in upper:
            tt_score = 5
        elif "PASS" in upper:
            tt_score = 3
        elif "AVOID" in upper or "ELIMINATE" in upper:
            tt_score = 1

    skep_upper = skeptic.upper()
    if "ELIMINATE" in skep_upper:
        skeptic_verdict = "ELIMINATE"
    elif "AVOID" in skep_upper:
        skeptic_verdict = "AVOID"
    elif "PASS" in skep_upper:
        skeptic_verdict = "PASS"
    elif "HOLD" in skep_upper:
        skeptic_verdict = "HOLD"
    elif "BUY" in skep_upper:
        skeptic_verdict = "BUY"
    else:
        skeptic_verdict = "NEUTRAL"

    return tt_score, skeptic_verdict, overall


# ── Fix #11: Recommended Action ───────────────────────────────────────────────

def get_recommended_action(signal: str, tt_score: int, prior_signal: str,
                           skeptic_verdict: str, accounting_flag: bool = False) -> str:
    """
    Decision tree for recommended action per ticker.
      BUY NOW      : STRONG_BUY AND tt_score >= 6 AND no accounting flag
      RERUN NEEDED : prior run in DB had different signal
      WATCHLIST    : (BUY OR prob>=0.45) AND signal != STRONG_BUY
      AVOID        : skeptic=ELIMINATE AND tt_score <= 2
      PASS         : everything else
    """
    if skeptic_verdict == "ELIMINATE" and tt_score <= 2:
        return "AVOID"
    if prior_signal and prior_signal != signal:
        return "RERUN NEEDED"
    if signal == "STRONG_BUY" and tt_score >= 6 and not accounting_flag:
        return "BUY NOW"
    if signal in ("BUY", "STRONG_BUY") and signal != "STRONG_BUY":
        return "WATCHLIST"
    if signal in ("WATCH", "HOLD"):
        return "WATCHLIST"
    return "PASS"


# ── Fix #18: Risk Category Tags ────────────────────────────────────────────────

def get_risk_category(ticker: str, l5: dict, fd: dict) -> str:
    """
    Assign a primary risk category to a ticker.
    Logic: biotech+phase3=BINARY EVENT; accounting concern=ACCOUNTING RISK;
           small cap or high P/S=VALUATION RISK; competitive language=COMPETITIVE RISK;
           else=EXECUTION RISK.
    """
    sector    = (fd.get("sector", "") or "").lower()
    catalyst  = (l5.get("catalyst", "") or "").lower()
    skeptic   = (l5.get("skeptic", "") or "").lower()
    scout     = (l5.get("scout", "") or "").lower()
    fund      = (l5.get("fundamentals", "") or "").lower()
    cap       = fd.get("market_cap_b", 0) or 0
    ps        = fd.get("price_to_sales", 0) or 0

    biotech_sectors = {"biotechnology", "biopharmaceuticals", "pharmaceuticals",
                       "healthcare", "life sciences"}
    is_biotech = any(s in sector for s in biotech_sectors)
    phase3_keywords = {"phase 3", "phase3", "pdufa", "bla ", "nda ", "fda approval", "binary"}
    has_binary = any(kw in catalyst for kw in phase3_keywords)

    acct_keywords = {"accounting", "audit", "restatement", "fraud", "channel stuffing",
                     "inventory", "receivables", "revenue recognition"}
    has_acct_concern = any(kw in skeptic or kw in fund for kw in acct_keywords)

    comp_keywords = {"hyperscaler", "commodit", "bundle", "platform", "competition",
                     "microsoft", "google", "amazon", "aws", "azure", "moat"}
    has_comp_risk = any(kw in skeptic or kw in scout for kw in comp_keywords)

    if is_biotech and has_binary:
        return "BINARY EVENT"
    if has_acct_concern:
        return "ACCOUNTING RISK"
    if cap < 2.0 or ps > 20:
        return "VALUATION RISK"
    if has_comp_risk:
        return "COMPETITIVE RISK"
    return "EXECUTION RISK"


# ── Fix #5: Divergence Warning ─────────────────────────────────────────────────

def divergence_check(rankings: list, layer5: dict) -> list:
    """
    Return list of divergence warning dicts for tickers where TT score < 4
    but sim signal is STRONG_BUY or BUY.
    """
    warnings = []
    for r in rankings:
        ticker = r["ticker"]
        signal = r.get("signal", "")
        if signal not in ("STRONG_BUY", "BUY"):
            continue
        l5 = layer5.get(ticker, {})
        tt_score, skeptic_verdict, tt_overall = _parse_tt_info(l5)
        if tt_score < 4:
            warnings.append({
                "ticker":          ticker,
                "signal":          signal,
                "tt_score":        tt_score,
                "tt_overall":      tt_overall,
                "skeptic_verdict": skeptic_verdict,
            })
    return warnings


# ── PASS Classification Table (STEP 3 — KEY INSIGHT) ──────────────────────────

def _get_pass_reason(ticker: str, r: dict, l5: dict, prior_signal: str,
                     rounds_data: list, fd: dict) -> str:
    """Classify the reason for a PASS/WATCH or the basis for a BUY/STRONG_BUY."""
    sig      = r.get("signal", "PASS")
    skeptic  = (l5.get("skeptic", "") or "").upper()
    catalyst = (l5.get("catalyst", "") or "").lower()
    sector   = (fd.get("sector", "") or "").lower()

    # For buy signals, classify what's driving them
    if sig in ("STRONG_BUY", "BUY"):
        biotech_terms = {"biotechnology", "pharmaceuticals", "healthcare"}
        binary_terms  = {"phase 3", "pdufa", "bla ", "fda"}
        if any(s in sector for s in biotech_terms) and any(t in catalyst for t in binary_terms):
            return "Binary Event"
        if "MOMENTUM" in catalyst.upper() or "TECHNICAL" in catalyst.upper():
            return "Momentum-driven"
        return "Fundamental"

    # For PASS/WATCH/HOLD — classify the reason
    if prior_signal and prior_signal != sig:
        return "Signal Fragile"

    if "ELIMINATE" in skeptic or "AVOID" in skeptic:
        return "Fundamental (Skeptic Eliminate)"

    acct_terms = {"accounting", "audit", "restatement", "fraud"}
    if any(t in skeptic.lower() for t in acct_terms):
        return "Accounting Risk"

    binary_terms = {"phase 3", "pdufa", "bla ", "fda approval"}
    if any(t in catalyst for t in binary_terms):
        return "Binary Event"

    # Check if probability declined primarily during injection rounds
    r3 = next((rd.get("market_probs", {}).get(ticker) for rd in rounds_data if rd["round"] == 3), None)
    r1 = next((rd.get("market_probs", {}).get(ticker) for rd in rounds_data if rd["round"] == 1), None)
    final_prob = r.get("probability", 0.5)
    if r1 is not None and r3 is not None and r3 < r1 - 0.10 and final_prob < 0.45:
        return "Injection-driven decline"

    return "Low Conviction"


def pass_classification_table(rankings: list, layer5: dict, rounds_data: list,
                               fd_cache: dict, prior_runs_map: dict) -> str:
    """
    Build the PASS Classification Table (Section I).
    KEY INSIGHT: not all PASSes are equal — classify the reason for each signal.
    """
    lines = []
    a = lines.append
    a("SECTION I — SIGNAL CLASSIFICATION TABLE")
    a("")
    a("Key insight: not all PASSes are equal. This table classifies the basis for every signal.")
    a("")

    hdr = (f"  {'Ticker':<6}  {'Signal':<11}  {'TT Verdict':<22}  "
           f"{'Signal Basis':<30}  {'Recommended Action'}")
    a(hdr)
    a("  " + "─" * (len(hdr) - 2))

    for r in rankings:
        ticker = r["ticker"]
        sig    = r.get("signal", "PASS")
        l5     = layer5.get(ticker, {})
        fd     = fd_cache.get(ticker, {})
        prior_runs = prior_runs_map.get(ticker, [])
        prior_signal = prior_runs[-1]["signal"] if prior_runs else None

        tt_score, skeptic_verdict, tt_overall = _parse_tt_info(l5)
        tt_short = (tt_overall[:22] + "…") if len(tt_overall) > 22 else tt_overall or "—"
        pass_reason = _get_pass_reason(ticker, r, l5, prior_signal, rounds_data, fd)
        rec_action  = get_recommended_action(sig, tt_score, prior_signal, skeptic_verdict)

        sig_display = f"{sig:<11}"
        a(f"  {ticker:<6}  {sig_display}  {tt_short:<22}  {pass_reason:<30}  {rec_action}")

    a("")
    return "\n".join(lines)


# ── Fix #6: Signal Conflict Resolution ────────────────────────────────────────

def conflict_resolution_section(rankings: list, layer5: dict,
                                 fd_cache: dict, prior_runs_map: dict) -> str:
    """
    Build Signal Conflict Resolution section for tickers where sim signal != TT signal.
    """
    conflicts = []
    for r in rankings:
        ticker = r["ticker"]
        sig    = r.get("signal", "PASS")
        l5     = layer5.get(ticker, {})
        tt_score, skeptic_verdict, tt_overall = _parse_tt_info(l5)

        # Determine TT signal from score
        if tt_score >= 8:
            tt_signal = "STRONG_BUY"
        elif tt_score >= 6:
            tt_signal = "BUY"
        elif tt_score >= 4:
            tt_signal = "HOLD"
        else:
            tt_signal = "PASS"

        if sig != tt_signal:
            prior_runs   = prior_runs_map.get(ticker, [])
            prior_signal = prior_runs[-1]["signal"] if prior_runs else None
            rec_action   = get_recommended_action(sig, tt_score, prior_signal, skeptic_verdict)
            conflicts.append({
                "ticker":          ticker,
                "sim_signal":      sig,
                "tt_signal":       tt_signal,
                "tt_score":        tt_score,
                "tt_overall":      tt_overall,
                "skeptic_verdict": skeptic_verdict,
                "recommended":     rec_action,
            })

    if not conflicts:
        return ""

    lines = []
    a = lines.append
    a("SIGNAL CONFLICT RESOLUTION")
    a("")
    a("The following tickers showed divergence between the simulation signal and Think Tank verdict.")
    a("Review before acting on any position.")
    a("")

    for c in conflicts:
        a(f"  {c['ticker']}:  Sim={c['sim_signal']}  |  TT={c['tt_signal']} (score {c['tt_score']}/10)")
        if c["tt_overall"]:
            a(f"    TT Overall: {c['tt_overall'][:120]}")
        a(f"    Skeptic verdict: {c['skeptic_verdict']}")
        a(f"    RECOMMENDED ACTION: {c['recommended']}")
        a("")

    a(DIVIDER)
    a("")
    return "\n".join(lines)


# ── Fix #16: Regression to Mean Flag ──────────────────────────────────────────

def regression_flag_check(ticker: str, prob_hist: dict) -> tuple:
    """
    Returns (flagged: bool, note: str).
    Flags if abs(final_prob - round1_prob) > 0.20 AND 0.45 <= round1_prob <= 0.55.
    """
    hist = prob_hist.get(ticker, [])
    if len(hist) < 2:
        return False, ""
    r1_prob    = hist[0]
    final_prob = hist[-1]
    if abs(final_prob - r1_prob) > 0.20 and 0.45 <= r1_prob <= 0.55:
        direction = "up" if final_prob > r1_prob else "down"
        return True, (
            f"LARGE MOVE FROM NEUTRAL PRIOR — verify injection amplification. "
            f"Started at {round(r1_prob*100)}% (near-neutral), moved {direction} "
            f"to {round(final_prob*100)}%. Injection-driven momentum may be overstated."
        )
    return False, ""


# ── Fix #17: Retrospective Contrarian Flagging ─────────────────────────────────

def retrospective_contrarian_flagging(rounds_data: list, rankings: list) -> dict:
    """
    For each agent, detect if they held a contrarian stance 5+ rounds vs final signal.
    Returns {(agent_name, ticker): highest_conviction_post_dict} for flagged cases.
    """
    final_signals = {r["ticker"]: r.get("signal", "PASS") for r in rankings}

    # Build agent-ticker stance history: {(agent, ticker): [stance_r1, stance_r2, ...]}
    stance_history = {}
    # Also track posts for conviction extraction
    agent_posts    = {}

    for rd in rounds_data:
        rnum = rd["round"]
        for p in rd["posts"]:
            agent   = p.get("agent", "")
            stances = p.get("stances", {})
            conv    = p.get("conviction", 0.5)
            post    = p.get("post", "")
            for ticker, stance in stances.items():
                key = (agent, ticker)
                if key not in stance_history:
                    stance_history[key] = []
                stance_history[key].append(stance)
                if key not in agent_posts:
                    agent_posts[key] = []
                agent_posts[key].append({"round": rnum, "conviction": conv, "post": post[:500]})

    flagged = {}
    for (agent, ticker), stances in stance_history.items():
        if len(stances) < 5:
            continue
        final_sig = final_signals.get(ticker, "PASS")
        # Contrarian = bearish when final signal is BUY/STRONG_BUY, or bullish when PASS
        if final_sig in ("STRONG_BUY", "BUY"):
            contrarian_stance = "bearish"
        elif final_sig == "PASS":
            contrarian_stance = "bullish"
        else:
            continue

        contrarian_count = sum(1 for s in stances if s == contrarian_stance)
        if contrarian_count >= 5:
            # Flag their highest-conviction post
            posts_for_key = agent_posts.get((agent, ticker), [])
            if posts_for_key:
                best = max(posts_for_key, key=lambda x: x["conviction"])
                flagged[(agent, ticker)] = {
                    "agent":         AGENT_NAMES.get(agent, agent),
                    "ticker":        ticker,
                    "rounds_contrarian": contrarian_count,
                    "conviction":    best["conviction"],
                    "round":         best["round"],
                    "post":          best["post"],
                    "label":         "[EARLY CONTRARIAN — REVIEW]",
                }
    return flagged


# ── Fix #9: Extract Injection Context for Rerun Seed ──────────────────────────

def extract_injections_context(rounds_data: list) -> list:
    """
    Build injection context list for rerun seed.
    Returns [{round, injection_text, winner_ticker, loser_ticker}].
    """
    context = []
    for rd in rounds_data:
        inj = rd.get("injection", "")
        if not inj:
            continue
        mp     = rd.get("market_probs", {})
        stocks = list(mp.keys())
        # Single-stock: winner/loser based on prob delta vs prior round
        if len(stocks) <= 1:
            sym = stocks[0] if stocks else "—"
            # Find prior round's probability for this stock
            round_num = rd.get("round", 0)
            prior_prob = None
            for prior_rd in rounds_data:
                if prior_rd.get("round", 0) == round_num - 1:
                    prior_prob = prior_rd.get("market_probs", {}).get(sym)
                    break
            curr_prob = mp.get(sym)
            if prior_prob is not None and curr_prob is not None:
                delta = curr_prob - prior_prob
                if delta < -0.05:
                    winner = "—"
                    loser = sym
                elif delta > 0.05:
                    winner = sym
                    loser = "—"
                else:
                    winner = "—"
                    loser = "—"
            else:
                winner = "—"
                loser = "—"
        else:
            winner = max(mp, key=mp.get)
            loser  = min(mp, key=mp.get)
            # Don't show same ticker as both winner and loser
            if winner == loser:
                loser = "—"
        context.append({
            "round":          rd["round"],
            "injection_text": inj[:80],
            "winner_ticker":  winner,
            "loser_ticker":   loser,
        })
    return context


# ── Fix #1 / #20: Signal Stability ────────────────────────────────────────────

def signal_stability_note(ticker: str, prior_runs: list, current_signal: str) -> str:
    """
    Returns stability label based on run history.
      STABLE      : 3+ runs all same signal
      PROVISIONAL : only 1 run (current)
      FRAGILE ⚠   : 2+ runs with different signals
    """
    all_signals = [r["signal"] for r in prior_runs] + [current_signal]
    unique      = set(all_signals)
    run_count   = len(all_signals)

    if run_count >= 3 and len(unique) == 1:
        return "STABLE"
    if run_count >= 2 and len(unique) > 1:
        return "FRAGILE ⚠"
    return "PROVISIONAL"


# ── Pass/watch brief builder (Tier 3/4/5) ─────────────────────────────────────

def _build_pass_brief(
    ticker: str,
    r: dict,
    fd: dict,
    l5: dict,
    l6: dict,
    top_posts: list,
    api_key: str,
) -> str:
    """
    Generate a 2-paragraph pass/watch brief for Tier 3/4/5 stocks.
    Shorter than the full Einhorn memo but still substantive — explains
    WHY it passed, what would need to change, and the key risk.
    Used when tier_num >= 3 (WATCH, PASS, AVOID).
    """
    price    = fd.get("price", 0) or 0
    high52   = fd.get("52wk_high", 0) or 0
    dip      = round((high52 - price) / high52 * 100, 1) if high52 > 0 else 0
    cap      = fd.get("market_cap_b", 0) or 0
    rev      = fd.get("rev_growth_pct", 0) or 0

    scout_line  = l5.get("scout", "")
    skep_line   = l5.get("skeptic", "")
    fund_line   = l5.get("fundamentals", "")
    overall     = l5.get("overall", "")
    catalyst    = l5.get("catalyst", "")
    kill_cond   = l5.get("kill_condition", "")
    bull_arg    = l6.get("top_bull_argument", "")
    bear_arg    = l6.get("top_bear_argument", "")
    munger_inv  = l6.get("munger_inversion", "")

    posts_text = ""
    for p in top_posts[:2]:
        posts_text += f"\n[{p['agent']} R{p['round']} {int(p['conviction']*100)}%]: {p['post'][:250]}"

    if api_key:
        system = (
            "You are a senior portfolio manager writing a brief investment pass memo. "
            "Your style is direct, specific, and honest — like a Greenlight Capital pass note. "
            "Write exactly 2 paragraphs of flowing prose. No bullet points. No headers. No hedging. "
            "Paragraph 1: What this company actually is, what it does, why it matters, "
            "and what the key metrics show — be specific with numbers. "
            "Then explain clearly and specifically WHY the panel passed it: was it valuation, "
            "a specific forensic flag, wrong stage of business cycle, or wrong framework applied? "
            "Name the exact reason, not just 'Skeptic ELIMINATE'. "
            "Paragraph 2: What would need to change for this to become a BUY. "
            "What is the specific trigger — a price level, a catalyst, a metric improvement, "
            "a resolved risk — that would move this from PASS to INVESTIGATE. "
            "End with the key tail risk in one sentence. "
            "Use first-person plural (we/our). Be direct. Every sentence must add information."
        )
        user = f"""Write a 2-paragraph pass brief for {ticker}.

SNAPSHOT:
- Price: ${price:.2f} | 52-week high: ${high52:.2f} | Off peak by {dip}%
- Market cap: ${cap:.1f}B | Revenue growth: +{rev:.1f}% YoY

THINK TANK VERDICTS:
- Scout: {scout_line}
- Skeptic: {skep_line}
- Fundamentals: {fund_line}
- Overall: {overall}

BULL ARGUMENT (from synthesis layer): {bull_arg[:400]}

BEAR ARGUMENT (from synthesis layer): {bear_arg[:400]}

MUNGER INVERSION: {munger_inv[:200]}

AGENT DEBATE HIGHLIGHTS:{posts_text}

CATALYST: {catalyst}
KILL CONDITION: {kill_cond}

Write exactly 2 paragraphs. Be specific about WHY it passed and what changes the verdict."""

        prose = _call_api(system, user, api_key, max_tokens=700)
        if prose and len(prose) > 150:
            return prose

    # Fallback if API fails
    why_passed = skep_line[:200] if skep_line else fund_line[:200]
    return (
        f"{ticker} is a ${cap:.1f}B company trading at ${price:.2f}, "
        f"down {dip}% from its 52-week high of ${high52:.2f}, "
        f"with revenue growing {rev:.1f}% year-over-year. "
        f"The panel passed this name for the following reason: {why_passed}. "
        f"\n\n"
        f"To revisit: {catalyst or 'no specific catalyst identified'}. "
        f"Key risk: {kill_cond or munger_inv or 'see full Think Tank report'}."
    )


# ── Thesis prose builder ───────────────────────────────────────────────────────

def _build_thesis_prose(
    ticker: str,
    r: dict,
    fd: dict,
    l5: dict,
    l6: dict,
    top_posts: list,
    api_key: str,
) -> str:
    """Generate 6-paragraph thesis prose. Uses API if available, else extracts from Layer 6."""
    price    = fd.get("price", 0) or 0
    high52   = fd.get("52wk_high", 0) or 0
    dip      = round((high52 - price) / high52 * 100, 1) if high52 > 0 else 0
    cap      = fd.get("market_cap_b", 0) or 0
    rev      = fd.get("rev_growth_pct", 0) or 0
    anlst    = fd.get("analyst_upside_pct", 0) or 0
    fwd      = fd.get("forward_eps", 0) or 0
    ttm      = fd.get("trailing_eps", 0) or 0
    prob     = r.get("probability", 0)
    vel      = r.get("velocity", 0)
    cs       = r.get("converted_skeptics", 0)

    scout_line = l5.get("scout", "")
    skep_line  = l5.get("skeptic", "")
    fund_line  = l5.get("fundamentals", "")
    overall    = l5.get("overall", "")
    catalyst   = l5.get("catalyst", "")
    kill_cond  = l5.get("kill_condition", "")

    bull_arg   = l6.get("top_bull_argument", "")
    bear_arg   = l6.get("top_bear_argument", "")
    flywheel   = l6.get("flywheel", "")
    tail_risk  = l6.get("tail_risk", "")
    munger_inv = l6.get("munger_inversion", "")
    l6_raw     = l6.get("_raw", "")

    posts_text = ""
    for p in top_posts[:3]:
        posts_text += f"\n[{p['agent']} R{p['round']} {int(p['conviction']*100)}% conviction]: {p['post'][:300]}"

    if api_key:
        system = (
            "You are a senior portfolio manager and investment analyst writing a formal investment memo. "
            "Your style is modeled after David Einhorn (Greenlight Capital), Bill Ackman (Pershing Square), "
            "and Howard Marks (Oaktree) — precise, analytical, deeply researched, with specific numbers and "
            "genuine conviction. You do not hedge everything. You take a position and defend it. "
            "Write exactly 6 paragraphs of flowing prose. No bullet points. No headers. No fluff. "
            "Paragraph 1: What this company actually is and what it does — explain the business model clearly. "
            "What problem does it solve, who pays for it, how does it make money, what is the moat. "
            "Do not start with the stock price. Start with the business. "
            "Paragraph 2: The current situation — price action, what the stock has done and why, "
            "where it sits vs 52-week high, what the market's concern is. Be specific with numbers. "
            "Paragraph 3: The bull case — build it with specific metrics from the data. "
            "Why is the market mispricing this? What does the platform/NRR/RPO/ARPU/NAV data show "
            "that the market is ignoring? What is the specific catalyst and when? "
            "Paragraph 4: What the multi-agent simulation debate revealed — name specific agents "
            "by their investor persona (e.g. growth_compounder, probabilist, saas_specialist). "
            "Describe the key tension in the debate. What did the converted skeptics argue? "
            "Paragraph 5: The competitive landscape and key risks — write the bear case seriously "
            "and specifically. Name the specific competitors. Quantify the risk where possible. "
            "Paragraph 6: Verdict and position sizing — conviction score, recommended position size "
            "as a percentage of portfolio, the specific sell trigger, and one sentence on what "
            "changes the verdict from BUY to PASS or vice versa. "
            "Use first-person plural (we / our). Be direct. Every sentence must add information. "
            "Do not repeat numbers already stated. Build the argument progressively."
        )
        user = f"""Write a 6-paragraph investment memo for {ticker}.

SNAPSHOT:
- Price: ${price:.2f} | 52-week high: ${high52:.2f} | Off peak by {dip}%
- Market cap: ${cap:.1f}B | Revenue growth: +{rev:.1f}% YoY | Analyst consensus upside: +{anlst:.0f}%
- TTM EPS: ${ttm:.2f} → Forward EPS: ${fwd:.2f}
- Simulation result: {round(prob*100)}% buy probability | Velocity: {vel:+.3f} | Converted skeptics: {cs}

THINK TANK VERDICTS:
- Scout: {scout_line}
- Skeptic: {skep_line}
- Fundamentals: {fund_line}
- Overall: {overall}

BULL ARGUMENT: {bull_arg[:600]}

BEAR ARGUMENT: {bear_arg[:600]}

FLYWHEEL / MOAT: {flywheel[:300]}

AGENT DEBATE HIGHLIGHTS:{posts_text}

CATALYST: {catalyst}
KILL CONDITION: {kill_cond}

Write exactly 6 paragraphs of prose investment memo. No headers, no bullets."""

        prose = _call_api(system, user, api_key, max_tokens=2500)
        if prose and len(prose) > 200:
            return prose

    # Fallback: construct from Layer 6 raw text
    if bull_arg and bear_arg:
        p1 = (f"{ticker} is a {fd.get('sector','') or 'technology'} company currently trading at "
              f"${price:.2f}, down {dip}% from its 52-week high of ${high52:.2f}. "
              f"With a ${cap:.1f}B market cap and revenue growing at {rev:.1f}% year-over-year, "
              f"the stock sits at an inflection point that our 29-agent research system flagged as "
              f"a primary conviction opportunity.")
        p2 = bull_arg[:500] if bull_arg else ""
        p3 = (f"Our simulation debate ran {len(top_posts)} agent posts over 8 rounds, with "
              f"{cs} skeptics ultimately converted to the bull case. "
              + (skep_line[:300] if skep_line else ""))
        p4 = bear_arg[:500] if bear_arg else (kill_cond if kill_cond else "")
        return "\n\n".join(filter(None, [p1, p2, p3, p4]))

    if l6_raw:
        clean = re.sub(r"\*\*([A-Z ]+):\*\*", "", l6_raw)
        clean = re.sub(r"\*\*", "", clean)
        clean = re.sub(r"\n{3,}", "\n\n", clean)
        return clean[:1800].strip()

    return (f"{ticker} — simulation probability {round(prob*100)}%, "
            f"composite score {r.get('composite',0):.3f}. "
            f"Think Tank overall: {overall}. "
            f"Catalyst: {catalyst}.")


# ── v3 Conviction Parsing Helpers ─────────────────────────────────────────────

def parse_ev(fundamentals_str: str):
    """Parse EV% from 'EV: +104%' in FUNDAMENTALS field. Returns float or None."""
    if not fundamentals_str:
        return None
    m = re.search(r"EV:\s*([+-]?\d+\.?\d*)%", fundamentals_str)
    return float(m.group(1)) if m else None


def parse_panel_votes(panel_consensus_str: str) -> int:
    """Parse N from 'N/4' or 'N/5 bullish' in PANEL_CONSENSUS. Returns int 0-5."""
    if not panel_consensus_str:
        return 0
    # Match N/4 or N/5 format (5 panels now with Valuation Anchor)
    m = re.search(r"(\d)/[45]", panel_consensus_str)
    if m:
        return int(m.group(1))
    # Fallback keyword
    s = panel_consensus_str.upper()
    if "HIGH CONSENSUS" in s:
        return 4
    if "SPLIT" in s:
        return 2
    if "PANEL CONFLICT" in s:
        return 2
    return 0


def parse_tt_score(overall_str: str):
    """Parse score from 'Score: 9/10' in OVERALL field. Returns float or None."""
    if not overall_str:
        return None
    m = re.search(r"Score:\s*(\d+(?:\.\d+)?)/10", overall_str)
    return float(m.group(1)) if m else None


def parse_tt_signal_v3(overall_str: str) -> str:
    """Extract first signal keyword from OVERALL field (e.g. 'BUY', 'WATCH', 'PASS')."""
    if not overall_str:
        return "PASS"
    m = re.match(r"(STRONG[_ ]?BUY|BUY|HOLD|WATCH|PASS|AVOID)", overall_str.strip(), re.IGNORECASE)
    if m:
        raw = m.group(1).upper()
        return "STRONG_BUY" if raw.replace(" ", "_") == "STRONG_BUY" else raw
    return "PASS"


def classify_tier(ticker: str, r: dict, layer5: dict, prior_signal: str,
                  accounting_flag: bool = False) -> int:
    """
    Classify ticker into conviction tier 1-5.

    Tier 1 — Strong Fundamentals: tt_score>=7, panel>=3, ev>0, no accounting flag
    Tier 2 — Momentum Leaders: sim STRONG_BUY/BUY but tt_score<5
    Tier 3 — Catalyst Required: tt_signal BUY/WATCH but blocked from Tier 1/2
    Tier 4 — Signal Fragile: prior run had a different signal
    Tier 5 — Avoid / Pass: everything else
    """
    l5          = layer5.get(ticker, {})
    overall_str = l5.get("overall", "")
    fund_str    = l5.get("fundamentals", "")
    panel_str   = l5.get("panel_consensus", "")

    ev          = parse_ev(fund_str)
    panel_votes = parse_panel_votes(panel_str)
    tt_score_v  = parse_tt_score(overall_str)
    tt_score    = tt_score_v if tt_score_v is not None else 0.0
    tt_signal   = parse_tt_signal_v3(overall_str)
    sim_signal  = r.get("signal", "PASS")
    ev_val      = ev if ev is not None else 0.0

    # Tier 4 first — signal fragility overrides strong fundamentals for safety
    if prior_signal and prior_signal != sim_signal:
        return 4

    # Tier 1 — Strong Fundamentals (accounting concerns disqualify: no margin of safety)
    if tt_score >= 7 and panel_votes >= 3 and ev_val > 0 and not accounting_flag:
        return 1

    # Tier 2 — Momentum Leaders (sim says buy, fundamentals don't support)
    if sim_signal in ("STRONG_BUY", "BUY") and tt_score < 5:
        return 2

    # Tier 3 — Catalyst Required (TT sees merit but not fully deployable)
    if tt_signal in ("STRONG_BUY", "BUY", "WATCH"):
        return 3

    # Tier 5 — Avoid / Pass
    return 5


def get_recommended_action_v3(tier: int, ticker: str, r: dict,
                               layer5: dict, cache: dict) -> str:
    """Generate tier-specific recommended action string."""
    l5       = layer5.get(ticker, {})
    fd       = cache.get(ticker, {})
    catalyst = l5.get("catalyst", "") or ""
    kill     = l5.get("kill_condition", "") or ""
    price    = fd.get("price", 0) or 0

    if tier == 1:
        cat_short = catalyst.split("—")[0].strip()[:80] if catalyst else "next earnings"
        stop_note = f" Stop: {kill[:60]}." if kill else ""
        return f"BUY — ${price:.2f}. Catalyst: {cat_short}.{stop_note}"

    if tier == 2:
        stop_price = round(price * 0.85, 2)
        return (f"MANAGE ACTIVELY — Momentum trade only. "
                f"Hard stop ~${stop_price:.2f} (-15%). Do not size as conviction.")

    if tier == 3:
        trigger = catalyst.split("(")[0].strip()[:80] if catalyst else "specific trigger"
        return f"WATCHLIST — Trigger: {trigger}."

    if tier == 4:
        return "RERUN REQUIRED — Do not act until 3+ run consensus."

    skeptic = (l5.get("skeptic", "") or "").upper()
    if "ELIMINATE" in skeptic:
        return "AVOID — Skeptic: ELIMINATE."
    return "PASS"


def catalyst_calendar(tier_rankings: list, layer5: dict) -> str:
    """Build chronological catalyst table annotated with tier."""
    month_order = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    entries = []
    for item in tier_rankings:
        t    = item["ticker"]
        tier = item["tier"]
        l5   = layer5.get(t, {})
        cat  = (l5.get("catalyst", "") or "").strip()
        if not cat or cat == "—":
            continue
        sort_key  = 99
        cat_lower = cat.lower()
        for abbr, num in month_order.items():
            if abbr in cat_lower:
                sort_key = min(sort_key, num)
                break
        if "q1" in cat_lower:
            sort_key = min(sort_key, 3)
        elif "q2" in cat_lower:
            sort_key = min(sort_key, 6)
        elif "q3" in cat_lower:
            sort_key = min(sort_key, 9)
        elif "q4" in cat_lower:
            sort_key = min(sort_key, 12)
        entries.append({"sort_key": sort_key, "ticker": t, "tier": tier, "catalyst": cat[:100]})

    entries.sort(key=lambda x: (x["sort_key"], x["ticker"]))
    lines = [f"  {'Ticker':<6}  {'Tier':<6}  Catalyst", "  " + "─" * 70]
    for e in entries:
        lines.append(f"  {e['ticker']:<6}  T{e['tier']}      {e['catalyst']}")
    lines.append("")
    return "\n".join(lines)


def build_tier_section(
    tier_num: int,
    tier_name: str,
    tier_action: str,
    why_text: str,
    stocks_in_tier: list,
    rankings_map: dict,
    layer5: dict,
    layer6: dict,
    cache: dict,
    rounds_data: list,
    prob_hist: dict,
    api_key: str,
    risk_map: dict,
    stability_map: dict,
    reg_flags: dict,
    prior_runs_map: dict,
) -> str:
    """
    Build a tier section with deep dives for Tier 1/2, shorter format for Tier 3/4/5.
    Tier 1/2 get Opus prose via API. Tier 3/4/5 get concise catalyst + action lines.
    """
    if not stocks_in_tier:
        return ""

    lines = []
    a = lines.append
    use_deep_dive = True  # All tickers get prose — tier controls depth not existence
    is_deep_tier  = tier_num in (1, 2)   # True = full Einhorn-style memo, False = shorter pass/watch brief

    if why_text:
        a(why_text)
        a("")

    for r in stocks_in_tier:
        t       = r["ticker"]
        l5      = layer5.get(t, {})
        l6      = layer6.get(t, {})
        fd      = cache.get(t, {})
        price   = fd.get("price", 0) or 0
        high52  = fd.get("52wk_high", 0) or 0
        dip     = round((high52 - price) / high52 * 100, 1) if high52 > 0 else 0
        cap     = fd.get("market_cap_b", 0) or 0
        rev     = fd.get("rev_growth_pct", 0) or 0
        anlst   = fd.get("analyst_upside_pct", 0) or 0

        sim_signal  = r.get("signal", "PASS")
        prob        = r.get("probability", 0)
        cs          = r.get("converted_skeptics", 0)
        hist        = prob_hist.get(t, [])

        ev          = parse_ev(l5.get("fundamentals", ""))
        panel_votes = parse_panel_votes(l5.get("panel_consensus", ""))
        tt_score_v  = parse_tt_score(l5.get("overall", ""))
        tt_signal   = parse_tt_signal_v3(l5.get("overall", ""))
        catalyst    = (l5.get("catalyst", "") or "—")
        kill_cond   = (l5.get("kill_condition", "") or "—")
        skeptic_str = (l5.get("skeptic", "") or "")

        risk  = risk_map.get(t, "EXECUTION RISK")
        stab  = stability_map.get(t, "PROVISIONAL")

        ev_disp    = f"{ev:+.1f}%" if ev is not None else "—"
        tt_disp    = (f"{tt_signal} {tt_score_v:.1f}/10"
                      if tt_score_v is not None else l5.get("overall", "")[:20])
        panel_disp = f"{panel_votes}/5"
        prob_disp  = f"{round(prob*100)}%"

        a(f"── {t} {'─'*(60-len(t))}")
        a(f"  Sim: {sim_signal} {prob_disp}  |  TT: {tt_disp}  |  EV: {ev_disp}  |  Panel: {panel_disp}")
        a(f"  Risk: {risk}  |  Stability: {stab}")

        if is_deep_tier:
            snap_parts = []
            if price > 0:
                snap_parts.append(f"${price:.2f}")
            if dip > 0:
                snap_parts.append(f"Off {dip}% from ${high52:.2f} high")
            if cap > 0:
                snap_parts.append(f"${cap:.1f}B cap")
            if rev > 0:
                snap_parts.append(f"+{rev:.1f}% rev")
            if anlst > 0:
                snap_parts.append(f"{anlst:.0f}% analyst upside")
            if snap_parts:
                a("  Snapshot: " + "  |  ".join(snap_parts))
            if t in reg_flags:
                a(f"  ⚠ REGRESSION FLAG: {reg_flags[t]}")

            top_posts = get_top_posts(rounds_data, t, n=4)
            if api_key:
                print(f"  Generating thesis prose for {t} (Tier {tier_num}) via API...",
                      file=sys.stderr)
            prose = _build_thesis_prose(t, r, fd, l5, l6, top_posts, api_key)
            a("")
            a(prose)

            if hist:
                journey = " → ".join(f"R{i+1} {round(p*100)}%" for i, p in enumerate(hist))
                trend   = ("↑" if hist[-1] > hist[0] + 0.05
                           else "↓" if hist[-1] < hist[0] - 0.05 else "→")
                a(f"  Probability Journey: {journey}  {trend}")

            if catalyst != "—":
                a(f"  Catalyst:       {catalyst}")
            if kill_cond != "—":
                a(f"  Kill Condition: {kill_cond}")
            if cs > 0:
                a(f"  Note: {cs} converted skeptics.")

        else:
            # Tier 3/4/5 — generate shorter pass/watch brief prose
            top_posts = get_top_posts(rounds_data, t, n=3)
            if api_key:
                print(f"  Generating pass brief for {t} (Tier {tier_num}) via API...",
                      file=sys.stderr)
            pass_prose = _build_pass_brief(t, r, fd, l5, l6, top_posts, api_key)
            a("")
            a(pass_prose)
            a("")

            if catalyst != "—":
                a(f"  Catalyst:  {catalyst[:150]}")
            if kill_cond != "—":
                a(f"  Kill:      {kill_cond[:120]}")
            if skeptic_str:
                a(f"  Skeptic:   {skeptic_str[:150]}")

            if tier_num == 4:
                prior_runs = prior_runs_map.get(t, [])
                unique_sigs = list(dict.fromkeys(pr["signal"] for pr in prior_runs))
                a(f"  Prior signals: {unique_sigs} → current: {sim_signal}")
                a(f"  Reason: Signal inconsistency across runs. Await 3+ consistent results before acting.")

        rec = get_recommended_action_v3(tier_num, t, r, layer5, cache)
        a(f"  RECOMMENDED: {rec}")
        a("")

    return "\n".join(lines)


# ── Technical scoring (Secret Mindset framework, cache-based) ─────────────────

def get_technical_score(ticker: str, cache: dict) -> dict:
    """Approximate technical score from screener cache (no TradingView call needed)."""
    fd = cache.get(ticker, {})
    price   = fd.get('price', 0) or 0
    high52  = fd.get('52wk_high', 0) or 0
    low52   = fd.get('52wk_low', 0) or 0
    short   = fd.get('short_pct', 0) or 0
    short_pct = short * 100 if short <= 1.0 else short

    score = 0
    signals = []

    dip_from_high = ((high52 - price) / high52 * 100) if high52 > 0 else 0
    recovery_from_low = ((price - low52) / (high52 - low52) * 100) if (high52 - low52) > 0 else 50

    if dip_from_high > 40:
        score += 2; signals.append(f'HARSI_RESET (off {dip_from_high:.0f}% from high)')
    elif dip_from_high > 20:
        score += 1; signals.append(f'PULLBACK ({dip_from_high:.0f}% off high)')

    if 20 <= recovery_from_low <= 60:
        score += 1; signals.append('MID_RANGE (healthy base)')
    elif recovery_from_low > 80:
        signals.append('EXTENDED (near highs — smaller entry)')

    if short_pct >= 25:
        score += 1; signals.append(f'SQUEEZE_FUEL ({short_pct:.0f}% short)')
    elif short_pct >= 15:
        signals.append(f'SHORT_INTEREST ({short_pct:.0f}%)')

    verdict = 'STRONG_BUY' if score >= 4 else 'BUY' if score >= 3 else 'WATCH' if score >= 2 else 'AVOID'

    return {
        'score': score,
        'verdict': verdict,
        'signals': signals,
        'dip_from_high': round(dip_from_high, 1),
        'short_pct': round(short_pct, 1),
    }


# ── Main report generator ──────────────────────────────────────────────────────

def generate_report(sim_path: str, report_path: str) -> str:
    sim         = load_sim(sim_path)
    cache       = load_screener_cache()
    rep_text    = Path(report_path).read_text(encoding="utf-8", errors="ignore") \
                  if report_path and Path(report_path).exists() else ""
    layer5      = parse_layer5_blocks(rep_text)
    layer6      = parse_layer6_blocks(rep_text)

    stocks      = sim["stocks"]
    rankings    = sorted(sim["rankings"], key=lambda x: -x.get("composite", 0))
    rounds      = sim.get("rounds", 8)
    prob_hist   = sim.get("prob_history", {})
    rounds_data = sim.get("rounds_data", [])
    run_id      = sim["run_id"]
    model       = sim.get("model", "haiku")
    ts          = sim.get("timestamp", datetime.now().isoformat())[:10]

    api_key = _load_api_key()

    # ── Pre-compute ────────────────────────────────────────────────────────
    init_history_db()
    prior_runs_map = {t: query_history(t) for t in stocks}

    stability_map = {
        r["ticker"]: signal_stability_note(
            r["ticker"], prior_runs_map.get(r["ticker"], []), r.get("signal", "PASS")
        )
        for r in rankings
    }
    risk_map = {
        r["ticker"]: get_risk_category(r["ticker"], layer5.get(r["ticker"], {}), cache.get(r["ticker"], {}))
        for r in rankings
    }
    div_warnings   = divergence_check(rankings, layer5)
    reg_flags      = {}
    for ticker in stocks:
        flagged, note = regression_flag_check(ticker, prob_hist)
        if flagged:
            reg_flags[ticker] = note
    contrarians    = retrospective_contrarian_flagging(rounds_data, rankings)
    injections_ctx = extract_injections_context(rounds_data)

    # ── Classify tiers ────────────────────────────────────────────────────
    tier_map = {}
    for r in rankings:
        t            = r["ticker"]
        prior_runs   = prior_runs_map.get(t, [])
        prior_signal = prior_runs[-1]["signal"] if prior_runs else None
        acct_flag    = risk_map[t] == "ACCOUNTING RISK"
        tier_map[t]  = classify_tier(t, r, layer5, prior_signal, acct_flag)

    def ev_sort_key(r):
        ev = parse_ev((layer5.get(r["ticker"], {}) or {}).get("fundamentals", ""))
        return ev if ev is not None else -9999.0

    tier_groups: dict = {i: [] for i in range(1, 6)}
    for r in rankings:
        tier_groups[tier_map[r["ticker"]]].append(r)
    for grp in tier_groups.values():
        grp.sort(key=lambda r: -ev_sort_key(r))

    ranked       = [r for tier_num in range(1, 6) for r in tier_groups[tier_num]]
    rankings_map = {r["ticker"]: r for r in rankings}
    tier_rankings = [dict(r, tier=tier_map[r["ticker"]]) for r in ranked]

    lines = []
    a = lines.append

    # ── DIVERGENCE WARNING BLOCK (v4_4 — auto-generated, cannot suppress) ────
    # Collect FRAGILE signals from sim stability field
    fragile_tickers = [
        r["ticker"] for r in rankings
        if r.get("stability") == "FRAGILE"
    ]
    # Collect Sim=BUY but TT=PASS divergences
    sim_buy_tt_pass = []
    sim_pass_tt_buy = []
    for r in rankings:
        ticker  = r["ticker"]
        sig     = r.get("signal", "PASS")
        l5      = layer5.get(ticker, {})
        tt_sc, _, _ = _parse_tt_info(l5)
        if sig in ("STRONG_BUY", "BUY") and tt_sc < 4:
            sim_buy_tt_pass.append((ticker, sig, tt_sc))
        elif sig in ("PASS", "WATCH") and tt_sc >= 6:
            sim_pass_tt_buy.append((ticker, sig, tt_sc))

    has_any_divergence = div_warnings or fragile_tickers or sim_pass_tt_buy
    if has_any_divergence:
        a("╔" + "═" * 67 + "╗")
        a("║  ⚠  SIGNAL DIVERGENCE WARNINGS  (auto-generated, cannot suppress) ║")
        a("╚" + "═" * 67 + "╝")
        a("")
        # Sim=BUY, TT=PASS
        if div_warnings or sim_buy_tt_pass:
            a("  [SIM BUY → TT PASS] — Simulation crowd may be chasing narrative:")
            shown = {w["ticker"] for w in div_warnings}
            for w in div_warnings:
                a(f"  {w['ticker']:6}  Sim={w['signal']:<11}  TT={w['tt_score']}/10  "
                  f"skeptic={w['skeptic_verdict']}")
                a(f"         → Tier 2 MOMENTUM (narrative may outrun fundamentals)")
            for (t, s, sc) in sim_buy_tt_pass:
                if t not in shown:
                    a(f"  {t:6}  Sim={s:<11}  TT={sc}/10")
                    a(f"         → Tier 2 MOMENTUM (narrative may outrun fundamentals)")
            a("")
        # Sim=PASS, TT=BUY
        if sim_pass_tt_buy:
            a("  [SIM PASS → TT BUY] — Sim crowd missed the thesis:")
            for (t, s, sc) in sim_pass_tt_buy:
                a(f"  {t:6}  Sim={s:<6}  TT={sc}/10")
                a(f"         → Tier 1 FUNDAMENTAL (sim crowd missed the thesis — consider TT signal)")
            a("")
        # FRAGILE signals from parallel tracks
        if fragile_tickers:
            a("  [FRAGILE — parallel track divergence]:")
            for t in fragile_tickers:
                a(f"  {t:6}  FRAGILE signal — do not size until re-run confirms direction")
            a("")
        if not has_any_divergence:
            a("  No divergences detected.")
        a("ACTION: Do not act on flagged signals without resolving divergence.")
        a("")
        a(DIVIDER)
        a("")
    elif not div_warnings:
        # No divergences at all — print compact notice
        a("╔" + "═" * 67 + "╗")
        a("║  ⚠  SIGNAL DIVERGENCE WARNINGS  (auto-generated, cannot suppress) ║")
        a("╚" + "═" * 67 + "╝")
        a("")
        a("  No divergences detected. All signals align across sim and Think Tank.")
        a("")
        a(DIVIDER)
        a("")

    # ── HEADER ────────────────────────────────────────────────────────────
    a("ORACLE CAPITAL RESEARCH")
    a("Analyst Conviction Report")
    a(f"{ts}  |  Run ID: {run_id}  |  {len(stocks)} stocks  |  {rounds} rounds")
    a("Reordered by analyst conviction. Simulation probability is context, not primary ranking.")
    a("")
    a(DIVIDER)
    a("")

    # ── I. KEY FINDINGS & SIGNAL CLASSIFICATION ───────────────────────────
    a(pass_classification_table(rankings, layer5, rounds_data, cache, prior_runs_map))
    a(DIVIDER)
    a("")

    # ── II. MASTER RANKING — ANALYST CONVICTION ORDER ────────────────────
    a("II. MASTER RANKING — ANALYST CONVICTION ORDER")
    a("")
    hdr = (f"  {'#':>2}  {'Tier':<7}  {'Ticker':<7}  {'Action':<22}  "
           f"{'Sim Signal':<12}  {'Sim Prob':>8}  {'TT Score':<14}  {'EV':>7}  {'Panel':>5}  {'Technical':<10}")
    a(hdr)
    a("  " + "─" * (len(hdr) - 2))

    tech_scores = {t: get_technical_score(t, cache) for t in stocks}
    tech_opportunities = []  # sim PASS but technical BUY

    for rank_idx, r in enumerate(ranked, 1):
        t          = r["ticker"]
        tier       = tier_map[t]
        l5         = layer5.get(t, {})
        sim_sig    = r.get("signal", "PASS")
        prob       = round(r.get("probability", 0) * 100)
        ev         = parse_ev((l5 or {}).get("fundamentals", ""))
        tt_score_v = parse_tt_score((l5 or {}).get("overall", ""))
        tt_sig     = parse_tt_signal_v3((l5 or {}).get("overall", ""))
        panel_v    = parse_panel_votes((l5 or {}).get("panel_consensus", ""))
        ev_disp    = f"{ev:+.0f}%" if ev is not None else "—"
        tt_disp    = (f"{tt_sig} {tt_score_v:.1f}/10" if tt_score_v is not None else "—")
        rec        = get_recommended_action_v3(tier, t, r, layer5, cache)
        rec_short  = rec[:20]
        tech       = tech_scores[t]
        tech_v     = tech["verdict"][:6]
        footnote   = "★" if (sim_sig == "PASS" and tech["verdict"] in ("BUY", "STRONG_BUY")) else " "
        if footnote == "★":
            tech_opportunities.append(t)
        a(f"  {rank_idx:>2}  TIER {tier}   {t:<7}  {rec_short:<22}  "
          f"{sim_sig:<12}  {prob:>7}%  {tt_disp:<14}  {ev_disp:>7}  {panel_v:>3}/5  {footnote}{tech_v:<9}")

    if tech_opportunities:
        a("")
        a(f"  ★ = technical opportunity: sim PASS but Secret Mindset signals BUY")

    a("")
    a(DIVIDER)
    a("")

    # ── TECHNICAL vs FUNDAMENTAL DIVERGENCES ─────────────────────────────
    tech_divergences = [
        t for t in stocks
        if tech_scores.get(t, {}).get("verdict") == "BUY" and tier_map.get(t, 5) >= 3
    ]
    if tech_divergences:
        a("TECHNICAL vs FUNDAMENTAL DIVERGENCES")
        a("")
        a("Stocks where Secret Mindset signals BUY but fundamentals are cautious (Tier 3+):")
        a("")
        for t in tech_divergences:
            ts_data     = tech_scores[t]
            signals_str = " | ".join(ts_data.get("signals", [])) or "no signals"
            a(f"  {t:<6}  Signals: {signals_str}")
            a(f"         Why Fidelity may be right: if portfolio is GREEN here, thesis survived the selloff")
            a(f"         What to watch: RSI reset + catalyst combo; wait for MACD histogram to turn up")
            a("")
        a(DIVIDER)
        a("")

    # ── TIER SECTIONS III–VII ─────────────────────────────────────────────
    tier_specs = [
        (1, "STRONG FUNDAMENTALS", "BUY / ACCUMULATE",
         "These names lead because all three conviction signals align: TT score ≥7/10, "
         "panel consensus ≥3/4 bullish, and positive expected value — with no accounting "
         "concerns clouding the thesis. Simulation probability may be lower than the "
         "numbers suggest; that gap is the opportunity. Act on fundamental thesis, not sim rank."),
        (2, "MOMENTUM LEADERS", "MANAGE ACTIVELY",
         "High simulation conviction but fundamental PASS from the Think Tank. "
         "The Skeptic panel issued ELIMINATE warnings and quantitative EV is negative. "
         "These are momentum trades with no margin of safety — size small, use hard stops, "
         "do not treat as conviction longs."),
        (3, "CATALYST REQUIRED", "WATCHLIST",
         "Think Tank scores these BUY or WATCH but a specific blocker prevents deployment "
         "today. Monitor the listed catalyst conditions — when the trigger fires, re-run "
         "and re-classify."),
        (4, "SIGNAL FRAGILE", "RERUN REQUIRED",
         "Prior simulation runs produced a different signal. Inconsistency means we cannot "
         "rely on one run. Do not act until 3+ consistent runs establish signal stability."),
        (5, "AVOID / PASS", "AVOID",
         "Failed on fundamental thesis: 0/4 panel support, negative EV, ELIMINATE from "
         "Skeptic, accounting risk, or structural problems. Pass."),
    ]
    roman = {1: "III", 2: "IV", 3: "V", 4: "VI", 5: "VII"}

    for tier_num, tier_name, tier_action, why_text in tier_specs:
        stocks_in_tier = tier_groups[tier_num]
        if not stocks_in_tier:
            continue
        a(f"{roman[tier_num]}. TIER {tier_num} — {tier_name}: {tier_action}")
        a("")

        # If no Tier 1/2 stocks exist in this batch, force deep-dive prose
        # on top 3 Tier 3 stocks so the investor letter always has substantive writing
        no_tier12 = not tier_groups[1] and not tier_groups[2]
        effective_tier = tier_num
        if no_tier12 and tier_num == 3:
            effective_tier = 1  # trigger prose generation for top Tier 3

        section = build_tier_section(
            effective_tier, tier_name, tier_action, why_text,
            stocks_in_tier[:3] if (no_tier12 and tier_num == 3) else stocks_in_tier,
            rankings_map,
            layer5, layer6, cache, rounds_data, prob_hist,
            api_key, risk_map, stability_map, reg_flags, prior_runs_map,
        )
        # If we had more Tier 3 stocks beyond the prose ones, add them in short format
        if no_tier12 and tier_num == 3 and len(stocks_in_tier) > 3:
            remaining = build_tier_section(
                3, tier_name, tier_action, "",
                stocks_in_tier[3:], rankings_map,
                layer5, layer6, cache, rounds_data, prob_hist,
                api_key, risk_map, stability_map, reg_flags, prior_runs_map,
            )
            section = section + remaining
        a(section)
        a(DIVIDER)
        a("")

    # ── VIII. CATALYST CALENDAR ───────────────────────────────────────────
    a("VIII. CATALYST CALENDAR")
    a("")
    a(catalyst_calendar(tier_rankings, layer5))
    a(DIVIDER)
    a("")

    # ── IX. PROBABILITY TRAJECTORIES ─────────────────────────────────────
    a("IX. PROBABILITY TRAJECTORIES")
    a("")
    big_movers = []
    for t in stocks:
        hist = prob_hist.get(t, [])
        if len(hist) >= 2:
            big_movers.append((t, hist[-1] - hist[0], hist[0], hist[-1]))
    big_movers.sort(key=lambda x: -abs(x[1]))
    for t, move, p0, pf in big_movers:
        tier  = tier_map.get(t, 5)
        sig   = next((r.get("signal", "") for r in rankings if r["ticker"] == t), "")
        trend = "↑ Rising" if move > 0.05 else "↓ Falling" if move < -0.05 else "→ Flat"
        bar   = prob_bar(pf, width=8)
        rflag = "  ⚠ REGRESSION" if t in reg_flags else ""
        a(f"  {t:<6}  [T{tier}]  {bar}  {round(p0*100)}% → {round(pf*100)}%  {trend}  ({sig}){rflag}")
        # Conviction inflation warning: large jump in final free round
        prob_vals = hist
        if len(prob_vals) >= 2:
            final_jump = prob_vals[-1] - prob_vals[-2]
            if final_jump > 0.08:  # >8 point jump in last round
                a(f"  ⚠ CONVICTION INFLATION WARNING: +{final_jump:.0%} jump in final round — agents consolidated without new information. Treat with caution.")
    a("")
    a(DIVIDER)
    a("")

    # ── X. AGENT PERFORMANCE ─────────────────────────────────────────────
    a("X. AGENT PERFORMANCE")
    a("")
    agent_data: dict = {}
    for rd in rounds_data:
        for p in rd["posts"]:
            ag = AGENT_NAMES.get(p["agent"], p["agent"])
            agent_data.setdefault(ag, []).append(p.get("conviction", 0.5))
    if agent_data:
        sorted_agents = sorted(agent_data.items(), key=lambda x: -sum(x[1]) / len(x[1]))
        a(f"  {'Agent':<14}  {'Posts':>5}  {'Avg Conv':>8}")
        a("  " + "─" * 32)
        for ag, convs in sorted_agents:
            avg = sum(convs) / len(convs)
            a(f"  {ag:<14}  {len(convs):>5}  {avg*100:>7.1f}%")
    a("")
    a(DIVIDER)
    a("")

    # ── XI. SIMULATION DEBATE HIGHLIGHTS ─────────────────────────────────
    a("XI. SIMULATION DEBATE HIGHLIGHTS")
    a("")
    injections = [(rd["round"], rd.get("injection", "")) for rd in rounds_data if rd.get("injection")]
    if injections:
        for rnd, inj in injections[:3]:
            before = next((rd.get("market_probs", {}) for rd in rounds_data if rd["round"] == rnd - 1), {})
            after  = next((rd.get("market_probs", {}) for rd in rounds_data if rd["round"] == rnd), {})
            if before and after:
                deltas = {t: after.get(t, 0) - before.get(t, 0)
                          for t in stocks if t in after and t in before}
                if deltas:
                    winners = sorted(deltas.items(), key=lambda x: -x[1])[:2]
                    losers  = sorted(deltas.items(), key=lambda x: x[1])[:2]
                    w_str   = ", ".join(f"{t} +{round(d*100)}%" for t, d in winners if d > 0)
                    l_str   = ", ".join(f"{t} {round(d*100)}%" for t, d in losers if d < 0)
                    a(f"  Round {rnd}: \"{inj[:120]}\"")
                    if w_str:
                        a(f"  Benefited: {w_str}")
                    if l_str:
                        a(f"  Sold off:  {l_str}")
                    a("")
    if contrarians:
        a("Early Contrarian Flags:")
        for (agent_key, ticker), info in list(contrarians.items())[:5]:
            a(f"  [EARLY CONTRARIAN] {info['agent']} on {ticker} — "
              f"{info['rounds_contrarian']} rounds, "
              f"peak R{info['round']} @ {round(info['conviction']*100)}%")
            if info["post"]:
                a(f"    \"{info['post'][:200]}\"")
            a("")

        # P5 fix: surface Chanos floor vs snapshot downside conflict explicitly
        chanos_entries = {(ag, t): info for (ag, t), info in contrarians.items()
                         if ag == "short_seller"}
        if chanos_entries:
            a("  BEAR/BULL FLOOR CONFLICT:")
            for (ag, ticker), info in chanos_entries.items():
                l5  = layer5.get(ticker, {})
                fd  = cache.get(ticker, {})
                price = fd.get("price", 0) or 0
                # Try to extract a floor from Chanos post
                import re as _re
                floor_m = _re.search(r'\$(\d+)', info["post"])
                chanos_floor = floor_m.group(0) if floor_m else "unspecified"
                # Compare to kill condition downside
                kill = l5.get("kill_condition", "")
                a(f"  {ticker}: Chanos (short_seller) held bearish for {info['rounds_contrarian']} rounds.")
                a(f"    Chanos floor reference: {chanos_floor} | Current price: ${price:.0f}")
                a(f"    Kill condition: {kill[:100]}")
                a(f"    ⚠ If Chanos floor differs materially from snapshot downside, size accordingly.")
                a("")

    a(DIVIDER)
    a("")

    # ── XI-B. RISK CATEGORY TAGS + CONCENTRATION WARNING (v4_24) ─────────
    a("XI-B. RISK CATEGORY TAGS")
    a("")
    risk_tags_by_ticker: dict = {}
    _biotech_sec = {"biotechnology", "healthcare", "pharmaceuticals"}
    for r in ranked:
        t      = r["ticker"]
        fd     = cache.get(t, {})
        l5     = layer5.get(t, {})
        tags   = []
        sector = (fd.get("sector") or "").lower()
        catalyst = (l5.get("catalyst") or "").lower()

        # BINARY_EVENT: biotech + upcoming catalyst
        if any(s in sector for s in _biotech_sec):
            if any(w in catalyst for w in ("phase 3", "pdufa", "bla", "fda", "nda", "readout")):
                tags.append("BINARY_EVENT")

        # HIGH_LEVERAGE
        de = fd.get("debt_to_equity")
        if de is not None:
            try:
                if float(de) > 2.0:
                    tags.append("HIGH_LEVERAGE")
            except (ValueError, TypeError):
                pass

        # LOW_LIQUIDITY
        vol = fd.get("avg_volume") or fd.get("volume")
        if vol is not None:
            try:
                if float(vol) < 500_000:
                    tags.append("LOW_LIQUIDITY")
            except (ValueError, TypeError):
                pass

        # MOMENTUM
        if r.get("composite", 0) > 0.65 and r.get("stability") == "STABLE":
            tags.append("MOMENTUM")

        # DISTRESSED — within 20% of 52wk low
        price   = fd.get("price") or 0
        low52   = fd.get("fifty_two_week_low") or fd.get("low_52w") or fd.get("52wk_low") or 0
        try:
            if float(price) > 0 and float(low52) > 0:
                pct_above_low = (float(price) - float(low52)) / float(low52)
                if pct_above_low < 0.20:
                    tags.append("DISTRESSED")
        except (ValueError, TypeError):
            pass

        # VALUATION_RISK: trailing P/E > 50 or P/S > 15
        pe = fd.get("trailing_pe") or fd.get("pe_ratio") or 0
        ps = fd.get("price_to_sales") or fd.get("ps_ratio") or 0
        try:
            if float(pe) > 50 or float(ps) > 15:
                tags.append("VALUATION_RISK")
        except (ValueError, TypeError):
            pass

        # INSIDER_SELLING: check if skeptic layer mentions insider selling
        skept_text = (l5.get("skeptic") or "").lower()
        if any(w in skept_text for w in ("insider sell", "insider transaction", "executive sold", "director sold", "disposed", "10b5-1")):
            tags.append("INSIDER_SELLING")

        # GEOPOLITICAL_RISK: export controls, tariffs, China, sanctions
        skept_text2 = (l5.get("tech_macro") or l5.get("macro") or "").lower()
        if any(w in skept_text2 for w in ("export control", "tariff", "sanction", "china", "geopolit", "trade war", "itar")):
            tags.append("GEOPOLITICAL_RISK")

        # SPINOFF_EXECUTION_RISK: spinoff mentioned in scout or summary
        scout_text = (l5.get("scout") or "").lower()
        summary_text = (l5.get("summary") or "").lower()
        if any(w in scout_text + summary_text for w in ("spinoff", "spin-off", "spin off", "spinco", "remainco", "sum-of-parts", "sop valuation")):
            tags.append("SPINOFF_EXECUTION_RISK")

        if tags:
            risk_tags_by_ticker[t] = tags
            a(f"  {t:<6}  {' | '.join(tags)}")
        else:
            a(f"  {t:<6}  (no flags)")

    # Concentration warning: >3 tickers with same tag
    from collections import Counter as _Counter
    all_tags = [tag for tags in risk_tags_by_ticker.values() for tag in tags]
    tag_counts = _Counter(all_tags)
    concentration_warnings = [(tag, cnt) for tag, cnt in tag_counts.items() if cnt > 3]
    if concentration_warnings:
        a("")
        for tag, cnt in concentration_warnings:
            a(f"  ⚠ CONCENTRATION: {cnt} {tag} names — consider position limits")

    a("")
    a(DIVIDER)
    a("")

    # ── XII. RERUN REFERENCE ──────────────────────────────────────────────
    a("XII. RERUN REFERENCE")
    a("")
    stock_str = " ".join(stocks)
    a(f"Original analysis: {ts}")
    a(f"  python3 ~/ORACLE/engine/oracle_think_tank.py --stocks {stock_str}")
    a(f"  python3 ~/ORACLE/sim/run_sim.py --stocks {stock_str} --rounds {rounds}")
    a("")
    if injections_ctx:
        a("Injection Context:")
        for ic in injections_ctx:
            a(f"  R{ic['round']:>2}: \"{ic['injection_text'][:70]}\"  "
              f"winner={ic['winner_ticker']} loser={ic['loser_ticker']}")
        a("")

    a("Rerun Seed Table:")
    a(f"{'Ticker':<6}  {'Date':<10}  {'Signal':<12}  {'Prob':>5}  "
      f"{'Tier':<6}  {'Stability':<12}  Action")
    a("-" * 80)
    for r in ranked:
        t      = r["ticker"]
        sig_r  = r.get("signal", "?")
        prob_r = round(r.get("probability", 0) * 100)
        tier_r = tier_map.get(t, 5)
        stab_r = stability_map.get(t, "PROVISIONAL")
        act_r  = get_recommended_action_v3(tier_r, t, r, layer5, cache)[:40]
        a(f"{t:<6}  {ts:<10}  {sig_r:<12}  {prob_r:>4}%  "
          f"T{tier_r:<5}  {stab_r:<12}  {act_r}")
    a("")

    # ── XIII. PRICE RECONCILIATION GATE (Item 1+2) ───────────────────────
    # Force all price targets to reconcile against current live price.
    # Catches ghost prices from prior rounds bleeding into the verdict.
    a("XIII. PRICE RECONCILIATION GATE")
    a("")
    a("Canonical price table — all targets reconciled against current live price.")
    a("")
    _rec_header = f"  {'Ticker':<6}  {'Live $':>7}  {'Bear $':>7}  {'Base $':>7}  {'Bull $':>7}  {'Bear%':>6}  {'Base%':>6}  {'Bull%':>6}  {'Verdict at Live Price'}"
    a(_rec_header)
    a("  " + "─" * 100)
    _any_ghost = False
    for r in ranked:
        t    = r["ticker"]
        fd   = cache.get(t, {})
        l5   = layer5.get(t, {})
        live = fd.get("price") or 0
        try:
            live = float(live)
        except (ValueError, TypeError):
            live = 0.0

        # Extract bear/base/bull from layer5 valuation text
        import re as _re_rec
        val_text = (l5.get("valuation") or l5.get("fundamental") or l5.get("summary") or "")
        bear_m = _re_rec.search(r'bear[^\d$]*\$?\s*(\d+(?:\.\d+)?)', val_text, _re_rec.IGNORECASE)
        base_m = _re_rec.search(r'base[^\d$]*\$?\s*(\d+(?:\.\d+)?)', val_text, _re_rec.IGNORECASE)
        bull_m = _re_rec.search(r'bull[^\d$]*\$?\s*(\d+(?:\.\d+)?)', val_text, _re_rec.IGNORECASE)
        bear_p = float(bear_m.group(1)) if bear_m else 0.0
        base_p = float(base_m.group(1)) if base_m else 0.0
        bull_p = float(bull_m.group(1)) if bull_m else 0.0

        if live > 0 and base_p > 0:
            bear_pct = f"{(bear_p - live) / live * 100:+.0f}%" if bear_p else "N/A"
            base_pct = f"{(base_p - live) / live * 100:+.0f}%" if base_p else "N/A"
            bull_pct = f"{(bull_p - live) / live * 100:+.0f}%" if bull_p else "N/A"
            # Verdict at current price
            sig = r.get("signal", "PASS")
            if base_p > 0 and live > base_p * 1.10:
                verdict = "⚠ ABOVE BASE — reassess entry"
                _any_ghost = True
            elif base_p > 0 and live < base_p * 0.90:
                verdict = "✅ BELOW BASE — favourable entry"
            else:
                verdict = f"{sig} at current price"
            # Ghost price flag: any target > 20% above current = stale price
            if bull_p > live * 1.5 or (bear_p > 0 and bear_p > live * 1.2):
                verdict += " ⚠ GHOST PRICE DETECTED"
                _any_ghost = True
            a(f"  {t:<6}  ${live:>6.2f}  ${bear_p:>6.2f}  ${base_p:>6.2f}  ${bull_p:>6.2f}  {bear_pct:>6}  {base_pct:>6}  {bull_pct:>6}  {verdict}")
        else:
            a(f"  {t:<6}  ${live:>6.2f}  {'N/A':>7}  {'N/A':>7}  {'N/A':>7}  {'N/A':>6}  {'N/A':>6}  {'N/A':>6}  No price targets parsed")

    if _any_ghost:
        a("")
        a("  ⚠ GHOST PRICE WARNING: One or more price targets appear inconsistent with the current")
        a("    live price. Agents may have cited prices from prior rounds or hypothetical scenarios.")
        a("    Use only the 'Verdict at Live Price' column for actionable decisions.")
    a("")
    a(DIVIDER)
    a("")

    # ── XIV. INTERNAL CONTRADICTION CHECK (Item 3) ───────────────────────
    a("XIV. INTERNAL CONSISTENCY CHECK")
    a("")
    _contradictions = []
    for r in ranked:
        t    = r["ticker"]
        fd   = cache.get(t, {})
        l5   = layer5.get(t, {})
        live = float(fd.get("price") or 0)
        sig  = r.get("signal", "PASS")

        import re as _re_con
        val_text = (l5.get("valuation") or l5.get("fundamental") or l5.get("summary") or "")

        # Check 1: BUY signal but negative EV
        ev_raw = (l5.get("valuation") or "")
        ev_m = _re_con.search(r'EV:\s*([+-]?\d+\.?\d*)%', ev_raw)
        ev_val = float(ev_m.group(1)) if ev_m else None
        if sig in ("BUY", "STRONG_BUY") and ev_val is not None and ev_val < -20:
            _contradictions.append(f"  {t}: BUY signal but EV = {ev_val:.0f}% — fundamentals and sim are diverging")

        # Check 2: Bear floor > current price (impossible scenario)
        bear_m2 = _re_con.search(r'bear[^\d$]*\$?\s*(\d+(?:\.\d+)?)', val_text, _re_con.IGNORECASE)
        bear_p2 = float(bear_m2.group(1)) if bear_m2 else 0.0
        if bear_p2 > 0 and live > 0 and bear_p2 > live * 1.15:
            _contradictions.append(f"  {t}: Bear floor ${bear_p2:.0f} is ABOVE live price ${live:.0f} — scenario is impossible at current levels")

        # Check 3: Bull target < live price
        bull_m2 = _re_con.search(r'bull[^\d$]*\$?\s*(\d+(?:\.\d+)?)', val_text, _re_con.IGNORECASE)
        bull_p2 = float(bull_m2.group(1)) if bull_m2 else 0.0
        if bull_p2 > 0 and live > 0 and bull_p2 < live * 0.90:
            _contradictions.append(f"  {t}: Bull target ${bull_p2:.0f} is BELOW live price ${live:.0f} — bull case has no upside")

        # Check 4: Valuation mode conflict — Magic Formula framing on compounder
        tt_score_v = parse_tt_score((l5 or {}).get("overall", ""))
        if tt_score_v and tt_score_v >= 7:
            mf_keywords = ["earnings yield", "magic formula", "low p/e", "low earnings multiple"]
            val_lower = val_text.lower()
            if any(kw in val_lower for kw in mf_keywords):
                _contradictions.append(f"  {t}: High-conviction compounder (TT score {tt_score_v}) but Magic Formula framing detected in valuation — wrong lens")

    if _contradictions:
        a("  ⚠ CONTRADICTIONS FOUND — resolve before acting:")
        a("")
        for c in _contradictions:
            a(c)
    else:
        a("  ✅ No internal contradictions detected.")
    a("")
    a(DIVIDER)
    a("")

    # ── XV. UPSIDE/DOWNSIDE MATH SANITY CHECK (Item 5) ───────────────────
    a("XV. UPSIDE/DOWNSIDE SANITY CHECK")
    a("")
    a("  Any target requiring >50% move in under 6 months is flagged.")
    a("")
    a(f"  {'Ticker':<6}  {'Live $':>7}  {'Base $':>7}  {'Implied Return':>14}  {'6-mo Ann.':>10}  {'Flag'}")
    a("  " + "─" * 75)
    for r in ranked:
        t    = r["ticker"]
        fd   = cache.get(t, {})
        l5   = layer5.get(t, {})
        live = float(fd.get("price") or 0)
        if live <= 0:
            continue
        import re as _re_updn
        val_text = (l5.get("valuation") or l5.get("fundamental") or l5.get("summary") or "")
        base_m3 = _re_updn.search(r'base[^\d$]*\$?\s*(\d+(?:\.\d+)?)', val_text, _re_updn.IGNORECASE)
        base_p3 = float(base_m3.group(1)) if base_m3 else 0.0
        if base_p3 > 0:
            implied = (base_p3 - live) / live * 100
            ann_6mo = implied * 2  # annualized from 6-month horizon
            flag = "⚠ REQUIRES JUSTIFICATION" if abs(implied) > 50 else "✅ Reasonable"
            a(f"  {t:<6}  ${live:>6.2f}  ${base_p3:>6.2f}  {implied:>+13.1f}%  {ann_6mo:>+9.1f}%  {flag}")
    a("")
    a(DIVIDER)
    a("")

    # ── XVI. CURRENT EVENTS FRESHNESS CHECK (Item 4) ─────────────────────
    a("XVI. CURRENT EVENTS FRESHNESS CHECK")
    a("")
    a("  Risk factors are flagged if they reference political/regulatory framing")
    a("  that may be >90 days stale. Verify currency before acting.")
    a("")
    import datetime as _dt
    _today = _dt.date.today()
    _stale_keywords = [
        "irs direct file", "tax reform", "cfpb", "dodd-frank", "sec lawsuit",
        "antitrust", "congress", "senate bill", "house bill", "fda approval",
        "fdic", "tariff", "export control", "lobbying", "election",
    ]
    for r in ranked:
        t  = r["ticker"]
        l5 = layer5.get(t, {})
        risk_text = " ".join([
            l5.get("skeptic") or "",
            l5.get("tech_macro") or "",
            l5.get("summary") or "",
        ]).lower()
        found = [kw for kw in _stale_keywords if kw in risk_text]
        if found:
            a(f"  {t}: ⚠ Contains time-sensitive risk framing: {', '.join(found[:3])}")
            a(f"       Verify these factors are current as of {_today}.")
            a(f"       If sim cannot confirm currency, treat risk assessment as provisional.")
    a("")
    a("  NOTE: Run a fresh sim or web-fetch to verify any flagged risk factors.")
    a("")
    a(DIVIDER)
    a("")

    # ── XVII. GOLD-OZ INTEGRATION (Item 7) ───────────────────────────────
    a("XVII. GOLD-OZ VALUATION FRAMEWORK")
    a("")
    a("  Gold-oz value = Stock Price / XAUUSD Spot. Measures real purchasing power.")
    a("  Useful for entry/exit decisions independent of fiat currency effects.")
    a("")
    _gold_price = 0.0
    try:
        import yfinance as _yf_gold
        _gold_data = _yf_gold.Ticker("GC=F").history(period="1d")
        if not _gold_data.empty:
            _gold_price = float(_gold_data["Close"].iloc[-1])
    except Exception:
        pass
    if _gold_price <= 0:
        a("  ⚠ Gold price unavailable — skipping gold-oz calculations.")
    else:
        a(f"  Current XAUUSD: ${_gold_price:,.2f}/oz")
        a("")
        a(f"  {'Ticker':<6}  {'Live $':>8}  {'Gold-Oz':>8}  {'Bear oz':>8}  {'Base oz':>8}  {'Bull oz':>8}  {'Assessment'}")
        a("  " + "─" * 85)
        for r in ranked:
            t    = r["ticker"]
            fd   = cache.get(t, {})
            l5   = layer5.get(t, {})
            live = float(fd.get("price") or 0)
            if live <= 0 or _gold_price <= 0:
                continue
            gold_oz = live / _gold_price
            import re as _re_gold
            val_text = (l5.get("valuation") or l5.get("fundamental") or l5.get("summary") or "")
            bear_mg = _re_gold.search(r'bear[^\d$]*\$?\s*(\d+(?:\.\d+)?)', val_text, _re_gold.IGNORECASE)
            base_mg = _re_gold.search(r'base[^\d$]*\$?\s*(\d+(?:\.\d+)?)', val_text, _re_gold.IGNORECASE)
            bull_mg = _re_gold.search(r'bull[^\d$]*\$?\s*(\d+(?:\.\d+)?)', val_text, _re_gold.IGNORECASE)
            bear_oz = float(bear_mg.group(1)) / _gold_price if bear_mg else 0.0
            base_oz = float(base_mg.group(1)) / _gold_price if base_mg else 0.0
            bull_oz = float(bull_mg.group(1)) / _gold_price if bull_mg else 0.0
            # Get 52-week range for gold-oz context
            lo52 = float(fd.get("fifty_two_week_low") or fd.get("52wk_low") or 0)
            hi52 = float(fd.get("fifty_two_week_high") or fd.get("52wk_high") or 0)
            lo_oz = lo52 / _gold_price if lo52 > 0 else 0.0
            hi_oz = hi52 / _gold_price if hi52 > 0 else 0.0
            # Assessment
            if lo_oz > 0 and hi_oz > lo_oz:
                pct_of_range = (gold_oz - lo_oz) / (hi_oz - lo_oz) * 100
                if pct_of_range < 25:
                    oz_assess = f"GOLD-OZ LOW ({pct_of_range:.0f}% of 52wk range) — accumulation zone"
                elif pct_of_range > 75:
                    oz_assess = f"GOLD-OZ HIGH ({pct_of_range:.0f}% of 52wk range) — extended"
                else:
                    oz_assess = f"Mid-range ({pct_of_range:.0f}% of 52wk gold-oz range)"
            else:
                oz_assess = "52wk range unavailable"
            bear_str = f"{bear_oz:.4f}" if bear_oz > 0 else "N/A"
            base_str = f"{base_oz:.4f}" if base_oz > 0 else "N/A"
            bull_str = f"{bull_oz:.4f}" if bull_oz > 0 else "N/A"
            a(f"  {t:<6}  ${live:>7.2f}  {gold_oz:>8.4f}  {bear_str:>8}  {base_str:>8}  {bull_str:>8}  {oz_assess}")
        a("")
        a("  Integration points for decision-making:")
        a("  — Entry: prefer buying when gold-oz is in bottom 25% of 3-year range")
        a("  — Bear case: state bear target in oz — tells you real cost of being wrong")
        a("  — Verdict: compare gold-oz to prior accumulation zones before sizing")
    a("")
    a(DIVIDER)
    a("")

    # ── XVIII. FALSIFIABLE PREDICTION SCORECARD (Item 8) ─────────────────
    a("XVIII. FALSIFIABLE PREDICTION SCORECARD")
    a("")
    a("  Dated, falsifiable predictions. Check these on the NEXT sim run.")
    a("  Format: [ ] = open  [✓] = confirmed  [✗] = falsified")
    a("")
    _score_date = (_dt.date.today() + _dt.timedelta(days=90)).isoformat()
    _top_picks = [r for r in ranked if tier_map.get(r["ticker"], 5) <= 2][:3]
    if not _top_picks:
        _top_picks = ranked[:3]
    for r in _top_picks:
        t    = r["ticker"]
        fd   = cache.get(t, {})
        l5   = layer5.get(t, {})
        live = float(fd.get("price") or 0)
        sig  = r.get("signal", "PASS")
        prob = r.get("probability", 0.5)
        tier = tier_map.get(t, 5)
        import re as _re_fp
        val_text = (l5.get("valuation") or l5.get("fundamental") or l5.get("summary") or "")
        base_mfp = _re_fp.search(r'base[^\d$]*\$?\s*(\d+(?:\.\d+)?)', val_text, _re_fp.IGNORECASE)
        base_pfp = float(base_mfp.group(1)) if base_mfp else 0.0
        cat_text = (l5.get("catalyst") or "catalyst not specified")[:80]
        a(f"  {t} — Tier {tier} | {sig} | Live: ${live:.2f} | Check by: {_score_date}")
        a(f"  [ ] 1. {t} trades above ${base_pfp:.2f} (base target) within 90 days")
        if live > 0 and base_pfp > live:
            upside = (base_pfp - live) / live * 100
            a(f"          Implied move: +{upside:.1f}% from current price")
        a(f"  [ ] 2. Primary catalyst materializes: {cat_text}")
        a(f"  [ ] 3. Sim conviction holds at {prob*100:.0f}%+ on next rerun")
        if _gold_price > 0 and live > 0:
            oz_now = live / _gold_price
            a(f"  [ ] 4. Gold-oz value improves from current {oz_now:.4f} oz (appreciation vs gold)")
        a("")

    # Check prior predictions from run history and grade them
    _graded_any = False
    for t in stocks:
        prior = prior_runs_map.get(t, [])
        if len(prior) >= 2:
            prev = prior[-2]
            curr_r = next((r for r in rankings if r["ticker"] == t), None)
            if curr_r and not _graded_any:
                a("  PRIOR PREDICTION GRADES (from previous runs):")
                _graded_any = True
            if curr_r:
                prev_sig = prev.get("signal", "?")
                curr_sig = curr_r.get("signal", "?")
                prev_prob = prev.get("probability", 0)
                curr_prob = curr_r.get("probability", 0)
                match = "✓" if prev_sig == curr_sig else "✗"
                a(f"  [{match}] {t}: Predicted {prev_sig} ({prev_prob*100:.0f}%) → Current {curr_sig} ({curr_prob*100:.0f}%)")
    if _graded_any:
        a("")
    a(DIVIDER)
    a("")

    # ── Write to history DB ───────────────────────────────────────────────
    for r in rankings:
        t  = r["ticker"]
        l5 = layer5.get(t, {})
        _, _, tt_overall = _parse_tt_info(l5)
        tt_score_v = parse_tt_score((l5 or {}).get("overall", ""))
        write_to_history(run_id, t, {
            "date_analyzed":      ts,
            "signal":             r.get("signal", ""),
            "probability":        r.get("probability", 0.0),
            "composite":          r.get("composite", 0.0),
            "tt_overall":         tt_overall,
            "tt_score":           str(tt_score_v) if tt_score_v is not None else "",
            "catalyst":           (l5 or {}).get("catalyst", ""),
            "velocity":           r.get("velocity", 0.0),
            "converted_skeptics": r.get("converted_skeptics", 0),
            "injections_fired":   injections_ctx,
        })
    print(f"  Wrote {len(rankings)} ticker results to oracle_history.db", file=sys.stderr)

    return "\n".join(lines)


def save_report(content: str, run_id: str) -> str:
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"ORACLE_MEMO_{run_id}_{date_str}.md"
    path = FINAL_DIR / filename
    path.write_text(content, encoding="utf-8")
    return str(path)


def list_final_reports() -> list:
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    reports = []
    for f in sorted(FINAL_DIR.glob("ORACLE_*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        reports.append({
            "filename": f.name,
            "path":     str(f),
            "size":     f.stat().st_size,
            "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return reports


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate ORACLE Investment Memo")
    parser.add_argument("--sim",    required=True, help="Path to sim JSON")
    parser.add_argument("--report", default="",   help="Path to composite report md")
    args = parser.parse_args()

    print("Generating investment memo...", file=sys.stderr)
    content = generate_report(args.sim, args.report)
    path    = save_report(content, Path(args.sim).stem)
    print(f"Saved: {path}", file=sys.stderr)
    print(f"Size:  {len(content):,} chars", file=sys.stderr)
    print()
    print("=== FIRST 80 LINES ===")
    for line in content.split("\n")[:80]:
        print(line)
