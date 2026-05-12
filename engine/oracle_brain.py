#!/usr/bin/env python3
"""
ORACLE Brain — persistent memory across screener and Think Tank runs.
Every stock analyzed by the Think Tank gets a dated log entry here so
future runs can compare trajectory over time.
"""

import os
import re
import datetime
from pathlib import Path

BRAIN_PATH = Path(os.path.expanduser("~/Documents/Trading Vault/04_Bot_Rules/ORACLE_BRAIN.md"))

BRAIN_HEADER = """\
# ORACLE Brain
## Purpose: Persistent memory across all screener and Think Tank runs
## Every stock that passes through the Think Tank gets logged here by date

---

## TICKER HISTORY LOG

"""


# ── Internal helpers ────────────────────────────────────────────────────────

def _read_brain() -> str:
    if not BRAIN_PATH.exists():
        return ""
    try:
        return BRAIN_PATH.read_text(encoding="utf-8")
    except Exception:
        return ""


def _write_brain(content: str) -> None:
    BRAIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    BRAIN_PATH.write_text(content, encoding="utf-8")


def _ensure_brain() -> str:
    """Return brain content, creating with header if missing."""
    content = _read_brain()
    if not content:
        _write_brain(BRAIN_HEADER)
        return BRAIN_HEADER
    return content


def _trunc(s: str, n: int) -> str:
    s = str(s).strip()
    return (s[:n] + "…") if len(s) > n else s


# ── Public: parse ───────────────────────────────────────────────────────────

def parse_run_for_brain(results: dict, stocks: list,
                        fundamentals: str = "",
                        screener_context: str = "") -> list:
    """
    Parse Think Tank results into a list of per-stock dicts for append_to_brain().
    Gracefully falls back to 'N/A' on any parse failure.

    Returns list of dicts with keys:
      date, ticker, price, verdict, conviction, bull, bear,
      catalyst, rev_growth, eps, scout, screener_score
    """
    today = datetime.date.today().isoformat()
    summary = results.get("summary", "")
    verdict_text = results.get("verdict", "")

    # ── Parse structured summary blocks: ---STOCK: TICKER--- ... ---END--- ──
    summary_data = {}
    for ticker, block in re.findall(
        r'---STOCK:\s*([A-Z]+)---(.*?)---END---', summary, re.DOTALL
    ):
        ticker = ticker.strip()

        def _sm(pat, b=block):
            m = re.search(pat, b)
            return m.group(1).strip() if m else "N/A"

        summary_data[ticker] = {
            "verdict":   _sm(r'OVERALL:\s*(BUY|WATCH|PASS|DISQUALIFIED)'),
            "score":     _sm(r'Score:\s*(\d+)/10'),
            "catalyst":  _sm(r'CATALYST:\s*(.+)'),
            "scout":     _sm(r'SCOUT:\s*(INVESTIGATE\s*FURTHER|INVESTIGATE|PASS|WARN)'),
            "fund_conv": _sm(r'Conviction:\s*(\d+)/10'),
            "consensus": _sm(r'PANEL_CONSENSUS:\s*(\d+/4[^-\n]*)'),
        }

    # ── Parse verdict section for bull/bear thesis per ticker ───────────────
    # Each ticker block begins with "TICKER: SYMBOL" on its own line
    verdict_data = {}
    for block in re.split(r'\n(?=TICKER:\s*[A-Z])', verdict_text):
        tm = re.match(r'TICKER:\s*([A-Z]+)', block.strip())
        if not tm:
            continue
        ticker = tm.group(1).strip()

        def _vm(pat, b=block):
            m = re.search(pat, b, re.DOTALL)
            if not m:
                return "N/A"
            # Take first non-empty line of match
            for line in m.group(1).strip().split('\n'):
                line = line.strip()
                if line:
                    return line
            return "N/A"

        # Stop each field at next ALL-CAPS label (e.g. "MUNGER INVERSION:")
        _stop = r'(?=\n[A-Z][A-Z ]+:|\Z)'
        verdict_data[ticker] = {
            "verdict":    _vm(r'VERDICT:\s*(BUY|WATCH|PASS|DISQUALIFIED)'),
            "conviction": _vm(r'CONVICTION:\s*(\d+/10)'),
            "bull":       _vm(r'TOP BULL ARGUMENT:\s*(.+?)' + _stop),
            "bear":       _vm(r'TOP BEAR ARGUMENT:\s*(.+?)' + _stop),
            "catalyst":   _vm(r'CATALYST:\s*(.+?)' + _stop),
        }

    # ── Parse fundamentals blob for price / rev growth / EPS ───────────────
    fund_data = {}
    for stock in stocks:
        esc = re.escape(stock)
        # Price: "$XX.XX" within 300 chars of ticker mention
        price_m = re.search(
            rf'\b{esc}\b.{{0,300}}\$(\d{{1,6}}\.?\d*)',
            fundamentals, re.DOTALL | re.IGNORECASE
        )
        # Revenue growth: a % figure near "revenue" or "rev" near the ticker block
        rev_m = re.search(
            rf'\b{esc}\b.{{0,500}}(?:revenue|rev)[^\n]{{0,150}}([+-]?\d+\.?\d*%)',
            fundamentals, re.DOTALL | re.IGNORECASE
        )
        # EPS: a dollar/number figure near "EPS" near the ticker block
        eps_m = re.search(
            rf'\b{esc}\b.{{0,500}}(?:EPS|earnings per share)[^\n]{{0,150}}([+-]?\$?\d+\.?\d+)',
            fundamentals, re.DOTALL | re.IGNORECASE
        )
        fund_data[stock] = {
            "price":      f"${price_m.group(1)}" if price_m else "N/A",
            "rev_growth": rev_m.group(1) if rev_m else "N/A",
            "eps":        eps_m.group(1) if eps_m else "N/A",
        }

    # ── Parse screener context for score (format: "SYM: score=XX/50 |...") ─
    screener_scores = {}
    for stock in stocks:
        sm = re.search(
            rf'\b{re.escape(stock)}\b[^\n]*?(\d+)/50',
            screener_context
        )
        screener_scores[stock] = f"{sm.group(1)}/50" if sm else "N/A"

    # ── Assemble per-stock entry dicts ──────────────────────────────────────
    entries = []
    for stock in stocks:
        sd = summary_data.get(stock, {})
        vd = verdict_data.get(stock, {})
        fd = fund_data.get(stock, {})

        # Prefer verdict section (richer text) over summary for key fields
        verdict = (vd.get("verdict", "N/A")
                   if vd.get("verdict", "N/A") != "N/A"
                   else sd.get("verdict", "PASS"))

        conv_raw = (vd.get("conviction", "N/A")
                    if vd.get("conviction", "N/A") != "N/A"
                    else sd.get("score", "?"))
        conviction = conv_raw if "/" in str(conv_raw) else f"{conv_raw}/10"

        catalyst = (sd.get("catalyst", "N/A")
                    if sd.get("catalyst", "N/A") != "N/A"
                    else vd.get("catalyst", "N/A"))

        entries.append({
            "date":           today,
            "ticker":         stock,
            "price":          fd.get("price", "N/A"),
            "verdict":        verdict,
            "conviction":     conviction,
            "consensus":      sd.get("consensus", "N/A"),
            "bull":           vd.get("bull", "N/A"),
            "bear":           vd.get("bear", "N/A"),
            "catalyst":       catalyst,
            "rev_growth":     fd.get("rev_growth", "N/A"),
            "eps":            fd.get("eps", "N/A"),
            "scout":          sd.get("scout", "N/A"),
            "screener_score": screener_scores.get(stock, "N/A"),
        })

    return entries


# ── Public: append ──────────────────────────────────────────────────────────

def append_to_brain(stocks_data: list, report_path: str = "") -> None:
    """
    Append per-stock run data to ORACLE_BRAIN.md.
    Creates the file with header if it doesn't exist.
    Never overwrites existing rows — append only.
    Any error is caught and printed; never crashes a Think Tank run.
    """
    try:
        content = _ensure_brain()

        for entry in stocks_data:
            ticker     = entry.get("ticker", "?")
            date       = entry.get("date", datetime.date.today().isoformat())
            price      = entry.get("price", "N/A")
            verdict    = entry.get("verdict", "N/A")
            conviction = entry.get("conviction", "?/10")
            consensus  = _trunc(entry.get("consensus", "N/A"), 30)
            scout      = entry.get("scout", "N/A")

            # Truncate long free-text fields so the table stays readable
            bull     = _trunc(entry.get("bull", "N/A"), 70)
            bear     = _trunc(entry.get("bear", "N/A"), 70)
            catalyst = _trunc(entry.get("catalyst", "N/A"), 60)
            rev_growth = entry.get("rev_growth", "N/A")
            eps        = entry.get("eps", "N/A")

            new_row = (
                f"| {date} | {price} | {verdict} | {conviction} | {consensus} | "
                f"{bull} | {bear} | {catalyst} | {rev_growth} | {eps} |"
            )

            section_header = f"### {ticker}"

            if section_header in content:
                lines = content.split('\n')

                # Find section start line index
                sec_line = next(
                    (i for i, ln in enumerate(lines)
                     if ln.strip() == section_header),
                    None
                )
                if sec_line is None:
                    # Fallback: can't locate — append new section instead
                    content = _append_new_section(
                        content, ticker, new_row, date, verdict, conviction, scout
                    )
                    continue

                # Find boundary of this section (next ### or EOF)
                next_sec_idx = len(lines)
                for i in range(sec_line + 1, len(lines)):
                    if lines[i].strip().startswith('### ') and i > sec_line:
                        next_sec_idx = i
                        break

                # Find last data row inside this section
                last_row_idx = -1
                for i in range(sec_line + 1, next_sec_idx):
                    if lines[i].startswith('|') and re.search(r'\| 20\d\d', lines[i]):
                        last_row_idx = i

                # Compute trend description
                if last_row_idx != -1:
                    prev = lines[last_row_idx]
                    pv_m = re.search(r'\|\s*(BUY|WATCH|PASS|DISQUALIFIED)\s*\|', prev)
                    pc_m = re.search(r'\|\s*(\d+)/10\s*\|', prev)
                    prev_verdict = pv_m.group(1) if pv_m else "?"
                    prev_conv    = int(pc_m.group(1)) if pc_m else 0
                    curr_conv_s  = conviction.split('/')[0]
                    curr_conv    = int(curr_conv_s) if curr_conv_s.isdigit() else 0

                    if verdict != prev_verdict:
                        trend_note = (
                            f"{prev_verdict} → {verdict}; "
                            f"conviction {prev_conv}/10 → {curr_conv}/10"
                        )
                    elif curr_conv > prev_conv:
                        trend_note = f"{verdict} conviction UP {prev_conv}→{curr_conv}/10"
                    elif curr_conv < prev_conv:
                        trend_note = f"{verdict} conviction DOWN {prev_conv}→{curr_conv}/10"
                    else:
                        trend_note = f"{verdict} unchanged at {curr_conv}/10 ({date})"

                    # Insert new row after last data row
                    lines.insert(last_row_idx + 1, new_row)
                    next_sec_idx += 1  # insertion shifted everything down

                    # Update or insert Trend line within this section
                    trend_idx = None
                    for i in range(sec_line, min(next_sec_idx, len(lines))):
                        if lines[i].startswith('**Trend:**'):
                            trend_idx = i
                            break
                    if trend_idx is not None:
                        lines[trend_idx] = f"**Trend:** {trend_note}"
                    else:
                        lines.insert(last_row_idx + 2, f"**Trend:** {trend_note}")

                else:
                    # Section exists but table has no data rows yet
                    sep_idx = None
                    for i in range(sec_line, min(next_sec_idx, len(lines))):
                        if lines[i].startswith('|---'):
                            sep_idx = i
                            break
                    insert_at = (sep_idx + 1) if sep_idx is not None else (sec_line + 1)
                    lines.insert(insert_at, new_row)

                content = '\n'.join(lines)

            else:
                content = _append_new_section(
                    content, ticker, new_row, date, verdict, conviction, scout
                )

        _write_brain(content)

    except Exception as e:
        print(f"  [Brain] Warning: could not update brain — {e}")


def _append_new_section(content: str, ticker: str, new_row: str,
                        date: str, verdict: str, conviction: str,
                        scout: str) -> str:
    """Append a brand-new ticker section to brain content."""
    new_section = (
        f"\n\n### {ticker}\n"
        f"| Date | Price | Verdict | Conviction | Consensus | Bull Thesis | Bear Thesis | "
        f"Catalyst | Rev Growth | EPS |\n"
        f"|------|-------|---------|------------|-----------|-------------|-------------|"
        f"----------|------------|-----|\n"
        f"{new_row}\n\n"
        f"**Trend:** First run — {date} {verdict} {conviction} "
        f"(scout: {scout})\n"
        f"**Running accuracy:** TBD\n"
    )
    return content.rstrip('\n') + new_section


# ── Public: read context ────────────────────────────────────────────────────

def read_brain_context(tickers: list) -> str:
    """
    Return a formatted string of prior run history for the given tickers.
    Returns empty string gracefully if brain file doesn't exist or no history found.

    Output format:
      PREVIOUS ORACLE ANALYSIS:
      [ZETA: 2 prior runs — 2026-05-11 PASS 1/10 @ $16.51 | 2026-05-18 WATCH 5/10 @ $18.00]
      [INSM: 1 prior run — 2026-05-11 WATCH 4/10 @ $38.22]
    """
    try:
        content = _read_brain()
        if not content:
            return ""

        lines_out = []
        for ticker in tickers:
            section_header = f"### {ticker}"
            if section_header not in content:
                continue

            sec_start = content.find(section_header)
            next_sec  = content.find('\n### ', sec_start + 1)
            sec_end   = next_sec if next_sec != -1 else len(content)
            section   = content[sec_start:sec_end]

            rows = []
            for line in section.split('\n'):
                if line.startswith('|') and re.search(r'\| 20\d\d', line):
                    parts = [p.strip() for p in line.split('|') if p.strip()]
                    if len(parts) >= 4:
                        row_date    = parts[0]
                        row_price   = parts[1]
                        row_verdict = parts[2]
                        row_conv    = parts[3]
                        rows.append(
                            f"{row_date} {row_verdict} {row_conv} @ {row_price}"
                        )

            if rows:
                label = "run" if len(rows) == 1 else "runs"
                lines_out.append(
                    f"[{ticker}: {len(rows)} prior {label} — {' | '.join(rows)}]"
                )

        if not lines_out:
            return ""

        return "PREVIOUS ORACLE ANALYSIS:\n" + "\n".join(lines_out)

    except Exception:
        return ""
