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

        results = run_simulation(
            run_id       = run_id,
            stocks       = stocks,
            fundamentals = fundamentals,
            report_text  = report_text,
            num_rounds   = rounds,
            model        = model,
            api_key      = api_key,
            event_callback = cb,
        )

        driver = get_driver() if results.get("driver_active") else None

        rankings = score_simulation(
            driver     = driver,
            run_id     = run_id,
            markets    = results["markets"],
            all_rounds = results["rounds"],
            stocks     = stocks,
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

    except Exception as e:
        cb({"type": "error", "msg": str(e)})
        cb({"type": "sim_complete", "rankings": [], "prob_history": {}})

    # Remove queue and replay buffer after 10 minutes
    threading.Timer(600, lambda: (_sim_queues.pop(run_id, None), _sim_replays.pop(run_id, None))).start()


# ── Routes ────────────────────────────────────────────────────────────────────

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


@app.route("/api/run", methods=["POST"])
def run_sim():
    body   = request.get_json(force=True) or {}
    stocks = [s.upper() for s in body.get("stocks", [])]
    rounds = int(body.get("rounds", 8))
    fast   = bool(body.get("fast", False))
    report = body.get("report_path", "")

    if not stocks:
        return jsonify({"error": "stocks required"}), 400

    today  = datetime.date.today().strftime("%Y%m%d")
    abbrev = "_".join(s[:4] for s in stocks[:3])
    run_id = f"sim_{today}_{abbrev}"

    # BUG 7: validate stocks before starting thread — advisory only, never blocks
    warnings = validate_stocks_basic(stocks)

    _sim_queues[run_id]  = queue.Queue()
    _sim_replays[run_id] = []   # replay buffer for late-connecting browsers

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


if __name__ == "__main__":
    SIMS_DIR.mkdir(parents=True, exist_ok=True)
    print("\n" + "=" * 55)
    print("  ORACLE Web Dashboard")
    print("  http://localhost:5050")
    print("=" * 55 + "\n")
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
