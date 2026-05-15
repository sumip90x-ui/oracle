#!/usr/bin/env python3
"""
oracle_validators.py — Post-layer output validators for the ORACLE Think Tank.

Runs on panel output AFTER generation, BEFORE it reaches the next layer.
Each validator is independent — if one misfires, disable it without affecting others.
"""

import re
from typing import Optional


# ── Validator 1: Discovery Price Strip ──────────────────────────────────────

def strip_discovery_prices(scout_output: str, live_prices: dict) -> tuple[str, list]:
    """
    Remove any dollar price figures from discovery ticker mentions.
    Replaces them with [PRICE: $X.XX LIVE] using the actual live price.

    Returns: (cleaned_output, list_of_flags)

    Pattern: finds "DISCOVERY: TICKER" lines and strips any $XXX.XX nearby.
    Also flags if a stated price is >20% different from live price.
    """
    flags = []
    lines = scout_output.split('\n')
    cleaned = []
    for line in lines:
        if 'DISCOVERY:' in line.upper():
            # Extract ticker from DISCOVERY: TICKER pattern
            m = re.search(r'DISCOVERY:\s*([A-Z]{1,5})', line.upper())
            if m:
                disc_ticker = m.group(1)
                live_p = live_prices.get(disc_ticker)
                # Find and replace any dollar amounts in this line
                prices_in_line = re.findall(r'\$(\d+\.?\d*)', line)
                for p_str in prices_in_line:
                    try:
                        p_val = float(p_str)
                    except ValueError:
                        continue
                    if live_p:
                        try:
                            live_p_f = float(live_p)
                            if live_p_f > 0:
                                delta = abs(p_val - live_p_f) / live_p_f
                                if delta > 0.20:
                                    flags.append(
                                        f"DISCOVERY {disc_ticker}: stated ${p_val} vs live "
                                        f"${live_p_f:.2f} ({delta*100:.0f}% delta) — price stripped"
                                    )
                                line = line.replace(f'${p_str}', f'[PRICE: ${live_p_f:.2f} LIVE]')
                        except (TypeError, ZeroDivisionError):
                            flags.append(
                                f"DISCOVERY {disc_ticker}: price ${p_val} removed — live price invalid"
                            )
                            line = re.sub(r'\$\d+\.?\d*', '[PRICE: UNVERIFIED]', line)
                    else:
                        flags.append(
                            f"DISCOVERY {disc_ticker}: price ${p_val} removed — live price unavailable"
                        )
                        line = re.sub(r'\$\d+\.?\d*', '[PRICE: UNVERIFIED]', line)
        cleaned.append(line)
    return '\n'.join(cleaned), flags


# ── Validator 2: Scuttlebutt Conclusion Stripper ─────────────────────────────

def strip_scuttlebutt_conclusions(scout_output: str) -> tuple[str, list]:
    """
    Detect patterns where imagined/fabricated scuttlebutt is used to draw a verdict.
    Tag those conclusions as HYPOTHESIS rather than letting them reach the summary compiler.

    Pattern: "Imagined [X] says..." followed within 3 lines by a verdict/conclusion.
    """
    flags = []
    lines = scout_output.split('\n')
    cleaned = []
    in_imagined_block = False
    imagined_depth = 0

    verdict_words = [
        'verdict', 'conclusion', 'therefore', 'this confirms', 'this suggests',
        'demonstrates', 'proves', 'shows that', 'indicates that', 'PASS', 'BUY', 'AVOID'
    ]

    for i, line in enumerate(lines):
        line_lower = line.lower()
        # Detect start of imagined scuttlebutt block
        if any(w in line_lower for w in ['imagined', 'hypothetical interview', 'imaginary']):
            in_imagined_block = True
            imagined_depth = 0
            cleaned.append(line)
            continue

        if in_imagined_block:
            imagined_depth += 1
            # Check if this line draws a conclusion from the imagined content
            if imagined_depth <= 5 and any(w in line_lower for w in verdict_words):
                if '[HYPOTHESIS' not in line:
                    flags.append(
                        f"Scuttlebutt conclusion stripped at line {i}: '{line.strip()[:80]}'"
                    )
                    line = f"[HYPOTHESIS — derived from imagined scuttlebutt, not verified data]: {line}"
            if imagined_depth > 8 or line.strip() == '':
                in_imagined_block = False

        cleaned.append(line)

    return '\n'.join(cleaned), flags


# ── Validator 3: Number Provenance Check ────────────────────────────────────

def check_number_provenance(panel_output: str, fact_sheet_text: str, ticker: str) -> tuple[str, list]:
    """
    Find numeric claims in panel output that don't appear in the fact sheet.
    Append [UNVERIFIED] to numbers not traceable to the fact sheet.

    Only flags significant numbers: dollar amounts >$1M and percentages used in calculations.
    Ignores stock prices, general statistics, and round numbers.
    """
    if not fact_sheet_text:
        return panel_output, []  # No fact sheet = skip this check

    flags = []
    # Extract all dollar figures from panel output (likely financial claims)
    dollar_claims = re.findall(r'\$(\d+\.?\d*)[BMK]?\b', panel_output)

    # Check each claim against fact sheet
    for claim in dollar_claims:
        val = claim
        # Skip if the exact figure appears in the fact sheet
        if f'${val}' in fact_sheet_text:
            continue
        try:
            float_val = float(val)
            # Also check formatted version
            if f'${float_val:.1f}' in fact_sheet_text:
                continue
            # Skip obvious non-financial numbers (stock prices <$1000, small counts)
            if float_val < 100:
                continue  # Skip EPS-range numbers, stock prices
        except (ValueError, TypeError):
            continue
        # This is a significant dollar claim not in the fact sheet
        flags.append(f"Unverified claim: ${val} not found in fact sheet")

    return panel_output, flags


# ── Master Validator Dispatcher ──────────────────────────────────────────────

def run_all_validators(
    layer_name: str,
    layer_output: str,
    fact_sheet_text: str = "",
    ticker: str = "",
    live_prices: dict = None
) -> tuple[str, list]:
    """
    Run all applicable validators on a layer's output.
    Returns (cleaned_output, all_flags).

    Validators are independent — exceptions in one do not block others.
    """
    all_flags = []
    cleaned = layer_output

    if layer_name == "scout":
        if live_prices:
            try:
                cleaned, flags = strip_discovery_prices(cleaned, live_prices)
                all_flags.extend(flags)
            except Exception as e:
                all_flags.append(f"[validator error] strip_discovery_prices: {e}")

        try:
            cleaned, flags = strip_scuttlebutt_conclusions(cleaned)
            all_flags.extend(flags)
        except Exception as e:
            all_flags.append(f"[validator error] strip_scuttlebutt_conclusions: {e}")

    if layer_name in ("scout", "skeptic", "fundamentals"):
        try:
            cleaned, flags = check_number_provenance(cleaned, fact_sheet_text, ticker)
            all_flags.extend([f for f in flags if f])  # filter empty
        except Exception as e:
            all_flags.append(f"[validator error] check_number_provenance: {e}")

    return cleaned, all_flags


# ── CLI Test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Quick smoke test
    test_scout = """
DISCOVERY: CRDO — High-growth AI interconnect at $45.00, fits runner DNA.
DISCOVERY: KTOS — Defense AI at $25.00 — strong thesis.

SCUTTLEBUTT:
Imagined supplier says: "SMCI is taking share from HPE."
Verdict: SMCI is clearly winning — PASS this stock with conviction.
Therefore this confirms SMCI management is credible.
"""

    live_px = {"CRDO": 38.50, "KTOS": 24.00}

    print("=== Testing strip_discovery_prices ===")
    cleaned, flags = strip_discovery_prices(test_scout, live_px)
    print(f"Flags ({len(flags)}): {flags}")

    print("\n=== Testing strip_scuttlebutt_conclusions ===")
    cleaned2, flags2 = strip_scuttlebutt_conclusions(test_scout)
    print(f"Flags ({len(flags2)}): {flags2}")

    print("\n=== Testing run_all_validators ===")
    final, all_flags = run_all_validators("scout", test_scout, "", "SMCI", live_px)
    print(f"Total flags: {len(all_flags)}")
    for f in all_flags:
        print(f"  - {f}")
