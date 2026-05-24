#!/usr/bin/env python3
"""
oracle_v2_seed_formatter.py

Takes Claude's fundamental analysis (pasted as text or file)
and formats it as a MiroFish seed + text prompt with
embedded EDGAR verification requirements for each agent.

Usage:
  python3 oracle_v2_seed_formatter.py --ticker SNOW --interactive
  python3 oracle_v2_seed_formatter.py --ticker SNOW --input snow_analysis.txt
  xclip -o | python3 oracle_v2_seed_formatter.py --ticker SNOW --stdin
"""

import argparse
import sys
import json
import datetime
import re
from pathlib import Path

ORACLE_DIR = Path.home() / "ORACLE"
SEEDS_DIR = ORACLE_DIR / "mirofish_seeds"
SEEDS_DIR.mkdir(parents=True, exist_ok=True)


def read_claude_analysis(args) -> str:
    if args.stdin:
        print("Reading from stdin...")
        return sys.stdin.read()
    if args.input:
        path = Path(args.input)
        if not path.exists():
            print(f"File not found: {args.input}")
            sys.exit(1)
        return path.read_text()
    if args.interactive:
        print(f"\nPaste Claude's fundamental analysis for {args.ticker}")
        print("Press ENTER twice then Ctrl+D when done:")
        print("-" * 60)
        lines = []
        try:
            while True:
                line = input()
                lines.append(line)
        except EOFError:
            pass
        return "\n".join(lines)
    print("Specify --interactive, --input FILE, or --stdin")
    sys.exit(1)


def extract_key_claims(analysis_text: str, ticker: str) -> dict:
    claims = {
        "revenue_figures": [],
        "eps_figures": [],
        "growth_rates": [],
        "price_targets": [],
        "rating": None,
        "conviction": None,
        "key_metrics": [],
        "risks": [],
        "catalysts": [],
    }
    revenue_pattern = r'\$(\d+\.?\d*)\s*(?:billion|B)\s*(?:in\s+)?(?:revenue|TTM|annual)'
    for match in re.finditer(revenue_pattern, analysis_text, re.IGNORECASE):
        claims["revenue_figures"].append(f"${match.group(1)}B revenue")
    eps_pattern = r'EPS.*?\$(\d+\.?\d+)|(\$\d+\.?\d+).*?(?:per share|EPS)'
    for match in re.finditer(eps_pattern, analysis_text, re.IGNORECASE):
        val = match.group(1) or match.group(2)
        if val:
            claims["eps_figures"].append(f"${val} EPS")
    growth_pattern = r'(\d+)%\s*(?:year.over.year|YoY|growth|increase)'
    for match in re.finditer(growth_pattern, analysis_text, re.IGNORECASE):
        claims["growth_rates"].append(f"{match.group(1)}% growth")
    rating_match = re.search(
        r'(?:Rating|Verdict):\s*(BUY|SELL|HOLD|PASS|WATCH|INVESTIGATE|STRONG BUY)',
        analysis_text, re.IGNORECASE
    )
    if rating_match:
        claims["rating"] = rating_match.group(1).upper()
    conviction_match = re.search(r'[Cc]onviction[:\s]+(\d+)/10', analysis_text)
    if conviction_match:
        claims["conviction"] = f"{conviction_match.group(1)}/10"
    metric_patterns = [
        r'NRR.*?(\d+)%',
        r'RPO.*?\$(\d+\.?\d*)\s*(?:billion|B)',
        r'AISC.*?\$(\d+)',
        r'(\d+)%\s*(?:gross margin|operating margin)',
    ]
    for pattern in metric_patterns:
        for match in re.finditer(pattern, analysis_text, re.IGNORECASE):
            claims["key_metrics"].append(match.group(0)[:80])
    return claims


def _get_mode_guidance(valuation_mode: str) -> str:
    guidance = {
        "platform_compounder": (
            "PRIMARY METRICS: NRR, RPO growth, ARPU trajectory, platform asset growth, attachment rates.\n"
            "DO NOT use EPV as primary valuation — it assumes zero growth.\n"
            "Focus on flywheel mechanics: does scale benefit customers?"
        ),
        "commodity_producer": (
            "PRIMARY METRICS: NAV, P/NAV at current commodity price, AISC margin, reserve life, jurisdiction risk.\n"
            "ALL margin scenarios use current commodity spot price.\n"
            "Verify commodity price independently before any calculation."
        ),
        "mature_stalwart": (
            "PRIMARY METRICS: EPV, earnings yield, margin of safety.\n"
            "Require 30%+ discount to intrinsic value.\n"
            "Compare earnings yield to current risk-free rate (T-bill)."
        ),
        "inflection_stage": (
            "PRIMARY METRICS: Probability of catalyst success, asymmetry of outcomes, capital runway, catalyst timing.\n"
            "Binary event risk dominates. Size position for survivable loss."
        ),
        "defense_government_services": (
            "PRIMARY METRICS: Backlog coverage ratio, book-to-burn, contract duration, regulatory risk (already priced).\n"
            "DOGE/budget cuts: assess mission-criticality of contracts."
        ),
        "cyclical_recovery": (
            "PRIMARY METRICS: Normalized mid-cycle earnings, cycle position, balance sheet to survive trough, ROIC at mid-cycle.\n"
            "Do NOT use trough earnings for EPV — use normalized figures."
        ),
    }
    return guidance.get(valuation_mode, "Apply the analytical framework appropriate for this company type.")


def build_verification_requirements(ticker: str, claims: dict, analysis_text: str) -> str:
    today = datetime.date.today().isoformat()
    verifications = []
    if claims["revenue_figures"]:
        rev = claims["revenue_figures"][0]
        verifications.append(
            f"VERIFY: {rev} — Search SEC EDGAR: "
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22+%22revenue%22"
            f"&dateRange=custom&startdt=2025-01-01&enddt={today}"
        )
    if claims["growth_rates"]:
        rate = claims["growth_rates"][0]
        verifications.append(f"VERIFY: {rate} — Search: \"{ticker} revenue growth 2026 SEC filing\"")
    for metric in claims["key_metrics"][:3]:
        verifications.append(f"VERIFY: {metric[:60]} — Search: \"{ticker} {metric[:30]} 2026\"")
    verifications.append(f"VERIFY competitive position — Search: \"{ticker} competitors market share 2026\"")
    verifications.append(f"VERIFY recent developments — Search: \"{ticker} news May 2026\"")
    verifications.append(
        f"VERIFY insider activity — Search SEC EDGAR Form 4: "
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=4&dateb=&owner=include&count=10"
    )
    result = "\n## AGENT VERIFICATION REQUIREMENTS\n"
    result += "Each agent MUST verify at least ONE of the following before forming their position:\n\n"
    for i, v in enumerate(verifications, 1):
        result += f"{i}. {v}\n"
    result += """
## AGENT POSTING RULES
- If your search CONFIRMS a claim: state "EDGAR/Web confirms: [claim]"
- If your search CONTRADICTS a claim: state "CONTRADICTION FOUND: [detail]" and adjust your position
- If your search is inconclusive: state "Unverified: [claim]" and weight at 50%
- Posts without search citation are flagged LOW CONFIDENCE and weighted 50% in prediction market

## WHAT TO CHALLENGE
The analysis above is Claude's output — treat it as a well-researched starting position, not ground truth.
Specifically challenge:
- Revenue and EPS figures (verify against SEC EDGAR)
- Growth rate claims (check against most recent quarterly filings)
- Competitive position claims (search for counter-evidence)
- Risk assessments (are there risks Claude missed?)
- Catalyst timing (verify exact dates against company announcements)
"""
    return result


def format_as_mirofish_seed(ticker: str, claude_analysis: str, claims: dict, valuation_mode: str) -> str:
    today = datetime.date.today().isoformat()
    rating = claims.get("rating", "UNSPECIFIED")
    conviction = claims.get("conviction", "?/10")
    verification_section = build_verification_requirements(ticker, claims, claude_analysis)
    seed = f"""# ORACLE V2 INVESTMENT SEED: {ticker}
## Source: Claude Fundamental Analysis | Date: {today}
## Claude Rating: {rating} | Conviction: {conviction}

---

## IMPORTANT: HOW TO USE THIS SEED

This seed contains Claude's fundamental analysis of {ticker}.
Your job as a simulation agent is NOT to accept this uncritically.
Your job is to:
1. Read Claude's analysis carefully
2. Search EDGAR and the web to verify the key claims
3. Form your own position based on what you confirm or contradict
4. Debate other agents based on evidence you found

The prediction market reflects the collective verdict after verification.

---

## VALUATION MODE: {valuation_mode.upper().replace('_', ' ')}

{_get_mode_guidance(valuation_mode)}

---

## CLAUDE'S FUNDAMENTAL ANALYSIS
*(Starting position — verify before accepting)*

{claude_analysis}

---

{verification_section}

---
*Seed generated by ORACLE V2 oracle_v2_seed_formatter.py on {today}*
"""
    return seed


def build_text_prompt(ticker: str, claims: dict, claude_analysis: str, valuation_mode: str) -> str:
    rating = claims.get("rating", "WATCH")
    conviction = claims.get("conviction", "?/10")
    prompt = f"""ORACLE V2 SIMULATION: {ticker}

CONTEXT:
Claude's fundamental analysis rates {ticker} as {rating} ({conviction}).
You have received Claude's full analysis as the seed document.

YOUR MISSION:
1. Search EDGAR and web to verify Claude's key claims
2. Challenge any claim you find contradicting evidence for
3. Form your OWN position based on verified evidence
4. Debate to consensus

THE CORE QUESTION:
Does the evidence — as YOU have verified it — support Claude's {rating} rating?
Or does your independent research lead to a different conclusion?

WHAT MATTERS:
- Evidence you found beats claims you haven't verified
- EDGAR data beats analyst estimates
- Recent news beats stale data
- Specific numbers beat general impressions

PREDICTION MARKET:
Starts at 50/50 (BULL/BEAR). Your trades move it.
>0.65 = BUY confirmed
0.50-0.65 = WATCH/HOLD
0.35-0.50 = PASS
<0.35 = ELIMINATE / Claude was wrong

VALUATION FRAMEWORK: {valuation_mode.upper().replace('_', ' ')}
{_get_mode_guidance(valuation_mode)}
"""
    return prompt


def get_valuation_mode_from_text(analysis_text: str, ticker: str) -> str:
    text_lower = analysis_text.lower()
    platform_keywords = ["net revenue retention", "nrr", "remaining performance obligations",
                          "rpo", "arpu", "platform assets", "flywheel", "network effects",
                          "monthly recurring", "arr", "subscription revenue"]
    commodity_keywords = ["aisc", "all-in sustaining", "gold", "silver", "copper",
                           "oil", "barrel", "ounce", "mine", "mining", "reserves",
                           "xauusd", "xagusd"]
    defense_keywords = ["backlog", "book-to-burn", "government contract", "doge",
                         "pentagon", "defense", "federal contract", "cost-plus"]
    biotech_keywords = ["fda", "clinical trial", "phase", "approval", "pdufa",
                         "binary", "catalyst", "pipeline"]
    scores = {
        "platform_compounder": sum(1 for kw in platform_keywords if kw in text_lower),
        "commodity_producer": sum(1 for kw in commodity_keywords if kw in text_lower),
        "defense_government_services": sum(1 for kw in defense_keywords if kw in text_lower),
        "inflection_stage": sum(1 for kw in biotech_keywords if kw in text_lower),
    }
    best_mode = max(scores, key=scores.get)
    if scores[best_mode] < 2:
        return "mature_stalwart"
    return best_mode


def main():
    parser = argparse.ArgumentParser(description="ORACLE V2: Format Claude's analysis as MiroFish seed + prompt")
    parser.add_argument("--ticker", required=True, help="Stock ticker (e.g. SNOW)")
    parser.add_argument("--input", help="Path to file containing Claude's analysis")
    parser.add_argument("--interactive", action="store_true", help="Paste Claude's analysis interactively")
    parser.add_argument("--stdin", action="store_true", help="Read Claude's analysis from stdin")
    parser.add_argument("--mode", choices=["platform_compounder", "commodity_producer", "mature_stalwart",
                                            "inflection_stage", "defense_government_services", "cyclical_recovery"],
                        help="Override valuation mode (auto-detected if not specified)")
    parser.add_argument("--output-dir", default=str(SEEDS_DIR), help="Directory to write seed and prompt files")
    args = parser.parse_args()
    ticker = args.ticker.upper()

    print(f"\n[1/4] Reading Claude's analysis for {ticker}...")
    claude_analysis = read_claude_analysis(args)
    if len(claude_analysis) < 200:
        print("Analysis too short — paste the full Claude output")
        sys.exit(1)
    print(f"  Read {len(claude_analysis)} characters")

    print(f"[2/4] Extracting verifiable claims...")
    claims = extract_key_claims(claude_analysis, ticker)
    print(f"  Revenue figures: {len(claims['revenue_figures'])}")
    print(f"  Growth rates: {len(claims['growth_rates'])}")
    print(f"  Key metrics: {len(claims['key_metrics'])}")
    print(f"  Rating detected: {claims.get('rating', 'not found')}")
    print(f"  Conviction: {claims.get('conviction', 'not found')}")

    if args.mode:
        valuation_mode = args.mode
        print(f"[3/4] Valuation mode: {valuation_mode} (manual override)")
    else:
        valuation_mode = get_valuation_mode_from_text(claude_analysis, ticker)
        print(f"[3/4] Valuation mode: {valuation_mode} (auto-detected)")

    print(f"[4/4] Formatting MiroFish seed and prompt...")
    seed = format_as_mirofish_seed(ticker, claude_analysis, claims, valuation_mode)
    prompt = build_text_prompt(ticker, claims, claude_analysis, valuation_mode)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    seed_path = output_dir / f"{ticker}_{today}_seed.md"
    prompt_path = output_dir / f"{ticker}_{today}_prompt.txt"
    seed_path.write_text(seed)
    prompt_path.write_text(prompt)

    # Also write to MiroFish seed directory if it exists
    mirofish_seed_dir = Path.home() / "Documents" / "MiroFish" / "data" / "seed"
    if mirofish_seed_dir.exists():
        mf_seed_path = mirofish_seed_dir / f"oracle_{ticker}.md"
        mf_seed_path.write_text(seed)
        print(f"\n  Seed → MiroFish: {mf_seed_path}")

    print(f"\n{'='*60}")
    print(f"ORACLE V2 SEED READY: {ticker}")
    print(f"{'='*60}")
    print(f"  Valuation Mode: {valuation_mode}")
    print(f"  Claude Rating:  {claims.get('rating', '?')}")
    print(f"  Conviction:     {claims.get('conviction', '?')}")
    print(f"  Seed:   {seed_path}")
    print(f"  Prompt: {prompt_path}")
    print(f"\nNEXT STEPS:")
    print(f"  1. Open MiroFish at http://localhost:3000")
    print(f"  2. Load seed: {seed_path}")
    print(f"  3. Use prompt: {prompt_path}")
    print(f"  4. Run simulation")
    print(f"  5. When complete: python3 ~/ORACLE/scripts/oracle_v2_combine.py --ticker {ticker}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
