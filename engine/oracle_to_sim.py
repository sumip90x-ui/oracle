#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
oracle_to_sim.py — Bridge: ORACLE Think Tank report → MiroShark simulation package.

Parses a composite Think Tank report, extracts per-stock intelligence, and generates
a complete 6-stock simulation package ready to load into MiroShark.

Usage:
    python3 ~/ORACLE/engine/oracle_to_sim.py
    python3 ~/ORACLE/engine/oracle_to_sim.py --report ~/ORACLE/reports/ORACLE_INSM_BBIO_ZETA_20260511_composite.md
    python3 ~/ORACLE/engine/oracle_to_sim.py --tickers INSM BBIO ZETA SNOW PLTR PATH
    python3 ~/ORACLE/engine/oracle_to_sim.py --launch
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── Directory constants ─────────────────────────────────────────────────────
ORACLE_DIR = Path.home() / "ORACLE"
REPORTS_DIR = ORACLE_DIR / "reports"
CACHE_DIR = ORACLE_DIR / "cache"
SIMS_DIR = ORACLE_DIR / "sims"
SCREENER_CACHE_PATH = CACHE_DIR / "screener_cache.json"

# ─── Sector classification ────────────────────────────────────────────────────
# Hard-coded assignments for well-known tickers — avoids misclassification when
# screener sector labels don't map cleanly to biotech/saas/data_ai clusters.
KNOWN_SECTORS: dict = {
    # Biotech / pharma
    "INSM": "biotech", "BBIO": "biotech", "CRSP": "biotech",
    "NTLA": "biotech", "VRTX": "biotech", "REGN": "biotech",
    "GILD": "biotech", "MRNA": "biotech", "BNTX": "biotech",
    "SRRK": "biotech", "MESO": "biotech", "KTOS": "biotech",
    # Data / AI infrastructure
    "SNOW": "data_ai", "PLTR": "data_ai", "MDB": "data_ai",
    "ESTC": "data_ai", "DBX": "data_ai", "DOMO": "data_ai",
    "ANET": "data_ai", "NET": "data_ai",
    # SaaS / automation
    "ZETA": "saas", "PATH": "saas", "CRM": "saas",
    "NOW": "saas", "HUBS": "saas", "DDOG": "saas",
    "GTLB": "saas", "ZS": "saas", "OKTA": "saas",
}

BIOTECH_KEYWORDS = {
    "health", "biotech", "pharma", "bioscience", "therapeutics",
    "medical", "drug", "clinical", "biopharmaceutical", "rare disease",
}
DATA_AI_KEYWORDS = {
    "data", "analytics", "intelligence", "machine learning",
    "government", "defense", "warehouse", "database",
}
SAAS_KEYWORDS = {
    "software", "saas", "cloud", "automation", "crm", "erp",
    "enterprise", "rpa", "marketing", "technology services",
}


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1 — REPORT PARSER
# ═════════════════════════════════════════════════════════════════════════════

def find_latest_report() -> Optional[Path]:
    """Find the most recently modified composite report in ~/ORACLE/reports/."""
    matches = list(REPORTS_DIR.glob("ORACLE_*.md"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def parse_tickers_from_filename(path: Path) -> list:
    """
    Extract ticker symbols from the report filename.

    ORACLE_INSM_BBIO_ZETA_20260511_composite.md → ['INSM', 'BBIO', 'ZETA']
    Note: filename only reflects the first batch; parse_report extracts all 6.
    """
    stem = path.stem  # e.g. ORACLE_INSM_BBIO_ZETA_20260511_composite
    tickers = []
    for part in stem.split("_")[1:]:  # skip leading 'ORACLE'
        if re.match(r"^\d{8}$", part):
            break  # hit the date segment
        if re.match(r"^[A-Z]{2,5}$", part):
            tickers.append(part)
    return tickers


def load_screener_cache() -> dict:
    """Load screener cache JSON from ~/ORACLE/cache/screener_cache.json."""
    if not SCREENER_CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(SCREENER_CACHE_PATH.read_text(encoding="utf-8"))
        # Cache structure: {"timestamp": "...", "data": {"TICKER": {...}, ...}}
        if isinstance(raw, dict) and "data" in raw and isinstance(raw["data"], dict):
            return raw["data"]
        if isinstance(raw, dict):
            return raw
        return {}
    except Exception as exc:
        print(f"[WARN] Screener cache load failed: {exc}", file=sys.stderr)
        return {}


def parse_structured_summary(text: str) -> dict:
    """
    Parse LAYER 5 ---STOCK: TICKER--- blocks (the most machine-readable section).

    Block format:
        ---STOCK: INSM---
        SCOUT: INVESTIGATE | Category: Fast Grower | Secret: ...
        SKEPTIC: WARN | Key risk: ...
        FUNDAMENTALS: HOLD/PASS | Conviction: 3/10 | EV: -9.5%
        TECH+MACRO: ACCELERATING | Macro: HEADWIND
        OVERALL: WATCH | Score: 4/10
        CATALYST: ...
        KILL CONDITION: ...
        ---END---
    """
    stocks: dict = {}
    pattern = r"---STOCK:\s*(\w+)---\s*(.*?)---END---"
    for m in re.finditer(pattern, text, re.DOTALL):
        ticker = m.group(1).strip()
        block = m.group(2).strip()
        entry: dict = {"ticker": ticker}

        try:
            scout_m = re.search(
                r"SCOUT:\s*([^|\n]+?)(?:\|\s*Category:\s*([^|\n]+?))?(?:\|\s*Secret:\s*(.*?))?$",
                block, re.MULTILINE,
            )
            if scout_m:
                entry["scout_verdict"] = scout_m.group(1).strip()
                if scout_m.group(2):
                    entry["lynch_category"] = scout_m.group(2).strip()
                if scout_m.group(3):
                    entry["thiel_secret"] = scout_m.group(3).strip()
        except Exception:
            pass

        try:
            skep_m = re.search(
                r"SKEPTIC:\s*([^|\n]+?)(?:\|\s*Key risk:\s*(.*))?$",
                block, re.MULTILINE,
            )
            if skep_m:
                entry["skeptic_verdict"] = skep_m.group(1).strip()
                if skep_m.group(2):
                    entry["key_risk"] = skep_m.group(2).strip()
        except Exception:
            pass

        try:
            fund_m = re.search(
                r"FUNDAMENTALS:\s*([^|\n]+?)\|\s*Conviction:\s*(\d+)/10\s*\|\s*EV:\s*([^\n]+)",
                block,
            )
            if fund_m:
                entry["fundamentals_verdict"] = fund_m.group(1).strip()
                entry["conviction"] = int(fund_m.group(2))
                entry["ev_note"] = fund_m.group(3).strip()
        except Exception:
            pass

        try:
            overall_m = re.search(
                r"OVERALL:\s*([^|\n]+?)\|\s*Score:\s*(\d+)/10",
                block,
            )
            if overall_m:
                entry["overall_verdict"] = overall_m.group(1).strip()
                entry["score"] = int(overall_m.group(2))
        except Exception:
            pass

        try:
            cat_m = re.search(
                r"CATALYST:\s*(.*?)(?=\nKILL CONDITION:|\Z)",
                block, re.DOTALL,
            )
            if cat_m:
                entry["catalyst"] = cat_m.group(1).strip()
        except Exception:
            pass

        try:
            kill_m = re.search(r"KILL CONDITION:\s*(.*)", block, re.DOTALL)
            if kill_m:
                entry["kill_condition"] = kill_m.group(1).strip()
        except Exception:
            pass

        stocks[ticker] = entry
    return stocks


def parse_stock_section_headers(text: str) -> dict:
    """
    Parse per-stock ## TICKER (Company Name) - $PRICE headers.

    Returns ticker → {company, price} for each unique stock header found.
    """
    stocks: dict = {}
    pattern = r"^## ([A-Z]{2,5}) \(([^)]+)\)\s*-\s*\$([0-9,.]+)"
    for m in re.finditer(pattern, text, re.MULTILINE):
        ticker = m.group(1)
        if ticker in stocks:
            continue  # keep first occurrence (Scout section)
        company = m.group(2).strip()
        try:
            price = float(m.group(3).replace(",", ""))
        except ValueError:
            price = 0.0
        stocks[ticker] = {"ticker": ticker, "company": company, "price": price}
    return stocks


def parse_bull_bear_arguments(text: str) -> dict:
    """
    Parse LAYER 6 TOP BULL ARGUMENT / TOP BEAR ARGUMENT per ticker.

    LAYER 6 format:
        ### TICKER: INSM
        **VERDICT:** WATCH
        ...
        **TOP BULL ARGUMENT:**
        ...text...
        **TOP BEAR ARGUMENT:**
        ...text...
        ---
    """
    stocks: dict = {}

    # Anchor search to LAYER 6 section if present
    layer6_m = re.search(r"LAYER 6.*?\n", text, re.IGNORECASE)
    search_text = text[layer6_m.start():] if layer6_m else text

    ticker_blocks = re.split(r"###\s*TICKER:\s*([A-Z]{2,5})", search_text)
    # ticker_blocks = [pre, TICKER1, block1, TICKER2, block2, ...]
    i = 1
    while i < len(ticker_blocks) - 1:
        ticker = ticker_blocks[i].strip()
        block = ticker_blocks[i + 1]
        entry: dict = {}

        try:
            bull_m = re.search(
                r"\*\*TOP BULL ARGUMENT:\*\*\s*(.*?)(?=\*\*TOP BEAR ARGUMENT:\*\*|\*\*CATALYST:\*\*|\*\*SELL TRIGGER:\*\*|^---|\Z)",
                block, re.DOTALL | re.MULTILINE,
            )
            if bull_m:
                entry["bull_thesis"] = bull_m.group(1).strip()
        except Exception:
            pass

        try:
            bear_m = re.search(
                r"\*\*TOP BEAR ARGUMENT:\*\*\s*(.*?)(?=\*\*CATALYST:\*\*|\*\*SELL TRIGGER:\*\*|^---|\Z)",
                block, re.DOTALL | re.MULTILINE,
            )
            if bear_m:
                entry["bear_thesis"] = bear_m.group(1).strip()
        except Exception:
            pass

        try:
            verdict_m = re.search(r"\*\*VERDICT:\*\*\s*([^\n*]+)", block)
            if verdict_m:
                entry["synthesis_verdict"] = verdict_m.group(1).strip()
        except Exception:
            pass

        try:
            kelly_m = re.search(r"\*\*KELLY SIZE:\*\*\s*([^\n]+)", block)
            if kelly_m:
                entry["kelly_size"] = kelly_m.group(1).strip()
        except Exception:
            pass

        if entry:
            stocks[ticker] = entry
        i += 2

    return stocks


def parse_scout_verdicts_fallback(text: str) -> dict:
    """
    Parse SCOUT VERDICT and THIEL SECRET from LAYER 1 per-stock sections.

    Used as a fallback when LAYER 5 summary blocks are incomplete.
    """
    stocks: dict = {}
    # Find LAYER 1 region
    layer1_m = re.search(r"LAYER 1.*?\n", text, re.IGNORECASE)
    if not layer1_m:
        return stocks
    layer1_text = text[layer1_m.start():]

    # Stop at LAYER 2
    layer2_m = re.search(r"\nLAYER 2", layer1_text, re.IGNORECASE)
    if layer2_m:
        layer1_text = layer1_text[: layer2_m.start()]

    # Split by stock headers
    segments = re.split(r"^## ([A-Z]{2,5}) \(", layer1_text, flags=re.MULTILINE)
    i = 1
    while i < len(segments) - 1:
        try:
            ticker = segments[i].strip()
            block = segments[i + 1]
            entry: dict = {}

            secret_m = re.search(
                r"\*\*THE SECRET[^:]*:\*\*[^*\n]*\*([^*]+)\*", block
            ) or re.search(
                r"\*\*THE SECRET[^:]*:\*\*\s*_([^_]+)_", block
            ) or re.search(
                r"\*\*THE SECRET[^:]*:\*\*\s*[*_]*([^\n*_]+)", block
            )
            if secret_m:
                entry["thiel_secret"] = secret_m.group(1).strip()

            verdict_m = re.search(
                r"\*\*SCOUT VERDICT:\*\*\s*\*\*([^*\n]+)\*\*", block
            )
            if verdict_m:
                entry["scout_verdict"] = verdict_m.group(1).strip()

            if entry:
                stocks[ticker] = entry
        except Exception:
            pass
        i += 2

    return stocks


def parse_report(report_path: Path) -> dict:
    """
    Parse the Think Tank markdown report and extract per-stock structured data.

    Merges data from three sources in priority order:
    1. LAYER 5 structured summary blocks (most reliable for verdicts/scores)
    2. LAYER 6 synthesis (bull/bear arguments, Kelly sizes)
    3. Section headers (company name, price)
    4. LAYER 1 Scout fallback (thiel secret, scout verdict)
    """
    text = report_path.read_text(encoding="utf-8")

    summary_data: dict = {}
    header_data: dict = {}
    bull_bear_data: dict = {}
    scout_fallback: dict = {}

    try:
        summary_data = parse_structured_summary(text)
    except Exception as exc:
        print(f"[WARN] LAYER 5 parse failed: {exc}", file=sys.stderr)

    try:
        header_data = parse_stock_section_headers(text)
    except Exception as exc:
        print(f"[WARN] Header parse failed: {exc}", file=sys.stderr)

    try:
        bull_bear_data = parse_bull_bear_arguments(text)
    except Exception as exc:
        print(f"[WARN] LAYER 6 parse failed: {exc}", file=sys.stderr)

    try:
        scout_fallback = parse_scout_verdicts_fallback(text)
    except Exception as exc:
        print(f"[WARN] Scout fallback parse failed: {exc}", file=sys.stderr)

    all_tickers = set(
        list(summary_data) + list(header_data) + list(bull_bear_data) + list(scout_fallback)
    )

    merged: dict = {}
    for ticker in all_tickers:
        # Layer lowest-priority sources first, overlay higher-priority ones
        entry: dict = {}
        entry.update(header_data.get(ticker, {}))
        entry.update(scout_fallback.get(ticker, {}))
        entry.update(summary_data.get(ticker, {}))
        entry.update(bull_bear_data.get(ticker, {}))
        entry["ticker"] = ticker
        merged[ticker] = entry

    return merged


# ═════════════════════════════════════════════════════════════════════════════
# SCREENER CACHE ENRICHMENT
# ═════════════════════════════════════════════════════════════════════════════

def enrich_with_screener(stocks: dict, cache: dict) -> dict:
    """
    Enrich stock dicts with screener cache data.

    Adds sector, industry, analyst_upside_pct, rev_growth_pct,
    forward_eps, trailing_eps, short_pct, market_cap_b, full_name.
    Does not overwrite existing values from the report.
    """
    for ticker, entry in stocks.items():
        sc = cache.get(ticker)
        if not sc:
            continue
        entry.setdefault("company", sc.get("full_name", ticker))
        entry.setdefault("price", sc.get("price", 0.0))
        # Screener fields always win for quantitative data
        for field in (
            "sector", "industry", "analyst_upside_pct", "rev_growth_pct",
            "forward_eps", "trailing_eps", "short_pct", "market_cap_b",
        ):
            if sc.get(field) is not None:
                entry[field] = sc[field]
    return stocks


# ═════════════════════════════════════════════════════════════════════════════
# SECTOR CLUSTERING
# ═════════════════════════════════════════════════════════════════════════════

def infer_sector_cluster(ticker: str, entry: dict) -> str:
    """
    Classify a stock into 'biotech', 'data_ai', 'saas', or 'other'.

    Uses KNOWN_SECTORS hard-coded dict first, then keyword matching on
    screener sector/industry fields.
    """
    if ticker in KNOWN_SECTORS:
        return KNOWN_SECTORS[ticker]

    sector_text = (
        (entry.get("sector", "") + " " + entry.get("industry", "")).lower()
    )

    biotech_score = sum(1 for kw in BIOTECH_KEYWORDS if kw in sector_text)
    data_ai_score = sum(1 for kw in DATA_AI_KEYWORDS if kw in sector_text)
    saas_score = sum(1 for kw in SAAS_KEYWORDS if kw in sector_text)

    if biotech_score >= max(data_ai_score, saas_score) and biotech_score > 0:
        return "biotech"
    if data_ai_score >= saas_score and data_ai_score > 0:
        return "data_ai"
    if saas_score > 0:
        return "saas"
    return "other"


def cluster_stocks(stocks: dict, tickers: list) -> dict:
    """
    Group tickers into 3 clusters of 2: biotech, saas, data_ai.

    Redistributes overflow stocks and fills sparse clusters from 'other'
    or by pulling from the largest cluster.
    """
    clusters: dict = {"biotech": [], "saas": [], "data_ai": [], "other": []}

    for ticker in tickers:
        entry = stocks.get(ticker, {})
        cluster = infer_sector_cluster(ticker, entry)
        clusters[cluster].append(ticker)

    # Collect unassigned and overflow
    assigned: set = set()
    for k, v in clusters.items():
        assigned.update(v)
    spare: list = clusters.pop("other", [])
    spare += [t for t in tickers if t not in assigned]

    primary_keys = ["biotech", "saas", "data_ai"]

    # Fill clusters that have fewer than 2 stocks
    for key in primary_keys:
        while len(clusters[key]) < 2 and spare:
            clusters[key].append(spare.pop(0))

    # Move overflow (>2) to sparse clusters
    for key in primary_keys:
        while len(clusters[key]) > 2:
            extra = clusters[key].pop()
            for fill_key in primary_keys:
                if len(clusters[fill_key]) < 2:
                    clusters[fill_key].append(extra)
                    break
            else:
                spare.append(extra)

    return {k: v for k, v in clusters.items() if v}


# ═════════════════════════════════════════════════════════════════════════════
# FORMATTING HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def fmt_rev_growth(entry: dict) -> str:
    """Format revenue growth percentage from screener cache or report text."""
    rg = entry.get("rev_growth_pct")
    if rg is not None:
        try:
            # Screener stores as percentage value (229.6 = 229.6% growth)
            return f"{float(rg):.0f}% YoY"
        except (ValueError, TypeError):
            pass
    # Fallback: extract from text fields
    for field in ("thiel_secret", "key_risk", "catalyst", "bull_thesis"):
        text = entry.get(field, "")
        m = re.search(r"(\d+\.?\d*)%\s*YoY", text, re.IGNORECASE)
        if m:
            return f"{m.group(1)}% YoY"
    return "N/A"


def fmt_eps_status(entry: dict) -> str:
    """Return human-readable EPS status from screener or report data."""
    fwd = entry.get("forward_eps")
    trail = entry.get("trailing_eps")
    for val, label in [(fwd, "fwd"), (trail, "TTM")]:
        if val is not None:
            try:
                f = float(val)
                status = "Profitable" if f > 0 else "Pre-profit"
                return f"{status} ({label} ${f:.2f})"
            except (ValueError, TypeError):
                pass
    return "EPS: N/A"


def fmt_analyst_upside(entry: dict) -> str:
    """Format analyst upside percentage (screener stores as 97.08 = +97%)."""
    upside = entry.get("analyst_upside_pct")
    if upside is not None:
        try:
            return f"+{float(upside):.0f}%"
        except (ValueError, TypeError):
            pass
    return "N/A"


def fmt_catalyst(entry: dict) -> str:
    """Return short catalyst description (first sentence of catalyst field)."""
    cat = entry.get("catalyst", "")
    if cat and len(cat) > 10:
        return re.split(r"[.;(]", cat)[0].strip()[:120]
    return "Earnings / product update"


def first_n_sentences(text: str, n: int = 2) -> str:
    """Return the first n sentences of a text block."""
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:n])


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2 — SEED DOCUMENT BUILDER
# ═════════════════════════════════════════════════════════════════════════════

_CLUSTER_PAIR_TEMPLATES = {
    "biotech": (
        "{t1} and {t2} are both rare-disease biopharma names competing for the same specialist "
        "investor dollar — binary FDA catalysts dominate both theses. "
        "{t1}'s bull case is '{secret1}'; {t2}'s bull case centers on pipeline breadth with "
        "a different concentration-risk profile. "
        "A portfolio manager owning both is implicitly double-betting on rare-disease approval cycles; "
        "the opportunity-cost question is which binary event has better expected value at current prices."
    ),
    "saas": (
        "{t1} and {t2} are both SaaS/automation names fighting the same secular question: "
        "does AI commoditize their product or differentiate it? "
        "{t1} is navigating '{short_key_risk1}'; {t2} faces '{short_key_risk2}'. "
        "Capital that flows to {t1} is capital NOT flowing to {t2} — "
        "if {t2}'s turnaround fails, the SaaS rotation goes to {t1} defensively; "
        "if {t2} re-accelerates, {t1} becomes the obvious cut."
    ),
    "data_ai": (
        "{t1} and {t2} are both positioned as AI infrastructure plays but occupy very different "
        "risk profiles — {t1} is a data-warehouse-to-AI-platform bet while {t2} is a "
        "government-plus-commercial AI decisioning bet. "
        "Both compete for the same 'picks-and-shovels AI' allocation from growth fund managers. "
        "The hyperscaler bundling threat (AWS/Azure/GCP giving away competing functionality) "
        "is the shared kill condition that agents must track for both names simultaneously."
    ),
}

_CLUSTER_DESCRIPTIONS = {
    "biotech": (
        "BIOTECH CLUSTER: Rare-disease biopharma names where binary FDA events dominate price action. "
        "Both names compete for the same rare-disease specialist investor — "
        "growth funds with tolerance for pre-profitability biotech, dedicated biotech funds pricing "
        "approval probabilities, and hedge funds sizing against catalyst timelines. "
        "The key differentiator is single-drug concentration risk vs multi-drug platform breadth. "
        "In risk-off environments, both are sold; in risk-on biotech cycles, "
        "the name with the nearer catalyst captures the rotation first."
    ),
    "saas": (
        "SAAS/AUTOMATION CLUSTER: Enterprise software names where the central debate is whether AI "
        "disruption is a tailwind (enabling new capabilities) or a headwind (commoditizing existing products). "
        "Both names compete for the same enterprise IT budget cycle. "
        "Revenue growth trajectory and net revenue retention (NRR) are the decisive metrics: "
        "acceleration signals moat; deceleration signals disruption. "
        "In this cluster, owning both hedges away the core differentiation — "
        "pick the one with the better AI narrative durability."
    ),
    "data_ai": (
        "DATA/AI INFRASTRUCTURE CLUSTER: Names where the bull thesis is that AI workloads "
        "create a secular demand surge for data management and intelligence platforms. "
        "Both compete for enterprise data budgets and the same 'AI infrastructure' allocation. "
        "The key risk for this cluster is hyperscaler bundling: "
        "AWS, Azure, and GCP have historically given away best-of-breed data functionality for free "
        "to lock in platform spend. Agents must model this risk continuously."
    ),
}


def _cross_stock_sentences(clusters: dict, stocks: dict) -> str:
    """
    Generate one cross-stock co-mention sentence per cluster pair.

    These sentences force NER to create cross-stock graph edges in MiroShark.
    One sentence per cluster pair is the minimum; they must name both tickers explicitly.
    """
    lines = []
    for cluster_name, cluster_tickers in clusters.items():
        if len(cluster_tickers) < 2:
            continue
        t1, t2 = cluster_tickers[0], cluster_tickers[1]
        e1 = stocks.get(t1, {})
        e2 = stocks.get(t2, {})

        secret1 = first_n_sentences(e1.get("thiel_secret", "no clear secret identified"), 1)
        secret2 = first_n_sentences(e2.get("thiel_secret", "no clear secret identified"), 1)
        kr1 = first_n_sentences(e1.get("key_risk", "execution risk"), 1)
        kr2 = first_n_sentences(e2.get("key_risk", "execution risk"), 1)

        template = _CLUSTER_PAIR_TEMPLATES.get(cluster_name, (
            "{t1} and {t2} share a sector cluster and compete for the same allocation dollar — "
            "a position in {t1} is implicitly a statement that {t1} offers better risk-adjusted "
            "return than {t2} at current prices."
        ))
        sentence = template.format(
            t1=t1, t2=t2,
            secret1=secret1[:80], secret2=secret2[:80],
            short_key_risk1=kr1[:60], short_key_risk2=kr2[:60],
        )
        lines.append(sentence)

    return "\n\n".join(lines)


def build_seed_document(stocks: dict, clusters: dict, tickers: list) -> str:
    """
    Build the 4-section combined seed document for MiroShark NER ingestion.

    Section 1: Universe comparison table + cross-stock co-mention sentences
    Section 2: Sector cluster descriptions
    Section 3: Per-stock data blocks with bull/bear thesis and post templates
    Section 4: Opportunity cost framing
    """
    out = []

    # ── SECTION 1: UNIVERSE COMPARISON TABLE ─────────────────────────────────
    out.append("=== SECTION 1: UNIVERSE COMPARISON TABLE ===\n")
    out.append("| Ticker | Sector | Rev Growth | EPS Status | Analyst Upside | Key Catalyst |")
    out.append("|--------|--------|------------|------------|----------------|-------------|")
    for t in tickers:
        if t not in stocks:
            continue
        e = stocks[t]
        sector = (e.get("sector") or e.get("industry") or "N/A")[:22]
        out.append(
            f"| {t} | {sector} | {fmt_rev_growth(e)} | {fmt_eps_status(e)} "
            f"| {fmt_analyst_upside(e)} | {fmt_catalyst(e)[:50]} |"
        )

    out.append("")
    out.append("CROSS-STOCK CO-MENTION ANALYSIS (critical for graph edge creation):")
    out.append("")
    out.append(_cross_stock_sentences(clusters, stocks))
    out.append("")

    # ── SECTION 2: SECTOR CLUSTERS ────────────────────────────────────────────
    out.append("=== SECTION 2: SECTOR CLUSTERS ===\n")
    for cluster_name, cluster_tickers in clusters.items():
        if not cluster_tickers:
            continue
        tickers_str = " + ".join(cluster_tickers)
        label = cluster_name.replace("_", "/").upper()
        description = _CLUSTER_DESCRIPTIONS.get(
            cluster_name,
            f"{label} CLUSTER: Mixed-sector grouping. Evaluate each name on individual merit.",
        )
        out.append(f"[{tickers_str}] — {description}")
        out.append("")

    # ── SECTION 3: PER-STOCK DATA BLOCKS ─────────────────────────────────────
    out.append("=== SECTION 3: PER-STOCK DATA BLOCKS ===\n")
    for t in tickers:
        if t not in stocks:
            continue
        e = stocks[t]
        company = e.get("company", t)
        price = e.get("price", 0.0)
        price_str = f"${price:.2f}" if price else "price N/A"
        score = e.get("score", e.get("conviction", "?"))

        # Bull and bear thesis (prefer LAYER 6 full arguments, fall back to summary)
        bull_long = e.get("bull_thesis") or e.get("thiel_secret") or "Bull thesis not extracted."
        bear_long = e.get("bear_thesis") or e.get("key_risk") or "Bear thesis not extracted."
        bull_short = first_n_sentences(bull_long, 2)
        bear_short = first_n_sentences(bear_long, 2)

        out.append(f"[{t} BLOCK] {company} — {price_str}")
        out.append(f"Panel score: {score}/10")
        out.append(f"Bull thesis: {bull_short}")
        out.append(f"Bear thesis: {bear_short}")
        out.append(f"Revenue growth: {fmt_rev_growth(e)}")
        out.append(f"EPS status: {fmt_eps_status(e)}")
        out.append(f"Analyst upside: {fmt_analyst_upside(e)}")
        out.append(f"Key catalyst: {fmt_catalyst(e)}")
        out.append("")

        # Post templates — written as opinionated social media posts with numbers
        rev = fmt_rev_growth(e)
        upside = fmt_analyst_upside(e)
        eps = fmt_eps_status(e)

        out.append(f"POST TEMPLATE 1 (bull — ${t}):")
        out.append(
            f"Thread: Why I'm watching ${t} here at {price_str}. "
            f"Revenue {rev}, EPS {eps.lower()}, consensus upside {upside}. "
            f"The bear thesis is '{bear_short[:90]}...' But here's what the bears are missing: "
            f"{bull_short[:120]}. "
            f"At a panel score of {score}/10 this is not a consensus long — "
            f"which is exactly why the risk/reward is asymmetric. DYOR."
        )
        out.append("")
        out.append(f"POST TEMPLATE 2 (bear — ${t}):")
        out.append(
            f"Counter-thesis on ${t} at {price_str}: before you buy the story, run the math. "
            f"{bear_short[:120]}. "
            f"Revenue growing {rev} sounds good until you realize analyst upside is already baked in at {upside}. "
            f"Compare the capital allocation question: is ${t} actually better than the five alternatives "
            f"in this universe at current valuations? The panel score of {score}/10 suggests the answer "
            f"is contested. What's your edge that the 11 agents in this sim don't already have?"
        )
        out.append("")

    # ── SECTION 4: OPPORTUNITY COST FRAMING ──────────────────────────────────
    out.append("=== SECTION 4: OPPORTUNITY COST FRAMING ===\n")
    ticker_list = ", ".join(f"${t}" for t in tickers)
    t0 = tickers[0] if tickers else "STOCK_A"
    t1 = tickers[1] if len(tickers) > 1 else "STOCK_B"
    t_last = tickers[-1] if tickers else "STOCK_Z"

    out.append(
        f"This simulation is a capital allocation competition, not independent stock analysis. "
        f"Every dollar committed to one of {ticker_list} is a dollar explicitly NOT deployed "
        f"into the other five alternatives. The question is never 'Is ${t0} a good company?' — "
        f"it is 'Is ${t0} a better risk-adjusted bet than ${t1}, ${t_last}, and the other three "
        f"names at current prices, given their respective catalyst timelines and downside scenarios?'"
    )
    out.append("")
    out.append(
        f"In a forced-ranking exercise, even the highest-conviction name must be defended against "
        f"the explicit argument that capital would compound faster in another name in this universe. "
        f"Agents are required to make cross-universe comparisons explicit in every round — "
        f"single-stock monologues disconnected from the other five names are not permitted."
    )
    out.append("")
    out.append(
        f"Macro and sector events do not affect all six names equally. "
        f"A rates shock hits high-multiple growth names hardest; "
        f"a biotech FDA ruling is sector-contained; "
        f"a big-tech competitive announcement may destroy one cluster while benefiting another. "
        f"Agents must model differential impacts and update relative rankings after each round."
    )
    out.append("")
    out.append(
        f"The simulation ends with a forced-choice: if you could hold exactly ONE of these six stocks "
        f"({', '.join(tickers)}) for 12 months with no ability to exit, which would you choose? "
        f"The answer must be defended against five counter-arguments from the other names."
    )

    return "\n".join(out)


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 3 — PREDICTION MARKETS
# ═════════════════════════════════════════════════════════════════════════════

def _initial_probability(entry: dict) -> float:
    """Estimate initial YES probability from panel score (range 0.25–0.75)."""
    for field in ("score", "conviction"):
        val = entry.get(field)
        if val is not None:
            try:
                return round(0.25 + (int(val) / 10) * 0.5, 2)
            except (ValueError, TypeError):
                pass
    return 0.45  # slight bearish default


def build_markets(tickers: list, clusters: dict, stocks: dict) -> list:
    """
    Generate 10 prediction market configs.

    6 individual: [TICKER] achieves 50%+ total return in 12 months
    3 head-to-heads: one per cluster pair
    1 forced-choice: pick the single winner from all 6
    """
    markets: list = []

    # ── 6 individual markets ──────────────────────────────────────────────────
    for ticker in tickers:
        e = stocks.get(ticker, {})
        price = e.get("price", 0.0)
        price_str = f"${price:.2f}" if price else ""
        markets.append({
            "id": f"individual_{ticker}",
            "type": "binary",
            "question": f"{ticker} achieves 50%+ total return in 12 months: YES or NO",
            "description": (
                f"{ticker} {price_str} — panel score: {e.get('score', '?')}/10. "
                f"Catalyst: {fmt_catalyst(e)}. "
                f"Kill condition: {str(e.get('kill_condition', 'Not specified'))[:100]}"
            ),
            "resolution": "YES if closing price >= 1.5x entry price at any point during 12-month window",
            "initial_probability": _initial_probability(e),
        })

    # ── 3 cluster head-to-heads ───────────────────────────────────────────────
    for cluster_name, cluster_tickers in clusters.items():
        if len(cluster_tickers) < 2:
            continue
        t1, t2 = cluster_tickers[0], cluster_tickers[1]
        label = cluster_name.replace("_", "/").upper()
        s1 = stocks.get(t1, {}).get("score", "?")
        s2 = stocks.get(t2, {}).get("score", "?")
        markets.append({
            "id": f"h2h_{t1}_vs_{t2}",
            "type": "binary",
            "question": f"{t1} outperforms {t2} on total return over 12 months: YES or NO",
            "description": (
                f"Head-to-head within {label} cluster. "
                f"{t1} score {s1}/10 vs {t2} score {s2}/10. "
                f"Capital in {t1} is capital NOT in {t2} — this market forces explicit comparison."
            ),
            "resolution": f"YES if {t1} total return > {t2} total return at 12-month mark",
            "initial_probability": 0.50,
        })

    # ── 1 forced-choice ranking market ───────────────────────────────────────
    markets.append({
        "id": "forced_choice_winner",
        "type": "multi_choice",
        "question": (
            "If forced to hold only ONE of these 6 stocks for 12 months, "
            f"the winner is: {'/'.join(tickers)}"
        ),
        "description": (
            "Forced-choice capital allocation tournament. "
            "Agents must argue for one name above all others. "
            "Aggregate agent conviction weighted by Kelly sizes determines consensus."
        ),
        "options": tickers,
        "resolution": "Determined by 12-month total return ranking at simulation end",
        "initial_probability": {t: round(1.0 / len(tickers), 3) for t in tickers},
    })

    return markets


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 4 — AGENT ROSTER
# ═════════════════════════════════════════════════════════════════════════════

def build_agents(tickers: list, clusters: dict) -> list:
    """
    Generate 11 agent configs in 3 layers.

    Layer 1 (5 agents): Cross-stock investment style — evaluate all 6 stocks
    Layer 2 (3 agents): Sector specialists — deep on their 2, can comment on all
    Layer 3 (3 agents): Adversarial — all stocks, mandatory quantification rules
    """
    ticker_list = ", ".join(tickers)
    universal = (
        f"UNIVERSAL RULE: Every post MUST reference at least one other stock in the universe "
        f"by ticker symbol ({ticker_list}). Single-stock monologues are not permitted."
    )

    biotech = clusters.get("biotech", tickers[:2])
    saas = clusters.get("saas", tickers[2:4])
    data_ai = clusters.get("data_ai", tickers[4:6])
    biotech_str = " and ".join(biotech)
    saas_str = " and ".join(saas)
    data_ai_str = " and ".join(data_ai)

    agents: list = []

    # ── Layer 1: Cross-stock investment style ────────────────────────────────
    agents.append({
        "id": "growth_compounder",
        "layer": 1,
        "name": "Growth Compounder (Lynch/Fisher)",
        "style": "Lynch PEG + Fisher scuttlebutt: seeks fast growers with explainable moats",
        "universe": tickers,
        "hard_constraint": (
            f"MUST rank at least 3 of the 6 stocks by growth quality in every post "
            f"(revenue acceleration, PEG ratio, business explainability). {universal}"
        ),
        "system_prompt": (
            f"You are a growth-focused portfolio manager combining Peter Lynch's napkin-test "
            f"mentality with Philip Fisher's scuttlebutt research. "
            f"Universe: {ticker_list}. "
            f"Evaluation criteria: (1) revenue acceleration vs deceleration, "
            f"(2) PEG ratio (under 1.0 = cheap, over 2.0 = expensive), "
            f"(3) whether the business model is explainable to a 12-year-old, "
            f"(4) management execution quality vs stated plan. "
            f"MANDATORY each post: rank at least 3 of 6 stocks by growth quality "
            f"with explicit reasoning. {universal}"
        ),
    })

    agents.append({
        "id": "probabilist",
        "layer": 1,
        "name": "Probabilist (Ed Thorp / Kelly Criterion)",
        "style": "Thorp/Kelly: quantifies win probability, payoff ratio, and Kelly % before any bet",
        "universe": tickers,
        "hard_constraint": (
            f"MUST include numerical Kelly % calculation for top 2 picks in every post. {universal}"
        ),
        "system_prompt": (
            f"You are a quantitative investor applying Kelly criterion and expected value analysis. "
            f"Universe: {ticker_list}. "
            f"Never take a position without calculating: "
            f"(1) win probability, (2) payoff ratio, (3) Kelly % (full and half). "
            f"MANDATORY each post: explicit Kelly calculations for top 2 picks. "
            f"Format: '[TICKER]: [X]% win prob × [Y]:1 payoff = Kelly [Z]%, "
            f"half-Kelly position [Z/2]%.' "
            f"{universal}"
        ),
    })

    agents.append({
        "id": "tail_risk_skeptic",
        "layer": 1,
        "name": "Tail Risk Skeptic (Taleb)",
        "style": "Hunts for hidden fragility, fat tails, and binary events causing permanent capital loss",
        "universe": tickers,
        "hard_constraint": (
            f"MUST identify the single most fragile stock with specific failure probability "
            f"every post. Vague skepticism = invalid post. {universal}"
        ),
        "system_prompt": (
            f"You are a tail risk specialist in the tradition of Nassim Taleb. "
            f"Universe: {ticker_list}. "
            f"Focus exclusively on asymmetric downside: what can go catastrophically wrong "
            f"that is NOT priced in? "
            f"MANDATORY each post: "
            f"(1) name the single most fragile stock in the universe, "
            f"(2) assign a specific failure probability (e.g., '40% chance of -70%+ drawdown'), "
            f"(3) describe the specific black swan / tail event. "
            f"Vague warnings ('this is risky') are INVALID. You must quantify. {universal}"
        ),
    })

    agents.append({
        "id": "quality_compounder",
        "layer": 1,
        "name": "Quality Compounder (Munger)",
        "style": "Pays fair price for wonderful businesses — ROIC, moat durability, flywheel economics",
        "universe": tickers,
        "hard_constraint": (
            f"MUST contrast highest-moat vs lowest-moat name in every post. {universal}"
        ),
        "system_prompt": (
            f"You are a quality-focused investor in the tradition of Charlie Munger. "
            f"Universe: {ticker_list}. "
            f"Single lens: would this business still be dominant in 10 years if a well-capitalized "
            f"competitor threw $10B at disrupting it? "
            f"Evaluate ROIC, switching costs, network effects, and scale economies. "
            f"MANDATORY each post: contrast the highest-moat and lowest-moat names in the universe — "
            f"explain specifically why the moats differ and what that means for 10-year holding returns. "
            f"{universal}"
        ),
    })

    agents.append({
        "id": "momentum_trader",
        "layer": 1,
        "name": "Momentum Trader (Darvas Box)",
        "style": "Price action, short interest, days-to-cover, catalyst-driven breakout setups",
        "universe": tickers,
        "hard_constraint": (
            f"MUST cite short% and days-to-cover for at least 2 names in every post. {universal}"
        ),
        "system_prompt": (
            f"You are a momentum and technical trader using Darvas box method and short squeeze analysis. "
            f"Universe: {ticker_list}. "
            f"Key inputs: (1) price action vs 52-week range, (2) short interest %, "
            f"(3) days-to-cover (short interest / avg daily volume), "
            f"(4) catalyst-driven breakout setups. "
            f"MANDATORY each post: cite short% and days-to-cover for at least 2 names. "
            f"Format: '[TICKER]: [X]% short interest, [Y] days-to-cover — "
            f"a positive catalyst creates mechanical short squeeze pressure.' "
            f"{universal}"
        ),
    })

    # ── Layer 2: Sector Specialists ──────────────────────────────────────────
    agents.append({
        "id": "biotech_specialist",
        "layer": 2,
        "name": "Biotech Specialist",
        "style": "FDA timelines, clinical trial design, approval probability, rare-disease franchise economics",
        "primary_coverage": biotech,
        "secondary_coverage": tickers,
        "hard_constraint": (
            f"Deep focus on {biotech_str}. May comment on all others. {universal}"
        ),
        "system_prompt": (
            f"You are a biotech specialist with deep expertise in FDA approval processes, "
            f"clinical trial endpoint design, and rare-disease franchise economics. "
            f"Primary coverage: {biotech_str}. "
            f"Core lens: FDA approval probability, trial design strength, "
            f"commercial launch trajectory, payer dynamics, and patent cliff timing. "
            f"You may comment on other names ({ticker_list}) but anchor every post "
            f"on the biotech pair. "
            f"You must explicitly compare biotech risk/reward against software and data alternatives. "
            f"{universal}"
        ),
    })

    agents.append({
        "id": "saas_specialist",
        "layer": 2,
        "name": "SaaS Specialist",
        "style": "NRR, Rule of 40, CAC/LTV, AI moat differentiation vs commoditization",
        "primary_coverage": saas,
        "secondary_coverage": tickers,
        "hard_constraint": (
            f"Deep focus on {saas_str}. May comment on all others. {universal}"
        ),
        "system_prompt": (
            f"You are a SaaS investor with deep expertise in enterprise software metrics. "
            f"Primary coverage: {saas_str}. "
            f"Core lens: Net Revenue Retention (NRR), Rule of 40 score (growth % + FCF margin %), "
            f"AI moat (does AI differentiate or commoditize the product?), "
            f"and CAC/LTV unit economics. "
            f"Key question you must address each post: "
            f"which name in the SaaS pair has more durable pricing power, and why? "
            f"You may comment on all other names but must anchor on the SaaS pair. "
            f"{universal}"
        ),
    })

    agents.append({
        "id": "data_ai_specialist",
        "layer": 2,
        "name": "Data/AI Infrastructure Specialist",
        "style": "Data flywheel strength, government vs commercial AI adoption, hyperscaler bundling threat",
        "primary_coverage": data_ai,
        "secondary_coverage": tickers,
        "hard_constraint": (
            f"Deep focus on {data_ai_str}. May comment on all others. {universal}"
        ),
        "system_prompt": (
            f"You are a data infrastructure and AI platform specialist. "
            f"Primary coverage: {data_ai_str}. "
            f"Core lens: data flywheel strength (does more usage create defensible moat?), "
            f"government vs commercial AI adoption rates, hyperscaler bundling threat, "
            f"and whether these are 'picks-and-shovels AI' (durable) or 'AI marketing' (hollow). "
            f"Core question each post: does more usage create a moat AWS/Azure can't replicate for free? "
            f"You may comment on all other names but must anchor on the data/AI pair. "
            f"{universal}"
        ),
    })

    # ── Layer 3: Adversarial Agents ──────────────────────────────────────────
    agents.append({
        "id": "short_seller",
        "layer": 3,
        "name": "Short Seller (Chanos / Muddy Waters)",
        "style": "Forensic skeptic: accounting irregularities, business model flaws, terminal risks",
        "universe": tickers,
        "hard_constraint": (
            f"MUST assign specific failure probability with stated reason for each stock "
            f"every round. Vague skepticism = invalid post. {universal}"
        ),
        "system_prompt": (
            f"You are a short-seller in the tradition of Chanos, Muddy Waters, and Hindenburg Research. "
            f"Universe: {ticker_list}. "
            f"Your job: find the most compelling short thesis for every name. "
            f"MANDATORY FORMAT per stock each round: "
            f"'[TICKER]: [X]% probability of [Y]%+ drawdown because [SPECIFIC REASON].' "
            f"Example: 'INSM: 45% probability of -70%+ drawdown because FDA filing depends on "
            f"trial data not yet published, and channel-stuffing risk makes the 229% YoY "
            f"revenue growth suspect.' "
            f"Vague statements like 'this is risky' are INVALID. Quantify and specify. {universal}"
        ),
    })

    agents.append({
        "id": "opportunity_cost_accountant",
        "layer": 3,
        "name": "Opportunity Cost Accountant",
        "style": "Never evaluates a stock in isolation — always compares to best available alternative",
        "universe": tickers,
        "hard_constraint": (
            f"MUST argue that at least one stock should be sold or cut for another in every post. "
            f"Must name the trade explicitly (e.g., 'Sell PLTR, add INSM'). {universal}"
        ),
        "system_prompt": (
            f"You are an opportunity cost analyst. "
            f"Universe: {ticker_list}. "
            f"Your job: identify capital misallocation relative to better alternatives in this universe. "
            f"MANDATORY each post: "
            f"(1) identify at least one stock that should be sold or reduced, "
            f"(2) name the explicit replacement trade with rationale "
            f"('Cut PLTR to 0, add INSM — better catalyst, better valuation, similar AI exposure'), "
            f"(3) quantify the expected improvement in risk-adjusted returns. "
            f"You never evaluate any stock in isolation — always relative to the best alternative. "
            f"{universal}"
        ),
    })

    agents.append({
        "id": "catalyst_skeptic",
        "layer": 3,
        "name": "Catalyst Skeptic",
        "style": "Stress-tests catalyst timelines — slipping catalysts destroy returns",
        "universe": tickers,
        "hard_constraint": (
            f"MUST challenge catalyst timeline credibility for at least 2 stocks with "
            f"specific reasons in every post. {universal}"
        ),
        "system_prompt": (
            f"You are a catalyst credibility analyst. "
            f"Universe: {ticker_list}. "
            f"Your job: stress-test whether cited catalysts will actually happen on stated timelines. "
            f"MANDATORY each post: challenge at least 2 catalysts from the universe. "
            f"Format: "
            f"'[TICKER] bull case requires [CATALYST] by [DATE] — but [SPECIFIC REASON this is optimistic]. "
            f"Realistic timeline: [DATE]. That means [X] months of dead money at minimum.' "
            f"Example: 'INSM bull case requires Brinsupri FDA approval by end-2026 — but FDA BLA review "
            f"requires 12 months after filing, and filing requires 6 months post-trial completion. "
            f"Realistic approval: Q2 2027. Stock is pricing 2026. That's 12 months of dead money.' "
            f"{universal}"
        ),
    })

    return agents


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 5 — DIRECTOR INJECTION SCHEDULE
# ═════════════════════════════════════════════════════════════════════════════

def _find_lowest_conviction_ticker(tickers: list, stocks: dict) -> str:
    """Return the ticker with the lowest panel score or conviction."""
    scored = []
    for t in tickers:
        e = stocks.get(t, {})
        val = e.get("score", e.get("conviction", 5))
        try:
            scored.append((t, int(val)))
        except (ValueError, TypeError):
            scored.append((t, 5))
    scored.sort(key=lambda x: x[1])
    return scored[0][0] if scored else tickers[0]


def build_director(tickers: list, clusters: dict, stocks: dict) -> dict:
    """
    Generate director injection schedule for 7 rounds.

    Round 3: macro shock — rates/risk-off
    Round 4: biotech sector shock — ambiguous FDA regulatory signal
    Round 5: SaaS sector shock — big tech bundles competing feature
    Round 6: ambiguous catalyst — lowest-conviction stock mystery 8-K
    Round 7: free run — final synthesis and forced-choice vote
    """
    biotech = clusters.get("biotech", tickers[:2])
    saas = clusters.get("saas", tickers[2:4])
    data_ai = clusters.get("data_ai", tickers[4:6])
    lowest = _find_lowest_conviction_ticker(tickers, stocks)

    biotech_str = " and ".join(biotech)
    saas_str = " and ".join(saas)
    data_ai_str = " and ".join(data_ai)

    injections = {
        "round_3": {
            "round": 3,
            "type": "macro_shock",
            "title": "Macro Shock: Federal Reserve 'Higher for Longer'",
            "injection": (
                "DIRECTOR INJECTION — ROUND 3\n\n"
                "Breaking: Federal Reserve signals 'higher for longer' after CPI prints at 3.8% YoY, "
                "above the 3.5% consensus. The 10-year Treasury yield spikes 25bps to 4.85%. "
                "Risk assets selling off broadly — S&P 500 futures -1.8%, Nasdaq futures -2.4%.\n\n"
                "DIFFERENTIAL IMPACT BY SECTOR:\n"
                f"• SOFTWARE/DATA-AI ({saas_str}, {data_ai_str}): NEGATIVE. "
                f"High-multiple growth stocks reprice first in rate-shock environments. "
                f"Multiple compression is most acute for unprofitable names and those trading >10x sales.\n"
                f"• BIOTECH ({biotech_str}): NEUTRAL-TO-NEGATIVE. "
                f"Biotech trades more on binary catalyst outcomes than rate sensitivity, "
                f"but speculative biotech bids are reduced in risk-off environments.\n\n"
                "ALL AGENTS: Update your rankings and Kelly sizes given this macro shock. "
                "Which names are most exposed to multiple compression? "
                "Which names are most insulated because their thesis is catalyst-driven rather than "
                "multiple-dependent? Explicit cross-universe comparison required."
            ),
            "differential_impact": {
                "biotech": "neutral",
                "saas": "negative — multiple compression risk",
                "data_ai": "negative — multiple compression risk",
            },
        },
        "round_4": {
            "round": 4,
            "type": "sector_shock_biotech",
            "title": f"Biotech Sector Shock: Ambiguous FDA Regulatory Signal ({biotech_str})",
            "injection": (
                f"DIRECTOR INJECTION — ROUND 4\n\n"
                f"FDA Center for Drug Evaluation and Research (CDER) issues a guidance memo "
                f"that could affect programs in the {biotech_str} cluster:\n\n"
                f"'The Agency is reviewing its evidentiary standards for accelerated approval "
                f"in chronic respiratory and cardiovascular rare diseases. Sponsors with ongoing "
                f"Phase 3 programs are encouraged to consult with their respective review divisions "
                f"regarding potential endpoint modifications before BLA submission.'\n\n"
                f"THIS SIGNAL IS DELIBERATELY AMBIGUOUS:\n"
                f"(A) BEARISH READ: FDA raising the bar for approval → pipeline timeline extends → "
                f"BLA submissions delayed 6-12 months → stock reprices downward.\n"
                f"(B) BULLISH READ: FDA clarifying accelerated pathway → faster review lanes → "
                f"potential for Priority Review designation → approval timeline shortens.\n\n"
                f"BIOTECH SPECIALIST + ALL AGENTS: What is your interpretation of this signal for "
                f"{biotech_str}? Update your approval probability estimates explicitly. "
                f"Is this a buying opportunity or a warning sign? You must take a definitive stance."
            ),
            "differential_impact": {
                "biotech": "ambiguous — agents must debate interpretation",
                "saas": "none",
                "data_ai": "none",
            },
        },
        "round_5": {
            "round": 5,
            "type": "sector_shock_saas",
            "title": f"SaaS Sector Shock: Big Tech Bundles Competing Feature ({saas_str})",
            "injection": (
                f"DIRECTOR INJECTION — ROUND 5\n\n"
                f"Microsoft announces at Ignite 2026 that Copilot Studio will now include "
                f"enterprise workflow orchestration and agentic process automation natively "
                f"bundled in Microsoft 365 Business Premium at no additional charge. "
                f"This directly competes with the core product thesis of at least one name "
                f"in the SaaS cluster.\n\n"
                f"Simultaneously, Google announces 'Marketing Intelligence Suite' — "
                f"a CDP and audience analytics platform bundled into Google Workspace Enterprise "
                f"at no incremental cost for customers spending >$50K/year on Google Ads.\n\n"
                f"AFFECTED CLUSTER: {saas_str}\n\n"
                f"SAAS SPECIALIST + ALL AGENTS: Does this announcement validate the bear thesis "
                f"that hyperscalers will bundle away the moat? Which name in the SaaS cluster "
                f"is more insulated, and why? "
                f"Update your moat assessments and position sizing. "
                f"Is this a buy-the-dip or a thesis-breaker?"
            ),
            "differential_impact": {
                "biotech": "none",
                "saas": "negative — direct competitive pressure, potential bear thesis validation",
                "data_ai": "mixed — Google announcement may signal data infrastructure competition",
            },
        },
        "round_6": {
            "round": 6,
            "type": "ambiguous_catalyst",
            "title": f"Ambiguous Catalyst: {lowest} Mystery Filing (Lowest-Conviction Stock)",
            "injection": (
                f"DIRECTOR INJECTION — ROUND 6\n\n"
                f"${lowest} files an unexpected 8-K with the following language:\n\n"
                f"'The Company is engaged in strategic discussions that may or may not result "
                f"in a material transaction or change to the Company's capital structure. "
                f"The Board has retained financial advisors. The Company does not intend to "
                f"make further public disclosures regarding this matter unless required by applicable law.'\n\n"
                f"Additionally, the CEO made an unscheduled appearance on a sell-side investor call "
                f"this morning, answering questions about 'strategic optionality' in unusually "
                f"guarded language.\n\n"
                f"THIS SIGNAL IS DELIBERATELY AMBIGUOUS — possible interpretations:\n"
                f"(A) Acquisition target — strategic buyer circling at premium (VERY BULLISH)\n"
                f"(B) Partnership / licensing deal — catalyst without dilution (NEUTRAL-TO-BULLISH)\n"
                f"(C) Dilutive capital raise — management selling the story at current prices (BEARISH)\n"
                f"(D) Material adverse change — company preparing bad news disclosure (VERY BEARISH)\n\n"
                f"ALL AGENTS: You must interpret this signal and update your ${lowest} position. "
                f"Sitting on the fence is NOT permitted — take a definitive stance and defend it. "
                f"How does this change the opportunity-cost calculus for the other five names?"
            ),
            "differential_impact": {
                lowest: "ambiguous — forced interpretation, potential forced ranking change",
            },
        },
        "round_7": {
            "round": 7,
            "type": "free_run",
            "title": "Free Run — Final Synthesis and Forced-Choice Vote",
            "injection": (
                "DIRECTOR INJECTION — ROUND 7\n\n"
                "No new macro or sector events this round. "
                "Operate on available information from all prior rounds.\n\n"
                "THIS IS THE FINAL SYNTHESIS ROUND. Each agent must:\n"
                "1. Declare their single highest-conviction long in the universe and defend it "
                "against all five counter-arguments\n"
                "2. Declare the one name they would NOT own at current prices\n"
                "3. Cast their vote in the forced-choice ranking market\n"
                "4. Update Kelly sizing based on the full arc of rounds 1-6\n\n"
                f"THE FORCED-CHOICE QUESTION: If you had to hold exactly ONE of these six stocks "
                f"({', '.join(tickers)}) for the next 12 months with zero ability to exit, "
                "which would you choose and why? "
                "Your answer must be defended against the five strongest counter-arguments "
                "from other names in the universe."
            ),
            "differential_impact": "synthesis round — all names reviewed",
        },
    }

    return {
        "total_rounds": 7,
        "injection_rounds": [3, 4, 5, 6],
        "free_rounds": [1, 2, 7],
        "lowest_conviction_ticker": lowest,
        "injections": injections,
    }


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 6 — OUTPUT WRITER
# ═════════════════════════════════════════════════════════════════════════════

def _build_readme(tickers: list, sim_dir: Path, manifest: dict) -> str:
    """Build human-readable README for the simulation package."""
    lines = [
        f"# MiroShark Simulation: {' | '.join(tickers)}",
        f"\nGenerated: {manifest['generated_at']}",
        f"Source report: `{Path(manifest['report_source']).name}`",
        "",
        "## What This Simulation Does",
        "",
        f"A 7-round prediction market simulation where **11 agents** debate a "
        f"**6-stock portfolio** ({', '.join(tickers)}).",
        "",
        "**Agent layers:**",
        "- Layer 1 (5 agents): Cross-stock investment style "
        "(Lynch/Fisher, Thorp/Kelly, Taleb, Munger, Darvas)",
        "- Layer 2 (3 agents): Sector specialists (Biotech, SaaS, Data/AI)",
        "- Layer 3 (3 agents): Adversarial (Short Seller, Opportunity Cost Accountant, Catalyst Skeptic)",
        "",
        "## Files",
        "",
        "| File | Purpose |",
        "|------|---------|",
        "| `seed.txt` | 4-section seed document — **read this first** before loading |",
        "| `markets.json` | 10 prediction markets (6 individual + 3 head-to-heads + 1 forced-choice) |",
        "| `agents.json` | 11 agent configs with constraints and system prompts |",
        "| `director.json` | Pre-planned event injections across rounds 3-6 |",
        "| `sim_manifest.json` | Machine-readable manifest with file paths and API endpoints |",
        "",
        "## How to Run",
        "",
        "1. **Review `seed.txt`** — verify Section 1 cross-stock sentences look correct",
        "   (these determine which graph edges NER creates — wrong sentences = missing edges)",
        "2. **Start MiroShark:**",
        f"   ```",
        f"   {manifest['miroshark_launch_command']}",
        f"   ```",
        "3. Load seed document → New Simulation → Upload `seed.txt`",
        "4. Import markets → Markets tab → Import → `markets.json`",
        "5. Import agents → Agents tab → Import → `agents.json`",
        "6. Import director schedule → Director tab → Import → `director.json`",
        "7. Start simulation → monitor round-by-round",
        "",
        "## Director Event Schedule",
        "",
        "| Round | Event | Affected Cluster |",
        "|-------|-------|-----------------|",
        "| 1–2 | Free run — agents establish baseline positions | All |",
        "| 3 | **Macro shock**: Fed 'higher for longer', rates spike | Software/Data-AI (negative) |",
        "| 4 | **Biotech shock**: Ambiguous FDA guidance memo | Biotech pair (debate forced) |",
        "| 5 | **SaaS shock**: MSFT + Google bundle competing features | SaaS pair (negative) |",
        "| 6 | **Mystery filing**: 8-K from lowest-conviction stock | Single stock (all interpret) |",
        "| 7 | Free run — final synthesis, forced-choice vote | All |",
        "",
        "## Key Agent Constraints",
        "",
        "All agents share one universal rule: **every post must reference at least one other "
        "ticker by symbol** — single-stock monologues are invalid.",
        "",
        "Layer 3 adversarial agents have hard quantification requirements:",
        "- Short Seller: must assign failure probability % with reason for each stock",
        "- Opportunity Cost Accountant: must name explicit sell/buy trade each round",
        "- Catalyst Skeptic: must challenge at least 2 catalyst timelines with specifics",
        "",
        "## Prediction Markets",
        "",
        f"- **6 individual** (50%+ return in 12 months): {', '.join(tickers)}",
        "- **3 head-to-heads** (one per sector cluster)",
        f"- **1 forced-choice** (pick the single winner from all 6)",
    ]
    return "\n".join(lines)


def write_sim_package(
    sim_dir: Path,
    seed: str,
    markets: list,
    agents: list,
    director: dict,
    tickers: list,
    report_path: Path,
) -> dict:
    """
    Write all generated files to the sim directory.

    Returns the sim manifest dict with all file paths and metadata.
    """
    sim_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "tickers": tickers,
        "report_source": str(report_path),
        "generated_at": datetime.now().isoformat(),
        "sim_dir": str(sim_dir),
        "miroshark_launch_command": (
            f"bash {Path.home()}/Documents/MiroShark/start-miroshark.sh"
        ),
        "miroshark_api_base": "http://localhost:5001",
        "load_instructions": {
            "step_1_seed": f"POST /api/seed — upload {sim_dir}/seed.txt",
            "step_2_markets": f"POST /api/markets — upload {sim_dir}/markets.json",
            "step_3_agents": f"POST /api/agents — upload {sim_dir}/agents.json",
            "step_4_director": f"POST /api/director — upload {sim_dir}/director.json",
            "step_5_start": "POST /api/simulation/start",
        },
        "files": {},
    }

    files = {
        "seed.txt": seed,
        "markets.json": json.dumps(markets, indent=2),
        "agents.json": json.dumps(agents, indent=2),
        "director.json": json.dumps(director, indent=2),
    }

    for filename, content in files.items():
        path = sim_dir / filename
        path.write_text(content, encoding="utf-8")
        manifest["files"][filename] = str(path)

    manifest_path = sim_dir / "sim_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["files"]["sim_manifest.json"] = str(manifest_path)

    readme = _build_readme(tickers, sim_dir, manifest)
    readme_path = sim_dir / "README.md"
    readme_path.write_text(readme, encoding="utf-8")
    manifest["files"]["README.md"] = str(readme_path)

    return manifest


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """Main entry point for oracle_to_sim.py."""
    parser = argparse.ArgumentParser(
        description="Bridge ORACLE Think Tank report → MiroShark simulation package",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--report",
        type=Path,
        metavar="PATH",
        help="Path to Think Tank markdown report (auto-detects latest if omitted)",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        metavar="TICKER",
        help="Override ticker list (e.g. --tickers INSM BBIO ZETA SNOW PLTR PATH)",
    )
    parser.add_argument(
        "--sim-name",
        metavar="NAME",
        help="Custom simulation name suffix (default: TICKER1_TICKER2_...)",
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Print MiroShark launch instructions after generating the package",
    )
    args = parser.parse_args()

    # ── Step 1: Locate report ────────────────────────────────────────────────
    if args.report:
        report_path = Path(args.report).expanduser()
        if not report_path.exists():
            print(f"ERROR: Report not found: {report_path}", file=sys.stderr)
            sys.exit(1)
    else:
        report_path = find_latest_report()
        if report_path is None:
            print(
                "ERROR: No ORACLE_*.md report found in ~/ORACLE/reports/\n"
                "Pass --report PATH to specify one explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"[AUTO] Using latest report: {report_path.name}")

    print(f"\n{'=' * 60}")
    print("ORACLE → MIROSHARK BRIDGE")
    print(f"Report : {report_path.name}")
    print(f"{'=' * 60}")

    # ── Step 2: Parse report ─────────────────────────────────────────────────
    print("\n[1/6] Parsing Think Tank report...")
    stocks = parse_report(report_path)
    print(f"       Found {len(stocks)} stock entries: {', '.join(sorted(stocks.keys()))}")

    # ── Step 3: Resolve ticker list ──────────────────────────────────────────
    if args.tickers:
        tickers = [t.upper().strip() for t in args.tickers]
        print(f"[2/6] Tickers (--tickers flag): {', '.join(tickers)}")
    else:
        # Prefer report content (LAYER 5 blocks) — more complete than filename
        report_tickers = list(stocks.keys())
        filename_tickers = parse_tickers_from_filename(report_path)
        if len(report_tickers) >= 4:
            tickers = report_tickers
        elif filename_tickers:
            tickers = filename_tickers
        else:
            tickers = report_tickers
        print(f"[2/6] Tickers (auto-detected from report): {', '.join(tickers)}")

    if len(tickers) < 2:
        print("ERROR: Need at least 2 tickers to build a simulation.", file=sys.stderr)
        sys.exit(1)

    # Ensure all tickers have an entry
    for t in tickers:
        stocks.setdefault(t, {"ticker": t})

    # ── Step 4: Load screener cache ──────────────────────────────────────────
    print("[3/6] Loading screener cache...")
    cache = load_screener_cache()
    if cache:
        print(f"       Cache contains {len(cache)} tickers")
        stocks = enrich_with_screener(stocks, cache)
        enriched = [t for t in tickers if t in cache]
        print(f"       Enriched from cache: {', '.join(enriched) or 'none'}")
    else:
        print("       No screener cache found — using report data only")

    # ── Step 5: Cluster stocks ───────────────────────────────────────────────
    print("[4/6] Clustering by sector...")
    clusters = cluster_stocks(stocks, tickers)
    for name, members in clusters.items():
        print(f"       {name.upper():10s}: {', '.join(members)}")

    # ── Step 6: Build simulation components ─────────────────────────────────
    print("\n[5/6] Building simulation package...")

    print("       Seed document...")
    seed = build_seed_document(stocks, clusters, tickers)

    print("       Prediction markets...")
    markets = build_markets(tickers, clusters, stocks)

    print("       Agent roster...")
    agents = build_agents(tickers, clusters)

    print("       Director injection schedule...")
    director = build_director(tickers, clusters, stocks)

    # ── Step 7: Write output files ───────────────────────────────────────────
    print("\n[6/6] Writing output files...")
    today = datetime.now().strftime("%Y%m%d")
    sim_suffix = args.sim_name if args.sim_name else "_".join(tickers[:6])
    sim_dir = SIMS_DIR / f"{today}_{sim_suffix}"

    manifest = write_sim_package(
        sim_dir, seed, markets, agents, director, tickers, report_path
    )

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("SIMULATION PACKAGE GENERATED")
    print(f"{'=' * 60}")
    print(f"Directory : {sim_dir}")
    print(f"Tickers   : {', '.join(tickers)}")
    print(f"")
    print("Files generated:")
    for fname, fpath in manifest["files"].items():
        size = Path(fpath).stat().st_size if Path(fpath).exists() else 0
        print(f"  {fname:<22}  {size:>8,} bytes")
    print(f"")
    print(f"Markets   : {len(markets)} prediction markets")
    print(f"Agents    : {len(agents)} agents in 3 layers")
    print(f"Injections: {len(director['injections'])} director events across rounds 3-6")

    # ── Launch instructions ──────────────────────────────────────────────────
    if args.launch:
        print(f"\n{'=' * 60}")
        print("MIROSHARK LAUNCH INSTRUCTIONS")
        print(f"{'=' * 60}")
        print(f"\n1. Review the seed document before loading:")
        print(f"   less {sim_dir}/seed.txt")
        print(f"\n   Check Section 1 cross-stock sentences — these drive NER graph edges.")
        print(f"\n2. Start MiroShark:")
        print(f"   {manifest['miroshark_launch_command']}")
        print(f"\n3. Load simulation files into MiroShark UI:")
        for step, instruction in manifest["load_instructions"].items():
            print(f"   [{step}] {instruction}")
        print(f"\n4. Start simulation and monitor round-by-round.")
        print(f"\nNOTE: Verify seed.txt looks correct before starting.")
        print(f"The cross-stock sentences in Section 1 determine which graph edges")
        print(f"NER creates. Wrong or missing sentences = missing cross-stock edges.")

    print(f"\nDone. Sim package at: {sim_dir}/")


if __name__ == "__main__":
    main()
