#!/usr/bin/env python3
"""
ORACLE Option 5 — Alpaca Drawdown Candidates
Methodology: scan Fidelity CSV for today's biggest dollar losers per account,
consolidate across all accounts, rank by total dollar lost today, return top N.
Same logic used in the manual trading session on 2026-05-12.
"""
import csv, os, sys, json
from collections import defaultdict

SKIP_SYMS = {
    'SPAXX','FZFXX','FDRXX','FCASH','SPAXX**','FZFXX**',
    'GLL','PSQ','SH','DOG','VIXY',
    'SGOL','VOO','QQQ','DIA','GLD','SCHD','VYM','HDV','IDV','DVY',
    'VIG','JEPI','JEPQ','DGRO','DIVO','SMH','BTC',
    'AMD','GOOGL','AMZN','NVDA','MU','MSFT','AAPL','META','AVGO','BRKB',
    'QBTS','RGTI','IONQ','QUBT',   # quantum hype — skip
    'AECOM',                        # Fidelity shows company abbrev, real ticker is ACM
}

def to_float(s):
    try:
        return float(s.strip().replace('$','').replace(',','').replace('+','').replace('%',''))
    except:
        return 0.0

def parse_csv(csv_path):
    """Parse Fidelity CSV, return {sym: today_gl_dollar} summed across all accounts."""
    sym_today = defaultdict(float)
    sym_accts = defaultdict(int)

    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        headers = None
        for row in reader:
            if not row or not row[0].strip(): continue
            if 'Account Name' in row[0] or (len(row)>1 and 'Account Name' in row[1]):
                headers = row; continue
            if headers is None: continue
            try:
                # Detect new vs old format
                if len(row)>3 and row[3].strip() and row[3].strip()[0].isalpha() and len(row[3].strip())<=6:
                    sym       = row[3].strip()
                    today_s   = row[8].strip() if len(row)>8 else ''
                else:
                    sym       = row[2].strip() if len(row)>2 else ''
                    today_s   = row[8].strip() if len(row)>8 else ''

                if not sym or sym in SKIP_SYMS or sym.startswith('*'): continue
                if len(sym) > 6: continue

                today_gl = to_float(today_s)
                if row[8].strip().startswith('-'): today_gl = -abs(today_gl)

                sym_today[sym] += today_gl
                sym_accts[sym] += 1
            except:
                continue

    return sym_today, sym_accts

def validate_tickers(tickers):
    """Drop any tickers yfinance can't find — avoids Think Tank errors."""
    import yfinance as yf
    valid = []
    for sym in tickers:
        try:
            info = yf.Ticker(sym).fast_info
            price = getattr(info, 'last_price', None) or getattr(info, 'regularMarketPrice', None)
            if price and float(price) > 0:
                valid.append(sym)
            else:
                print(f"  [skip] {sym} — no price data, dropping")
        except:
            print(f"  [skip] {sym} — yfinance error, dropping")
    return valid


def get_top_losers(csv_path, top_n=10):
    """Return top_n stocks with biggest $ loss today across all Fidelity accounts."""
    sym_today, sym_accts = parse_csv(csv_path)

    # Only stocks that are DOWN today
    losers = [(sym, gl) for sym, gl in sym_today.items() if gl < 0]

    # Sort by biggest loss (most negative first)
    losers.sort(key=lambda x: x[1])

    top = losers[:top_n]
    return top, sym_accts

def find_latest_csv():
    """Find the most recently downloaded Fidelity CSV."""
    import glob
    patterns = [
        os.path.expanduser("~/Downloads/Portfolio_Positions_*.csv"),
        os.path.expanduser("~/portfolio.csv"),
    ]
    candidates = []
    for pat in patterns:
        candidates.extend(glob.glob(pat))
    if not candidates:
        return None
    # Prefer (1) suffix (latest re-download), then most recent mtime
    one_suffix = [f for f in candidates if f.endswith('(1).csv')]
    if one_suffix:
        return sorted(one_suffix, key=os.path.getmtime, reverse=True)[0]
    return sorted(candidates, key=os.path.getmtime, reverse=True)[0]

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv',     default='', help='Path to Fidelity CSV')
    parser.add_argument('--top',     type=int, default=10, help='How many stocks to return')
    parser.add_argument('--json',    action='store_true', help='Output as JSON (for scripting)')
    args = parser.parse_args()

    csv_path = args.csv or find_latest_csv()
    if not csv_path or not os.path.exists(csv_path):
        print("ERROR: No Fidelity CSV found. Use --csv PATH or place it in ~/Downloads/")
        sys.exit(1)

    top, sym_accts = get_top_losers(csv_path, top_n=args.top * 2)  # fetch extra to account for drops

    # Validate — drop tickers yfinance can't find
    raw_syms = [sym for sym, _ in top]
    if args.json:
        # For JSON mode just filter known bad ones from SKIP_SYMS, no live check
        result = [{'sym': s, 'today_gl': round(gl, 2), 'accounts': sym_accts[s]}
                  for s, gl in top if s not in SKIP_SYMS][:args.top]
        print(json.dumps(result))
        sys.exit(0)

    print(f"\n  Scanning: {os.path.basename(csv_path)}")
    print(f"  Methodology: biggest $ losers today across all accounts")
    print(f"  Validating tickers against yfinance...\n")

    valid_syms = validate_tickers(raw_syms)
    # Re-filter top to only valid, preserve original order, cap at top_n
    top = [(sym, gl) for sym, gl in top if sym in valid_syms][:args.top]

    print(f"  {'#':<3} {'SYM':<8} {'TODAY_LOSS':>12}  {'ACCTS':>6}")
    print("  " + "-"*36)
    for i, (sym, gl) in enumerate(top, 1):
        print(f"  {i:<3} {sym:<8} ${gl:>10.2f}  {sym_accts[sym]:>6}")

    tickers = [sym for sym, _ in top]
    print(f"\n  Top {len(tickers)} tickers: {' '.join(tickers)}")
    print(f"\n  Think Tank candidates (triage order): {tickers}")
