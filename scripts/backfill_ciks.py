#!/usr/bin/env python3
"""
backfill_ciks.py — Pre-populate CIKs for all known tickers in ticker_names.json.

Uses EDGAR's company_tickers.json (authoritative ticker-to-CIK mapping).
Run once after deploying the CIK-in-ticker_names.json fix.
"""
import requests, json, time
from pathlib import Path

HEADERS = {"User-Agent": "ORACLE-Research oracle@research.local"}
names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"

print("Fetching EDGAR company tickers index...")
resp = requests.get(
    "https://www.sec.gov/files/company_tickers.json",
    headers=HEADERS,
    timeout=30,
)

cik_map = {}
if resp.status_code == 200:
    data = resp.json()
    for entry in data.values():
        ticker = entry.get("ticker", "").upper()
        cik = str(entry.get("cik_str", "")).zfill(10)
        if ticker:
            cik_map[ticker] = cik
    print(f"Loaded {len(cik_map)} tickers from EDGAR index")
else:
    print(f"EDGAR fetch failed: {resp.status_code}")
    raise SystemExit(1)

known = json.loads(names_path.read_text())
updated = 0

for ticker, entry in known.items():
    if isinstance(entry, dict) and entry.get("cik"):
        print(f"  {ticker}: already has CIK={entry['cik']} — skipping")
        continue

    cik = cik_map.get(ticker.upper())
    if cik:
        if isinstance(entry, dict):
            entry["cik"] = cik
        else:
            known[ticker] = {"name": entry, "cik": cik, "source": "edgar_backfill"}
        print(f"  {ticker}: CIK={cik}")
        updated += 1
    else:
        print(f"  {ticker}: NOT FOUND in EDGAR index")

names_path.write_text(json.dumps(known, indent=2))
print(f"\nUpdated {updated} tickers with CIK. Total entries: {len(known)}")
