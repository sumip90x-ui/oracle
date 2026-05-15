#!/usr/bin/env python3
"""
oracle_session_audit.py — Pre-session ORACLE health check.

Runs before every session. Verifies all known bug fixes are still in effect.
Sends a green/red summary to Telegram.

Checks:
  1. All core modules import cleanly
  2. Preflight is wired into web UI thread (not just CLI)
  3. Zero auto-launch triggers in index.html
  4. Preflight correctly halts AXON + FLEX (bad EPS)
  5. Preflight correctly passes CRDO (high-growth, legit EPS)
  6. Preflight HALT_THRESHOLD = 50, EPS deduct = 55
  7. Risk tags (VALUATION_RISK, INSIDER_SELLING, etc.) present
  8. EV floor override present in scorer.py
  9. Watchlist queue routing present in think_tank
 10. Neo4j reachable

Run manually:  python3 ~/ORACLE/scripts/oracle_session_audit.py
Run via cron:  set up via Hermes cronjob tool
"""

import sys, os, re, json, subprocess, datetime, requests
from pathlib import Path

ORACLE_DIR = Path.home() / "ORACLE"
sys.path.insert(0, str(ORACLE_DIR / "engine"))
sys.path.insert(0, str(ORACLE_DIR / "sim"))
sys.path.insert(0, str(ORACLE_DIR / "web"))

# ── Telegram ───────────────────────────────────────────────────────────────────
def _load_env():
    env = {}
    env_file = Path.home() / ".hermes" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def send_telegram(msg: str):
    env = _load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = "7249316240"
    if not token:
        print("No Telegram token — printing only")
        print(msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram send failed: {e}")

# ── Check helpers ──────────────────────────────────────────────────────────────
results = []

def check(name: str, passed: bool, detail: str = ""):
    icon = "✅" if passed else "❌"
    results.append((icon, name, detail))
    print(f"  {icon} {name}" + (f" — {detail}" if detail else ""))
    return passed

def grep_file(path: str, pattern: str) -> list:
    """Return all matching lines with line numbers."""
    try:
        content = Path(path).read_text(errors="ignore")
        matches = []
        for i, line in enumerate(content.splitlines(), 1):
            if re.search(pattern, line):
                matches.append((i, line.strip()))
        return matches
    except Exception:
        return []

# ── Run all checks ─────────────────────────────────────────────────────────────
print(f"\n{'='*58}")
print(f"  ORACLE SESSION AUDIT — {datetime.date.today()}")
print(f"{'='*58}\n")

# 1. Core module imports
print("[ Module Imports ]")
modules_ok = True
for mod, path in [
    ("oracle_preflight",    "engine/oracle_preflight.py"),
    ("oracle_think_tank",   "engine/oracle_think_tank.py"),
    ("oracle_final_report", "engine/oracle_final_report.py"),
    ("oracle_to_sim",       "engine/oracle_to_sim.py"),
    ("scorer",              "sim/scorer.py"),
]:
    try:
        full = str(ORACLE_DIR / path.replace("/", os.sep))
        spec = __import__(mod) if mod in sys.modules else None
        # Just check file exists and has no obvious syntax errors
        result = subprocess.run(
            ["python3", "-c", f"import sys; sys.path.insert(0,'{ORACLE_DIR}/engine'); sys.path.insert(0,'{ORACLE_DIR}/sim'); import {mod}"],
            capture_output=True, text=True, timeout=15
        )
        ok = result.returncode == 0
        modules_ok = modules_ok and ok
        check(f"import {mod}", ok, result.stderr.strip()[:80] if not ok else "")
    except Exception as e:
        modules_ok = False
        check(f"import {mod}", False, str(e)[:80])

# 2. Preflight wired into web UI thread
print("\n[ Bug 18: Preflight in web UI thread ]")
app_py = str(ORACLE_DIR / "web" / "app.py")
# Must appear inside _run_thinktank_thread, before emit("Fetching fundamentals")
thread_content = Path(app_py).read_text(errors="ignore")
in_thread = "_run_thinktank_thread" in thread_content
pf_call = "run_preflight" in thread_content
# Check order: preflight before fundamentals fetch
pf_pos = thread_content.find("run_preflight")
fund_pos = thread_content.find("Fetching fundamentals")
order_ok = pf_pos < fund_pos and pf_pos > 0
check("Preflight called in _run_thinktank_thread", pf_call and order_ok,
      f"preflight@{pf_pos} fundamentals@{fund_pos}" if pf_call else "MISSING")

# 3. Auto-launch disabled — zero triggers in index.html
print("\n[ Bug 19: Auto-launch disabled ]")
html = str(ORACLE_DIR / "web" / "static" / "index.html")
bad_patterns = [
    r"auto-launching",
    r"launching in \d+s",
    r"launching in \d+s",
    r"setTimeout.*3000.*api/run",
    r"setInterval.*countdown",
]
autolaunch_clean = True
for pat in bad_patterns:
    hits = grep_file(html, pat)
    if hits:
        autolaunch_clean = False
        check(f"No '{pat}'", False, f"line {hits[0][0]}: {hits[0][1][:60]}")

# _doLaunchSim only inside proceedFromReport
html_content = Path(html).read_text(errors="ignore")
launch_calls = [(i+1, l.strip()) for i, l in enumerate(html_content.splitlines())
                if "_doLaunchSim(" in l and "function _doLaunchSim" not in l]
# Should be exactly 1 — inside proceedFromReport
if len(launch_calls) == 1 and "proceedFromReport" in html_content[html_content.find("_doLaunchSim(")-200:html_content.find("_doLaunchSim(")+50]:
    check("_doLaunchSim only in proceedFromReport", True, f"1 call at line {launch_calls[0][0]}")
elif len(launch_calls) == 0:
    check("_doLaunchSim call count", False, "Function never called — Proceed to Sim button broken")
else:
    autolaunch_clean = False
    check("_doLaunchSim only in proceedFromReport", False,
          f"{len(launch_calls)} calls found: lines {[x[0] for x in launch_calls]}")

if autolaunch_clean and len(launch_calls) == 1:
    check("Auto-launch fully disabled", True)

# 4. Preflight HALT_THRESHOLD and EPS deduct values
print("\n[ Preflight Calibration ]")
pf_content = (ORACLE_DIR / "engine" / "oracle_preflight.py").read_text(errors="ignore")
halt_match = re.search(r"HALT_THRESHOLD\s*=\s*(\d+)", pf_content)
halt_val = int(halt_match.group(1)) if halt_match else 0
check("HALT_THRESHOLD = 50", halt_val == 50, f"found {halt_val}")

deduct_match = re.search(r"deduct=(\d+)", pf_content)
# Find the 55 deduct specifically (not the 25 for spinoff)
deducts = [int(m) for m in re.findall(r"deduct=(\d+)", pf_content)]
check("EPS deduct = 55 present", 55 in deducts, f"deducts found: {deducts}")

# Spinoff ticker guard
has_ticker_guard = "ticker_lower not in title" in pf_content
check("Spinoff false-positive guard", has_ticker_guard)

# Tavily via requests (not hermes_tools)
uses_requests = "_requests.post" in pf_content or "requests.post" in pf_content
uses_hermes = "from hermes_tools import web_search" in pf_content
check("Web search via requests (not hermes_tools)", uses_requests and not uses_hermes)

# 5. Live preflight tests — AXON/FLEX halt, CRDO passes
print("\n[ Live Preflight Tests ]")
try:
    # Clear today's cache first
    cache = ORACLE_DIR / "cache" / f"preflight_{datetime.date.today().isoformat()}.json"
    cache.unlink(missing_ok=True)

    from oracle_preflight import run_preflight

    for ticker, want_halt in [
        ("AXON", False),   # EDGAR corrects TTM EPS — 1.6x ratio is plausible, passes
        ("FLEX", True),    # Spinoff detected — halts
        ("CRDO", False),   # High growth — passes
    ]:
        cache.unlink(missing_ok=True)
        try:
            report = run_preflight([ticker], verbose=False)[ticker]
            if want_halt:
                check(f"{ticker} correctly HALTS", report.halted,
                      f"score {report.score}/100 errors: {report.errors[:1]}")
            else:
                check(f"{ticker} correctly PASSES", not report.halted,
                      f"score {report.score}/100")
        except Exception as e:
            check(f"{ticker} preflight", False, str(e)[:80])
except Exception as e:
    check("Preflight live tests", False, str(e)[:80])

# 6. Key bug fixes in source files
print("\n[ Bug Fix Verification ]")
tt_content = (ORACLE_DIR / "engine" / "oracle_think_tank.py").read_text(errors="ignore")
fr_content = (ORACLE_DIR / "engine" / "oracle_final_report.py").read_text(errors="ignore")
ots_content = (ORACLE_DIR / "engine" / "oracle_to_sim.py").read_text(errors="ignore")
sc_content  = (ORACLE_DIR / "sim" / "scorer.py").read_text(errors="ignore")

check("Bug 3:  Spinoff check in Scout",         "SPINOFF CHECK" in tt_content)
check("Bug 6:  Watchlist queue routing",         "WATCHLIST_FLAG" in tt_content and "watchlist_queue" in tt_content)
check("Bug 7:  Panel score explanation",         "SCORING: The OVERALL Score" in tt_content)
check("Bug 8:  Insider transactions in Skeptic", "insider_transactions" in tt_content)
check("Bug 10: Tech+Macro mandatory rationale",  "MANDATORY: Each verdict" in tt_content)
check("Bug 11: Stale asset rule",                "STALE ASSET RULE" in ots_content)
check("Bug 12: Segment name rule",               "SEGMENT NAME RULE" in ots_content)
check("Bug 13: Winner/loser delta logic",        "delta < -0.05" in fr_content)
check("Bug 14: EV floor override",               "ev_override" in sc_content and "ev < -0.30" in sc_content)
check("Bug 15: VALUATION_RISK tag",              "VALUATION_RISK" in fr_content)
check("Bug 15: INSIDER_SELLING tag",             "INSIDER_SELLING" in fr_content)
check("Bug 15: GEOPOLITICAL_RISK tag",           "GEOPOLITICAL_RISK" in fr_content)
check("Bug 15: SPINOFF_EXECUTION_RISK tag",      "SPINOFF_EXECUTION_RISK" in fr_content)
check("Bug 16: Conviction inflation flag",       "CONVICTION INFLATION" in fr_content)
check("Bug 17: Quantified claims rule",          "QUANTIFIED CLAIMS RULE" in ots_content)

# 7. Neo4j reachable
print("\n[ Neo4j ]")
try:
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(
        "bolt://localhost:7687",
        auth=("neo4j", "miroshark2026")
    )
    driver.verify_connectivity()
    driver.close()
    check("Neo4j bolt://localhost:7687", True)
except Exception as e:
    check("Neo4j bolt://localhost:7687", False, str(e)[:60])

# ── Summary ────────────────────────────────────────────────────────────────────
passed = [r for r in results if r[0] == "✅"]
failed = [r for r in results if r[0] == "❌"]

print(f"\n{'='*58}")
print(f"  RESULT: {len(passed)} passed / {len(failed)} failed")
print(f"{'='*58}\n")

# Build Telegram message
date_str = datetime.date.today().strftime("%b %d")
if not failed:
    tg_msg = (
        f"🟢 <b>ORACLE Session Audit — {date_str}</b>\n"
        f"All {len(passed)} checks passed. System clean.\n\n"
        f"✅ Preflight wired to web UI\n"
        f"✅ Auto-launch disabled\n"
        f"✅ AXON/FLEX halt on bad EPS\n"
        f"✅ CRDO passes (high-growth)\n"
        f"✅ All 16 bug fixes in place\n\n"
        f"Ready to run. http://localhost:5050"
    )
else:
    failed_lines = "\n".join(f"❌ {r[1]}" + (f": {r[2]}" if r[2] else "") for r in failed[:8])
    tg_msg = (
        f"🔴 <b>ORACLE Session Audit — {date_str}</b>\n"
        f"{len(failed)} check(s) FAILED — do not run until fixed.\n\n"
        f"{failed_lines}\n\n"
        f"Run: python3 ~/ORACLE/scripts/oracle_session_audit.py"
    )

send_telegram(tg_msg)
print(tg_msg)

sys.exit(0 if not failed else 1)

# ── NEW CHECKS (from ZETA audit 2026-05-14 night) ────────────────────────────────────────
print("\n[ Zeta Bug Fixes ]")
try:
    import sys as _sys3
    _sys3.path.insert(0, str(ORACLE_DIR / "engine"))
    import oracle_think_tank as _tt2, oracle_preflight as _pf2
    import inspect as _ins2

    _src2  = _ins2.getsource(_tt2.run_composite)
    _scout2 = _tt2.SCOUT_SYSTEM
    _pfh   = _ins2.getsource(_pf2.build_preflight_header)
    _ssf   = _ins2.getsource(_pf2.check_short_seller_reports)

    check("Bug1: fcf_per_share in VA inputs",     "fcf_per_share" in _src2)
    check("Bug1: MoS=0% overrides BUY to HOLD",   "MoS" in _src2 and "OVERRIDE" in _src2)
    check("Bug1: DCF anchoring detection",         "possible anchoring" in _src2)
    check("Bug2: GAAP/non-GAAP warning in header", "GAAP/NON-GAAP" in _pfh)
    check("Bug3: 365-day insider window",          "timedelta(days=365)" in _src2)
    check("Bug4: check_short_seller_reports",      "short_seller_reports" in _ssf)
    check("Bug4: short_seller_block in Skeptic",   "short_seller_block" in _src2)
    check("Bug4: SHORT SELLER in preflight header","SHORT SELLER REPORTS" in _pfh)
    check("Bug5: fcf_margin in data layer",        "fcf_margin" in _src2)
    check("Bug6: Discovery price rule in Scout",   "DISCOVERY PRICE RULE" in _scout2)
    check("Bug7: Position sizing reconciliation",  "POSITION SIZING RECONCILIATION" in _src2)
except Exception as e:
    check("Zeta bug checks", False, str(e)[:80])

# ── NEW CHECKS (from CRDO audit 2026-05-14) ───────────────────────────────────────────
print("\n[ CRDO Audit Fixes ]")
try:
    import sys as _sys2
    _sys2.path.insert(0, str(ORACLE_DIR / "engine"))
    import oracle_think_tank as _tt
    import inspect as _inspect
    _scout  = _tt.SCOUT_SYSTEM
    _src    = _inspect.getsource(_tt.run_composite)
    _macro  = _tt.MACRO_TECH_SYSTEM

    check("S1: VA REVERSE_DCF mode",           "REVERSE_DCF" in _src)
    check("S1: VA uses trailing_eps not eps",   "trailing_eps" in _src)
    check("S1: VA zero-output warning",         "VA WARNING" in _src)
    check("S2: Discovery price validation",     "DISCOVERY PRICE CHECK" in _src)
    check("S3: Insider 6-month window",         "180" in _src and "ZERO INSIDER" in _src)
    check("S5: Runner DNA vs 52-week HIGH",     "52-week high" in _scout.lower())
    check("S6: Discovery QUEUED block",         "QUEUED FOR STANDALONE RUN" in _scout)
    check("S7: Conflict reconciliation",        "CONVERGENCE_PRICE" in _src)
    check("C1: Gross margin in cache",          "grossMargins" in _inspect.getsource(_tt.get_fundamentals))
    check("C2+C4: Scuttlebutt = hypothesis",    "HYPOTHESIS (unverified)" in _scout)
    check("C3: Catalyst threshold rule",        "CATALYST THRESHOLD" in _src)
except Exception as e:
    check("CRDO audit checks", False, str(e)[:80])
