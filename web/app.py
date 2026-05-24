#!/usr/bin/env python3
"""
ORACLE Web Dashboard — Flask backend
Serves on http://localhost:5050
"""

import os
import sys
import json
import re
import queue
import threading
import time
import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context, send_file
from flask_cors import CORS

# Make sim/ importable
_SIM_DIR = str(Path(os.path.expanduser("~/ORACLE/sim")))
if _SIM_DIR not in sys.path:
    sys.path.insert(0, _SIM_DIR)

# Make engine/ importable for screener
_ENGINE_DIR = str(Path(os.path.expanduser("~/ORACLE/engine")))
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

SIMS_DIR    = Path(os.path.expanduser("~/ORACLE/sims"))
ORACLE_VAULT = Path(os.path.expanduser(
    "~/Documents/Trading Vault/03_Stock_Analysis/ORACLE"
))
SIM_SCRIPT  = Path(os.path.expanduser("~/ORACLE/sim/run_sim.py"))

NEO4J_URI   = "bolt://localhost:7687"
NEO4J_AUTH  = ("neo4j", "miroshark2026")

running_procs = {}   # run_id -> Popen (legacy, kept for /status endpoint)
_sim_queues   = {}   # run_id -> queue.Queue  (SSE streaming)
_sim_replays  = {}   # run_id -> list of all events (replay buffer)

_screen_queues  = {}   # job_id -> queue.Queue
_screen_results = {}   # job_id -> dict with keys: status, log_lines, results, triage

_tt_queues  = {}   # job_id -> queue.Queue
_tt_results = {}   # job_id -> dict: status, log_lines, report_path, tickers

_screen_replays = {}   # job_id -> list of all screen events (replay buffer)
_tt_replays     = {}   # job_id -> list of all TT events (replay buffer)

_STATE_FILE = Path.home() / "ORACLE" / "web_state.json"

_sim_stop_events  = {}  # run_id -> threading.Event  (set = stop requested)
_sim_pause_events = {}  # run_id -> threading.Event  (set = paused)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_neo4j_driver():
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
        driver.verify_connectivity()
        return driver
    except Exception:
        return None


def _signal_color(signal: str) -> str:
    s = (signal or "").upper()
    if "STRONG_BUY" in s or "STRONG BUY" in s:
        return "#00d4aa"
    if "BUY" in s or "INVESTIGATE" in s:
        return "#4488ff"
    if "HOLD" in s:
        return "#ffcc44"
    if "WATCH" in s:
        return "#ff8844"
    if "PASS" in s or "ELIMINATE" in s:
        return "#ff4444"
    return "#888888"


def _prob_color(prob: float) -> str:
    if prob >= 0.52:
        return "#00d4aa"
    if prob >= 0.40:
        return "#4488ff"
    if prob >= 0.30:
        return "#ffcc44"
    return "#ff4444"


def _parse_round_md(md_text: str) -> dict:
    """Parse a round_XX.md file into structured JSON."""
    result = {"injection": "", "posts": [], "probabilities": {}}

    # Extract frontmatter injection
    fm_match = re.search(r'injection:\s*"([^"]*)"', md_text)
    if fm_match:
        result["injection"] = fm_match.group(1)

    # Extract Director Injection block
    dir_match = re.search(r'## Director Injection\s*\n> (.+?)(?=\n## |\Z)', md_text, re.DOTALL)
    if dir_match:
        result["injection"] = dir_match.group(1).strip()

    # Extract agent posts — split on "### Agent Name  `CONVICTION: XX%`"
    agent_blocks = re.split(r'\n(?=### .+?`CONVICTION)', md_text)
    for block in agent_blocks:
        if not block.startswith("###"):
            continue
        header_m = re.match(r'### (.+?)\s+`CONVICTION:\s*(\d+)%`', block)
        if not header_m:
            continue
        agent_name  = header_m.group(1).strip()
        conviction  = int(header_m.group(2))
        post_text   = block[header_m.end():].strip()
        # Remove stance footnote
        post_text   = re.sub(r'\n\*Stances:.*\*\s*$', '', post_text, flags=re.MULTILINE).strip()
        # Extract stances
        stances     = {}
        stance_m    = re.search(r'\*Stances:\s*(.+?)\*', block)
        if stance_m:
            for tok in stance_m.group(1).split():
                parts = tok.split(":")
                if len(parts) == 2:
                    stances[parts[0]] = "bullish" if parts[1].upper() == "B" else "bearish"
        result["posts"].append({
            "agent":      agent_name,
            "conviction": conviction,
            "stances":    stances,
            "post":       post_text,
        })

    # Extract market probabilities table
    prob_section = re.search(r'## Market Probabilities\s*\n(.+?)(?=\n## |\Z)', md_text, re.DOTALL)
    if prob_section:
        for row in prob_section.group(1).splitlines():
            m = re.match(r'\|\s*([A-Z]{2,6})\s*\|\s*([\d.]+)%', row)
            if m:
                result["probabilities"][m.group(1)] = float(m.group(2)) / 100.0

    return result


def _build_graph_from_json(run_id: str) -> dict:
    """Construct a vis-network compatible graph from sim JSON when Neo4j is unavailable."""
    # Try consolidated JSON
    json_file = SIMS_DIR / f"{run_id}.json"
    if not json_file.exists():
        # Try subdir manifest
        subdir = SIMS_DIR / run_id
        if not subdir.is_dir():
            return {"nodes": [], "edges": [], "source": "empty"}

        agents_file   = subdir / "agents.json"
        markets_file  = subdir / "markets.json"
        manifest_file = subdir / "sim_manifest.json"

        stocks = []
        if manifest_file.exists():
            manifest = json.loads(manifest_file.read_text())
            stocks   = manifest.get("tickers", [])

        nodes, edges = [], []

        if markets_file.exists():
            for mkt in json.loads(markets_file.read_text()):
                mkt_id = mkt.get("id", "")
                if mkt.get("type") == "binary" and "_vs_" not in mkt_id and "forced" not in mkt_id:
                    for ticker in stocks:
                        if ticker in mkt_id:
                            prob = mkt.get("initial_probability", 0.5)
                            nodes.append({
                                "id": f"stock_{ticker}", "label": ticker, "type": "Stock",
                                "properties": {"probability": prob, "ticker": ticker,
                                               "signal": "WATCH"},
                            })
                            break

        if agents_file.exists():
            for agent in json.loads(agents_file.read_text()):
                aid = f"agent_{agent['id']}"
                nodes.append({
                    "id": aid, "label": agent["name"][:25], "type": "Agent",
                    "properties": {"name": agent["name"], "layer": agent.get("layer", 1)},
                })
                for ticker in agent.get("universe", []):
                    edges.append({
                        "source": aid, "target": f"stock_{ticker}",
                        "type": "FOLLOWS", "properties": {},
                    })

        return {"nodes": nodes, "edges": edges, "source": "manifest"}

    # Use consolidated JSON
    data   = json.loads(json_file.read_text())
    stocks = data.get("stocks", [])
    rankings = {r["ticker"]: r for r in data.get("rankings", [])}

    nodes, edges = [], []

    for ticker in stocks:
        r = rankings.get(ticker, {})
        nodes.append({
            "id": f"stock_{ticker}", "label": ticker, "type": "Stock",
            "properties": {
                "signal": r.get("signal", "WATCH"),
                "score":  r.get("score", 5.0),
                "ticker": ticker,
            },
        })

    # COMPETES_WITH between all stocks
    for i, t1 in enumerate(stocks):
        for t2 in stocks[i + 1:]:
            edges.append({
                "source": f"stock_{t1}", "target": f"stock_{t2}",
                "type": "COMPETES_WITH", "properties": {},
            })

    # Agents + stance edges from rounds_data
    agent_names   = set()
    stance_tally  = {}   # (agent, ticker) -> {bullish, bearish}

    for rd in data.get("rounds_data", []):
        for post in rd.get("posts", []):
            agent = post.get("agent", "")
            if not agent:
                continue
            agent_names.add(agent)
            for ticker, stance in post.get("stances", {}).items():
                key = (agent, ticker)
                if key not in stance_tally:
                    stance_tally[key] = {"bullish": 0, "bearish": 0}
                if stance in ("bullish", "bearish"):
                    stance_tally[key][stance] += 1

    for agent_name in sorted(agent_names):
        aid = f"agent_{agent_name}"
        nodes.append({
            "id": aid, "label": agent_name[:25], "type": "Agent",
            "properties": {"name": agent_name},
        })
        for ticker in stocks:
            counts  = stance_tally.get((agent_name, ticker), {})
            bull    = counts.get("bullish", 0)
            bear    = counts.get("bearish", 0)
            if bull > bear and bull > 0:
                edges.append({"source": aid, "target": f"stock_{ticker}",
                              "type": "BULLISH_ON", "properties": {"count": bull}})
            elif bear > bull and bear > 0:
                edges.append({"source": aid, "target": f"stock_{ticker}",
                              "type": "BEARISH_ON", "properties": {"count": bear}})

    return {"nodes": nodes, "edges": edges, "source": "json"}


# ── Stock validation ──────────────────────────────────────────────────────────

def validate_stocks_basic(stocks):
    """
    Load fundamentals synchronously and return advisory warning strings.
    Called before starting the sim thread so warnings appear in the POST response.
    Returns [] on any error so it never blocks sim start.
    """
    try:
        from run_sim import _load_fundamentals, validate_stocks
        fundamentals = _load_fundamentals(stocks)
        return validate_stocks(stocks, fundamentals)
    except Exception:
        return []


# ── SSE simulation thread ─────────────────────────────────────────────────────

def _serialisable(obj):
    if hasattr(obj, "__dict__"):
        return {k: _serialisable(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialisable(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _serialisable(v) for k, v in obj.items()}
    return obj


def _run_sim_thread(run_id, stocks, rounds, fast, report_path):
    """Background thread: runs simulation, scores, saves JSON, emits SSE events."""
    def cb(event):
        q = _sim_queues.get(run_id)
        if q:
            q.put(event)
        # Always store in replay buffer
        if run_id in _sim_replays:
            _sim_replays[run_id].append(event)

    try:
        from run_sim import _load_api_key, _load_fundamentals, _load_report
        from round_loop import run_simulation
        from scorer import score_simulation
        from graph_builder import get_driver

        HAIKU = "anthropic/claude-3.5-haiku"
        model = HAIKU  # fast flag ignored at model level — Haiku is already default

        api_key = _load_api_key()

        for ticker in stocks:
            cb({"type": "sim_log", "msg": f"⟳ Fetching live data from yfinance for {ticker}...", "level": "active"})
        fundamentals = _load_fundamentals(stocks)
        cb({"type": "sim_log", "msg": f"✓ Fundamentals loaded — {len(stocks)} stocks", "level": "success"})

        # BUG 7: advisory validation — emit warnings but never block
        from run_sim import validate_stocks as _validate_stocks
        for w in _validate_stocks(stocks, fundamentals):
            cb({"type": "sim_log", "msg": f"⚠ {w}", "level": "active"})

        cb({"type": "sim_log", "msg": "⟳ Building knowledge graph in Neo4j...", "level": "active"})
        report_text  = _load_report(report_path if report_path else None)

        stop_event  = threading.Event()
        pause_event = threading.Event()
        _sim_stop_events[run_id]  = stop_event
        _sim_pause_events[run_id] = pause_event

        results = run_simulation(
            run_id       = run_id,
            stocks       = stocks,
            fundamentals = fundamentals,
            report_text  = report_text,
            num_rounds   = rounds,
            model        = model,
            api_key      = api_key,
            event_callback = cb,
            stop_event   = stop_event,
            pause_event  = pause_event,
        )

        driver = get_driver() if results.get("driver_active") else None

        rankings = score_simulation(
            driver          = driver,
            run_id          = run_id,
            markets         = results["markets"],
            all_rounds      = results["rounds"],
            stocks          = stocks,
            intended_rounds = rounds,
        )
        if driver:
            driver.close()

        for r in rankings:
            r["score"] = r["composite"]

        prob_history = {t: [] for t in stocks}
        for rd in results["rounds"]:
            mprobs = rd.get("market_probs", {})
            for t in stocks:
                prob_history[t].append(round(mprobs.get(t, 0.5), 4))

        SIMS_DIR.mkdir(parents=True, exist_ok=True)
        output = {
            "run_id":       run_id,
            "stocks":       stocks,
            "model":        model,
            "rounds":       rounds,
            "rankings":     rankings,
            "prob_history": prob_history,
            "markets":      [_serialisable(m) for m in results["markets"]],
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
                for r in results["rounds"]
            ],
            "timestamp": datetime.datetime.now().isoformat(),
        }
        (SIMS_DIR / f"{run_id}.json").write_text(json.dumps(output, indent=2))

        cb({"type": "sim_complete", "rankings": rankings, "prob_history": prob_history})

        # Auto-generate final report (investment memo)
        try:
            if _ENGINE_DIR not in sys.path:
                sys.path.insert(0, _ENGINE_DIR)
            from oracle_final_report import generate_report as _gen_report, save_report as _save_report
            # find latest composite report matching these stocks
            _reports = sorted((Path.home() / "ORACLE" / "reports").glob("*_composite.md"),
                              key=lambda x: x.stat().st_mtime, reverse=True)
            _report_path = str(_reports[0]) if _reports else ""
            _final_content = _gen_report(str(SIMS_DIR / f"{run_id}.json"), _report_path)
            _final_path = _save_report(_final_content, run_id)
            cb({"type": "sim_log", "msg": f"✓ Investment memo generated: {Path(_final_path).name}", "level": "success"})
        except Exception as _fe:
            cb({"type": "sim_log", "msg": f"⚠ Final report generation failed: {_fe}", "level": "warn"})

    except Exception as e:
        cb({"type": "error", "msg": str(e)})
        cb({"type": "sim_complete", "rankings": [], "prob_history": {}})

    # Remove queue and replay buffer after 10 minutes
    threading.Timer(600, lambda: (_sim_queues.pop(run_id, None), _sim_replays.pop(run_id, None))).start()


# ── Routes ────────────────────────────────────────────────────────────────────

def save_web_state(key: str, value: str):
    """Persist active job IDs so browser can reconnect after refresh."""
    try:
        state = {}
        if _STATE_FILE.exists():
            state = json.loads(_STATE_FILE.read_text())
        state[key] = value
        state["updated"] = datetime.datetime.now().isoformat()
        _STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass


def load_web_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def status():
    neo4j_ok = _get_neo4j_driver() is not None
    # Clean up finished procs
    finished = [rid for rid, p in running_procs.items() if p.poll() is not None]
    for rid in finished:
        running_procs.pop(rid, None)
    return jsonify({
        "neo4j":        neo4j_ok,
        "running_sims": list(running_procs.keys()),
    })


@app.route("/api/state")
def get_web_state():
    """Return persisted job IDs so frontend can reconnect after refresh."""
    state = load_web_state()
    # Enrich with current status from in-memory dicts
    screen_id = state.get("screen_job_id", "")
    tt_id     = state.get("tt_job_id", "")
    sim_id    = state.get("sim_run_id", "")
    return jsonify({
        "screen_job_id":      screen_id,
        "screen_status":      _screen_results.get(screen_id, {}).get("status", "unknown") if screen_id else "none",
        "screen_has_results": bool(_screen_results.get(screen_id, {}).get("results")),
        "tt_job_id":          tt_id,
        "tt_status":          _tt_results.get(tt_id, {}).get("status", "unknown") if tt_id else "none",
        "tt_report_path":     _tt_results.get(tt_id, {}).get("report_path", "") if tt_id else "",
        "sim_run_id":         sim_id,
        "sim_running":        sim_id in _sim_queues,
    })


@app.route("/api/sims")
def list_sims():
    results = []
    seen    = set()

    # Consolidated JSON files
    for f in sorted(SIMS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            run_id = data.get("run_id", f.stem)
            if run_id in seen:
                continue
            seen.add(run_id)
            results.append({
                "run_id":    run_id,
                "stocks":    data.get("stocks", []),
                "rounds":    data.get("rounds", 8),
                "timestamp": data.get("timestamp", ""),
                "source":    "json",
                "rankings":  data.get("rankings", []),
            })
        except Exception:
            pass

    # Subdirectory manifests
    for d in sorted(SIMS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        manifest = d / "sim_manifest.json"
        if not manifest.exists():
            continue
        run_id = d.name
        if run_id in seen:
            continue
        seen.add(run_id)
        try:
            mdata = json.loads(manifest.read_text())
            results.append({
                "run_id":    run_id,
                "stocks":    mdata.get("tickers", []),
                "rounds":    None,
                "timestamp": mdata.get("generated_at", ""),
                "source":    "manifest",
                "rankings":  [],
            })
        except Exception:
            pass

    return jsonify(results)


@app.route("/api/sims/<run_id>")
def get_sim(run_id):
    # Consolidated JSON
    json_file = SIMS_DIR / f"{run_id}.json"
    if json_file.exists():
        return jsonify(json.loads(json_file.read_text()))

    # Subdir manifest fallback
    subdir = SIMS_DIR / run_id
    if subdir.is_dir():
        manifest = subdir / "sim_manifest.json"
        data = {}
        if manifest.exists():
            data = json.loads(manifest.read_text())
        markets_data = []
        if (subdir / "markets.json").exists():
            markets_data = json.loads((subdir / "markets.json").read_text())
        agents_data = []
        if (subdir / "agents.json").exists():
            agents_data = json.loads((subdir / "agents.json").read_text())
        return jsonify({
            "run_id":    run_id,
            "stocks":    data.get("tickers", []),
            "timestamp": data.get("generated_at", ""),
            "source":    "manifest",
            "markets":   markets_data,
            "agents":    agents_data,
            "rankings":  [],
            "rounds_data": [],
        })

    # Return running status if sim is in progress instead of 404
    if run_id in _sim_queues:
        return jsonify({"run_id": run_id, "status": "running", "rankings": [], "rounds_data": [], "stocks": []})
    return jsonify({"error": "not found"}), 404


@app.route("/api/graph/<run_id>")
def get_graph(run_id):
    driver = _get_neo4j_driver()
    if driver is None:
        return jsonify(_build_graph_from_json(run_id))

    try:
        nodes, node_id_map = [], {}
        edges = []

        with driver.session() as s:
            # Nodes
            for i, record in enumerate(s.run(
                "MATCH (n {run_id: $run_id}) RETURN n, labels(n) AS lbls LIMIT 300",
                run_id=run_id
            )):
                node   = record["n"]
                labels = record["lbls"]
                nid    = str(node.id)
                node_id_map[nid] = nid
                props  = dict(node)
                ntype  = labels[0] if labels else "Unknown"

                if ntype == "Stock":
                    label = props.get("ticker", nid)
                    signal = props.get("scout_verdict", "WATCH")
                    conv   = props.get("conviction_score", 0.5)
                    signal_mapped = (
                        "STRONG_BUY" if conv >= 0.75 else
                        "BUY"        if conv >= 0.52 else
                        "WATCH"      if conv >= 0.38 else
                        "PASS"
                    )
                elif ntype == "Agent":
                    label = props.get("name", nid)[:25]
                    signal_mapped = "AGENT"
                elif ntype == "Sector":
                    label = props.get("name", nid)
                    signal_mapped = "SECTOR"
                elif ntype == "Catalyst":
                    label = props.get("text", "")[:30]
                    signal_mapped = "CATALYST"
                else:
                    label = str(props)[:20]
                    signal_mapped = "UNKNOWN"

                nodes.append({
                    "id":         nid,
                    "label":      label,
                    "type":       ntype,
                    "signal":     signal_mapped,
                    "properties": {k: v for k, v in props.items() if k != "run_id"},
                })

            # Edges — both directions, deduplicated
            seen_edges = set()
            for record in s.run(
                "MATCH (n {run_id: $run_id})-[r]-(m {run_id: $run_id}) "
                "RETURN id(n) AS src, id(m) AS tgt, type(r) AS rel, properties(r) AS props "
                "LIMIT 500",
                run_id=run_id
            ):
                src, tgt = str(record["src"]), str(record["tgt"])
                rel      = record["rel"]
                ekey     = (min(src, tgt), max(src, tgt), rel)
                if ekey in seen_edges:
                    continue
                seen_edges.add(ekey)
                edges.append({
                    "source":     src,
                    "target":     tgt,
                    "type":       rel,
                    "properties": dict(record["props"]) if record["props"] else {},
                })

        # BUG 2: filter out Sector nodes — sectors are encoded as stock border colors on the frontend
        nodes = [n for n in nodes if n["type"] != "Sector"]
        shown_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in shown_ids and e["target"] in shown_ids]

        return jsonify({"nodes": nodes, "edges": edges, "source": "neo4j"})

    except Exception as e:
        driver.close()
        return jsonify(_build_graph_from_json(run_id) | {"neo4j_error": str(e)})
    finally:
        try:
            driver.close()
        except Exception:
            pass


@app.route("/api/rounds/<run_id>")
def get_rounds(run_id):
    """Return round data from Trading Vault markdown files, or from sim JSON."""
    # Try Trading Vault round files first
    vault_dir = ORACLE_VAULT / "sims" / run_id
    rounds    = []

    if vault_dir.is_dir():
        for md_file in sorted(vault_dir.glob("round_*.md")):
            rnum_m = re.search(r"round_(\d+)", md_file.name)
            rnum   = int(rnum_m.group(1)) if rnum_m else 0
            try:
                parsed = _parse_round_md(md_file.read_text())
                parsed["round"] = rnum
                rounds.append(parsed)
            except Exception:
                rounds.append({"round": rnum, "injection": "", "posts": [], "probabilities": {}})

    # Fall back to rounds_data in consolidated JSON
    if not rounds:
        json_file = SIMS_DIR / f"{run_id}.json"
        if json_file.exists():
            data = json.loads(json_file.read_text())
            for rd in data.get("rounds_data", []):
                rounds.append({
                    "round":         rd.get("round", 0),
                    "injection":     rd.get("injection", ""),
                    "posts":         [
                        {
                            "agent":      p.get("agent", ""),
                            "conviction": int(float(p.get("conviction", 0)) * 100),
                            "stances":    p.get("stances", {}),
                            "post":       p.get("post", ""),
                        }
                        for p in rd.get("posts", [])
                    ],
                    "probabilities": rd.get("market_probs", {}),
                })

    return jsonify(sorted(rounds, key=lambda x: x["round"]))


@app.route("/api/analyze_and_run", methods=["POST"])
def analyze_and_run():
    """
    ORACLE V2: One-click analyze + simulate.
    1. Generates Claude fundamental analysis for the ticker
    2. Formats it as a simulation seed/report
    3. Starts the simulation with that seed as context

    Request body: {"ticker": "SNOW", "rounds": 6}
    Returns: {"run_id": "...", "status": "started", "ticker": "..."}
    """
    body    = request.get_json(force=True) or {}
    ticker  = body.get("ticker", "").upper().strip()
    rounds  = int(body.get("rounds", 6))

    if not ticker:
        return jsonify({"error": "ticker required"}), 400

    if not re.match(r'^[A-Z]{1,5}$', ticker):
        return jsonify({"error": f"Invalid ticker format: {ticker}"}), 400

    today   = datetime.date.today().strftime("%Y%m%d")
    run_id  = f"v2_{today}_{ticker}"

    _sim_queues[run_id]  = queue.Queue()
    _sim_replays[run_id] = []
    save_web_state("sim_run_id", run_id)

    def cb(event):
        q = _sim_queues.get(run_id)
        if q:
            q.put(event)
        if run_id in _sim_replays:
            _sim_replays[run_id].append(event)

    def run_v2_pipeline():
        try:
            # ── PHASE 1: Claude Fundamental Analysis ──────────────────
            cb({"type": "sim_log", "msg": f"⟳ Generating fundamental analysis for {ticker}...", "level": "active"})

            analysis_text = _generate_claude_analysis(ticker, cb)

            if not analysis_text:
                cb({"type": "sim_log", "msg": f"✗ Analysis generation failed for {ticker}", "level": "error"})
                cb({"type": "sim_complete", "error": "Analysis failed"})
                return

            cb({"type": "sim_log", "msg": f"✓ Fundamental analysis complete ({len(analysis_text)} chars)", "level": "success"})

            # ── PHASE 2: Format as seed/report ────────────────────────
            cb({"type": "sim_log", "msg": "⟳ Formatting analysis as simulation seed...", "level": "active"})

            report_path = _format_as_seed_report(ticker, analysis_text, run_id)

            cb({"type": "sim_log", "msg": f"✓ Seed ready: {report_path}", "level": "success"})

            # ── PHASE 3: Load fundamentals and run simulation ─────────
            cb({"type": "sim_log", "msg": f"⟳ Fetching live market data for {ticker}...", "level": "active"})

            time.sleep(2)  # Give browser time to connect to SSE

            _run_sim_thread(run_id, [ticker], rounds, False, report_path)

        except Exception as e:
            import traceback
            cb({"type": "sim_log", "msg": f"✗ Pipeline error: {e}", "level": "error"})
            cb({"type": "sim_log", "msg": traceback.format_exc()[:300], "level": "error"})

    t = threading.Thread(target=run_v2_pipeline, daemon=True)
    t.start()

    return jsonify({"run_id": run_id, "status": "started", "ticker": ticker})


def _generate_claude_analysis(ticker: str, cb) -> str:
    """
    Generate a comprehensive fundamental analysis for the ticker using Claude.
    Uses oracle_factsheet.py data pipeline — NOT raw yfinance.
    The factsheet pipeline has all the fixes: commodity anchor, AISC extractor,
    6-K handler for foreign filers, material events, permit deadlines etc.
    """
    import requests as _req

    # ── Use the existing fixed factsheet pipeline ──────────────────────────────
    # This avoids ALL the known yfinance/EDGAR problems:
    # - Stale gold prices ($2,700 vs actual $4,553)
    # - Wrong AISC for miners
    # - Missing 6-K events for foreign filers (BTG, AEM)
    # - Missing material events (Goose Mine fire, CEO transitions)
    # - Forward EPS not calibrated to current commodity price
    market_data = ""
    sector      = ""
    industry    = ""
    val_mode_override = None

    try:
        sys.path.insert(0, os.path.expanduser("~/ORACLE"))
        sys.path.insert(0, os.path.expanduser("~/ORACLE/engine"))
        sys.path.insert(0, os.path.expanduser("~/ORACLE/data"))

        # Use format_fundamentals_batch — the fixed data layer
        from data.oracle_data import format_fundamentals_batch, get_fundamentals
        cb({"type": "sim_log", "msg": f"  Using ORACLE data pipeline for {ticker}...", "level": "active"})

        market_data = format_fundamentals_batch([ticker], fresh=True)

        # Also get structured fundamentals for valuation mode detection
        fund = get_fundamentals(ticker, fresh=True)
        sector   = fund.get("sector", "") or ""
        industry = fund.get("industry", "") or ""

        # Check ticker_names.json for explicit valuation_mode override
        names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
        if names_path.exists():
            known = json.loads(names_path.read_text())
            entry = known.get(ticker.upper(), {})
            if isinstance(entry, dict):
                val_mode_override = entry.get("valuation_mode")
                if not sector:
                    sector = entry.get("sector", "")

        cb({"type": "sim_log", "msg": f"  ORACLE data loaded for {ticker} ({len(market_data)} chars)", "level": "active"})

    except Exception as e:
        cb({"type": "sim_log", "msg": f"  Factsheet pipeline failed ({e}), falling back to yfinance", "level": "active"})
        # Fallback: basic yfinance — better than nothing but known to have issues
        try:
            import yfinance as yf
            info    = yf.Ticker(ticker).info or {}
            sector  = info.get("sector", "")
            industry = info.get("industry", "")
            price   = info.get("currentPrice") or info.get("regularMarketPrice", 0)
            high52  = info.get("fiftyTwoWeekHigh", 0)
            mktcap  = info.get("marketCap", 0)
            rev_ttm = info.get("totalRevenue", 0)
            market_data = (
                f"MARKET DATA (yfinance fallback — verify against SEC filings):\n"
                f"- {ticker}: ${price:,.2f} | 52wk high ${high52:,.2f} | "
                f"Cap ${mktcap/1e9:.1f}B | Rev ${rev_ttm/1e9:.2f}B\n"
                f"- Sector: {sector} | Industry: {industry}\n"
                f"NOTE: This is fallback data. Verify all figures against SEC EDGAR before trading.\n"
            )
        except Exception as e2:
            market_data = f"Data fetch failed: {e} / {e2}. Proceed with caution.\n"

    cb({"type": "sim_log", "msg": f"  Data ready for {ticker}", "level": "active"})

    sector_lower   = sector.lower() if sector else ""
    industry_lower = industry.lower() if industry else ""

    # ticker_names.json override takes priority
    if val_mode_override:
        mode_map = {
            "platform_compounder":        ("PLATFORM COMPOUNDER",        "DO NOT use EPV — it assumes zero growth and is wrong for platforms. Primary metrics: NRR, RPO, ARPU trajectory, platform asset growth, Rule of 40. Margin of safety = 20% discount to DCF fair value, not EPV."),
            "commodity_producer":         ("COMMODITY PRODUCER",         "Use NAV as primary valuation anchor, not EPV. All margin calculations use CURRENT commodity spot price shown in the data. Report AISC vs current commodity price and P/NAV ratio."),
            "inflection_stage":           ("INFLECTION STAGE",           "Use probability-weighted scenario analysis. Identify specific catalyst (FDA date, Phase 3 readout) and timeline. State your p_success estimate explicitly."),
            "defense_government_services":("DEFENSE / GOVERNMENT SERVICES","Primary metrics: backlog coverage ratio, book-to-burn, contract duration. Regulatory risk is structural and priced — focus on contract pipeline."),
            "cyclical_recovery":          ("CYCLICAL RECOVERY",          "Use normalized mid-cycle earnings, not trough earnings. Identify cycle position. Balance sheet must survive trough."),
            "mature_stalwart":            ("MATURE STALWART",            "Use EPV, earnings yield vs T-bill rate, Klarman margin of safety. Require 30%+ discount to intrinsic value."),
        }
        val_mode, val_guidance = mode_map.get(val_mode_override, ("MATURE STALWART", "Apply appropriate framework."))
    elif any(w in industry_lower for w in ["gold", "silver", "copper", "mining", "oil", "gas", "petroleum"]):
        val_mode = "COMMODITY PRODUCER"
        val_guidance = (
            "Use NAV as primary valuation anchor, not EPV. "
            "All margin calculations use current commodity spot price. "
            "Report AISC vs current commodity price and P/NAV ratio."
        )
    elif any(w in industry_lower for w in ["software", "internet", "fintech", "exchange", "marketplace", "saas"]):
        val_mode = "PLATFORM COMPOUNDER"
        val_guidance = (
            "DO NOT use EPV — it assumes zero growth and is wrong for platforms. "
            "Primary metrics: NRR, RPO, ARPU trajectory, platform asset growth, Rule of 40. "
            "Margin of safety = 20% discount to DCF fair value, not EPV."
        )
    elif any(w in industry_lower for w in ["biotechnology", "pharmaceutical", "drug", "clinical"]):
        val_mode = "INFLECTION STAGE"
        val_guidance = (
            "Use probability-weighted scenario analysis. "
            "Identify specific catalyst (FDA date, Phase 3 readout) and timeline. "
            "State your p_success estimate explicitly."
        )
    elif any(w in industry_lower for w in ["defense", "government", "aerospace"]):
        val_mode = "DEFENSE / GOVERNMENT SERVICES"
        val_guidance = (
            "Primary metrics: backlog coverage ratio, book-to-burn, contract duration. "
            "Regulatory risk is structural and priced — focus on contract pipeline."
        )
    elif any(w in industry_lower for w in ["semiconductor", "memory", "steel", "chemical"]):
        val_mode = "CYCLICAL RECOVERY"
        val_guidance = (
            "Use normalized mid-cycle earnings, not trough earnings. "
            "Identify cycle position. Balance sheet must survive trough."
        )
    else:
        val_mode = "MATURE STALWART"
        val_guidance = (
            "Use EPV, earnings yield vs T-bill rate, Klarman margin of safety. "
            "Require 30%+ discount to intrinsic value."
        )

    system_prompt = f"""You are a senior portfolio manager producing a comprehensive fundamental analysis memo.
Your style is modeled after David Einhorn (Greenlight Capital) and Bill Ackman (Pershing Square) —
precise, analytical, deeply researched, with specific numbers and genuine conviction.

VALUATION MODE: {val_mode}
{val_guidance}

You will produce a full fundamental analysis with these sections:

1. COMPANY CONFIRMED — identity, exchange, what the business actually does
2. VERIFIED DATA BLOCK — all key financial metrics with specific numbers
3. WHAT THIS COMPANY ACTUALLY IS — business model explanation in plain English
4. THE CORE THESIS — bull case built from data
5. KEY RISKS — bear case, specific and quantified where possible
6. VALUATION — apply the correct framework for {val_mode}
7. VERDICT — BUY/WATCH/PASS with conviction 1-10 and position sizing

Be specific. Use numbers. Every claim must be supported by data.
State your VALUATION MODE at the top.
Do not hedge. Take a position and defend it."""

    user_prompt = f"""Produce a comprehensive fundamental analysis for {ticker}.

{market_data}

Apply the {val_mode} analytical framework.

Write the complete analysis now. Include all 7 sections.
Be specific with numbers. No hedging. Take a position."""

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        try:
            raw = open(os.path.expanduser("~/.hermes/.env")).read()
            for line in raw.splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass

    try:
        resp = _req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://oracle.local",
                "X-Title": "ORACLE V2 Analysis",
            },
            json={
                "model": "anthropic/claude-sonnet-4-6",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 4000,
            },
            timeout=120,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        else:
            cb({"type": "sim_log", "msg": f"  API error {resp.status_code}: {resp.text[:200]}", "level": "error"})
            return ""
    except Exception as e:
        cb({"type": "sim_log", "msg": f"  Claude API call failed: {e}", "level": "error"})
        return ""


def _format_as_seed_report(ticker: str, analysis_text: str, run_id: str) -> str:
    """
    Format Claude's analysis as a markdown report file that the sim uses as context.
    Saves to ~/ORACLE/reports/ and returns the path.
    """
    today = datetime.date.today().strftime("%Y%m%d")
    reports_dir = Path.home() / "ORACLE" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    report_content = f"""# ORACLE V2 FUNDAMENTAL ANALYSIS: {ticker}
## Generated by Claude | Run ID: {run_id} | Date: {today}

---

## AGENT INSTRUCTIONS

This analysis was produced by Claude's fundamental research engine.
You are simulation agents. Your job is to:
1. Read Claude's analysis below carefully
2. Verify key claims against EDGAR and public data where possible
3. Challenge any claims you find contradicting evidence for
4. Form your own conviction and trade the prediction market accordingly

The prediction market reflects collective verified consensus.
Evidence you find beats unsupported claims.
EDGAR data beats analyst estimates.

---

{analysis_text}

---

## VERIFICATION TARGETS FOR AGENTS

Search EDGAR for {ticker}:
- Latest 10-K/10-Q: https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt=2025-01-01
- Form 4 (insider transactions): https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=4&count=10
- 8-K filings (monthly data): https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=8-K&count=5

If Claude stated specific revenue, EPS, or growth figures — verify them.
If Claude stated competitive claims — search for counter-evidence.
Post your findings and adjust your conviction accordingly.
"""

    report_path = reports_dir / f"ORACLE_{ticker}_{today}_v2_composite.md"
    report_path.write_text(report_content)

    seeds_dir = Path.home() / "ORACLE" / "mirofish_seeds"
    seeds_dir.mkdir(parents=True, exist_ok=True)
    seed_path = seeds_dir / f"{ticker}_{today}_seed.md"
    seed_path.write_text(report_content)

    return str(report_path)


@app.route("/api/run", methods=["POST"])
def run_sim():
    body      = request.get_json(force=True) or {}
    stocks    = [s.upper() for s in body.get("stocks", [])]
    rounds    = int(body.get("rounds", 8))
    fast      = bool(body.get("fast", False))
    report    = body.get("report_path", "")
    seed_path = body.get("seed_path", "")

    if not stocks:
        return jsonify({"error": "stocks required"}), 400

    # Auto-find latest TT report if none provided (e.g. re-run button)
    if not report:
        reports_dir = Path.home() / "ORACLE" / "reports"
        candidates = sorted(reports_dir.glob("*_composite.md"),
                           key=lambda x: x.stat().st_mtime, reverse=True)
        if candidates:
            report = str(candidates[0])

    # Merge seed into report context so sim agents see Debate Fodder in Round 1
    if seed_path and os.path.exists(seed_path):
        try:
            seed_text = Path(seed_path).read_text(encoding="utf-8", errors="ignore")
            # Prepend seed before TT report — agents see opening positions first
            separator = "\n\n" + "=" * 60 + "\nORACLE SIMULATION SEED (Pre-seeded agent positions):\n" + "=" * 60 + "\n"
            report = separator + seed_text + "\n\n" + "=" * 60 + "\nTHINK TANK REPORT:\n" + "=" * 60 + "\n" + (Path(report).read_text(encoding="utf-8", errors="ignore") if report and os.path.exists(report) else report)
        except Exception as _merge_err:
            pass  # fall through — sim runs with TT report only

    today  = datetime.date.today().strftime("%Y%m%d")
    abbrev = "_".join(s[:4] for s in stocks[:3])
    run_id = f"sim_{today}_{abbrev}"

    # BUG 7: validate stocks before starting thread — advisory only, never blocks
    warnings = validate_stocks_basic(stocks)

    _sim_queues[run_id]  = queue.Queue()
    _sim_replays[run_id] = []   # replay buffer for late-connecting browsers
    save_web_state("sim_run_id", run_id)   # persist so browser can reconnect after refresh

    def delayed_start():
        # Give browser 2 seconds to connect to the SSE stream before sim starts emitting
        time.sleep(2)
        _run_sim_thread(run_id, stocks, rounds, fast, report)

    t = threading.Thread(target=delayed_start, daemon=True)
    t.start()

    return jsonify({"run_id": run_id, "status": "started", "warnings": warnings})


@app.route("/api/run/<run_id>/stream")
def stream_sim(run_id):
    def generate():
        # First replay all events already emitted (handles page refresh / late connect)
        replay = _sim_replays.get(run_id, [])
        for event in replay:
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "sim_complete":
                return  # sim already done, no need to wait

        # Then stream new events from the live queue
        q = _sim_queues.get(run_id)
        if not q:
            yield 'data: {"type":"error","msg":"no queue"}\n\n'
            return
        while True:
            try:
                event = q.get(timeout=60)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "sim_complete":
                    break
            except queue.Empty:
                yield 'data: {"type":"heartbeat"}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/run/<run_id>/stop", methods=["POST"])
def run_stop(run_id):
    ev = _sim_stop_events.get(run_id)
    if ev:
        ev.set()
    return jsonify({"status": "stopping"})


@app.route("/api/run/<run_id>/pause", methods=["POST"])
def run_pause(run_id):
    ev = _sim_pause_events.get(run_id)
    if ev:
        ev.set()
    return jsonify({"status": "paused"})


@app.route("/api/run/<run_id>/resume", methods=["POST"])
def run_resume(run_id):
    ev = _sim_pause_events.get(run_id)
    if ev:
        ev.clear()
    return jsonify({"status": "resumed"})


@app.route("/api/run/<run_id>/status")
def run_status(run_id):
    proc = running_procs.get(run_id)
    if proc is None:
        # Check if result JSON exists
        json_file = SIMS_DIR / f"{run_id}.json"
        if json_file.exists():
            return jsonify({"run_id": run_id, "status": "complete"})
        return jsonify({"run_id": run_id, "status": "unknown"})

    poll = proc.poll()
    if poll is None:
        return jsonify({"run_id": run_id, "status": "running", "pid": proc.pid})

    running_procs.pop(run_id, None)
    json_file = SIMS_DIR / f"{run_id}.json"
    status = "complete" if json_file.exists() else "failed"
    return jsonify({"run_id": run_id, "status": status, "returncode": poll})


@app.route("/api/neo4j/query", methods=["GET", "POST"])
def neo4j_query():
    body  = request.get_json(force=True) or {}
    query = body.get("query", "") or request.args.get("query", "")
    if not query:
        return jsonify({"error": "query required"}), 400

    driver = _get_neo4j_driver()
    if driver is None:
        return jsonify({"error": "Neo4j unavailable"}), 503

    try:
        with driver.session() as s:
            results = s.run(query).data()
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        driver.close()


@app.route("/api/reports")
def list_reports():
    """List available Think Tank report .md files from Trading Vault."""
    import re as _re
    reports = []
    search_dirs = [
        ORACLE_VAULT,
        ORACLE_VAULT / "runs",
        ORACLE_VAULT / "_runs",
    ]
    for d in search_dirs:
        if d.is_dir():
            for f in sorted(d.glob("**/*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
                # Only composite Think Tank reports — skip layer notes, catalyst files, etc.
                if not (f.name.startswith("ORACLE_") and f.name.endswith("_composite.md")):
                    continue
                parts = f.stem.split("_")  # e.g. ['ORACLE','BBIO','INSM','ZETA','20260511','composite']
                try:
                    date_str = parts[-2]  # YYYYMMDD
                    from datetime import datetime
                    label_date = datetime.strptime(date_str, "%Y%m%d").strftime("%b %-d %Y")

                    # Parse tickers from INSIDE the file (### TICKER: X) — authoritative, handles 6+ stocks
                    try:
                        content = f.read_text(encoding="utf-8", errors="ignore")
                        tickers = _re.findall(r"###\s+TICKER:\s+([A-Z]{1,6})", content)
                        tickers = list(dict.fromkeys(tickers))  # dedupe, preserve order
                    except Exception:
                        tickers = []

                    # Fallback to filename if file parse gave nothing
                    if not tickers:
                        tickers = parts[1:-2]

                    label = " · ".join(tickers) + " — " + label_date
                except Exception:
                    label = f.name
                    tickers = []
                reports.append({"name": label, "path": str(f), "tickers": tickers})
    return jsonify(reports[:50])


@app.route("/api/report_content")
def get_report_content():
    """Return raw text of a report file for display in the UI."""
    path = request.args.get("path", "")
    if not path:
        return jsonify({"error": "path required"}), 400
    try:
        p = Path(path).expanduser()
        # Safety: must be under ORACLE_VAULT or ~/ORACLE/reports — do NOT resolve symlinks
        allowed = [
            str(Path.home() / "Documents" / "Trading Vault"),
            str(Path.home() / "ORACLE"),
        ]
        if not any(str(p).startswith(a) for a in allowed):
            return jsonify({"error": "path not allowed"}), 403
        if not p.exists():
            return jsonify({"error": "file not found"}), 404
        content = p.read_text(encoding="utf-8", errors="ignore")
        return jsonify({"content": content, "path": str(p), "size": len(content)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Final Reports ─────────────────────────────────────────────────────────────

_FINAL_REPORTS_DIR = Path.home() / "ORACLE" / "reports" / "final"
_ENGINE_DIR_FINAL  = str(Path.home() / "ORACLE" / "engine")

@app.route("/api/final_reports")
def list_final_reports_api():
    try:
        if _ENGINE_DIR_FINAL not in sys.path:
            sys.path.insert(0, _ENGINE_DIR_FINAL)
        from oracle_final_report import list_final_reports as _list
        return jsonify(_list())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/final_report/generate", methods=["POST"])
def generate_final_report_api():
    body        = request.get_json(force=True) or {}
    sim_path    = body.get("sim_path", "")
    report_path = body.get("report_path", "")

    if not sim_path:
        sims = sorted((Path.home() / "ORACLE" / "sims").glob("sim_*.json"),
                      key=lambda x: x.stat().st_mtime, reverse=True)
        if not sims:
            return jsonify({"error": "no sims found"}), 404
        sim_path = str(sims[0])

    if not report_path:
        rpts = sorted((Path.home() / "ORACLE" / "reports").glob("*_composite.md"),
                      key=lambda x: x.stat().st_mtime, reverse=True)
        if rpts:
            report_path = str(rpts[0])

    try:
        if _ENGINE_DIR_FINAL not in sys.path:
            sys.path.insert(0, _ENGINE_DIR_FINAL)
        from oracle_final_report import generate_report as _gen, save_report as _save
        content     = _gen(sim_path, report_path)
        run_id      = Path(sim_path).stem
        output_path = _save(content, run_id)
        return jsonify({"path": output_path, "size": len(content), "preview": content[:500]})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/api/final_report/content")
def get_final_report_content_api():
    path = request.args.get("path", "")
    if not path:
        return jsonify({"error": "path required"}), 400
    try:
        # expanduser but do NOT resolve — preserves symlink paths
        p = Path(path).expanduser()
        # normalize without following symlinks
        p_str = str(p)
        allowed_roots = [
            str(Path.home() / "ORACLE"),
            str(Path.home() / "Documents"),
        ]
        if not any(p_str.startswith(a) for a in allowed_roots):
            return jsonify({"error": "path not allowed"}), 403
        if not p.exists():
            return jsonify({"error": "not found"}), 404
        return jsonify({"content": p.read_text(encoding="utf-8", errors="ignore"), "path": str(p)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/speak", methods=["POST"])
def speak():
    """Generate speech via Piper TTS and return WAV audio."""
    import io, subprocess, tempfile, shutil
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "no text"}), 400

    voice_dir  = Path(os.path.expanduser("~/ORACLE/voice"))
    model      = voice_dir / "en_US-lessac-high.onnx"
    if not model.exists():
        return jsonify({"error": "voice model not found"}), 503

    try:
        # Piper: echo text | piper --model MODEL --output_raw | aplay
        # We return raw WAV so browser can play it directly
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        proc = subprocess.run(
            ["python3", "-m", "piper",
             "--model", str(model),
             "--output_file", tmp_path,
             "--length_scale", "0.85"],   # 0.85 = ~18% faster than default
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=15
        )
        if proc.returncode != 0:
            return jsonify({"error": proc.stderr.decode()}), 500

        return send_file(tmp_path, mimetype="audio/wav",
                         as_attachment=False,
                         download_name="oracle_voice.wav")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Screener ──────────────────────────────────────────────────────────────────

def _run_screen_thread(job_id, refresh=False, portfolio=""):
    q = _screen_queues[job_id]
    def emit(msg, kind="log"):
        event = {"type": kind, "msg": msg}
        q.put(event)
        _screen_results[job_id].setdefault("log_lines", []).append(msg)
        if job_id in _screen_replays:
            _screen_replays[job_id].append(event)

    try:
        from oracle_runner_screener import (
            sync_latest_csv, parse_fidelity_csv, parse_fidelity_csv_filtered,
            fetch_all_fundamentals,
            run_screen, haiku_triage, CSV_PATH, CACHE_PATH, DESTINATION_HOLDS
        )
        import os

        emit("Syncing latest Fidelity CSV...")
        _, csv_updated = sync_latest_csv()

        if csv_updated or refresh:
            if os.path.exists(CACHE_PATH):
                os.remove(CACHE_PATH)
            emit("Cache cleared — fetching fresh data.")

        if portfolio:
            emit(f"Filtering to portfolio: {portfolio}")
            holdings = parse_fidelity_csv_filtered(CSV_PATH, portfolio)
        else:
            emit(f"Parsing CSV: {CSV_PATH}")
            holdings = parse_fidelity_csv(CSV_PATH)
        if not holdings:
            emit("ERROR: No holdings loaded from CSV.", "error")
            _screen_results[job_id]["status"] = "error"
            q.put({"type": "done", "error": "No holdings"})
            return
        emit(f"Loaded {len(holdings)} symbols.")

        symbols = [s for s in holdings.keys() if s not in DESTINATION_HOLDS]
        emit(f"Fetching live data for {len(symbols)} symbols (this takes ~30s)...")
        live_data = fetch_all_fundamentals(symbols)
        emit(f"Live data fetched for {len(live_data)} symbols.")

        emit("Scoring and ranking...")
        results = run_screen(holdings, live_data, top_n=15)
        emit(f"Found {len(results)} runner candidates.")

        if not results:
            emit("No candidates found. Try with Refresh.", "warn")
            _screen_results[job_id]["status"] = "done"
            _screen_results[job_id]["results"] = []
            _screen_results[job_id]["triage"] = []
            q.put({"type": "done", "results": [], "triage": []})
            return

        emit("Running Haiku triage to pick best 6...")
        triage = haiku_triage(results[:15], live_data)
        emit(f"Triage complete: {', '.join(triage)}")

        # Generate seed from triage results — feeds Debate Fodder into sim Round 1
        seed_path = ""
        try:
            from oracle_runner_screener import generate_seed_and_prompt, save_outputs, OR_KEY
            if OR_KEY:
                sym_to_result = {r["symbol"]: r for r in results}
                top_results   = [sym_to_result[s] for s in triage if s in sym_to_result]
                if top_results:
                    emit(f"Generating simulation seed for {[r['symbol'] for r in top_results]}...")
                    seed, prompt = generate_seed_and_prompt(top_results, top_n=min(5, len(top_results)))
                    seed_path, _ = save_outputs(seed, prompt)
                    emit(f"Seed ready: {os.path.basename(seed_path)}", "ok")
        except Exception as _se:
            emit(f"Seed generation skipped: {_se}", "warn")

        # Build structured results for the table
        # NOTE: run_screen() returns keys: rev_growth, analyst_up, price, market_cap_b
        # dip_pct and eps_status must be computed from live_data
        table = []
        for r in results:
            sym   = r.get("symbol", "")
            live  = live_data.get(sym, {})
            price = r.get("price", 0) or live.get("price", 0) or 0
            high  = live.get("52wk_high", 0) or 0
            dip   = ((high - price) / high * 100) if high > 0 else 0
            fwd   = live.get("forward_eps") or 0
            trail = live.get("trailing_eps") or 0
            if trail < 0 and fwd > 0:
                eps_label = "turning ✓"
            elif fwd > 0 and trail > 0:
                eps_label = "profitable"
            elif fwd > 0:
                eps_label = "fwd+"
            else:
                eps_label = "negative"
            table.append({
                "symbol":         sym,
                "score":          r.get("score", 0),
                "sector":         r.get("sector", "") or live.get("sector", ""),
                "eps_status":     eps_label,
                "rev_growth":     r.get("rev_growth", 0),
                "dip_pct":        round(-dip, 1),
                "analyst_upside": r.get("analyst_up", 0),
                "in_triage":      sym in triage,
            })

        _screen_results[job_id]["status"]    = "done"
        _screen_results[job_id]["results"]   = table
        _screen_results[job_id]["triage"]    = triage
        _screen_results[job_id]["seed_path"] = seed_path
        q.put({"type": "done", "results": table, "triage": triage, "seed_path": seed_path})

    except Exception as e:
        import traceback
        err = traceback.format_exc()
        emit(f"ERROR: {e}", "error")
        _screen_results[job_id]["status"] = "error"
        q.put({"type": "done", "error": str(e)})


@app.route("/api/screen/portfolios", methods=["GET"])
def list_portfolios():
    """Return all account names from the current portfolio CSV."""
    try:
        from oracle_runner_screener import get_portfolio_accounts, CSV_PATH
        accounts = get_portfolio_accounts(CSV_PATH)
        return jsonify({"portfolios": accounts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/screen", methods=["POST"])
def start_screen():
    body      = request.get_json(force=True) or {}
    refresh   = bool(body.get("refresh", False))
    portfolio = body.get("portfolio", "")   # optional account name filter
    job_id  = f"screen_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _screen_queues[job_id]  = queue.Queue()
    _screen_results[job_id] = {"status": "running", "log_lines": [], "results": [], "triage": []}
    _screen_replays[job_id] = []
    t = threading.Thread(target=_run_screen_thread, args=(job_id, refresh, portfolio), daemon=True)
    t.start()
    save_web_state("screen_job_id", job_id)
    return jsonify({"job_id": job_id, "portfolio": portfolio or "ALL"})


@app.route("/api/screen/upload_csv", methods=["POST"])
def upload_csv():
    """Accept a Fidelity portfolio CSV upload and save to ~/portfolio.csv"""
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if not f.filename.endswith(".csv"):
        return jsonify({"error": "must be a .csv file"}), 400
    dest = Path.home() / "portfolio.csv"
    f.save(str(dest))
    # Also copy to ORACLE portfolio_csv folder for history
    csv_folder = Path.home() / "ORACLE" / "portfolio_csv"
    csv_folder.mkdir(parents=True, exist_ok=True)
    import shutil, datetime
    dated = csv_folder / f"portfolio_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    shutil.copy2(str(dest), str(dated))
    return jsonify({"status": "ok", "path": str(dest), "rows": sum(1 for _ in open(str(dest)))-1})


@app.route("/api/screen/<job_id>/stream")
def stream_screen(job_id):
    if job_id not in _screen_queues and job_id not in _screen_results:
        return jsonify({"error": "job not found"}), 404

    def generate():
        # Replay all events already emitted (handles page refresh / late connect)
        replay = _screen_replays.get(job_id, [])
        for event in replay:
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "done":
                return  # already complete, no need to wait

        # Job still running — drain the queue
        q = _screen_queues.get(job_id)
        if q is None:
            return  # job finished before we connected, replay was enough
        while True:
            try:
                event = q.get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done":
                    break
            except Exception:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Think Tank ────────────────────────────────────────────────────────────────

def _run_thinktank_thread(job_id, tickers, fast=False):
    q = _tt_queues[job_id]

    def emit(msg, kind="log"):
        # Normalize: always send type="log" with a kind field so frontend handles it uniformly
        event = {"type": "log", "kind": kind, "msg": msg}
        q.put(event)
        _tt_results[job_id].setdefault("log_lines", []).append(msg)
        if job_id in _tt_replays:
            _tt_replays[job_id].append(event)

    try:
        import sys, os
        _ENGINE_DIR = os.path.expanduser("~/ORACLE/engine")
        if _ENGINE_DIR not in sys.path:
            sys.path.insert(0, _ENGINE_DIR)

        from oracle_think_tank import run_composite, save_output, get_fundamentals
        import datetime as _dt

        emit(f"Think Tank starting for: {', '.join(tickers)}")

        # ── Preflight data validation ─────────────────────────────────────────────
        emit("Running pre-flight data validation...")
        try:
            import sys as _sys
            _ENGINE_DIR2 = os.path.expanduser("~/ORACLE/engine")
            if _ENGINE_DIR2 not in _sys.path:
                _sys.path.insert(0, _ENGINE_DIR2)
            from oracle_preflight import run_preflight as _run_preflight, build_preflight_header as _build_pf_header
            pf_reports = _run_preflight(tickers, verbose=False)
            halted_tickers = [t for t, r in pf_reports.items() if r.halted]
            for t, r in pf_reports.items():
                for err in r.errors:
                    emit(f"  PRE-FLIGHT ERROR [{t}]: {err}", "error")
                for warn in r.warnings:
                    emit(f"  PRE-FLIGHT WARN [{t}]: {warn}")
            if halted_tickers:
                emit(f"PRE-FLIGHT HALT: {', '.join(halted_tickers)} — data quality too low to run panels.", "error")
                emit("Fix the errors above. Use --preflight-override to force through (not recommended).", "error")
                _tt_results[job_id]["status"] = "error"
                q.put({"type": "done", "error": f"Pre-flight halted: {', '.join(halted_tickers)}"})
                return
            emit("Pre-flight passed. Starting Think Tank panels...")
        except ImportError as _pf_e:
            emit(f"Pre-flight module not available ({_pf_e}) — proceeding without validation.")

        # ── Fact sheet validation (data quality gate) ─────────────────────────
        emit("Running fact sheet validation...")
        try:
            import sys as _sys_fv
            _sys_fv.path.insert(0, os.path.expanduser("~/ORACLE/engine"))
            from oracle_factsheet import build_fact_sheet, CACHE_DIR as _FS_CACHE_DIR
            import datetime as _dt_fv, pathlib as _pl_fv

            _fs_failures = []
            _fs_warnings = []

            for _sym in tickers:
                try:
                    # Clear stale cache to force fresh fetch
                    for _cf in _pl_fv.Path(_FS_CACHE_DIR).glob(f"factsheet_{_sym}_*.json"):
                        _cf.unlink(missing_ok=True)

                    _fs = build_fact_sheet(_sym)
                    _pr = _fs.get("press_release", {})
                    _legal = _fs.get("legal_proceedings", {})
                    _metrics = _fs.get("metrics", {})

                    # Check 1: 8-K date freshness
                    _filing_date = _pr.get("filing_date", "")
                    if _filing_date:
                        _fd = _dt_fv.date.fromisoformat(_filing_date)
                        _age = (_dt_fv.date.today() - _fd).days
                        if _age > 90:
                            _fs_failures.append(f"{_sym}: 8-K is {_age} days old (>90) — may be missing recent earnings")
                        elif _age > 45:
                            _fs_warnings.append(f"{_sym}: 8-K is {_age} days old — verify earnings are current")
                    elif not _pr.get("parse_success"):
                        _fs_warnings.append(f"{_sym}: No earnings 8-K found — panels will use XBRL data only")

                    # Check 2: Gross margin range (0-100%)
                    _gm = (_pr.get("gross_margin_gaap", {}) or {}).get("value")
                    if _gm is not None and (_gm > 1.0 or _gm < 0):
                        _fs_failures.append(f"{_sym}: GAAP gross margin {_gm*100:.1f}% is outside 0-100% — wrong field extracted. HALTING.")

                    # Check 3: Going concern — must not be inferred, only from auditor opinion text
                    if _legal.get("going_concern"):
                        # Verify it's from actual auditor language, not false positive
                        _lpt = _legal.get("legal_proceedings_text", "").lower()
                        _gc_confirmed = any(p in _lpt for p in [
                            "substantial doubt", "going concern", "ability to continue"
                        ])
                        if not _gc_confirmed:
                            # False positive — clear it
                            _legal["going_concern"] = False
                            _fs_warnings.append(f"{_sym}: Going concern flag cleared — not found in actual auditor text")
                        else:
                            _fs_warnings.append(f"{_sym}: Going concern warning CONFIRMED in filing — Skeptic must address")

                    # Check 4: XBRL revenue period freshness
                    _rev_period = (_metrics.get("revenue_ttm") or {}).get("period", "")
                    if _rev_period and len(_rev_period) >= 4:
                        try:
                            _rev_year = int(_rev_period[:4])
                            if _dt_fv.date.today().year - _rev_year > 2:
                                _fs_failures.append(f"{_sym}: XBRL revenue period {_rev_period} is >2 years old — stale data. HALTING.")
                        except (ValueError, TypeError):
                            pass

                    # Check 5: Revenue plausibility (P/S sanity)
                    _rev_q = (_pr.get("revenue_quarter") or {}).get("value")
                    _price = _fs.get("price") or 0
                    _shares = _fs.get("shares_outstanding") or 0
                    if _rev_q and _price and _shares:
                        _mktcap = _price * _shares
                        _ann_rev = _rev_q * 4
                        if _ann_rev > 0:
                            _ps = _mktcap / _ann_rev
                            if _ps > 500 or _ps < 0.01:
                                _fs_failures.append(f"{_sym}: P/S ratio {_ps:.0f}x is implausible — revenue figure wrong")

                except Exception as _fve:
                    _fs_warnings.append(f"{_sym}: Fact sheet validation error: {str(_fve)[:80]}")

            # Emit results
            for _fw in _fs_warnings:
                emit(f"  \u26a0 VALIDATION WARN: {_fw}")
            for _ff in _fs_failures:
                emit(f"  \u2717 VALIDATION FAIL: {_ff}", "error")

            if _fs_failures:
                emit(f"FACT SHEET VALIDATION FAILED: {len(_fs_failures)} check(s). Run aborted — fix data before proceeding.", "error")
                _tt_results[job_id]["status"] = "error"
                q.put({"type": "done", "error": f"Fact sheet validation failed: {'; '.join(_fs_failures[:2])}"})
                return
            else:
                emit(f"\u2713 Fact sheet validation passed. Proceeding to Think Tank panels.")

        except Exception as _fv_outer:
            emit(f"  Fact sheet validation skipped: {_fv_outer}", "")
            # Non-fatal — proceed even if validation module errors

        emit("Fetching fundamentals (from cache if available)...")

        fundamentals = get_fundamentals(tickers)
        emit(f"Fundamentals loaded ({len(fundamentals)} chars)")

        model = "anthropic/claude-3.5-haiku" if fast else "anthropic/claude-sonnet-4.5"
        emit(f"Running composite analysis — model: {'Haiku (fast)' if fast else 'Sonnet'}")
        emit("Layer 1: Scout (Fisher + Lynch + Li Lu + Thiel)...")

        import io
        import threading

        result_box = {}
        error_box  = {}

        def do_run():
            try:
                date = _dt.date.today().strftime("%Y%m%d")
                results = run_composite(
                    stocks=tickers,
                    fundamentals=fundamentals,
                    model=model,
                    screener_context="",
                    date=date,
                    mode="composite",
                )
                result_box["results"] = results
                result_box["date"]    = date
            except Exception as e:
                import traceback
                error_box["error"] = str(e)
                error_box["tb"]    = traceback.format_exc()

        # Capture stdout to get real [N/TOTAL] batch progress lines
        import io, re as _re

        original_stdout = sys.stdout

        class _StdoutCapture(io.TextIOBase):
            def __init__(self, original):
                self._orig = original
                self._buf  = ""
            def write(self, s):
                self._orig.write(s)
                self._orig.flush()
                self._buf += s
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    m = _re.search(r'\[(\d+)/(\d+)\]', line)
                    if m:
                        n, total = int(m.group(1)), int(m.group(2))
                        pct = min(97, int(n / total * 95))
                        label_m = _re.search(r'\]\s*(.+?)(?:\.\.\.)?$', line.strip())
                        label   = label_m.group(1).strip() if label_m else line.strip()
                        emit(f"[{pct}%] Batch {n}/{total} — {label}")
                return len(s)
            def flush(self):
                self._orig.flush()

        sys.stdout = _StdoutCapture(original_stdout)

        run_thread = threading.Thread(target=do_run, daemon=True)
        run_thread.start()
        run_thread.join()   # block — stdout capture handles all progress

        sys.stdout = original_stdout
        emit("[99%] Wrapping up...")

        if error_box:
            emit(f"Think Tank error: {error_box['error']}", "error")
            _tt_results[job_id]["status"] = "error"
            q.put({"type": "done", "error": error_box["error"]})
            return

        results = result_box["results"]
        date    = result_box["date"]

        emit("Saving report...")
        report_path = save_output(results, tickers, date, "composite")
        emit(f"Report saved: {os.path.basename(report_path)}", "ok")

        # Check report for truncation markers
        try:
            report_text = Path(report_path).read_text(encoding="utf-8", errors="ignore")
            trunc_count = report_text.count("TRUNCATED")
            if trunc_count > 0:
                emit(f"⚠ WARNING: {trunc_count} truncation(s) detected in report — some analysis may be cut off", "warn")
            else:
                emit("✓ No truncations detected — report looks complete", "ok")
            # Also emit word count as a quality indicator
            words = len(report_text.split())
            emit(f"Report: {words:,} words, {len(report_text):,} chars")
        except Exception:
            pass

        _tt_results[job_id]["status"]      = "done"
        _tt_results[job_id]["report_path"] = report_path
        _tt_results[job_id]["tickers"]     = tickers
        # Pass seed_path from screener state so sim auto-launch can load it
        screen_seed = ""
        try:
            screen_state = load_web_state()
            sid = screen_state.get("screen_job_id", "")
            screen_seed = _screen_results.get(sid, {}).get("seed_path", "")
        except Exception:
            pass
        q.put({"type": "done", "report_path": report_path, "tickers": tickers, "seed_path": screen_seed})

    except Exception as e:
        import traceback
        emit(f"Error: {e}", "error")
        emit(traceback.format_exc(), "error")
        _tt_results[job_id]["status"] = "error"
        q.put({"type": "done", "error": str(e)})


@app.route("/api/thinktank", methods=["POST"])
def start_thinktank():
    body    = request.get_json(force=True) or {}
    tickers = [t.upper() for t in body.get("tickers", [])]
    fast    = bool(body.get("fast", False))
    if not tickers:
        return jsonify({"error": "tickers required"}), 400
    job_id = f"tt_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _tt_queues[job_id]  = queue.Queue()
    _tt_results[job_id] = {"status": "running", "log_lines": [], "report_path": None, "tickers": tickers}
    _tt_replays[job_id] = []
    t = threading.Thread(target=_run_thinktank_thread, args=(job_id, tickers, fast), daemon=True)
    t.start()
    save_web_state("tt_job_id", job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/thinktank/<job_id>/stream")
def stream_thinktank(job_id):
    if job_id not in _tt_queues and job_id not in _tt_results:
        return jsonify({"error": "job not found"}), 404

    def generate():
        # Replay all events already emitted (handles page refresh / late connect)
        replay = _tt_replays.get(job_id, [])
        for event in replay:
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "done":
                return  # already complete, no need to wait

        # Job still running — drain the queue
        q = _tt_queues.get(job_id)
        if q is None:
            return  # job finished before we connected, replay was enough
        while True:
            try:
                event = q.get(timeout=60)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done":
                    break
            except Exception:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    SIMS_DIR.mkdir(parents=True, exist_ok=True)
    print("\n" + "=" * 55)
    print("  ORACLE Web Dashboard")
    print("  http://localhost:5050")
    print("=" * 55 + "\n")
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
