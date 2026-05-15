#!/usr/bin/env python3
"""
populate_ciks.py -- Add CIK to every entry in ticker_names.json
Run once: python3 ~/ORACLE/scripts/populate_ciks.py
"""
import json, requests
from pathlib import Path

HEADERS = {"User-Agent": "ORACLE-Research oracle@research.local"}
names_path = Path.home() / "ORACLE" / "data" / "ticker_names.json"
known = json.loads(names_path.read_text())

print("Fetching EDGAR company tickers index...")
resp = requests.get(
    "https://www.sec.gov/files/company_tickers.json",
    headers=HEADERS, timeout=30
)
edgar_map = {}
if resp.status_code == 200:
    for entry in resp.json().values():
        t = entry.get("ticker", "").upper()
        edgar_map[t] = str(entry.get("cik_str", "")).zfill(10)
    print(f"Loaded {len(edgar_map)} tickers from EDGAR index")
else:
    print(f"EDGAR fetch failed: {resp.status_code}")

# Special cases for ticker changes / ambiguous symbols
special_cases = {
    "GAP": "0000039911",   # formerly GPS
    "IOT": "0001861795",   # Samsara
    "GEN": "0001748790",   # Gen Digital (formerly NortonLifeLock / Symantec)
    "GCT": "0001936502",   # GigaCloud Technology
    "APP": "0001468702",   # AppLovin
}

updated = 0
for ticker in list(known.keys()):
    entry = known[ticker]
    if isinstance(entry, dict) and entry.get("cik"):
        continue  # already has CIK

    cik = special_cases.get(ticker) or edgar_map.get(ticker)

    if cik:
        if isinstance(entry, str):
            known[ticker] = {"name": entry, "cik": cik, "source": "edgar_backfill"}
        else:
            known[ticker]["cik"] = cik
        updated += 1
        print(f"  {ticker}: CIK={cik}")
    else:
        print(f"  {ticker}: NOT FOUND -- manual entry needed")

names_path.write_text(json.dumps(known, indent=2))
print(f"\nUpdated {updated} entries. Total: {len(known)}")
