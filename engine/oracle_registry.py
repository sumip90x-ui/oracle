"""
oracle_registry.py — Persistent ticker analysis registry.

Stores previous run verdicts, dates, and conviction scores.
Prevents re-queuing tickers already analyzed in the discovery system.

Usage:
    from oracle_registry import registry_check, registry_record, registry_load
"""

import json
import datetime
from pathlib import Path

REGISTRY_PATH = Path.home() / "ORACLE" / "cache" / "ticker_registry.json"


def registry_load() -> dict:
    """Load the registry. Returns empty dict if not found."""
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text())
        except Exception:
            pass
    return {}


def registry_save(registry: dict) -> None:
    """Save the registry to disk."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, default=str))


def registry_record(
    ticker: str,
    verdict: str,       # "BUY" | "HOLD" | "PASS" | "AVOID"
    conviction: int,    # 1-10
    summary: str = "",  # one-line thesis summary
    run_date: str = ""
) -> None:
    """Record a completed ticker analysis into the registry."""
    reg = registry_load()
    reg[ticker.upper()] = {
        "verdict": verdict.upper(),
        "conviction": conviction,
        "summary": summary[:200],
        "run_date": run_date or datetime.date.today().isoformat(),
        "updated_at": datetime.datetime.now().isoformat()[:19],
    }
    registry_save(reg)
    print(f"  [REGISTRY] Recorded {ticker} — {verdict} {conviction}/10")


def registry_check(ticker: str) -> dict | None:
    """
    Check if ticker was previously analyzed.
    Returns the registry entry dict if found, None otherwise.
    """
    reg = registry_load()
    return reg.get(ticker.upper())


def registry_summary_line(ticker: str) -> str:
    """
    Return a one-line summary for use in discovery panels.
    E.g.: "SMCI previously analyzed — PASS 2/10 — run 2026-05-14"
    Returns empty string if not in registry.
    """
    entry = registry_check(ticker)
    if not entry:
        return ""
    return (
        f"{ticker.upper()} previously analyzed — "
        f"{entry['verdict']} {entry['conviction']}/10 — "
        f"run {entry['run_date']}"
        + (f" — {entry['summary']}" if entry.get("summary") else "")
    )


def registry_filter_discoveries(tickers: list[str]) -> tuple[list[str], list[str]]:
    """
    Split a list of discovery tickers into:
      - new: not in registry or analyzed >90 days ago
      - known: already in registry with recent run

    Returns (new_tickers, known_summaries_list)
    """
    new_tickers = []
    known_summaries = []
    cutoff = datetime.date.today() - datetime.timedelta(days=90)
    reg = registry_load()

    for ticker in tickers:
        t = ticker.upper()
        entry = reg.get(t)
        if not entry:
            new_tickers.append(t)
            continue
        try:
            run_dt = datetime.date.fromisoformat(entry["run_date"])
        except Exception:
            new_tickers.append(t)
            continue
        if run_dt < cutoff:
            new_tickers.append(t)  # stale — re-analyze
        else:
            known_summaries.append(registry_summary_line(t))

    return new_tickers, known_summaries


def registry_list_all() -> list[dict]:
    """Return all registry entries sorted by run_date descending."""
    reg = registry_load()
    entries = [{"ticker": k, **v} for k, v in reg.items()]
    return sorted(entries, key=lambda x: x.get("run_date", ""), reverse=True)
