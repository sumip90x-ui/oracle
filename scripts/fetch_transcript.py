#!/usr/bin/env python3
"""
fetch_transcript.py — Fetch earnings call transcript via browser automation.
Called by Hermes as a pre-task before running Think Tank reports.

Saves transcript to two locations:
  1. ~/ORACLE/cache/transcript_{TICKER}_{DATE}.json  — Think Tank reads this
  2. ~/Documents/Obsidian Vault/Trading/oracle-transcript-{ticker}-{quarter}.md — permanent

Usage:
    python3 ~/ORACLE/scripts/fetch_transcript.py TTEK
    python3 ~/ORACLE/scripts/fetch_transcript.py TTEK "Q1 FY2026"
"""

import sys
import json
import datetime
from pathlib import Path

ORACLE_DIR = Path.home() / "ORACLE"
CACHE_DIR = ORACLE_DIR / "cache"
OBSIDIAN_TRADING = Path.home() / "Documents" / "Obsidian Vault" / "Trading"


def save_transcript(ticker: str, transcript_text: str, source_url: str,
                    company_name: str = "", quarter: str = ""):
    """
    Save transcript to two locations:
    1. JSON cache at ~/ORACLE/cache/ — for Think Tank consumption
    2. Obsidian note at ~/Documents/Obsidian Vault/Trading/ — permanent knowledge base

    After saving, prints the Obsidian pointer string for Hermes memory.
    """
    ticker = ticker.upper()
    today = datetime.date.today().isoformat()
    quarter_label = quarter or today[:7]

    # Save 1: JSON cache (Think Tank reads this)
    cache_file = CACHE_DIR / f"transcript_{ticker}_{today}.json"
    note_rel_path = f"Trading/oracle-transcript-{ticker.lower()}-{quarter_label}.md"
    data = {
        "ticker": ticker,
        "company_name": company_name,
        "quarter": quarter_label,
        "date": today,
        "source": source_url,
        "transcript_text": transcript_text,
        "char_count": len(transcript_text),
        "fetched_at": datetime.datetime.now().isoformat(),
        "obsidian_note": note_rel_path
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, indent=2))
    print(f"  [CACHE] JSON saved: {cache_file}")

    # Save 2: Obsidian note (permanent, searchable, memory-pointer target)
    OBSIDIAN_TRADING.mkdir(parents=True, exist_ok=True)
    note_name = f"oracle-transcript-{ticker.lower()}-{quarter_label}.md"
    note_path = OBSIDIAN_TRADING / note_name

    markdown = f"""# {ticker} Earnings Call Transcript — {quarter_label}

**Company:** {company_name or ticker}
**Quarter:** {quarter_label}
**Fetched:** {today}
**Source:** {source_url}
**Characters:** {len(transcript_text):,}

---

## Key Context for ORACLE Panels

This transcript provides management commentary that must reach the Think Tank panels.
Critical sections: CEO opening remarks, CFO financial commentary, Q&A guidance statements.

---

## Full Transcript

{transcript_text}

---

*Written by ORACLE fetch_transcript.py — {datetime.datetime.now().isoformat()}*
*JSON cache: ~/ORACLE/cache/transcript_{ticker}_{today}.json*
"""

    note_path.write_text(markdown, encoding="utf-8")
    print(f"  [OBSIDIAN] Note saved: {note_path}")

    # Print memory pointer for Hermes
    print(f"")
    print(f"  HERMES MEMORY POINTER:")
    print(f"  {ticker} transcript {quarter_label}: Full context in Obsidian: {note_rel_path}")
    print(f"")

    return cache_file, note_path


def save_oracle_finding(filename: str, content: str, summary_for_memory: str = "") -> Path:
    """
    Save an ORACLE session finding to Obsidian Trading/ folder.

    Usage:
        save_oracle_finding(
            filename="oracle-xbrl-fix-2026-05-14.md",
            content=full_details_string,
            summary_for_memory="XBRL fix: Full context in Obsidian: Trading/oracle-xbrl-fix-2026-05-14.md"
        )

    Returns path to the written note.
    """
    OBSIDIAN_TRADING.mkdir(parents=True, exist_ok=True)
    note_path = OBSIDIAN_TRADING / filename
    note_path.write_text(content, encoding="utf-8")
    print(f"  [OBSIDIAN] Finding saved: {note_path}")

    if summary_for_memory:
        print(f"")
        print(f"  HERMES MEMORY ENTRY:")
        print(f"  {summary_for_memory}")
        print(f"")

    return note_path


def get_cached_transcript(ticker: str) -> dict:
    """Load today's cached transcript if available."""
    ticker = ticker.upper()
    today = datetime.date.today().isoformat()
    cache_file = CACHE_DIR / f"transcript_{ticker}_{today}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass
    return {}


if __name__ == "__main__":
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "TTEK"
    quarter = sys.argv[2] if len(sys.argv) > 2 else ""

    cached = get_cached_transcript(ticker)
    if cached:
        chars = cached.get("char_count", 0)
        source = cached.get("source", "unknown")
        obsidian = cached.get("obsidian_note", "not saved")
        print(f"Cached transcript found for {ticker}:")
        print(f"  Characters: {chars}")
        print(f"  Source: {source}")
        print(f"  Obsidian note: {obsidian}")
        if chars < 500:
            print(f"  WARNING: transcript too short ({chars} chars) — may be empty or failed fetch")
    else:
        print(f"No cached transcript for {ticker} today.")
        print(f"To fetch: ask Hermes to navigate to Motley Fool transcript page for {ticker}")
        print(f"  URL: https://www.fool.com/earnings/call-transcripts/?symbol={ticker}")
        print(f"After fetching, call save_transcript(ticker, text, url) to save to cache + Obsidian")
