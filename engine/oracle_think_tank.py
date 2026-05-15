#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ORACLE Think Tank v3 - Composite Panel (6 calls)
=================================================
29 investor lenses collapsed into 6 composite calls.
Call 3 split into 3a (Fundamentals) + 3b (Tech+Macro) for full token breathing room.

~$0.06-0.12 (haiku) | ~$0.55-1.00 (sonnet) per run.

Call 1  - SCOUT:        Fisher + Lynch + Li Lu + Thiel
Call 2  - SKEPTIC:      Burry + Chanos + Block + Tilson + Greenberg
Call 3a - FUNDAMENTALS: Greenblatt + Pabrai + Klarman + Greenwald + Mauboussin
                         + Druckenmiller + Miller + Einhorn   (8 investors)
Call 3b - TECH + MACRO: Wood + Kessler + Christensen
                         + Marks + Soros + Dalio + Rogers     (7 investors)
Call 4  - VERDICT:      Munger + Thorp + Sleep + Taleb + Annie Duke (synthesis)

Usage:
  python3 ~/oracle_think_tank.py --stocks NTLA GERN VCEL PGEN
  python3 ~/oracle_think_tank.py --stocks SMCI AEHR FORM VCEL --fast
  python3 ~/oracle_think_tank.py --stocks NTLA GERN --deep   # full 29 separate calls
"""

import os, sys, json, re, datetime, argparse, requests, time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.hermes/.env"), override=True)
OR_KEY  = os.environ.get("OPENROUTER_API_KEY", "")

# ── Data layer (Phase 0) — single source of truth for all market data ──────
sys.path.insert(0, os.path.expanduser("~/ORACLE"))
try:
    from data.oracle_data import (
        format_fundamentals_batch,
        validate_price_vs_screener,
        check_problem_stock_news,
        get_fundamentals,
        get_fundamentals_batch,
    )
    _HAS_DATA_LAYER = True
except Exception as _dl_err:
    _HAS_DATA_LAYER = False
    def format_fundamentals_batch(t, fresh=False): return ""   # noqa: E301
    def validate_price_vs_screener(*a, **k): return True       # noqa: E301
    def check_problem_stock_news(*a, **k): return ""           # noqa: E301

# ── Brain memory (graceful fallback if module missing) ─────────────────────
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from oracle_brain import read_brain_context, parse_run_for_brain, append_to_brain
    _HAS_BRAIN = True
except Exception as _brain_err:
    _HAS_BRAIN = False
    def read_brain_context(t): return ""        # noqa: E301
    def parse_run_for_brain(*a, **k): return []  # noqa: E301
    def append_to_brain(*a, **k): pass           # noqa: E301

# ── Pre-flight data validation (graceful fallback if module missing) ────────
try:
    from engine.oracle_preflight import run_preflight, build_preflight_header, load_preflight_cache
    HAS_PREFLIGHT = True
except ImportError:
    HAS_PREFLIGHT = False
    def run_preflight(t, verbose=True): return {}       # noqa: E301
    def build_preflight_header(t): return ""            # noqa: E301
    def load_preflight_cache(t): return {}              # noqa: E301

SONNET  = "anthropic/claude-sonnet-4.5"
HAIKU   = "anthropic/claude-3.5-haiku"
SEARCH  = "anthropic/claude-3.5-haiku:online"
OUT_DIR              = os.path.expanduser("~/Documents/Trading Vault/03_Stock_Analysis/ORACLE")
OBSIDIAN_RUNS_DIR    = os.path.join(OUT_DIR, "runs")
OBSIDIAN_TICKERS_DIR = os.path.join(OUT_DIR, "tickers")

# ── Runner DNA - what the legends looked like before they ran ──────────────
RUNNER_DNA = """
CONFIRMED 10x-1000x RUNNER DNA - what these looked like BEFORE the run:
AMD  ($2→$455, 227x): Near-bankrupt + new CEO Lisa Su + Ryzen pivot + AI GPU.
     Revenue turned up first. Market hated it. 34% short interest at the bottom.
MU   (10x): Commodity cycle trough + AI HBM demand nobody priced + earnings inflection.
     Looked like a dying commodity business. Was actually AI infrastructure.
SNDK (650% in 12mo): Western Digital spinoff + NAND shortage + AI storage.
     "Boring" commodity hardware. Ignored by growth investors. Cheap on assets.
LITE (massive): "Boring" optical telecom components → AI data center interconnect.
     Revenue already there. Thesis changed overnight when hyperscalers needed optics.
LXRX (10x): Near-bankrupt pharma + surprise FDA approval + Novo Nordisk partnership.
     Everyone had written it off. Single binary event flipped it.
NVDA (26x): CUDA platform + transformer model explosion. Nobody priced the platform.
INTC (+559% MISSED): Beaten down large-cap + AI foundry pivot + CHIPS Act subsidy.
     Pattern was visible. Did not act. Lesson: never dismiss beaten-down large-cap
     with credible pivot + government backing.

PATTERN: beaten down + revenue inflecting + EPS improving + catalyst not yet priced
         + market embarrassed to own it + hot sector secular tailwind
"""

# ══════════════════════════════════════════════════════════════════
# COMPOSITE ANALYST SYSTEM PROMPTS
# ══════════════════════════════════════════════════════════════════

SCOUT_SYSTEM = """You are a composite investment scout combining the methodologies of four legendary investors:

PHILIP FISHER (Scuttlebutt):
- Talks to suppliers, customers, competitors, ex-employees BEFORE looking at a spreadsheet
- 15-point checklist: Does management have a plan to grow after current products peak?
- Holds forever if business keeps reinvesting well
- First question: "What do the competitors think of this company?"

PETER LYNCH (Napkin Test):
- Boring industry + hated by Wall Street + explainable to his kids = tenbagger setup
- 6 categories: slow growers, stalwarts, fast growers, cyclicals, turnarounds, asset plays
- PEG ratio: P/E divided by growth rate. Under 1.0 = cheap. Over 2.0 = expensive.
- "Never invest in any idea you cannot illustrate with a crayon"

LI LU (Duration Lens):
- Circle of competence absolutist - refuses any business he cannot understand as well as the owner
- Caught MU early but sold before AI HBM supercycle. HARD-WIRED LESSON: always ask
  "What is the duration of this thesis? Am I underestimating the tailwind length?"
- Framework: Is the moat deepening or decaying over a decade?

PETER THIEL (Secret Hunter):
- "What does almost nobody else believe yet that is provably true?"
- Looks for: proprietary technology (10x better not 2x), network effects, scale economies
- If everyone agrees, there is no secret, therefore no edge
- Contrarian by design: the best opportunities are ones the market is embarrassed to own

YOUR JOB IN THIS ANALYSIS:
1. Assign each stock a Lynch CATEGORY (fast grower / turnaround / asset play / etc)
2. Apply Fisher's scuttlebutt lens: what would suppliers/customers/competitors say?
3. Apply Li Lu's duration test: is the moat deepening or decaying? How long is the tailwind?
4. Apply Thiel's secret test: what does almost nobody else believe about this stock that is true?
5. Give a SCOUT VERDICT per stock: INVESTIGATE FURTHER / PASS
6. In your Discovery section: name ONE stock NOT on the candidate list that better fits
   the AMD/MU/SNDK runner DNA pattern. This is the most valuable output.
7. SPINOFF CHECK: If the stock has a pending or recently announced spinoff, flag it immediately.
   State: "SPINOFF ALERT: [company] is splitting into [RemainCo] and [SpinCo]. A sum-of-parts
   valuation is required. Single forward EPS is unreliable. Flag for SOP mode."
   Do NOT use single-entity forward EPS to calculate PEG if a spinoff is in progress.

STRICT RULES - NON-NEGOTIABLE:
- US markets ONLY: NYSE, NASDAQ, AMEX. No TSX, OTC, pink sheets, foreign exchanges.
- Minimum price $1.00. Preferred price $10.00+. Never recommend sub-$1 stocks.
- If a Discovery stock is under $10, flag it as [CAUTION <$10] and explain why it still qualifies.
- Any stock under $1 or on OTC/pink sheets = automatic disqualify, do not mention.

DISCOVERY REQUIREMENT: Each Scout persona MUST name exactly one off-list stock fitting AMD/MU/SNDK runner DNA. Constraints: NYSE/NASDAQ only, price >$10, market cap $500M-$15B.
Format: DISCOVERY: TICKER — one sentence reason.
HARD RULE: Discovery tickers receive NO conviction rating, NO Kelly sizing, NO position recommendation. The ONLY permissible output is: 'DISCOVERY: TICKER — [reason] [QUEUED FOR STANDALONE RUN — no position sizing until full analysis complete]'. Any conviction rating above 3/10 or Kelly size above 2% on a discovery ticker is PROHIBITED — it has not been stress-tested by the Skeptic or Valuation Anchor.

DISCOVERY PRICE RULE: Do NOT state or imply any specific price for a discovery ticker. You do not have reliable current price data. Write the discovery rationale purely based on business quality, DNA fit, and thesis — zero price references. The system will validate current price separately. Any price you state will be wrong.

RUNNER DNA CHECKLIST — PRICE RULES:
- 'Beaten down' must be evaluated at the CURRENT price vs the trailing 6-month average, NOT vs the 52-week low. A stock that was $57 a year ago and is now $189 is NOT beaten down.
- Use the current price and 52-week high/low from the LIVE HEADER DATA provided. Do not evaluate dip from training data.
- If current price is within 15% of 52-week high: mark 'Beaten down? NO — near 52wk high'
- If current price is >30% below 52-week high: mark 'Beaten down? YES — N% off high'
- All panels must use the same reference: current price vs 52-week high (NOT vs 52-week low)

CATALYST PRICING NUANCE: When marking 'catalyst not yet priced', distinguish between: (a) market is unaware of the catalyst, vs (b) market is aware but discounting it due to risk. For option (b), state the specific discount mechanism: 'AI demand is consensus but governance discount is X% — resolving governance IS the unpriced catalyst.'

PEER COMPARISON REQUIREMENT: If the primary risk on a stock is governance/accounting/legal (not competitive or market), identify the cleanest peer alternative in the same sector with equivalent exposure but lower risk. State it early in the analysis: 'CLEANER ALTERNATIVE: [TICKER] — [why it has same exposure with less risk]'. This gives simulation agents a concrete comparison point throughout the debate.

PRICE VALIDATION RULE: Before writing any thesis on a discovery ticker, you must state its current price using the data you have been given. If the discovery ticker's current price is not in your fundamental data block, flag it as: 'DISCOVERY: [TICKER] — [reason] [PRICE UNVERIFIED — requires live price check before any position sizing]'. Never write a full investment thesis on a discovery ticker whose current price you cannot confirm from the provided data.

SCUTTLEBUTT FRAMEWORK: Imagined scuttlebutt is a HYPOTHESIS GENERATION TOOL ONLY. Hard rules:
1. Label every imagined interview: 'HYPOTHESIS (unverified): [what this person might say]'
2. NEVER draw a final analytical verdict from imagined testimony. Any statement after 'therefore' or 'this confirms' that follows imagined content is PROHIBITED.
3. End each scuttlebutt section with EXACTLY: 'RESEARCH QUESTIONS this raises: [3 specific questions real research would answer]'
4. The validator will strip any verdict derived from imagined content. Write your verdicts from the verified data block only.
Any claim about a competitor's revenue, market share, or product roadmap that is not in the provided data block must be labeled [UNVERIFIED CLAIM] or [ESTIMATE].

Be specific. Use the data. No hedging."""


SKEPTIC_SYSTEM = """You are a composite forensic skeptic combining five of history's best short-sellers and investigative analysts:

MICHAEL BURRY (Footnote Forensics):
- Reads 10-K footnotes nobody reads: revenue recognition policies, capitalized costs,
  off-balance-sheet obligations, related party transactions, channel stuffing signals
- Rule: If management misleads on ONE thing, assume misleading on everything
- Found housing fraud in CDO prospectus footnotes that nobody else read

JIM CHANOS (Short-Side Pressure):
- Assumes management is misleading until PROVEN otherwise
- Found Enron by reading the 10-K - the business model made no sense
- Key test: "Can I explain how this business actually makes money? If not, why not?"
- Looks for: accounting red flags, overvalued acquisitions, debt hidden in structures

CARSON BLOCK (Muddy Waters):
- Emerging market and opaque structure fraud detection
- Looks for: inflated revenue in opaque operations, fake cash balances, related party fraud
- Method: verify the numbers from outside the company

WHITNEY TILSON (Variant Perception):
- Identifies the SPECIFIC assumption the market makes that the data contradicts
- Not just "I disagree" - "here is the exact market model error"

HERB GREENBERG (Journalist Skepticism):
- Asks the dumb question nobody else will ask
- "Who benefits from this narrative? Who is selling shares right now?"
- "If the story is too good to be true, what specifically is false?"

YOUR JOB:
1. For each stock: identify the SPECIFIC accounting or business model red flag
2. What does the market believe that the data contradicts?
3. Who is selling? Who benefits from the current narrative?
4. FORENSIC VERDICT per stock: PASS / WARN / ELIMINATE
   ELIMINATE if: business model incoherent, accounting suspicious, or narrative too clean
5. For any ELIMINATED stock: state the specific evidence

Be adversarial. Assume guilt until proven innocent. Quote specific data.

SCANDAL SEVERITY CLASSIFICATION: Not all corporate scandals are equal weight.
TIER 1 (Existential — lead with these): Active criminal indictment, active DOJ investigation, active SEC enforcement action, auditor resignation with 'unwilling to associate' language, active securities fraud class action. These must appear in your opening paragraph, not buried in a list.
TIER 2 (Serious — surface prominently): Accounting restatements, revenue recognition issues, related party transactions, channel stuffing signals.
TIER 3 (Monitor — note but don't lead with): Executive departures, regulatory inquiries without formal charges, whisper-number gaming.
If ANY Tier 1 events exist: your FORENSIC VERDICT must be ELIMINATE unless there is specific evidence the event is fully resolved. 'Undergoing investigation' is NOT resolved.

CASH FLOW FORENSIC RULE: If operating cash flow is significantly negative while net income is positive (or if operating cash outflow exceeds net income by >2x), this is a major red flag. Explicitly calculate: 'Net income = $X, Operating CF = $Y, gap = $Z.' Explain WHERE the cash went (working capital buildup, inventory, receivables). This is the Burry forensic check — lead with it if the gap is material."""


FUNDAMENTAL_SYSTEM = """You are a composite fundamental analyst combining 8 of history's greatest value and conviction investors:

JOEL GREENBLATT (Magic Formula):
- Return on Invested Capital (ROIC) above cost of capital = economic moat
- Earnings yield (EBIT/EV) = how cheap relative to earnings power
- Special situations: spinoffs, restructurings, bankruptcies create mispricing
- Key question: "Is the business earning more than it costs to run?"

MOHNISH PABRAI (Dhandho):
- Heads I win, tails I don't lose much - asymmetric payoff required
- 98-question checklist - every question must have a satisfying answer
- Base rates from similar historical situations
- Cloning: what are the best investors in this space doing RIGHT NOW?

SETH KLARMAN (Margin of Safety):
- Floor value first - what is it worth if growth stops TODAY?
- Liquidation value, private market value, sum-of-parts
- Never pay for hope. Pay for assets + current earnings only.
- Key question: "What is the absolute worst-case floor price?"

BRUCE GREENWALD (Competitive Advantage Period):
- How many years can this company earn above its cost of capital?
- Franchise value = NPV of excess returns during CAP
- When CAP ends, stock is worth book value - no more, no less
- Key question: "What specifically protects the excess returns?"

MICHAEL MAUBOUSSIN (Base Rates):
- Reference class forecasting - what happens to companies in this situation historically?
- What growth rate is the current price implying? Is that realistic?
- Reversion to mean is the most powerful force in finance
- Key question: "What does the outside view say before we hear the story?"

STANLEY DRUCKENMILLER (Macro + Fundamentals Alignment):
- Both macro AND fundamentals must point the same direction
- Where is the Fed? Where are earnings going? Are they diverging or converging?
- Concentration when conviction is highest - sizing is the key skill
- Key question: "Does the macro environment actively support this thesis right now?"

BILL MILLER (Probability-Weighted Expected Value):
- Market assigns a probability. You assign a different probability. The gap IS the edge.
- Example: market implies 20% chance of success, you think 60% - that's a 3x edge
- Build the actual EV tree: bull case x probability + bear case x probability
- Key question: "What is my actual edge expressed as a probability difference?"

DAVID EINHORN (Catalyst Discipline):
- Being right without a catalyst means waiting forever and losing opportunity cost
- Identify the SPECIFIC event that forces the market to reprice and WHEN it happens
- Without a catalyst, a thesis is just an opinion
- Key question: "What specific event forces the market to see what we see, and when?"

YOUR JOB - FUNDAMENTAL PANEL:
For each stock deliver:

VALUATION MODE CLASSIFICATION (do this first — it determines which framework to use):

  INFLECTION-STAGE: Use when revenue_growth > 100% OR (eps_ttm < 0 AND eps_fwd > 0) OR company is pre-profitability with accelerating revenue.
    → Do NOT use static EPV (it will always produce SELL on a name that isn't profitable yet — that's the wrong tool).
    → Use: forward EV/revenue multiple, PEG ratio on forward earnings, cash runway analysis.
    → Question: "At what forward revenue multiple does this trade, and what growth rate justifies it?"

  BINARY-EVENT: Use when phase3_pending OR binary_catalyst_within_6mo OR company is pre-revenue clinical stage.
    → Do NOT use EPV or EV/revenue — neither is meaningful.
    → Use: probability-weighted scenario analysis. YOU must input your own p_success estimate (not market-implied).
    → Formula: EV = (your_p_success × upside_pct) - (your_p_failure × downside_pct)
    → Example: if you think success probability is 45%, not 30% implied by price: EV = (0.45 × +75%) - (0.55 × -65%) = +33.75% - 35.75% = -2% EV. That is a very different number than if you blindly use market-implied probability.
    → Output your p_success estimate explicitly and show the EV tree.

  MATURE/STALWART: Use for profitable companies with stable revenue growth < 30%.
    → Use: EPV, earnings yield (EBIT/EV), margin of safety vs current price.
    → This is the Klarman/Greenblatt framework. Correct for mature businesses. Wrong for inflection names.

FLOOR VALUE: [liquidation/asset value if growth stops — Klarman]
ROIC vs COST OF CAPITAL: [above/below/at parity — Greenblatt]
EARNINGS YIELD: [EBIT/EV % — cheap or expensive]
COMPETITIVE ADVANTAGE PERIOD: [how many years of excess returns — Greenwald]
BASE RATE: [what happens to companies in this reference class — Mauboussin]
MARKET-IMPLIED GROWTH: [what growth rate is baked into the current price]
MACRO ALIGNMENT: [does the current macro environment support the thesis — Druckenmiller]
EXPECTED VALUE TREE: [3 scenarios with probabilities — bull/base/bear — Miller]
CATALYST: [specific event + estimated date that forces repricing — Einhorn]
ASYMMETRY CHECK: [heads I win how much / tails I lose how much — Pabrai]
FUNDAMENTAL VERDICT: STRONG BUY / BUY / HOLD / PASS - conviction 1-10

Be quantitative. Build the actual numbers. No vague commentary.
State your VALUATION MODE at the top of each stock analysis.

GAAP P/E RULE: When a GAAP/NON-GAAP WARNING appears in the validated data block, you MUST calculate and surface BOTH the GAAP forward P/E AND the non-GAAP forward P/E explicitly. Format: 'GAAP forward P/E: Xz (using $X.XX GAAP EPS) | Non-GAAP forward P/E: Xz (using $X.XX non-GAAP EPS)'. Do not use only the non-GAAP figure for your primary valuation anchor when GAAP profitability is the stated thesis."""


MACRO_TECH_SYSTEM = """You are a composite technology disruption and macro cycle analyst combining 7 legendary investors:

CATHIE WOOD (Wright's Law + TAM Expansion):
- Wright's Law: costs fall predictably as cumulative production doubles
- Where does the cost curve land in 2028-2032? What market does that unlock?
- TAM expansion: the total addressable market grows AS the cost falls
- Convergence: when multiple exponential technologies intersect, TAM explodes
- Key question: "What does the Wright's Law cost curve say this technology costs in 5 years?"

ANDY KESSLER (S-Curve Timing):
- Every technology follows an S-curve: slow start → explosive growth → plateau
- The money is made at the INFLECTION POINT - bottom of the S before it goes vertical
- Tools that increase productivity of other workers are the most valuable
- Key question: "Are we at the knee of the S-curve or already at the plateau?"

CLAYTON CHRISTENSEN (Disruption Framework):
- Disruption comes from BELOW - cheaper, simpler, good enough for non-consumers
- Incumbents always ignore the low end until it's too late
- Sustaining innovation (making good products better) = defensive
- Disruptive innovation (making bad products good enough) = offensive
- Key question: "Is this company disrupting from below, or is it the incumbent being disrupted?"

HOWARD MARKS (Cycle Positioning):
- Second-level thinking: "What does everyone else think, and what does that imply?"
- Where are we in the credit/economic cycle? Early/mid/late/turn?
- When everyone is bullish, risk is highest. When everyone is bearish, opportunity is greatest.
- Key question: "What does the consensus believe, and why is the consensus wrong?"

GEORGE SOROS (Reflexivity):
- Markets create the reality they anticipate - the feedback loop
- Find the prevailing bias: what misconception is currently driving the price?
- When does the misconception break? What triggers the reversal?
- Boom/bust sequence: fundamentals → bias forms → self-reinforcing trend → reversal
- Key question: "What is the reflexive feedback loop and when does it break?"

RAY DALIO (Debt Cycle Template):
- Short-term debt cycle (5-8 years) + long-term debt cycle (75-100 years)
- What regime are we in? Inflationary deleveraging? Deflationary? Beautiful deleveraging?
- Asset class performance is DETERMINED by the debt cycle phase
- Key question: "What macro regime are we in and what does history say outperforms?"

JIM ROGERS (Capital Flow + Fundamental Change):
- Follow the real economy - where is capital being forced to flow by fundamental change?
- Secular commodity cycles last 15-20 years. Identify the CAUSE not the symptom.
- The most hated assets in any cycle become the best investments at the turn
- Key question: "What fundamental structural change is forcing capital into this sector?"

YOUR JOB - TECH + MACRO PANEL:
For each stock deliver:

S-CURVE POSITION: [where on the adoption curve - Kessler]
WRIGHT'S LAW PROJECTION: [cost curve in 3-5 years, what market it unlocks - Wood]
TAM IN 2030: [realistic total addressable market estimate with assumptions]
DISRUPTION DIRECTION: [disrupting or being disrupted - Christensen]
CYCLE POSITION: [where are we in the relevant market cycle - Marks]
CONSENSUS BELIEF: [what everyone thinks, and why they're wrong - Marks]
REFLEXIVE LOOP: [what misconception drives current pricing, when does it break - Soros]
MACRO REGIME FIT: [does the current macro regime favor this asset - Dalio]
CAPITAL FLOW THESIS: [what structural force is pushing capital here - Rogers]
TECHNOLOGY VERDICT: ACCELERATING / STABLE / AT RISK - conviction 1-10
MACRO VERDICT: TAILWIND / NEUTRAL / HEADWIND for this stock right now

Surface the non-obvious. The consensus is already priced in.

RUNNER DNA PRICE RULE: 'Beaten down' = current price >30% below 52-week HIGH. Do NOT evaluate vs 52-week low or vs price from 12+ months ago.

COMPETITOR DATA RULE: Any claim about a named competitor's revenue, segment performance, or market position must either: (a) reference the specific earnings report it came from, or (b) be labeled [UNVERIFIED — hypothesis only]. Do NOT present synthesized competitor narratives as facts."""


VERDICT_SYSTEM = """You are the synthesis layer - a council of five legendary thinkers delivering final verdicts:

CHARLIE MUNGER (Chairman - Inversion):
- Inversion first: what would have to be true for this stock to go to ZERO?
- Lollapalooza: are multiple mental models converging in one direction?
- "Sit on your ass" test: if you have to be convinced, you are not convinced enough

ED THORP (Kelly Criterion):
- Mathematically optimal position size = edge / odds
- Never bet more than full Kelly. Use half-Kelly for safety.
- Calculate: what is our actual edge (probability advantage) here?

NICK SLEEP (Scale Economics):
- Does the flywheel close as volume grows?
- Amazon, Costco, Booking.com archetypes: business gets CHEAPER for customers as it scales
- Is there a compounding mechanism that gets stronger over time?

NASSIM TALEB (Tail Risk):
- What is the black swan that ends this thesis permanently?
- Is this company antifragile (gets stronger from volatility) or fragile?
- What stress scenario kills the thesis?

ANNIE DUKE (Decision Quality):
- Separate decision quality from outcome quality
- Was this a good PROCESS decision? Are we confusing a good story with a good bet?
- What cognitive biases are driving this? Which of the 20 known biases is at work?

YOUR JOB - FINAL VERDICTS:
For each stock that survived the Scout and Skeptic layers, deliver:

TICKER: [symbol]
VERDICT: BUY / WATCH / PASS
CONVICTION: X/10
MUNGER INVERSION: [what kills it]
KELLY SIZE: [fraction - e.g. "5% position" or "half-Kelly suggests 8%"]
FLYWHEEL: [does scale economics compound? yes/no/partial]
TAIL RISK: [the specific black swan]
DECISION QUALITY: [is this a good process bet regardless of outcome?]
TOP BULL ARGUMENT: [one paragraph, specific data points]
TOP BEAR ARGUMENT: [one paragraph, specific risk]
CATALYST: [specific event that forces repricing, with timeline]
SELL TRIGGER: [what makes you exit - specific, not price-based]

DISCOVERY POOL: Rank all new stocks surfaced across all rounds.
FINAL WATCHLIST: Top 5 stocks to research further, ranked by conviction.
WHAT THE PANEL MISSED: Is there a stock fitting the AMD/MU/SNDK pattern
that nobody mentioned? State it explicitly.

STRICT OUTPUT RULES - NON-NEGOTIABLE:
- Every stock in FINAL WATCHLIST and DISCOVERY POOL must be NYSE or NASDAQ listed.
- Every stock must be priced above $1.00. Preferred $10.00+.
- Mark any stock $1-$10 as [CAUTION <$10] with a one-line explanation.
    - Zero OTC, pink sheet, TSX, foreign-listed, or sub-$1 stocks in any output.
    - If a candidate stock is OTC/foreign/sub-$1, exclude it from verdicts entirely.

DISCOVERY TICKERS: If any ticker appears in the report ONLY as a discovery candidate (not as a primary research subject), output ONLY: '[TICKER]: QUEUED FOR FULL RUN — conviction rating and position sizing withheld pending complete analysis.' Do NOT assign a conviction score or Kelly size to any discovery ticker."""


SUMMARY_SYSTEM = """You are a synthesis compiler. Your job is to read analysis from 4 analyst panels and produce ONE compact structured summary table per stock. No narrative. Pure structured data.

For each stock produce EXACTLY this block:

---STOCK: [TICKER]---
SCOUT: [INVESTIGATE/PASS] | Category: [Lynch category] | Secret: [one line]
SKEPTIC: [PASS/WARN/ELIMINATE] | Key risk: [one line]
FUNDAMENTALS: [STRONG BUY/BUY/HOLD/PASS] | Conviction: [X/10] | EV: [+/-X%]
TECH+MACRO: [ACCELERATING/STABLE/AT RISK] | Macro: [TAILWIND/NEUTRAL/HEADWIND]
PANEL_CONSENSUS: X/4 bullish — [HIGH CONSENSUS / SPLIT / PANEL CONFLICT]
OVERALL: [BUY/WATCH/PASS] | Score: [X/10]
CATALYST: [one line, specific event + date]
KILL CONDITION: [one line, what sends it to zero]
---END---

PANEL_CONSENSUS scoring: count bullish verdicts across the 4 panels.
  Bullish = INVESTIGATE, BUY, STRONG BUY, ACCELERATING.
  Bearish = PASS, WARN, ELIMINATE, AT RISK.
  3-4 bullish → HIGH CONSENSUS. 2 bullish → SPLIT. Scout=BUY and Skeptic=ELIMINATE → PANEL CONFLICT.

Produce one block per stock. No other text. Pure structured table."""


# ══════════════════════════════════════════════════════════════════
# DEEP MODE - 29 separate investor calls (--deep flag)
# ══════════════════════════════════════════════════════════════════

INVESTORS = {
    "philip_fisher":   {"name": "Philip Fisher",   "layer": 1},
    "peter_lynch":     {"name": "Peter Lynch",     "layer": 1},
    "li_lu":           {"name": "Li Lu",           "layer": 1},
    "peter_thiel":     {"name": "Peter Thiel",     "layer": 1},
    "michael_burry":   {"name": "Michael Burry",   "layer": 2},
    "jim_chanos":      {"name": "Jim Chanos",      "layer": 2},
    "carson_block":    {"name": "Carson Block",    "layer": 2},
    "whitney_tilson":  {"name": "Whitney Tilson",  "layer": 2},
    "herb_greenberg":  {"name": "Herb Greenberg",  "layer": 2},
    "joel_greenblatt": {"name": "Joel Greenblatt", "layer": 3},
    "mohnish_pabrai":  {"name": "Mohnish Pabrai",  "layer": 3},
    "seth_klarman":    {"name": "Seth Klarman",    "layer": 3},
    "bruce_greenwald": {"name": "Bruce Greenwald", "layer": 3},
    "michael_mauboussin": {"name": "Michael Mauboussin", "layer": 3},
    "stanley_druckenmiller": {"name": "Stanley Druckenmiller", "layer": 4},
    "bill_miller":     {"name": "Bill Miller",     "layer": 4},
    "david_einhorn":   {"name": "David Einhorn",   "layer": 4},
    "cathie_wood":     {"name": "Cathie Wood",     "layer": 5},
    "andy_kessler":    {"name": "Andy Kessler",    "layer": 5},
    "clayton_christensen": {"name": "Clayton Christensen", "layer": 5},
    "howard_marks":    {"name": "Howard Marks",   "layer": 6},
    "george_soros":    {"name": "George Soros",   "layer": 6},
    "ray_dalio":       {"name": "Ray Dalio",      "layer": 6},
    "jim_rogers":      {"name": "Jim Rogers",     "layer": 6},
    "charlie_munger":  {"name": "Charlie Munger", "layer": 7},
    "ed_thorp":        {"name": "Ed Thorp",       "layer": 7},
    "nick_sleep":      {"name": "Nick Sleep",     "layer": 7},
    "nassim_taleb":    {"name": "Nassim Taleb",   "layer": 7},
    "annie_duke":      {"name": "Annie Duke",     "layer": 7},
}

LAYERS = {1: "Scouts", 2: "Forensic", 3: "Analysts",
          4: "Conviction", 5: "Technology", 6: "Macro", 7: "Synthesis"}


# ══════════════════════════════════════════════════════════════════
# CORE ENGINE
# ══════════════════════════════════════════════════════════════════

def estimate_tokens(text: str) -> int:
    return len(text) // 4


_MAX_PROMPT_TOKENS = 6000


def call_claude(system: str, user: str, model: str = None, max_tokens: int = 3500) -> str:
    total = estimate_tokens(system + user)
    if total > _MAX_PROMPT_TOKENS:
        print(f"\n  WARNING: CONTEXT TRIMMED: prompt was ~{total} tokens", flush=True)
        # Preserve system prompt; trim user to fit budget
        budget_chars = _MAX_PROMPT_TOKENS * 4 - len(system) - 200
        if budget_chars > 500:
            # Try to trim at fundamentals section boundary to preserve instructions
            fund_idx = user.find("FUNDAMENTAL DATA:")
            if fund_idx != -1:
                # Keep everything before fundamentals + a trimmed fundamentals block
                before = user[:fund_idx + len("FUNDAMENTAL DATA:")]
                after_fund = user[fund_idx + len("FUNDAMENTAL DATA:"):]
                keep_after = max(0, budget_chars - len(before))
                user = before + after_fund[:keep_after] + "\n[FUNDAMENTALS TRUNCATED FOR TOKEN BUDGET]"
            else:
                user = user[:budget_chars] + "\n[CONTEXT TRUNCATED FOR TOKEN BUDGET]"

    m = model or SONNET

    # Network retry — up to 3 attempts with backoff for connection errors / 5xx
    import time as _time
    last_err = None
    for _attempt in range(3):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OR_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://oracle.local",
                    "X-Title": "ORACLE Think Tank"
                },
                json={
                    "model": m,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user}
                    ],
                    "temperature": 0.3,
                    "max_tokens": max_tokens
                },
                timeout=120
            )
            # Retry on 5xx server errors but NOT on 4xx (402 credit, 400 bad request)
            if resp.status_code >= 500:
                raise requests.exceptions.ConnectionError(f"Server error {resp.status_code}")
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.HTTPError as e:
            # 4xx errors are not retryable — re-raise immediately
            raise
        except Exception as e:
            last_err = e
            if _attempt < 2:
                wait = 10 * (2 ** _attempt)  # 10s, 20s
                print(f"\n  WARNING: call_claude attempt {_attempt+1} failed ({e}) — retrying in {wait}s...", flush=True)
                _time.sleep(wait)
            else:
                print(f"\n  ERROR: call_claude failed after 3 attempts: {e}", flush=True)
                raise


def get_fundamentals(stocks: list, fresh: bool = False) -> str:
    """Fetch live market data via yfinance with 24hr disk cache (keyed by today's date).

    MARKET DATA ONLY — price, 52w range, short interest, beta, volume, market cap.
    EPS, revenue, gross margin, analyst targets, and forward estimates come from
    oracle_factsheet.py (EDGAR) — not from this function.
    """
    import yfinance as yf

    today_str  = datetime.date.today().strftime("%Y%m%d")
    cache_dir  = os.path.expanduser("~/ORACLE/cache")
    cache_file = os.path.join(cache_dir, f"fundamentals_{today_str}.json")
    os.makedirs(cache_dir, exist_ok=True)

    # --fresh: nuke all fundamentals_*.json files and force a clean re-fetch
    if fresh:
        for old in Path(cache_dir).glob("fundamentals_*.json"):
            try:
                old.unlink()
            except Exception:
                pass
        print("  [fresh] Deleted fundamentals cache — forcing live fetch.")

    cached_data: dict = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as fh:
                cached_data = json.load(fh)
            hits = [s for s in stocks if s in cached_data]
            if hits:
                print(f"  Market data: {len(hits)} ticker(s) from cache ({today_str}).")
        except Exception:
            cached_data = {}

    to_fetch = [s for s in stocks if s not in cached_data]
    if to_fetch:
        print(f"  Pulling live market data for {to_fetch}...", end="", flush=True)
        for sym in to_fetch:
            try:
                tkr  = yf.Ticker(sym)
                # Use fast_info for price (the one permitted yfinance price use)
                fast = tkr.fast_info
                price_val = getattr(fast, 'last_price', None) or getattr(fast, 'regular_market_price', None)
                if not price_val:
                    print(f"\n  yfinance unavailable for {sym} — using limited data")
                    cached_data[sym] = {"error": True, "ticker": sym}
                    continue

                price_val = float(price_val)

                # Market data fields only — no financial statement data
                info = tkr.info
                mkt_cap   = info.get("marketCap")
                mkt_cap_str = f"${mkt_cap/1e9:.1f}B" if mkt_cap else "N/A"
                short_raw = info.get("shortPercentOfFloat")
                short_str = (f"{short_raw*100:.1f}%" if short_raw and short_raw <= 1.0
                             else f"{short_raw:.1f}%" if short_raw else "N/A")
                beta      = info.get("beta")
                avg_vol   = info.get("averageVolume")

                cached_data[sym] = {
                    "ticker":      sym,
                    "price":       price_val,
                    "mkt_cap_str": mkt_cap_str,
                    "week52_high": info.get("fiftyTwoWeekHigh"),
                    "week52_low":  info.get("fiftyTwoWeekLow"),
                    "short_str":   short_str,
                    "beta":        beta,
                    "avg_volume":  avg_vol,
                }
            except Exception as e:
                print(f"\n  yfinance unavailable for {sym} — using limited data ({e})")
                cached_data[sym] = {"error": True, "ticker": sym}
            time.sleep(0.3)

        try:
            with open(cache_file, "w") as fh:
                json.dump(cached_data, fh)
        except Exception:
            pass
        print(" done.")

    # Format one text block per stock — market data only
    blocks = []
    for sym in stocks:
        d = cached_data.get(sym, {})
        if not d or d.get("error"):
            blocks.append(f"{sym} — market data unavailable")
            continue

        price = d.get("price", 0)
        hi    = d.get("week52_high")
        lo    = d.get("week52_low")
        short = d.get("short_str", "N/A")
        beta  = d.get("beta")
        vol   = d.get("avg_volume")

        hi_str    = f"${hi:.2f}" if hi else "N/A"
        lo_str    = f"${lo:.2f}" if lo else "N/A"
        beta_str  = f"{beta:.2f}" if beta is not None else "N/A"
        vol_str   = f"{vol/1e6:.1f}M" if vol else "N/A"

        blocks.append(
            f"{sym} — Price: ${price:.2f} | 52w: {lo_str}–{hi_str} | "
            f"Short: {short} | Beta: {beta_str} | Vol: {vol_str}"
        )

    return "\n\n".join(blocks)


def make_batches(stocks: list, size: int = 2) -> list:
    """Split stocks into batches of `size`. Handles any count."""
    return [stocks[i:i+size] for i in range(0, len(stocks), size)]


# ══════════════════════════════════════════════════════════════════
# OBSIDIAN LAYER WRITER + OUTPUT VALIDATORS
# ══════════════════════════════════════════════════════════════════

def _load_fund_cache(date: str = None) -> dict:
    """Load raw fundamentals cache from disk. Returns {} on any failure."""
    if date is None:
        date = datetime.date.today().strftime("%Y%m%d")
    cache_file = os.path.expanduser(f"~/ORACLE/cache/fundamentals_{date}.json")
    try:
        with open(cache_file) as fh:
            return json.load(fh)
    except Exception:
        return {}


def is_truncated(text: str) -> bool:
    """Return True if text appears cut off — does not end with a sentence terminator."""
    if not text:
        return True
    t = text.rstrip()
    return not (t.endswith(('.', '!', '?', '```', '---', '**', '*')))


def write_layer_note(ticker_or_batch, layer_name: str, content: str, date: str) -> None:
    """Write a layer analysis note to Obsidian vault for each ticker. Never crashes pipeline."""
    tickers = [ticker_or_batch] if isinstance(ticker_or_batch, str) else list(ticker_or_batch)
    for ticker in tickers:
        target_dir = os.path.join(OBSIDIAN_RUNS_DIR, date, ticker)
        try:
            os.makedirs(target_dir, exist_ok=True)
            with open(os.path.join(target_dir, f"{layer_name}.md"), "w", encoding="utf-8") as fh:
                fh.write(content)
        except Exception as e:
            print(f"  [Obsidian] WARNING: could not write {layer_name} for {ticker}: {e}")


def validate_run_completeness(stocks: list, date: str) -> list:
    """Check all 5 required layer notes exist per ticker. Prints result, returns missing list."""
    required = ["layer1_scout", "layer2_skeptic", "layer3a_fundamentals",
                "layer3b_techmacro", "layer5_summary"]
    missing = []
    for ticker in stocks:
        for layer in required:
            path = os.path.join(OBSIDIAN_RUNS_DIR, date, ticker, f"{layer}.md")
            if not os.path.exists(path):
                missing.append(f"{ticker}/{layer}")
    if missing:
        print(f"\n  *** INCOMPLETE RUN — missing {len(missing)} layer note(s):")
        for m in missing:
            print(f"      MISSING: {m}")
    else:
        print("\n  Validation passed — all 5 required layer notes present for every ticker.")
    return missing


def write_ticker_notes(ticker: str, fund_dict: dict, date: str,
                       verdict: str, conviction: str) -> None:
    """Write fundamentals snapshot and append verdict row to Obsidian tickers folder."""
    target_dir = os.path.join(OBSIDIAN_TICKERS_DIR, ticker)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as e:
        print(f"  [Obsidian] WARNING: could not create ticker dir for {ticker}: {e}")
        return

    # fundamentals_{date}.md — one bullet per field
    try:
        lines = [f"# {ticker} Fundamentals — {date}\n"]
        for k, v in fund_dict.items():
            if k != "error":
                lines.append(f"- **{k}**: {v}")
        with open(os.path.join(target_dir, f"fundamentals_{date}.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception as e:
        print(f"  [Obsidian] WARNING: could not write fundamentals for {ticker}: {e}")

    # verdict_history.md — append-only table
    hist_path = os.path.join(target_dir, "verdict_history.md")
    try:
        if not os.path.exists(hist_path):
            with open(hist_path, "w", encoding="utf-8") as fh:
                fh.write("# Verdict History\n\n| Date | Verdict | Conviction | Price |\n|------|---------|------------|-------|\n")
        price = fund_dict.get("price", "N/A")
        price_str = f"${price:.2f}" if isinstance(price, (int, float)) else str(price)
        with open(hist_path, "a", encoding="utf-8") as fh:
            fh.write(f"| {date} | {verdict} | {conviction}/10 | {price_str} |\n")
    except Exception as e:
        print(f"  [Obsidian] WARNING: could not append verdict history for {ticker}: {e}")


def build_live_header(batch: list, date: str) -> str:
    """Build the LIVE DATA header string for a batch of tickers (injected first in every panel prompt).

    Primary source: oracle_factsheet.build_fact_sheet() (EDGAR verified data).
    Fallback: fund_cache for market data only (52w range, short interest, beta, volume).
    Never reads EPS, revenue, or gross margin from fund_cache — those come from EDGAR only.
    """
    fund_cache = _load_fund_cache(date)
    # Use session-isolated price from oracle_factsheet — keeps all panel calls consistent
    try:
        from oracle_factsheet import get_session_price as _gsp
        for sym in batch:
            _live_p = _gsp(sym)
            if _live_p:
                d = fund_cache.get(sym, {})
                if d:
                    stored_p = d.get("price") or 0
                    if stored_p and abs(_live_p - stored_p) / _live_p > 0.03:
                        print(f"  [PRICE] {sym}: cache=${stored_p:.2f} updated to session=${_live_p:.2f}")
                    d["price"] = _live_p
    except Exception:
        pass

    lines = [f"LIVE DATA AS OF {date} — DO NOT USE TRAINING KNOWLEDGE FOR THESE FIELDS:"]

    # Primary: EDGAR fact sheets for each ticker (verified financials)
    fact_sheet_blocks = []
    for sym in batch:
        try:
            from oracle_factsheet import build_fact_sheet, format_fact_sheet_for_panels as _fmt_fs
            _fs = build_fact_sheet(sym)
            _fs_text = _fmt_fs(_fs)
            if _fs_text:
                fact_sheet_blocks.append(_fs_text)
        except Exception:
            pass  # fact sheet is optional — graceful degradation

    if fact_sheet_blocks:
        lines = fact_sheet_blocks + lines  # prepend fact sheets before market data

    # Fallback: market data only from fund_cache (52w, short interest, beta, volume)
    found_any = bool(fact_sheet_blocks)
    for ticker in batch:
        d = fund_cache.get(ticker, {})
        if not d or d.get("error"):
            continue
        found_any = True
        price     = d.get("price", 0)
        hi        = d.get("week52_high")
        lo        = d.get("week52_low")
        short_str = d.get("short_str", "N/A")
        beta      = d.get("beta")
        avg_vol   = d.get("avg_volume")

        price_s   = f"${price:.2f}"        if isinstance(price,   (int, float)) else "N/A"
        hi_s      = f"${hi:.2f}"           if hi else "N/A"
        lo_s      = f"${lo:.2f}"           if lo else "N/A"
        beta_s    = f"{beta:.2f}"          if isinstance(beta,    (int, float)) else "N/A"
        vol_s     = f"{avg_vol/1e6:.1f}M"  if isinstance(avg_vol, (int, float)) and avg_vol else "N/A"

        lines.append(
            f"{ticker} | Price: {price_s} | 52w: {lo_s}–{hi_s} | "
            f"Short: {short_str} | Beta: {beta_s} | AvgVol: {vol_s}"
        )

    if not found_any:
        return ""
    # Inject preflight validated data block if available
    if HAS_PREFLIGHT:
        for sym in batch:
            pf_header = build_preflight_header(sym)
            if pf_header:
                lines.append(pf_header)
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# GAP 3 HELPER — Truncation retry with compressed context
# ══════════════════════════════════════════════════════════════════

def _retry_with_compressed_ctx(
    system_prompt: str, layer_label: str, sbatch: list,
    fundamentals: str, model: str, max_tokens: int, date: str, instruction: str
) -> str:
    """Retry a truncated call with minimal context (live header + RUNNER_DNA + batch only)."""
    sbatch_str = ", ".join(sbatch)
    live_hdr = build_live_header(sbatch, date)
    compressed_user = (
        (live_hdr + "\n\n" if live_hdr else "") +
        f"STOCKS: {sbatch_str}\n\n"
        f"FUNDAMENTAL DATA:\n{fundamentals}\n\n"
        f"{RUNNER_DNA}\n\n"
        f"{instruction}"
    )
    try:
        retry = call_claude(system_prompt, compressed_user, model=model, max_tokens=max_tokens + 500)
        if is_truncated(retry):
            print(f"\n  RETRY ALSO TRUNCATED — {layer_label} ({sbatch_str})", flush=True)
        return retry
    except Exception as _re:
        print(f"\n  [Retry] Exception during retry for {layer_label}: {_re}", flush=True)
        return ""


# ══════════════════════════════════════════════════════════════════
# GAP 4 — Load upcoming catalysts from brain file
# ══════════════════════════════════════════════════════════════════

def load_upcoming_catalysts() -> tuple:
    """
    Read upcoming_catalysts.md. Parse tickers with days-away count.
    Returns (scout_inject, header_flag).
      scout_inject — text to append to Scout prompts for tickers <=14 days away.
      header_flag  — URGENT CATALYST WATCH block for tickers <=3 days away.
    """
    catalyst_path = os.path.expanduser(
        "~/Documents/Trading Vault/04_Bot_Rules/ORACLE/brain/upcoming_catalysts.md"
    )
    if not os.path.exists(catalyst_path):
        return ("", "")
    try:
        with open(catalyst_path, encoding="utf-8") as fh:
            content = fh.read()
    except Exception as _e:
        print(f"  [Catalysts] Could not read upcoming_catalysts.md: {_e}")
        return ("", "")

    within_14, within_3 = [], []
    for line in content.splitlines():
        # Table row format: | TICKER | date | days |
        m = re.search(r'\|\s*([A-Z]{2,5})\s*\|[^|]*\|\s*(\d+)\s*\|', line)
        if m:
            try:
                days = int(m.group(2).strip())
                ticker = m.group(1).strip()
                if days <= 14:
                    within_14.append((ticker, days))
                if days <= 3:
                    within_3.append((ticker, days))
            except ValueError:
                pass
            continue
        # Prose format: TICKER - 2 days away
        m2 = re.search(r'\b([A-Z]{2,5})\b.*?(\d+)\s+days?\s+away', line, re.IGNORECASE)
        if m2:
            try:
                days = int(m2.group(2))
                ticker = m2.group(1).upper()
                if days <= 14:
                    within_14.append((ticker, days))
                if days <= 3:
                    within_3.append((ticker, days))
            except ValueError:
                pass

    scout_inject = ""
    if within_14:
        tickers_str = ", ".join(f"{t} ({d}d)" for t, d in within_14)
        scout_inject = (
            f"\nPORTFOLIO CATALYSTS WITHIN 14 DAYS: {tickers_str}. "
            f"Prioritize in discovery if they fit runner DNA."
        )

    header_flag = ""
    if within_3:
        tickers_str = ", ".join(f"{t} ({d}d)" for t, d in within_3)
        header_flag = (
            f"\n> URGENT CATALYST WATCH: {tickers_str} — earnings within 3 days. "
            f"Position sizing and risk management required.\n"
        )

    return (scout_inject, header_flag)


# ══════════════════════════════════════════════════════════════════
# GAP 1 — Verdict reconciliation (Scout vs Synthesis conflicts)
# ══════════════════════════════════════════════════════════════════

def reconcile_verdicts(stocks: list, results: dict) -> str:
    """
    Compare Scout/Skeptic verdict vs Synthesis OVERALL per ticker.
    If Scout=PASS/ELIMINATE or Skeptic=ELIMINATE AND Synthesis=BUY/WATCH with Score>=5,
    emit a VERDICT CONFLICT block explaining which decision type each verdict applies to.
    Returns formatted markdown or "".
    """
    summary_text = results.get("summary", "")
    if not summary_text:
        return ""

    conflicts = []
    blocks = re.findall(r'---STOCK:\s*([A-Z]+)---(.*?)---END---', summary_text, re.DOTALL)
    for ticker, block in blocks:
        overall_m = re.search(r'OVERALL:\s*(BUY|WATCH|PASS)', block)
        score_m   = re.search(r'Score:\s*(\d+)/10', block)
        if not overall_m:
            continue
        overall_verdict = overall_m.group(1)
        score = int(score_m.group(1)) if score_m else 0
        if overall_verdict not in ("BUY", "WATCH") or score < 5:
            continue

        # Scout verdict from the structured summary SCOUT line
        scout_m   = re.search(r'SCOUT:\s*(INVESTIGATE|PASS|ELIMINATE|BUY)', block)
        skeptic_m = re.search(r'SKEPTIC:\s*(PASS|WARN|ELIMINATE)', block)
        scout_verdict   = scout_m.group(1).upper()   if scout_m   else None
        skeptic_verdict = skeptic_m.group(1).upper() if skeptic_m else None

        scout_negative  = scout_verdict in ("PASS", "ELIMINATE")
        skeptic_removed = skeptic_verdict == "ELIMINATE"

        if not (scout_negative or skeptic_removed):
            continue

        conflicts.append((ticker, scout_verdict, skeptic_verdict, overall_verdict, score))

    if not conflicts:
        return ""

    lines = [
        "## VERDICT CONFLICTS — Layer Reconciliation\n",
        "> Scout and/or Skeptic issued negative verdicts on stocks where Synthesis scored BUY/WATCH.\n"
        "> This is expected — each layer answered a different question. Read before acting.\n",
    ]

    for ticker, scout_v, skeptic_v, synth_v, score in conflicts:
        lines.append(
            f"\n### {ticker} — Scout: {scout_v or 'N/A'} | "
            f"Skeptic: {skeptic_v or 'N/A'} | Synthesis: {synth_v} ({score}/10)\n"
        )
        lines.append(
            f"**Scout answered:** Does this fit the AMD/MU/SNDK 10x runner DNA? "
            f"(beaten-down + inflecting + embarrassed-to-own + catalyst not priced) "
            f"→ **{scout_v or 'N/A'}**\n"
        )
        if skeptic_v == "ELIMINATE":
            lines.append(
                "**Skeptic answered:** Are there accounting red flags or a broken business model? "
                "→ **ELIMINATE**\n"
            )
        lines.append(
            f"**Synthesis answered:** Is there sufficient asymmetric upside for a value bet? "
            f"→ **{synth_v} ({score}/10)**\n"
        )
        lines.append("**Which verdict to use:**\n")
        lines.append(
            f"- **10x runner hunt** (high-conviction, concentrated, catalyst-driven breakout): "
            f"follow Scout → **{scout_v or 'N/A'}** — skip this stock\n"
        )
        lines.append(
            f"- **Asymmetric value bet** (moderate position, diversified, catalyst-optional): "
            f"follow Synthesis → **{synth_v}** — proceed with caution\n"
        )
        if skeptic_v == "ELIMINATE":
            lines.append(
                "- **Skeptic ELIMINATE overrides both** unless you have specific evidence "
                "contradicting the forensic red flag. Verify before acting.\n"
            )
        lines.append("\n---\n")

    return "\n".join(lines) + "\n"


# ══════════════════════════════════════════════════════════════════
# GAP 2 — Catalyst date validator (detect stale dates in synthesis)
# ══════════════════════════════════════════════════════════════════

def validate_catalyst_dates(stocks: list, synthesis_text: str, date: str) -> str:
    """
    Scan synthesis_text for past date patterns (Q[1-4] 202[0-5] or month 202[0-5]).
    For each ticker with a past date: fetch yfinance .calendar for next earnings date.
    Writes catalyst.md to tickers/ folder. Returns warning block string or "".
    """
    if not synthesis_text:
        return ""

    today = datetime.date.today()

    past_date_re = re.compile(
        r'(?:Q[1-4]\s+202[0-5]|'
        r'(?:January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+202[0-5])',
        re.IGNORECASE
    )

    warnings = []

    for ticker in stocks:
        ticker_idx = synthesis_text.find(ticker)
        if ticker_idx < 0:
            continue
        window = synthesis_text[max(0, ticker_idx - 50):ticker_idx + 1500]
        found = past_date_re.search(window)
        if not found:
            continue

        past_date_str = found.group(0)
        next_earnings = None

        try:
            import yfinance as _yf_cat
            tkr = _yf_cat.Ticker(ticker)
            cal = tkr.calendar
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if isinstance(ed, (list, tuple)) and ed:
                    for d in ed:
                        candidate = d.date() if hasattr(d, 'date') else None
                        if candidate and candidate >= today:
                            next_earnings = str(candidate)
                            break
                    if not next_earnings:
                        next_earnings = str(ed[0])
                elif ed is not None:
                    next_earnings = str(ed)
            elif hasattr(cal, 'to_dict'):
                try:
                    cal_d = cal.to_dict('list')
                    ed = cal_d.get("Earnings Date")
                    if ed:
                        next_earnings = str(ed[0])
                except Exception:
                    pass
        except Exception:
            pass

        # Write catalyst.md to tickers folder
        catalyst_dir = os.path.join(OBSIDIAN_TICKERS_DIR, ticker)
        try:
            os.makedirs(catalyst_dir, exist_ok=True)
            catalyst_md = (
                f"# {ticker} Catalyst\n\n"
                f"**Last Updated:** {date}\n"
                f"**Past date found in synthesis:** `{past_date_str}`\n"
                f"**Next Earnings (yfinance):** {next_earnings or 'N/A — fetch failed'}\n"
            )
            with open(os.path.join(catalyst_dir, "catalyst.md"), "w", encoding="utf-8") as fh:
                fh.write(catalyst_md)
        except Exception as _cwe:
            print(f"  [Catalyst] WARNING: could not write catalyst.md for {ticker}: {_cwe}")

        warnings.append(
            f"- **{ticker}**: Past date `{past_date_str}` in synthesis — "
            f"next earnings: **{next_earnings or 'N/A'}** (yfinance)"
        )

    if not warnings:
        return ""

    return (
        "\n\n## CATALYST DATE WARNINGS — Stale Dates Detected\n\n"
        "> The following tickers had past dates in the synthesis layer. "
        "Live yfinance calendar data substituted where available.\n\n"
        + "\n".join(warnings) + "\n"
    )


def _compute_reverse_dcf(price_per_share, fcf_per_share, discount_rate=0.10, terminal_multiple=20, years=10):
    """
    Solve for the implied annual FCF growth rate that makes NPV(FCF stream) = current price.
    Uses binary search between -20% and +200% growth.
    Returns implied_growth_pct (float) or None if FCF <= 0.
    """
    if not fcf_per_share or fcf_per_share <= 0 or not price_per_share or price_per_share <= 0:
        return None

    def npv_at_growth(g):
        pv = 0.0
        fcf = fcf_per_share
        for yr in range(1, years + 1):
            fcf = fcf * (1 + g)
            pv += fcf / (1 + discount_rate) ** yr
        terminal_val = fcf * terminal_multiple / (1 + discount_rate) ** years
        return pv + terminal_val

    # Binary search
    lo, hi = -0.20, 2.00
    for _ in range(60):
        mid = (lo + hi) / 2
        if npv_at_growth(mid) > price_per_share:
            hi = mid
        else:
            lo = mid
        if abs(hi - lo) < 0.0001:
            break

    implied = round((lo + hi) / 2 * 100, 1)
    return implied


def run_composite(stocks: list, fundamentals: str, model: str,
                  screener_context: str = "", date: str = None, mode: str = "composite") -> dict:
    """Run 7 composite analyst calls (v3 - call 3 split into fundamentals + tech/macro, plus summary compiler)."""
    stocks_str = ", ".join(stocks)
    results = {}

    # GAP 4: Load upcoming catalysts — inject into Scout prompts, flag in report header
    catalyst_inject, catalyst_header = load_upcoming_catalysts()
    results["header_flag"] = catalyst_header

    # Create a run directory for batch files (readable in Obsidian, useful for debugging)
    if date is None:
        date = datetime.date.today().strftime("%Y%m%d")
    # Include stock list fingerprint in run_dir to prevent checkpoint collision
    # between runs with different stocks on the same date/mode
    stocks_key = "_".join(sorted(s[:4].upper() for s in stocks[:6]))
    run_dir = os.path.join(OUT_DIR, "_runs", f"_run_{date}_{mode}_{stocks_key}")
    os.makedirs(run_dir, exist_ok=True)

    # Load raw fundamentals dict for live header injection and ticker note writing
    fund_cache = _load_fund_cache(date)

    screener_block = ""
    if screener_context:
        screener_block = f"\nSCREENER RESULTS (these stocks were flagged by AMD/MU/SNDK runner DNA scan):\n{screener_context}\n"

    # ── BUG 2: Price cross-validation — screener (live) vs fundamentals (cache) ──
    # Screener row format: SYM  SCORE  ACCTS  P&L%  PRICE  REV%  EPS  DIP52  ANLST  CAP  INDUSTRY
    # We must grab the PRICE column specifically, not market cap ($12.7B).
    # Price is a plain dollar amount (e.g. $64.68), market cap ends in B (e.g. $12.7B).
    # Regex: match $NUMBER that is NOT followed by B (to exclude market cap).
    if screener_context and fundamentals:
        from oracle_factsheet import get_session_price as _gsp_pv
        for sym in stocks:
            sc_m = re.search(rf'\b{re.escape(sym)}\b[^\n]{{0,200}}\$(\d+\.\d+)(?!B)', screener_context)
            fd_m = re.search(rf'\b{re.escape(sym)}\b[^\n]{{0,150}}\$(\d+\.?\d*)', fundamentals)
            if sc_m and fd_m:
                sc_price = float(sc_m.group(1))
                fd_price = float(fd_m.group(1))
                if sc_price > 0 and fd_price > 0 and abs(sc_price - fd_price) / sc_price > 0.10:
                    print(f"  PRICE MISMATCH: {sym} screener=${sc_price:.2f} vs cache=${fd_price:.2f} — forcing fresh fetch")
                    try:
                        new_price = _gsp_pv(sym)
                        if new_price:
                            fundamentals = fundamentals.replace(
                                f"{sym} — Price: ${fd_price:.2f}", f"{sym} — Price: ${new_price:.2f}", 1
                            )
                    except Exception as _pve:
                        print(f"  PRICE MISMATCH re-fetch failed for {sym}: {_pve}")

    brain_block = ""
    if _HAS_BRAIN:
        _prior = read_brain_context(stocks)
        if _prior:
            brain_block = (
                f"ORACLE BRAIN — PRIOR RUN HISTORY FOR THESE STOCKS:\n"
                f"{_prior}\n\n"
            )

    # ── BUG 4: Problem stock news — fetch headlines for high-short / low-target stocks ──
    news_block = ""
    if _HAS_DATA_LAYER:
        try:
            _fund_cache = get_fundamentals_batch(stocks)
            news_lines = []
            for sym in stocks:
                _fund = _fund_cache.get(sym, {})
                _news = check_problem_stock_news(sym, _fund)
                if _news:
                    news_lines.append(_news)
            if news_lines:
                news_block = "\n".join(news_lines) + "\n\n"
        except Exception as _ne:
            print(f"  [Data layer] News check warning: {_ne}")

    # Build canonical fact sheets for all stocks (EDGAR-verified, single source of truth)
    canonical_facts = {}
    canonical_text_parts = []
    for sym in stocks:
        try:
            from oracle_factsheet import build_fact_sheet as _bfs, format_fact_sheet_for_panels as _ffs
            _fs = _bfs(sym)
            _fs_text = _ffs(_fs)
            canonical_facts[sym] = _fs
            canonical_text_parts.append(_fs_text)
        except Exception as _e:
            canonical_text_parts.append(f"{sym}: fact sheet unavailable — {_e}")

    canonical_data = "\n\n".join(canonical_text_parts) if canonical_text_parts else fundamentals

    context = (
        f"CANDIDATE STOCKS: {stocks_str}\n"
        f"{screener_block}\n"
        f"VERIFIED FINANCIAL DATA (SEC EDGAR — authoritative source for all calculations):\n{canonical_data}\n\n"
        f"MARKET DATA (live — 52-week range, short interest, beta, volume):\n{fundamentals}\n\n"
        f"{news_block}"
        f"{brain_block}"
        f"{RUNNER_DNA}"
    )

    # Split stocks into batches of 2 — dynamic, handles any stock count
    batches     = make_batches(stocks)
    n_batches   = len(batches)
    total_calls = n_batches * 4 + 3   # +1 for Valuation Anchor

    # ── Scout Panel (batched 2 stocks each) ──────────────────────────
    scout_results = []
    for idx, sbatch in enumerate(batches, 1):
        if not sbatch:
            continue
        sbatch_str = ", ".join(sbatch)
        scout_file = os.path.join(run_dir, f"scout_{idx}.md")
        print(f"\n  [{idx}/{total_calls}] Scout Panel Batch {idx} ({sbatch_str})...", end="", flush=True)

        # ── CHECKPOINT RESUME: skip API call if file already exists ──
        if os.path.exists(scout_file):
            result = open(scout_file).read()
            print(f" (cached)", flush=True)
            scout_results.append(result)
            continue

        _scout_hdr = build_live_header(sbatch, date)
        _scout_hdr_block = _scout_hdr + "\n\n" if _scout_hdr else ""
        scout_user = (
            f"{_scout_hdr_block}"
            f"CANDIDATE STOCKS FOR THIS BATCH: {sbatch_str}\n\n"
            f"FUNDAMENTAL DATA:\n{fundamentals}\n\n"
            f"{brain_block}"
            f"{RUNNER_DNA}\n\n"
            f"Analyze ONLY the stocks in this batch: {sbatch_str}\n"
            "For each stock: Lynch category, scuttlebutt assessment, duration test, secret test, verdict.\n"
            f"End with: DISCOVERY - one stock NOT in [{stocks_str}] that better fits the AMD runner pattern."
            f"{catalyst_inject}"
        )
        result = call_claude(SCOUT_SYSTEM, scout_user, model=model, max_tokens=4000)
        # GAP 3: Auto-retry on truncation with compressed context
        if is_truncated(result):
            print(f"\n  WARNING: Scout batch {idx} truncated — retrying compressed", flush=True)
            _retry = _retry_with_compressed_ctx(
                SCOUT_SYSTEM, f"Scout {idx}", sbatch, fundamentals, model, 4000, date,
                f"Analyze ONLY: {sbatch_str}. Lynch category, scuttlebutt, duration, Thiel secret. "
                f"SCOUT VERDICT per stock: INVESTIGATE FURTHER / PASS. "
                f"DISCOVERY: one stock NOT in [{stocks_str}] fitting AMD/MU/SNDK runner DNA."
            )
            if len(_retry) > len(result):
                result = _retry

        # Run validators FIRST — before disk write or append
        try:
            from oracle_validators import run_all_validators
            # Build live prices dict for discovery price check
            _live_px = {s: _load_fund_cache(date).get(s, {}).get("price") for s in stocks}
            # Get fact sheet text if available (reuse canonical_facts built above)
            _fs_text = ""
            try:
                _canonical_sym = sbatch[0] if sbatch else (stocks[0] if stocks else "")
                if _canonical_sym in canonical_facts:
                    from oracle_factsheet import format_fact_sheet_for_panels as _ffs
                    _fs_text = _ffs(canonical_facts[_canonical_sym])
                else:
                    from oracle_factsheet import build_fact_sheet, format_fact_sheet_for_panels as _ffs
                    _fs_text = _ffs(build_fact_sheet(_canonical_sym))
            except Exception:
                pass
            result, validator_flags = run_all_validators("scout", result, _fs_text, str(sbatch), _live_px)
            if validator_flags:
                result += f"\n\n[VALIDATOR FLAGS: {'; '.join(validator_flags[:3])}]"
                print(f" [+{len(validator_flags)} validator flag(s)]", end="", flush=True)
        except Exception:
            pass  # validators are optional

        # S2: Discovery price validation — flag any discovery ticker whose price was cached vs live
        try:
            import re as _re2
            _disc_tickers = _re2.findall(r'DISCOVERY:\s*([A-Z]{1,5})\s*[—\-]', result)
            _disc_lines = []
            for _dt in _disc_tickers:
                if _dt in [s.upper() for s in stocks]:
                    continue  # already in run, skip
                try:
                    from oracle_factsheet import get_session_price as _gsp
                    _live_p = _gsp(_dt)
                    if _live_p:
                        _disc_lines.append(f"  {_dt} live price = ${_live_p:.2f} — verify thesis was written at this price")
                    else:
                        _disc_lines.append(f"  {_dt} — price unavailable")
                except Exception as _de:
                    _disc_lines.append(f"  {_dt} — price fetch failed ({_de})")
            if _disc_lines:
                result += "\n\nDISCOVERY PRICE CHECK:\n" + "\n".join(_disc_lines)
        except Exception:
            pass

        # Now append validated result and write to disk
        scout_results.append(result)
        write_layer_note(sbatch, "layer1_scout", result, date)
        # Write to disk
        with open(scout_file, "w") as f:
            f.write(result)

        print(" done.")

    results["scout"] = "\n\n---BATCH BREAK---\n\n".join(scout_results)

    # ── BUG 3: Discovery price check — fetch live prices for discovery stocks ──
    try:
        # Reserved words that are NOT tickers — filter these out
        _NOT_TICKERS = {"STOCK", "TICKER", "SYM", "ETF", "NYSE", "NASDAQ", "SEC",
                        "FDA", "CEO", "CFO", "TTM", "MRQ", "EPS", "YOY", "AI",
                        "BUY", "SELL", "PASS", "WATCH", "YES", "NO", "NA",
                        "OFF", "ON", "THE", "AND", "FOR", "NOT", "BUT", "ALL",
                        "NEW", "OLD", "TOP", "KEY", "DUE", "SET", "NET", "LOW",
                        "HIGH", "TAM", "DCF", "FCF", "IPO", "OTC", "ADR",
                        "NONE", "TBD", "NMF", "NAN", "NULL"}
        disc_tickers = list({
            t.upper() for t in re.findall(r'DISCOVERY[:\s\-]+([A-Z]{2,5})\b', results["scout"])
            if t.upper() not in stocks and t.upper() not in _NOT_TICKERS
        })
        if disc_tickers:
            from oracle_factsheet import get_session_price as _gsp
            disc_lines = ["\nDISCOVERY PRICE CHECK (live):"]
            for dt in disc_tickers:
                try:
                    _live_p = _gsp(dt)
                    if _live_p:
                        assumed_m = re.search(rf'\b{dt}\b[^\n]{{0,200}}\$(\d+\.?\d*)', results["scout"])
                        assumed   = float(assumed_m.group(1)) if assumed_m else None
                        if assumed and abs(_live_p - assumed) / _live_p > 0.20:
                            disc_lines.append(
                                f"  {dt} live price = ${_live_p:.2f} (analysis used ${assumed:.2f}) "
                                f"— WARNING: Panel thesis built on stale price — re-evaluate at current ${_live_p:.2f}"
                            )
                        else:
                            note = f" (analysis used ${assumed:.2f} — VALID)" if assumed else ""
                            disc_lines.append(f"  {dt} live price = ${_live_p:.2f}{note}")
                    else:
                        disc_lines.append(f"  {dt} — price unavailable")
                except Exception as _de:
                    disc_lines.append(f"  {dt} — price fetch failed ({_de})")
            results["scout"] += "\n" + "\n".join(disc_lines)
    except Exception:
        pass

    # ── Skeptic Panel (batched 2 stocks each) ────────────────────────
    skeptic_results = []
    for idx, sbatch in enumerate(batches, 1):
        if not sbatch:
            continue
        sbatch_str = ", ".join(sbatch)
        call_num = n_batches + idx
        skeptic_file = os.path.join(run_dir, f"skeptic_{idx}.md")
        print(f"  [{call_num}/{total_calls}] Skeptic Panel Batch {idx} ({sbatch_str})...", end="", flush=True)

        # ── CHECKPOINT RESUME ──
        if os.path.exists(skeptic_file):
            result = open(skeptic_file).read()
            print(f" (cached)", flush=True)
            skeptic_results.append(result)
            continue

        # Read the corresponding scout batch file
        scout_file = os.path.join(run_dir, f"scout_{idx}.md")
        scout_excerpt = open(scout_file).read()[:1200] if os.path.exists(scout_file) else ""

        # Insider transaction data — Form 4 data from oracle_factsheet.py is authoritative.
        # yfinance insider feed has been removed. Fact sheet EDGAR data is passed
        # via build_live_header() — no second insider feed needed here.
        insider_block = ""

        _skept_hdr = build_live_header(sbatch, date)
        _skept_hdr_block = _skept_hdr + "\n\n" if _skept_hdr else ""

        # Short seller context from preflight
        short_seller_block = ""
        try:
            from oracle_preflight import load_preflight_cache as _lpc
            _pf = _lpc(_sym if len(sbatch) == 1 else sbatch[0])
            _ss = _pf.get("short_seller_reports", [])
            if _ss:
                short_seller_block = (
                    f"\nSHORT SELLER REPORTS (must address these specific allegations):\n"
                    + "\n".join(f"  - {r}" for r in _ss[:3])
                    + "\nYour forensic analysis MUST start by evaluating each allegation above with specific evidence. "
                    + "Do not generate generic concerns when specific published allegations exist.\n"
                )
        except Exception:
            pass

        skeptic_user = (
            f"{_skept_hdr_block}"
            f"CANDIDATE STOCKS FOR THIS BATCH: {sbatch_str}\n\n"
            f"FUNDAMENTAL DATA:\n{fundamentals}\n\n"
            f"{RUNNER_DNA}\n\n"
            f"SCOUT FINDINGS FOR THIS BATCH:\n{scout_excerpt}\n\n"
            + (f"INSIDER ACTIVITY (12-month window — surface as evidence if significant):{insider_block}\n\n" if insider_block else "")
            + (short_seller_block if short_seller_block else "")
            + f"Apply forensic scrutiny to ONLY these stocks: {sbatch_str}\n"
            "For each stock: specific red flags, accounting concerns, narrative stress test.\n"
            "If insider SELLING is heavy (>$1M or zero buys with active selling), flag it explicitly.\n"
            "FORENSIC VERDICT: PASS / WARN / ELIMINATE with specific evidence.\n"
            "Be adversarial. Assume guilt until proven innocent.\n"
            "IMPORTANT: State your floor price (liquidation value) and a SPECIFIC base-case downside % separately from a stress-case downside %."
        )
        result = call_claude(SKEPTIC_SYSTEM, skeptic_user, model=model, max_tokens=4000)
        # GAP 3: Auto-retry on truncation
        if is_truncated(result):
            print(f"\n  WARNING: Skeptic batch {idx} truncated — retrying compressed", flush=True)
            _retry = _retry_with_compressed_ctx(
                SKEPTIC_SYSTEM, f"Skeptic {idx}", sbatch, fundamentals, model, 4000, date,
                f"Apply forensic scrutiny to ONLY: {sbatch_str}. "
                f"Red flags, accounting concerns, narrative stress test. "
                f"FORENSIC VERDICT: PASS / WARN / ELIMINATE with specific evidence."
            )
            if len(_retry) > len(result):
                result = _retry
        skeptic_results.append(result)
        write_layer_note(sbatch, "layer2_skeptic", result, date)
        with open(skeptic_file, "w") as f:
            f.write(result)
        print(" done.")

    results["skeptic"] = "\n\n---BATCH BREAK---\n\n".join(skeptic_results)

    # ── Fundamentals Panel (one call per batch, dynamic) ───────────────
    fund_results_list = []
    for bi, sbatch in enumerate(batches, 1):
        call_num = 2 * n_batches + bi
        sb_str   = ", ".join(sbatch)
        fund_file = os.path.join(run_dir, f"fundamental_{bi}.md")
        print(f"  [{call_num}/{total_calls}] Fundamental Panel Batch {bi} ({sb_str})...", end="", flush=True)

        # ── CHECKPOINT RESUME ──
        if os.path.exists(fund_file):
            fund_result = open(fund_file).read()
            print(f" (cached)", flush=True)
            fund_results_list.append(fund_result)
            results[f"fundamental_{bi}"] = fund_result
            continue

        scout_f_bi   = os.path.join(run_dir, f"scout_{bi}.md")
        skeptic_f_bi = os.path.join(run_dir, f"skeptic_{bi}.md")
        _scout_f   = open(scout_f_bi).read()[:1500]   if os.path.exists(scout_f_bi)   else results["scout"][:800]
        _skeptic_f = open(skeptic_f_bi).read()[:1500] if os.path.exists(skeptic_f_bi) else results["skeptic"][:800]

        _fund_hdr = build_live_header(sbatch, date)
        _fund_hdr_block = _fund_hdr + "\n\n" if _fund_hdr else ""
        fund_user = f"""{_fund_hdr_block}{context}

ANALYZE ONLY THESE STOCKS: {sb_str}

SCOUT FINDINGS:
{_scout_f}

FORENSIC FINDINGS:
{_skeptic_f}

You MUST analyze ALL stocks in the batch ({sb_str}). Do not skip any. Build the full fundamental model for each stock separately.
Floor value, ROIC, base rates, macro alignment, EV tree, catalyst.
Use the structured output format specified."""

        fund_result = call_claude(FUNDAMENTAL_SYSTEM, fund_user, model=model, max_tokens=4000)
        # GAP 3: Auto-retry on truncation
        if is_truncated(fund_result):
            print(f"\n  WARNING: Fundamentals batch {bi} truncated — retrying compressed", flush=True)
            _retry = _retry_with_compressed_ctx(
                FUNDAMENTAL_SYSTEM, f"Fundamentals {bi}", sbatch, fundamentals, model, 4000, date,
                f"Analyze ONLY: {sb_str}. "
                f"Floor value, ROIC, base rates, macro alignment, EV tree, catalyst. "
                f"Use the exact structured output format. Analyze each stock separately."
            )
            if len(_retry) > len(fund_result):
                fund_result = _retry
        fund_results_list.append(fund_result)
        results[f"fundamental_{bi}"] = fund_result
        write_layer_note(sbatch, "layer3a_fundamentals", fund_result, date)
        with open(fund_file, "w") as f:
            f.write(fund_result)
        print(" done.")

    results["fundamental"] = "\n\n".join(fund_results_list)

    # ── Tech + Macro Panel (one call per batch, dynamic) ────────────────
    macro_tech_list = []
    for bi, sbatch in enumerate(batches, 1):
        call_num = 3 * n_batches + bi
        sb_str   = ", ".join(sbatch)
        mt_file  = os.path.join(run_dir, f"macro_tech_{bi}.md")
        print(f"  [{call_num}/{total_calls}] Tech + Macro Panel Batch {bi} ({sb_str})...", end="", flush=True)

        # ── CHECKPOINT RESUME ──
        if os.path.exists(mt_file):
            mt_result = open(mt_file).read()
            print(f" (cached)", flush=True)
            macro_tech_list.append(mt_result)
            results[f"macro_tech_{bi}"] = mt_result
            continue

        _scout_mt   = (open(os.path.join(run_dir, f"scout_{bi}.md")).read()[:800]
                       if os.path.exists(os.path.join(run_dir, f"scout_{bi}.md"))
                       else results["scout"][:800])
        _skeptic_mt = (open(os.path.join(run_dir, f"skeptic_{bi}.md")).read()[:600]
                       if os.path.exists(os.path.join(run_dir, f"skeptic_{bi}.md"))
                       else results["skeptic"][:600])
        _fund_mt    = results.get(f"fundamental_{bi}", "")[:800]

        _mt_hdr = build_live_header(sbatch, date)
        _mt_hdr_block = _mt_hdr + "\n\n" if _mt_hdr else ""
        mt_user = (
            f"{_mt_hdr_block}"
            + context + "\n\n"
            f"ANALYZE ONLY THESE STOCKS: {sb_str}\n\n"
            "SCOUT FINDINGS:\n" + _scout_mt +
            "\n\nFORENSIC FINDINGS:\n" + _skeptic_mt +
            "\n\nFUNDAMENTAL FINDINGS:\n" + _fund_mt +
            "\n\nApply technology and macro lenses to the stocks listed above ONLY.\n"
            "S-curve, Wrights Law, disruption direction, cycle, reflexivity, macro regime.\n"
            "Use the structured output format specified.\n"
            "MANDATORY: Each verdict (FAVORABLE / AT RISK / AVOID / NEUTRAL) MUST include at least "
            "one sentence of specific supporting rationale. A verdict without rationale is invalid. "
            "For AT RISK specifically: name the exact macro headwind — rate environment, tariff exposure, "
            "FX risk, capex cycle reversal, or demand destruction. Generic 'macro headwind' is not acceptable."
        )
        mt_result = call_claude(MACRO_TECH_SYSTEM, mt_user, model=model, max_tokens=4000)
        # GAP 3: Auto-retry on truncation
        if is_truncated(mt_result):
            print(f"\n  WARNING: Tech+Macro batch {bi} truncated — retrying compressed", flush=True)
            _retry = _retry_with_compressed_ctx(
                MACRO_TECH_SYSTEM, f"Tech+Macro {bi}", sbatch, fundamentals, model, 4000, date,
                f"Apply technology and macro lenses to ONLY: {sb_str}. "
                f"S-curve, Wright's Law, disruption direction, cycle, reflexivity, macro regime. "
                f"Use the exact structured output format."
            )
            if len(_retry) > len(mt_result):
                mt_result = _retry
        macro_tech_list.append(mt_result)
        results[f"macro_tech_{bi}"] = mt_result
        write_layer_note(sbatch, "layer3b_techmacro", mt_result, date)
        with open(mt_file, "w") as f:
            f.write(mt_result)
        print(" done.")

    results["macro_tech"] = "\n\n".join(macro_tech_list)

    # ── Valuation Anchor Panel v4_14 (one call per stock) ────────────────
    va_call_num = 4 * n_batches + 1
    print(f"  [{va_call_num}/{total_calls}] Valuation Anchor (Reverse DCF / EPV)...", end="", flush=True)
    va_results = {}
    VA_SYSTEM = (
        "You are a cold, numbers-only Valuation Anchor analyst. "
        "You detect the appropriate valuation framework based on company stage:\n"
        "- GROWTH (rev growth >30%, FCF negative or thin): use REVERSE DCF — solve for what growth rate "
        "the current price implies, then assess achievability. "
        "Output: implied_growth_rate (%), achievable (yes/no), fair_value_bear ($), fair_value_bull ($).\n"
        "- INFLECTION (profitable, growing 15-30%): use forward P/E + PEG. "
        "Output: fair_value_base ($), peg_ratio, mos_pct (%).\n"
        "- MATURE (stable, FCF positive, <15% growth): use EPV (normalized EBIT / WACC). "
        "Output: epv ($), mos_pct (%), earnings_yield (%).\n"
        "You output ONLY valid JSON — no prose, no markdown. "
        'Format: {"TICKER": {"mode": "REVERSE_DCF|INFLECTION|MATURE", "signal": "BUY|PASS|AVOID", '
        '"epv": X.XX, "dcf": X.XX, "mos_pct": X, "implied_growth_pct": X, "achievable": "yes|no|unclear", '
        '"fair_value_bear": X.XX, "fair_value_bull": X.XX, "rationale": "one line"}}'
    )
    for stock in stocks:
        fd_s = fund_cache.get(stock, {})
        price        = fd_s.get("price") or 0
        trailing_eps = fd_s.get("trailing_eps") or fd_s.get("eps") or 0
        forward_eps  = fd_s.get("forward_eps") or 0
        rev_growth   = fd_s.get("rev_growth") or fd_s.get("revenue_growth") or 0
        mkt_cap      = fd_s.get("mkt_cap_str") or "unknown"
        rev_ttm      = fd_s.get("rev_ttm") or 0

        # Determine mode hint for the model
        if rev_growth and rev_growth > 30 or (trailing_eps <= 0 and forward_eps > 0):
            mode_hint = "GROWTH stage — use REVERSE DCF. Solve: what revenue/margin trajectory does the current price imply? Is it achievable?"
        elif rev_growth and 0 < rev_growth <= 30 and trailing_eps > 0:
            mode_hint = "INFLECTION stage — use forward P/E + PEG."
        else:
            mode_hint = "MATURE stage — use EPV (normalized EBIT / WACC)."

        # Build preflight header if available
        try:
            from oracle_preflight import build_preflight_header as _bph
            pf_block = _bph(stock)
        except Exception:
            pf_block = ""

        gm = fd_s.get("gross_margin")
        gm_str = f"{gm*100:.1f}%" if gm and isinstance(gm, float) else "unknown"
        fcf_margin_val = fd_s.get("fcf_margin")
        fcf_margin_str = f"{fcf_margin_val*100:.1f}%" if fcf_margin_val is not None else "unknown"

        # Compute Python reverse DCF — hard number for panel
        fcf_ps = fd_s.get("fcf_per_share") or 0
        rdcf_implied = _compute_reverse_dcf(price, fcf_ps) if fcf_ps > 0 else None
        rdcf_line = (
            f"PYTHON-COMPUTED REVERSE DCF: At current price ${price:.2f} with FCF/share ${fcf_ps:.2f}, "
            f"the market implies {rdcf_implied:.1f}% annual FCF growth for 10 years (10% discount, 20x terminal). "
            f"Assess whether this is achievable.\n"
            if rdcf_implied is not None else
            "PYTHON-COMPUTED REVERSE DCF: FCF/share unavailable — Claude must estimate from margins.\n"
        )

        va_user = (
            rdcf_line
            + f"Stock: {stock}\n"
            f"Price: ${price:.2f}\n"
            f"EPS TTM: {trailing_eps if trailing_eps else 'negative/unknown'}\n"
            f"Forward EPS (analyst consensus): {forward_eps if forward_eps else 'unknown'}\n"
            f"FCF per share: {fd_s.get('fcf_per_share', 'unknown')}\n"
            f"FCF Margin: {fcf_margin_str}\n"
            f"Revenue TTM: ${rev_ttm/1e9:.2f}B\n"
            f"Revenue Growth YoY: {rev_growth:.1f}%\n"
            f"Market Cap: {mkt_cap}\n"
            f"Gross Margin: {gm_str}\n\n"
            + (pf_block + "\n\n" if pf_block else "")
            + f"VALUATION MODE: {mode_hint}\n\n"
            "Rules:\n"
            "- If forward EPS is flagged UNVERIFIED in the data block above, use TTM EPS for all calculations.\n"
            "- Do NOT use EPV on a negative-EPS company — it will always return zero or negative.\n"
            "- If mode is REVERSE_DCF: state the implied revenue in year 5, implied net margin, and resulting EPS. "
            "Then state whether that growth trajectory is achievable given the competitive landscape.\n"
            "- Output ONLY valid JSON in the required format. No prose."
        )
        try:
            va_raw = call_claude(VA_SYSTEM, va_user, model=model, max_tokens=800)
            import json as _json
            _json_match = re.search(r'\{.*\}', va_raw, re.DOTALL)
            if _json_match:
                va_parsed = _json.loads(_json_match.group())
                if stock in va_parsed:
                    va_results[stock] = va_parsed[stock]
                else:
                    va_results[stock] = va_parsed
                # Validate — if epv and dcf are both 0.0 and mode is not REVERSE_DCF, flag as broken
                va_r = va_results[stock]
                if va_r.get("epv") == 0.0 and va_r.get("dcf") == 0.0 and va_r.get("mode") != "REVERSE_DCF":
                    va_r["rationale"] = f"[VA WARNING: zero outputs — inputs may be missing] {va_r.get('rationale','')}"
                # MoS=0% with BUY verdict is contradictory — override
                va_r = va_results[stock]
                mos = va_r.get("mos_pct")
                dcf = va_r.get("dcf")
                price_now = fund_cache.get(stock, {}).get("price", 0)
                if mos is not None and abs(mos) < 1 and va_r.get("signal") == "BUY":
                    va_r["signal"] = "HOLD"
                    va_r["rationale"] = f"[VA OVERRIDE: MoS={mos}% insufficient for BUY — changed to HOLD] {va_r.get('rationale','')}"
                # DCF anchoring detection: if DCF within 1% of current price, flag it
                if dcf and price_now and abs(dcf - price_now) / price_now < 0.01:
                    va_r["rationale"] = f"[VA WARNING: DCF=${dcf} matches current price exactly — possible anchoring] {va_r.get('rationale','')}"
                # None guard: if EPV and DCF both None on a profitable company, force simple calculation
                va_r = va_results[stock]
                if va_r.get("epv") is None and va_r.get("dcf") is None:
                    trailing_eps_val = fd_s.get("trailing_eps") or 0
                    fcf_val = fd_s.get("fcf_per_share") or 0
                    if trailing_eps_val > 0:
                        # Simple EPV: normalized earnings / WACC (8%)
                        simple_epv = round(trailing_eps_val / 0.08, 2)
                        va_r["epv"] = simple_epv
                        va_r["rationale"] = f"[AUTO-COMPUTED: EPV=TTM_EPS/WACC=${trailing_eps_val:.2f}/0.08=${simple_epv:.2f}] {va_r.get('rationale','')}"
                    elif fcf_val > 0:
                        # FCF positive but GAAP negative (SBC-heavy): use FCF-based EPV
                        simple_epv = round(fcf_val / 0.08, 2)
                        va_r["epv"] = simple_epv
                        va_r["rationale"] = f"[AUTO-COMPUTED: EPV=FCF/WACC=${fcf_val:.2f}/0.08=${simple_epv:.2f} — GAAP neg but FCF positive] {va_r.get('rationale','')}"
                    if va_r.get("dcf") is None and fcf_val > 0:
                        # Simple DCF: FCF * 15x
                        simple_dcf = round(fcf_val * 15, 2)
                        va_r["dcf"] = simple_dcf
                        va_r["rationale"] = (va_r.get("rationale", "") + f" [DCF=FCF×15=${fcf_val:.2f}×15=${simple_dcf:.2f}]").strip()
            else:
                va_results[stock] = {"signal": "PASS", "epv": None, "dcf": None, "mos_pct": None,
                                      "mode": "UNKNOWN", "rationale": "JSON parse failed"}
        except Exception as e:
            va_results[stock] = {"signal": "PASS", "epv": None, "dcf": None, "mos_pct": None,
                                  "mode": "ERROR", "rationale": f"error: {e}"}
        print(".", end="", flush=True)
    results["valuation_anchor"] = va_results
    print(" done.")

    # ── Summary Compiler (per stock, reads from disk) ──────────────────
    summary_call = 4 * n_batches + 2
    print(f"  [{summary_call}/{total_calls}] Compiling structured summary (per stock)...", end="", flush=True)

    stock_summaries = []

    # Map each stock to its batch number using the actual batches list
    stock_batch_map = {}
    for bn, batch in enumerate(batches, 1):
        for s in batch:
            stock_batch_map[s] = bn

    for stock in stocks:
        bn = stock_batch_map[stock]

        fund_file = os.path.join(run_dir, f"fundamental_{bn}.md")
        mt_file   = os.path.join(run_dir, f"macro_tech_{bn}.md")
        fund_text_raw = open(fund_file).read() if os.path.exists(fund_file) else ""
        mt_text_raw   = open(mt_file).read()   if os.path.exists(mt_file)   else ""
        # Fix 1: verdict lines are at the BOTTOM of each panel file — keep last 2000 chars
        # Truncating from the front would discard exactly the structured verdicts we need
        fund_text = fund_text_raw[-2000:] if len(fund_text_raw) > 2000 else fund_text_raw
        mt_text   = mt_text_raw[-2000:]   if len(mt_text_raw)   > 2000 else mt_text_raw

        scout_batch_file   = os.path.join(run_dir, f"scout_{bn}.md")
        skeptic_batch_file = os.path.join(run_dir, f"skeptic_{bn}.md")
        scout_excerpt   = open(scout_batch_file).read()[:600]   if os.path.exists(scout_batch_file)   else ""
        skeptic_excerpt = open(skeptic_batch_file).read()[:600] if os.path.exists(skeptic_batch_file) else ""

        _sum_hdr = build_live_header([stock], date)
        _sum_hdr_block = _sum_hdr + "\n\n" if _sum_hdr else ""
        _va_data = results.get("valuation_anchor", {}).get(stock, {})
        _va_str  = ""
        if _va_data:
            # S1 fix: if VA returned null outputs, set signal to ABSTAIN and exclude from vote
            _va_null = (_va_data.get("epv") is None and _va_data.get("fair_value_bear") is None)
            _va_signal = "ABSTAIN" if _va_null else _va_data.get("signal", "?")
            _va_mode   = _va_data.get("mode", "")
            _va_str = (
                f"\n\nVALUATION ANCHOR ({_va_mode}): signal={_va_signal} | "
                f"EPV=${_va_data.get('epv','?')} | DCF=${_va_data.get('dcf','?')} | "
                f"MoS={_va_data.get('mos_pct','?')}% | "
                f"Implied Growth={_va_data.get('implied_growth_pct','?')}% | "
                f"Bear=${_va_data.get('fair_value_bear','?')} | Bull=${_va_data.get('fair_value_bull','?')} | "
                f"{_va_data.get('rationale','')}"
                + (" [ABSTAIN — null output, excluded from panel vote count]" if _va_null else "")
            )
        per_stock_user = (
            f"{_sum_hdr_block}"
            f"STOCK TO SUMMARIZE: {stock}\n\n"
            f"TODAY'S DATE: {date} — All catalyst dates MUST be after this date. "
            f"Do NOT reference events from {int(date[:4])-1} or earlier as future catalysts. "
            f"If a catalyst already occurred, reference it as context and identify the NEXT upcoming catalyst.\n\n"
            f"SCOUT FINDINGS (excerpt):\n{scout_excerpt}\n\n"
            f"SKEPTIC FINDINGS (excerpt):\n{skeptic_excerpt}\n\n"
            f"FUNDAMENTAL PANEL (full batch containing {stock}):\n{fund_text}\n\n"
            f"TECH+MACRO PANEL (full batch containing {stock}):\n{mt_text}"
            f"{_va_str}\n\n"
            f"Produce ONE structured summary block for {stock} ONLY using the exact format:\n"
            f"---STOCK: {stock}---\n"
            f"SCOUT: [verdict] | Category: [category] | Secret: [one line]\n"
            f"SKEPTIC: [verdict] | Key risk: [one line]\n"
            f"FUNDAMENTALS: [verdict] | Conviction: [X/10] | EV: [+/-X%]\n"
            f"TECH+MACRO: [verdict] | Macro: [TAILWIND/NEUTRAL/HEADWIND]\n"
            f"VALUATION_ANCHOR: [BUY/PASS/AVOID] | EPV: $X | DCF: $X | MoS: X%\n"
            f"PANEL_CONSENSUS: X/5 bullish — [HIGH CONSENSUS if 4-5 bullish / SPLIT if 2-3 / PANEL CONFLICT if Scout=BUY and Skeptic=ELIMINATE]\n"
            f"OVERALL: [BUY/WATCH/PASS] | Score: [X/10]\n"
            f"CATALYST: [specific future event after {date} + estimated date]\n"
            f"KILL CONDITION: [what sends it to zero]\n"
            f"---END---\n\n"
            f"PANEL_CONSENSUS rules: count how many of the 5 panels gave a bullish verdict. "
            f"INVESTIGATE/BUY/STRONG BUY/ACCELERATING = bullish (1 point). "
            f"HOLD/WATCH/NEUTRAL = partial (0.5 points, round down). "
            f"PASS/WARN/ELIMINATE/AT RISK/AVOID = bearish (0 points). "
            f"ABSTAIN (VA with null output) = excluded from vote count — do not count as bullish or bearish. "
            f"4-5 bullish = HIGH CONSENSUS. 2-3 = SPLIT. Scout BUY + Skeptic ELIMINATE = PANEL CONFLICT. "
            f"CATALYST must be a FUTURE event — if the obvious catalyst already occurred before {date}, identify the next one.\n"
            f"SCORING: The OVERALL Score X/10 must be explained. State which panels voted bullish and which voted bearish. "
            f"Example: 'Score 6/10: Scout=bullish, Fundamentals=partial, Skeptic=bearish, Tech=bullish, VA=pass — net 2.5/5 rounded to 6/10'.\n"
            f"CONFLICT RECONCILIATION: If any two panels produce directionally opposed verdicts (one BUY/INVESTIGATE, one PASS/ELIMINATE/AVOID), you MUST output a CONFLICT_RESOLUTION block answering:\n"
            f"1. Which framework is more appropriate for this company's current stage (growth/inflection/mature)?\n"
            f"2. CONVERGENCE_PRICE: At what price would both frameworks agree? (e.g., 'Tech+Macro bull thesis and Fundamentals converge at approximately $X-$Y where forward P/E compresses to Z')\n"
            f"3. RESOLUTION_TRIGGER: What single observable datapoint would resolve the disagreement?\n"
            f"Do NOT numerically average conflicting verdicts. A 9/10 BUY and a negative-EV PASS cannot both be correct — identify which one applies at the current price.\n"
            f"POSITION SIZING RECONCILIATION: If multiple panels suggest different position sizes, you MUST reconcile them explicitly. Rules: (1) Thorp's Kelly calculation is the ONLY quantitative framework — it dominates over qualitative estimates. (2) State the Kelly fraction used and why. (3) Qualitative inputs (Munger's 5%, Taleb's 5%) are risk overlays that REDUCE Kelly, not independent sizes. (4) Final output: one position size with formula shown: 'Kelly=X%, risk overlays applied: -Y%, final=Z%'. Do not average multiple sizes silently.\n"
            f"CATALYST THRESHOLD RULE: Set the positive re-rate trigger ABOVE existing management guidance, not at it. If management has guided X, the positive catalyst trigger must be 'guidance of X+10-15% or higher.' Meeting existing guidance does not re-rate the stock — beating it does. Similarly, set the negative re-rate trigger below the growth rate already embedded in the stock price.\n"
            f"CROSS-TICKER DISCOVERIES: If the Scout or any panel flagged a DIFFERENT ticker as a better opportunity, "
            f"do NOT include it in this summary block. Add it as: WATCHLIST_FLAG: [TICKER] — [one-line reason] after the ---END--- marker."
        )

        stock_result = call_claude(SUMMARY_SYSTEM, per_stock_user, model=model, max_tokens=1200)

        # Bug 6 — Route WATCHLIST_FLAG entries to a queue file, strip from summary
        watchlist_flags = re.findall(r'WATCHLIST_FLAG:\s*([A-Z]+)\s*[—\-]+\s*(.+)', stock_result)
        if watchlist_flags:
            wl_path = os.path.join(os.path.dirname(run_dir), "watchlist_queue.md")
            with open(wl_path, "a") as wf:
                for wl_ticker, wl_reason in watchlist_flags:
                    wf.write(f"- {date} | {stock} run | {wl_ticker} — {wl_reason.strip()}\n")
            # Strip WATCHLIST_FLAG lines from active summary
            stock_result = re.sub(r'WATCHLIST_FLAG:.*\n?', '', stock_result)
            print(f" [+{len(watchlist_flags)} watchlist flag(s) routed to queue]", end="", flush=True)

        stock_summaries.append(stock_result)
        if is_truncated(stock_result):
            print(f"\n  WARNING: Summary for {stock} may be truncated", flush=True)
        write_layer_note([stock], "layer5_summary", stock_result, date)
        _verdict_m  = re.search(r'OVERALL:\s*(\w+)', stock_result)
        _convict_m  = re.search(r'Score:\s*(\d+)/10', stock_result)
        _verdict    = _verdict_m.group(1)  if _verdict_m  else "UNKNOWN"
        _conviction = _convict_m.group(1) if _convict_m else "?"
        write_ticker_notes(stock, fund_cache.get(stock, {}), date, _verdict, _conviction)
        print(f".", end="", flush=True)

    results["summary"] = "\n\n".join(stock_summaries)
    print(" done.")

    # ── Verdict ───────────────────────────────────────────────────────
    print(f"  [{total_calls}/{total_calls}] Synthesis (Munger + Thorp + Sleep + Taleb + Duke)...", end="", flush=True)
    _synth_hdr = build_live_header(stocks, date)
    _synth_hdr_block = _synth_hdr + "\n\n" if _synth_hdr else ""
    verdict_user = (
        f"{_synth_hdr_block}"
        f"STOCKS: {stocks_str}\n\n"
        "STRUCTURED SUMMARY FROM ALL PANELS:\n" + results["summary"] +
        "\n\nSCOUT DISCOVERIES MENTIONED:\n" + results["scout"][-800:] +
        "\n\nDeliver final verdicts in the exact structured format. "
        "Every stock in the summary must get a verdict. "
        "Discovery pool, final watchlist ranked by conviction, what the panel missed."
    )
    results["verdict"] = call_claude(VERDICT_SYSTEM, verdict_user, model=model, max_tokens=6000)
    if is_truncated(results["verdict"]):
        print(f"\n  WARNING: Synthesis output may be truncated", flush=True)
    write_layer_note(stocks, "layer6_synthesis", results["verdict"], date)
    print(" done.")

    # GAP 1: Verdict reconciliation — flag Scout vs Synthesis conflicts
    results["reconcile"] = reconcile_verdicts(stocks, results)
    if results["reconcile"]:
        print(f"  Verdict conflicts detected — reconciliation block generated.")

    # GAP 2: Catalyst date validation — detect past dates, fetch upcoming via yfinance
    results["catalyst_warnings"] = validate_catalyst_dates(stocks, results["verdict"], date)
    if results["catalyst_warnings"]:
        print(f"  Catalyst date warnings generated.")

    print("\n  Saving report...", end="", flush=True)
    return results


def run_deep(stocks: list, fundamentals: str, model: str,
             screener_context: str = "", date: str = None, mode: str = "deep") -> dict:
    """Run all 29 investors separately - deep mode."""
    print("  [Deep mode] Running all 29 investors separately...")
    print("  Note: This uses ~145 API calls. Use composite mode for daily runs.\n")
    return run_composite(stocks, fundamentals, model, screener_context, date=date, mode=mode)


def save_output(results: dict, stocks: list, date: str, mode: str) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    topic = "_".join(stocks)
    filename = f"ORACLE_{topic}_{date}_{mode}.md"
    path = os.path.join(OUT_DIR, filename)

    content = f"# ORACLE Think Tank — {', '.join(stocks)}\n"
    content += f"Date: {date} | Mode: {mode} | 29 lenses, 14 calls (2-stock batches, disk-first)\n\n---\n\n"

    # GAP 4: Prepend urgent catalyst header if present
    if results.get("header_flag"):
        content += results["header_flag"] + "\n\n"

    labels = {
        "scout":          "## LAYER 1 — SCOUT: Fisher + Lynch + Li Lu + Thiel\n\n",
        "skeptic":        "## LAYER 2 — SKEPTIC: Burry + Chanos + Block + Tilson + Greenberg\n\n",
        "fundamental_1":  "## LAYER 3a — FUNDAMENTALS Batch 1: Greenblatt + Pabrai + Klarman + Greenwald + Mauboussin + Druckenmiller + Miller + Einhorn\n\n",
        "fundamental_2":  "## LAYER 3a — FUNDAMENTALS Batch 2: (continued)\n\n",
        "fundamental_3":  "## LAYER 3a — FUNDAMENTALS Batch 3: (continued)\n\n",
        "macro_tech_1":   "## LAYER 3b — TECH + MACRO Batch 1: Wood + Kessler + Christensen + Marks + Soros + Dalio + Rogers\n\n",
        "macro_tech_2":   "## LAYER 3b — TECH + MACRO Batch 2: (continued)\n\n",
        "macro_tech_3":   "## LAYER 3b — TECH + MACRO Batch 3: (continued)\n\n",
        "summary":        "## LAYER 5 — STRUCTURED SUMMARY (All Panels)\n\n",
        "verdict":        "## LAYER 6 — SYNTHESIS: Munger + Thorp + Sleep + Taleb + Duke\n\n",
    }

    for key in ["scout", "skeptic", "fundamental_1", "fundamental_2", "fundamental_3", "macro_tech_1", "macro_tech_2", "macro_tech_3", "summary", "verdict"]:
        # GAP 1: Prepend verdict reconciliation block immediately before synthesis section
        if key == "verdict" and results.get("reconcile"):
            content += results["reconcile"] + "\n\n"
        if key in results and results[key]:
            content += labels.get(key, f"## {key.upper()}\n\n")
            content += results[key] + "\n\n---\n\n"
            # GAP 2: Append catalyst date warnings after synthesis section
            if key == "verdict" and results.get("catalyst_warnings"):
                content += results["catalyst_warnings"] + "\n\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def update_obsidian_watchlist(results: dict, stocks: list, date: str, report_path: str):
    """Extract discovery picks from verdict and append to Obsidian watchlist."""
    import re
    watchlist_path = os.path.expanduser("~/Documents/Trading Vault/01_Watchlist/ORACLE_WATCHLIST.md")
    os.makedirs(os.path.dirname(watchlist_path), exist_ok=True)

    # Create watchlist if it doesn't exist
    if not os.path.exists(watchlist_path):
        header = "# ORACLE Watchlist — Discovery Picks\n\n"
        header += "Auto-populated from Think Tank runs. Review weekly.\n\n"
        header += "| Date | Ticker | Conviction | Catalyst | Source Report |\n"
        header += "|------|--------|------------|----------|---------------|\n"
        with open(watchlist_path, "w") as f:
            f.write(header)

    # Extract BUY/WATCH verdicts from summary
    summary = results.get("summary", "")
    verdict = results.get("verdict", "")

    lines_to_add = []
    report_name = os.path.basename(report_path)

    # Parse the structured summary blocks
    blocks = re.findall(r'---STOCK: ([A-Z]+)---(.*?)---END---', summary, re.DOTALL)
    for ticker, block in blocks:
        overall_match = re.search(r'OVERALL: (BUY|WATCH)', block)
        conviction_match = re.search(r'Score: (\d+)/10', block)
        catalyst_match = re.search(r'CATALYST: (.+)', block)

        if overall_match:
            status = overall_match.group(1)
            conviction = conviction_match.group(1) if conviction_match else "?"
            catalyst = catalyst_match.group(1)[:60] if catalyst_match else "See report"
            lines_to_add.append(f"| {date} | {ticker} | {conviction}/10 | {catalyst} | {report_name} |\n")

    if lines_to_add:
        with open(watchlist_path, "a") as f:
            for line in lines_to_add:
                f.write(line)
        print(f"\n  Watchlist updated: {len(lines_to_add)} picks added to ORACLE_WATCHLIST.md")

    return watchlist_path


def append_session_note(stocks: list, date: str, report_path: str, results: dict):
    """Append a dated session entry to Trading Vault/02_Session_Notes/ORACLE_sessions.md"""
    notes_path = os.path.expanduser(
        "~/Documents/Trading Vault/02_Session_Notes/ORACLE_sessions.md"
    )
    os.makedirs(os.path.dirname(notes_path), exist_ok=True)

    if not os.path.exists(notes_path):
        with open(notes_path, "w") as f:
            f.write("# ORACLE Session Notes\nAuto-appended after every Think Tank run.\n\n---\n\n")

    verdicts = ""
    summary = results.get("summary", "")
    blocks = re.findall(r"---STOCK: ([A-Z]+)---(.*?)---END---", summary, re.DOTALL)
    for ticker, block in blocks:
        overall = re.search(r"OVERALL: (\w+)", block)
        conviction = re.search(r"Score: (\d+)/10", block)
        consensus = re.search(r"PANEL_CONSENSUS: ([^\n]+)", block)
        v = overall.group(1) if overall else "?"
        c = conviction.group(1) if conviction else "?"
        con = consensus.group(1).strip() if consensus else "?"
        verdicts += f"  - {ticker}: {v} {c}/10 | {con}\n"

    if not verdicts:
        verdicts = "  (see full report)\n"

    report_name = os.path.basename(report_path) if report_path else "unknown"
    entry = (
        f"## {date} — {', '.join(stocks)}\n\n"
        f"**Report:** `{report_name}`\n\n"
        f"**Verdicts:**\n"
        f"{verdicts}\n"
        f"---\n\n"
    )

    try:
        with open(notes_path, "a") as f:
            f.write(entry)
        print(f"  Session note appended: Trading Vault/02_Session_Notes/ORACLE_sessions.md")
    except Exception as _sne:
        print(f"  [Session notes] Warning: could not append — {_sne}")


# ══════════════════════════════════════════════════════════════════
# GAP 5 — Price consistency scan (post-assembly report check)
# ══════════════════════════════════════════════════════════════════

def scan_price_consistency(report_path: str, stocks: list, session_prices: dict) -> str:
    """
    Scan the assembled report for $XX.XX price mentions near each ticker.
    Compare to session_prices {ticker: live_price}.
    Appends PRICE CONSISTENCY WARNINGS block to report file for any mismatch >5%.
    Returns the warning block string or "".
    """
    if not session_prices or not report_path or not os.path.exists(report_path):
        return ""

    try:
        with open(report_path, encoding="utf-8") as fh:
            report_text = fh.read()
    except Exception as _re:
        print(f"  [PriceCheck] Could not read report: {_re}")
        return ""

    warnings = []

    for ticker in stocks:
        live_price = session_prices.get(ticker)
        if not live_price or live_price <= 0:
            continue

        # Match $XX.XX within 200 chars of the ticker (either side); exclude market cap ($X.XB)
        pattern = re.compile(
            rf'(?:\b{re.escape(ticker)}\b.{{0,200}}\$(\d+\.?\d+)(?!B)'
            rf'|\$(\d+\.?\d+)(?!B).{{0,200}}\b{re.escape(ticker)}\b)',
            re.DOTALL
        )

        flagged = set()
        for m in pattern.finditer(report_text):
            p_str = m.group(1) or m.group(2)
            try:
                p = float(p_str)
                if p > 0 and abs(p - live_price) / live_price > 0.05:
                    flagged.add(round(p, 2))
            except ValueError:
                pass

        for bad_price in sorted(flagged):
            pct = (bad_price - live_price) / live_price * 100
            warnings.append(
                f"- **{ticker}**: Report contains ${bad_price:.2f} vs live ${live_price:.2f} "
                f"({pct:+.1f}%)"
            )

    if not warnings:
        return ""

    block = (
        "\n\n---\n\n## PRICE CONSISTENCY WARNINGS\n\n"
        "> Price mentions in this report differ >5% from session live prices. "
        "May reflect stale cache or LLM training knowledge.\n\n"
        + "\n".join(warnings) + "\n"
    )

    try:
        with open(report_path, "a", encoding="utf-8") as fh:
            fh.write(block)
        print(f"\n  PRICE CONSISTENCY: {len(warnings)} warning(s) appended to report.")
    except Exception as _we:
        print(f"  [PriceCheck] Could not append warnings to report: {_we}")

    return block


def main():
    parser = argparse.ArgumentParser(
        description="ORACLE Think Tank - 29 investor lenses, 6 composite calls"
    )
    parser.add_argument(
        "--stocks", nargs="+",
        default=[],
        help="Stocks to analyze (auto-populated by screener in pipeline mode)"
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="Use Haiku (~$0.12 vs ~$0.70 for sonnet)"
    )
    parser.add_argument(
        "--deep", action="store_true",
        help="Run all 29 investors separately (~$3-6, maximum depth)"
    )
    parser.add_argument(
        "--no-search", action="store_true",
        help="Skip live fundamentals pull"
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Force fresh data fetch — emphasizes most-recent-quarter figures in the query"
    )
    parser.add_argument(
        "--screener-context", type=str, default="",
        help="Screener table text to inject as context (from oracle_runner_screener)"
    )
    parser.add_argument("--preflight-override", action="store_true",
                        help="Bypass pre-flight halt (not recommended)")
    parser.add_argument("--skip-validation", action="store_true",
                        help="Skip fact sheet validation (not recommended)")
    args = parser.parse_args()

    if not OR_KEY:
        print("ERROR: OPENROUTER_API_KEY not found in ~/.hermes/.env")
        sys.exit(1)

    stocks = [s.upper() for s in args.stocks]
    model  = HAIKU if args.fast else SONNET
    mode   = "deep" if args.deep else ("fast" if args.fast else "composite")
    date   = datetime.date.today().strftime("%Y%m%d")
    cost   = "~$0.35" if args.fast else ("~$3-6" if args.deep else "~$2.20")
    screener_context = args.screener_context

    print(f"\n{'='*58}")
    print(f"  ORACLE THINK TANK v3")
    print(f"  Stocks:  {', '.join(stocks)}")
    print(f"  Mode:    {mode} | Model: {model.split('/')[-1]}")
    print(f"  Cost:    {cost}")
    print(f"  Calls:   {'~145' if args.deep else '14 batched calls (2 stocks each, disk-first)'}")
    if screener_context:
        print(f"  Source:  Screener pipeline (DNA scores included in context)")
    print(f"{'='*58}\n")

    # ── Phase 0-A: Pre-flight data validation ─────────────────────────────
    preflight_reports = {}
    if HAS_PREFLIGHT and not args.no_search:
        preflight_reports = run_preflight(stocks, verbose=True)
        halted = [t for t, r in preflight_reports.items() if r.halted]
        if halted and not getattr(args, 'preflight_override', False):
            print(f"\nPRE-FLIGHT HALT: {', '.join(halted)}")
            print("Run with --preflight-override to bypass (not recommended).")
            sys.exit(1)

    # ── Phase 0-B: Fact sheet validation ──────────────────────────────────
    if not args.skip_validation and not args.no_search:
        print("  Running fact sheet data validation...")
        try:
            from oracle_factsheet import build_fact_sheet, CACHE_DIR as _FS_CACHE
            import pathlib as _pl_cli
            import datetime as _dt_cli

            _cli_failures = []
            for _sym in stocks:
                # Clear cache for fresh fetch
                for _cf in _pl_cli.Path(_FS_CACHE).glob(f"factsheet_{_sym}_*.json"):
                    _cf.unlink(missing_ok=True)

                _fs = build_fact_sheet(_sym)
                _pr = _fs.get("press_release", {})
                _legal = _fs.get("legal_proceedings", {})
                _metrics = _fs.get("metrics", {})

                # Gross margin check
                _gm = (_pr.get("gross_margin_gaap") or {}).get("value")
                if _gm is not None and (_gm > 1.0 or _gm < 0):
                    _cli_failures.append(f"{_sym}: Gross margin {_gm*100:.1f}% impossible — wrong field extracted")

                # XBRL staleness
                _rp = (_metrics.get("revenue_ttm") or {}).get("period", "")
                if _rp and len(_rp) >= 4:
                    try:
                        if _dt_cli.date.today().year - int(_rp[:4]) > 2:
                            _cli_failures.append(f"{_sym}: XBRL revenue period {_rp} is stale (>2yr old)")
                    except (ValueError, TypeError):
                        pass

                # 8-K age
                _fd_str = _pr.get("filing_date", "")
                if _fd_str:
                    _fd_dt = _dt_cli.date.fromisoformat(_fd_str)
                    _age = (_dt_cli.date.today() - _fd_dt).days
                    if _age > 90:
                        print(f"  WARNING: {_sym} 8-K is {_age} days old — may be missing recent earnings")

            if _cli_failures:
                print(f"\n  FACT SHEET VALIDATION FAILED:")
                for f in _cli_failures:
                    print(f"    \u2717 {f}")
                print(f"\n  Run aborted. Use --skip-validation to override (not recommended).")
                print(f"  Run oracle_validate.py {' '.join(stocks)} to diagnose.\n")
                sys.exit(1)
            else:
                print(f"  \u2713 Fact sheet validation passed.")

        except Exception as _fv_cli_e:
            print(f"  Fact sheet validation skipped: {_fv_cli_e}")

    # Pull fundamentals via data layer (Phase 0), fallback to legacy function
    if args.no_search:
        fundamentals = "Use your training knowledge for current fundamentals."
    elif _HAS_DATA_LAYER:
        fundamentals = format_fundamentals_batch(stocks, fresh=args.fresh)
    else:
        fundamentals = get_fundamentals(stocks, fresh=args.fresh)

    # Run the panel
    if args.deep:
        results = run_deep(stocks, fundamentals, model, screener_context, date=date, mode=mode)
    else:
        results = run_composite(stocks, fundamentals, model, screener_context, date=date, mode=mode)

    # Validate completeness before saving
    validate_run_completeness(stocks, date)

    # Save
    path = save_output(results, stocks, date, mode)
    watchlist_path = update_obsidian_watchlist(results, stocks, date, path)

    # GAP 5: Price consistency scan — flag report price mentions >5% from session live prices
    _session_prices = {}
    _sc_cache = _load_fund_cache(date)
    for _sym in stocks:
        _d = _sc_cache.get(_sym, {})
        if _d and not _d.get("error") and _d.get("price"):
            try:
                _session_prices[_sym] = float(_d["price"])
            except (TypeError, ValueError):
                pass
    scan_price_consistency(path, stocks, _session_prices)

    # Update persistent brain memory
    if _HAS_BRAIN:
        try:
            brain_entries = parse_run_for_brain(
                results, stocks,
                fundamentals=fundamentals,
                screener_context=screener_context
            )
            append_to_brain(brain_entries, report_path=path)
            print(f"  Brain updated: ~/Documents/Trading Vault/04_Bot_Rules/ORACLE_BRAIN.md")
        except Exception as _be:
            print(f"  [Brain] Warning: brain update failed — {_be}")

    # Append session note to Trading Vault
    append_session_note(stocks, date, path, results)

    # Print verdict section
    print(f"\n{'='*58}")
    print(f"  FINAL SYNTHESIS")
    print(f"{'='*58}\n")
    print(results.get("verdict", "No verdict generated."))

    print(f"\n{'='*58}")
    print(f"  Full report: {path}")
    print(f"{'='*58}\n")


if __name__ == "__main__":
    main()
