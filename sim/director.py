#!/usr/bin/env python3
"""
ORACLE Phase 2 — director.py
Director injections that stress-test agent conviction across rounds.
"""

import random


class Director:
    """
    Injects curated narrative shocks at specific rounds to force agents to reveal
    genuine conviction. Round 6 dynamically targets the lowest-probability stock.
    Fix #8: injections carry category tags; same category cannot fire consecutively.
    Fix #19: NEUTRAL/ambiguous injection type added.
    """

    # Each injection carries a category: macro / regulatory / competitive / insider / analyst / ambiguous
    INJECTIONS = {
        3: {
            "text": (
                "MACRO SHOCK: Federal Reserve signals 75bps rate hike. Risk assets selling off. "
                "Nasdaq down 4%. Which of your holdings do you hold through a 20% market drawdown "
                "and which do you cut first?"
            ),
            "category": "macro",
        },
        4: {
            "text": (
                "FDA WATCH: A competitor in a similar rare disease indication received a Complete "
                "Response Letter citing manufacturing concerns. Regulatory environment is tightening. "
                "How does this change your biotech conviction?"
            ),
            "category": "regulatory",
        },
        5: {
            "text": (
                "COMPETITIVE THREAT: A major hyperscaler just announced native data and AI capabilities "
                "shipping Q3, bundled into existing enterprise agreements at no additional cost. "
                "Which software or data company in this batch is most exposed?"
            ),
            "category": "competitive",
        },
        7: {"text": "", "category": None},  # Free round — no injection
    }

    RESERVE = [
        {
            "text": (
                "INSIDER SELLING: Form 4 filings show C-suite selling shares at current prices "
                "across the sector. Which position concerns you most and why?"
            ),
            "category": "insider",
            "direction": "negative",
        },
        {
            "text": (
                "ANALYST DOWNGRADE: A bulge bracket firm cuts price targets citing valuation "
                "compression. Which stock in this batch is most exposed to multiple compression?"
            ),
            "category": "analyst",
            "direction": "negative",
        },
        {
            "text": (
                "EARNINGS WHISPER: Channel checks suggest next earnings may disappoint consensus "
                "for high-multiple names. Which stock carries the most execution risk?"
            ),
            "category": "competitive",
            "direction": "negative",
        },
        {
            "text": (
                "NEUTRAL DATA POINT: New industry survey results are mixed — some segments are "
                "accelerating while others are slowing. No clear directional signal. Agents must "
                "interpret what this means for each name based on their own thesis. Does this data "
                "confirm or challenge your current conviction?"
            ),
            "category": "ambiguous",
            "direction": "neutral",
        },
        # ── Positive injections (Layer 1 fix: balance rule requires min 2 net-positive) ──
        {
            "text": (
                "EARNINGS BEAT: A leading company in this sector just reported revenue and margins "
                "significantly above consensus. Management raised full-year guidance. Institutional "
                "buyers are moving in. Which names in this batch benefit most from sector re-rating?"
            ),
            "category": "analyst",
            "direction": "positive",
        },
        {
            "text": (
                "REGULATORY TAILWIND: A competitor in the same indication received accelerated "
                "approval, validating the platform and reducing regulatory risk across the category. "
                "Which names benefit most from this precedent?"
            ),
            "category": "regulatory",
            "direction": "positive",
        },
        {
            "text": (
                "MACRO PIVOT: Federal Reserve signals a pause in rate hikes, citing cooling "
                "inflation. Growth assets are re-rating. Which names in this batch have the most "
                "upside from multiple expansion in a falling rate environment?"
            ),
            "category": "macro",
            "direction": "positive",
        },
        {
            "text": (
                "STRATEGIC CATALYST: A major enterprise customer publicly endorses one of the "
                "platform companies in this batch as their primary vendor for the next 5 years. "
                "Contract size implies 40%+ revenue growth acceleration. How does this change "
                "your conviction ranking?"
            ),
            "category": "competitive",
            "direction": "positive",
        },
        {
            "text": (
                "MANAGEMENT SIGNAL: The CEO of one of the beaten-down names in this batch "
                "announces a significant personal share purchase at current prices — largest "
                "insider buy in 3 years. Which name is this most credible for, and does it "
                "change your conviction?"
            ),
            "category": "insider",
            "direction": "positive",
        },
    ]

    def __init__(self, stocks, seed=None, fundamentals=None):
        if seed is not None:
            random.seed(seed)
        self.stocks                  = list(stocks)
        self.single_stock_mode       = len(stocks) == 1   # restructured Round 6
        self.fundamentals            = fundamentals or {}  # for sector-aware injections
        self._reserve                = list(self.RESERVE)
        self._divergence_call_count  = 0
        self._last_category          = None
        self._fired_categories       = []
        self._fired_directions       = []
        self.injection_log           = []   # v4_6: per-round injection log

        # Layer 1 fix: enforce positive injection balance before seeding
        self._enforce_positive_balance()

    def _detect_sector(self):
        """Detect dominant sector across stocks for context-aware injections."""
        _BIOTECH = {"biotechnology", "healthcare", "biopharmaceuticals", "pharmaceuticals"}
        _SEMI    = {"semiconductors", "semiconductor", "electronic components"}
        _SAAS    = {"software", "software—application", "software - application",
                    "software infrastructure", "software—infrastructure"}
        _DEFENSE = {"aerospace", "defense", "aerospace & defense"}
        _ENERGY  = {"energy", "oil", "utilities", "power"}

        sector_votes = {"biotech": 0, "semi": 0, "saas": 0, "defense": 0, "energy": 0, "other": 0}
        for ticker in self.stocks:
            fd  = self.fundamentals.get(ticker, {})
            sec = (fd.get("sector") or "").lower()
            ind = (fd.get("industry") or "").lower()
            text = sec + " " + ind
            if any(b in text for b in _BIOTECH):
                sector_votes["biotech"] += 1
            elif any(b in text for b in _SEMI):
                sector_votes["semi"] += 1
            elif any(b in text for b in _SAAS):
                sector_votes["saas"] += 1
            elif any(b in text for b in _DEFENSE):
                sector_votes["defense"] += 1
            elif any(b in text for b in _ENERGY):
                sector_votes["energy"] += 1
            else:
                sector_votes["other"] += 1
        return max(sector_votes, key=sector_votes.get)

    def _get_round4_injection(self):
        """Return a sector-appropriate, ticker-specific regulatory injection for Round 4."""
        sector = self._detect_sector()
        ticker = self.stocks[0] if len(self.stocks) == 1 else "companies in this batch"
        tickers_str = ", ".join(self.stocks[:3])

        if sector == "biotech":
            return (
                f"FDA WATCH: A competitor in a similar indication to {ticker} received a Complete Response Letter "
                f"citing manufacturing concerns. Regulatory environment is tightening across rare disease and specialty pharma. "
                f"How does this change your conviction on {tickers_str}, and what is the probability this sets a precedent "
                f"that delays or blocks {ticker}'s own regulatory pathway?"
            )
        elif sector == "semi":
            return (
                f"EXPORT CONTROL ESCALATION: The Commerce Department has expanded BIS entity list restrictions "
                f"on advanced semiconductor connectivity chips — the exact product category {ticker} sells. "
                f"A new license requirement is under review that could restrict sales to key Asian hyperscaler customers. "
                f"CFIUS is also reviewing a key {ticker} substrate supplier acquisition. "
                f"How much of {ticker}'s revenue is at risk, and does this change the supply chain thesis?"
            )
        elif sector == "saas":
            return (
                f"REGULATORY HEADWIND — {ticker}: The FTC has opened a formal investigation into {ticker}'s "
                f"subscription bundling practices and cancellation friction — specifically the same practices "
                f"that drove {ticker}'s high net revenue retention. The EU has issued a preliminary ruling "
                f"requiring data portability that could commoditize switching costs. "
                f"If {ticker} is forced to unbundle or simplify cancellation, what happens to NRR and ARR?"
            )
        elif sector == "defense":
            return (
                f"DEFENSE BUDGET WATCH — {ticker}: A continuing resolution has frozen new program starts, "
                f"and the latest budget proposal signals a 15% cut to discretionary defense spending. "
                f"Which of {ticker}'s programs have the highest contract concentration risk, "
                f"and what is the revenue downside if key programs are restructured or cancelled?"
            )
        elif sector == "energy":
            return (
                f"REGULATORY PIVOT — {ticker}: The administration has announced accelerated reversal of "
                f"permitting reforms and new methane regulations that increase operating costs 8-12% for producers. "
                f"How does this affect {ticker}'s margins and capital allocation, "
                f"and does it change the thesis on this name?"
            )
        else:
            return (
                f"REGULATORY HEADWIND — {ticker}: A major regulatory agency has announced an investigation into "
                f"competitive practices specifically targeting {ticker}'s market segment, "
                f"citing potential antitrust concerns around pricing power and market concentration. "
                f"Companies may face forced divestitures or operating restrictions within 18 months. "
                f"What is the worst-case regulatory outcome for {ticker} and how does it affect your conviction?"
            )

    def _enforce_positive_balance(self):
        """
        Layer 1 fix: Before seeding, ensure the reserve pool contains at least
        2 net-positive injections. If not, rotate positive injections to the front
        so they fire first when divergence triggers.
        """
        positive = [i for i in self._reserve if i.get("direction") == "positive"]
        if len(positive) < 2:
            print("  WARNING: Injection pool has fewer than 2 positive injections — balance check failed.")
            return
        # Move positive injections to front of reserve so they fire when balance is needed
        negative = [i for i in self._reserve if i.get("direction") != "positive"]
        # Interleave: pos, neg, pos, neg... ensures balance
        balanced = []
        pos_iter = iter(positive)
        neg_iter = iter(negative)
        pos_turn = True
        while True:
            try:
                if pos_turn:
                    balanced.append(next(pos_iter))
                else:
                    balanced.append(next(neg_iter))
                pos_turn = not pos_turn
            except StopIteration:
                break
        # Add remaining
        remaining_pos = list(pos_iter)
        remaining_neg = list(neg_iter)
        self._reserve = balanced + remaining_pos + remaining_neg
        pos_count = len([i for i in self._reserve if i.get("direction") == "positive"])
        print(f"  Injection pool balanced: {pos_count} positive / {len(self._reserve)-pos_count} negative+neutral")

    def get_injection(self, round_num, market_probs):
        """
        Return the injection string for this round.
        Round 4: sector-aware regulatory injection.
        Round 6 multi-stock: dynamically targets lowest-probability ticker.
        Round 6 single-stock: stock-specific adversarial thesis attack.
        """
        if round_num == 4:
            text = self._get_round4_injection()
            cat  = "regulatory"
            self._last_category = cat
            self._fired_categories.append(cat)
            self._fired_directions.append("negative")
            self.injection_log.append({
                "round":     4,
                "category":  cat,
                "direction": "negative",
                "text":      text[:80],
                "impact":    1.0,
            })
            return text
        if round_num == 6:
            if self.single_stock_mode:
                return self._build_round6_single_stock(market_probs)
            else:
                return self._build_round6_injection(market_probs)
        entry = self.INJECTIONS.get(round_num)
        if entry:
            text = entry["text"]
            cat  = entry["category"]
            if text and cat:
                self._last_category = cat
                self._fired_categories.append(cat)
                self._fired_directions.append(entry.get("direction", "negative"))
                self.injection_log.append({
                    "round":     round_num,
                    "category":  cat,
                    "direction": entry.get("direction", "negative"),
                    "text":      text[:80],
                    "impact":    entry.get("strength", 1.0),
                })
            return text
        return ""

    def _build_round6_injection(self, market_probs):
        """Build dynamic ambiguous injection targeting the lowest-conviction stock."""
        if not market_probs:
            ticker = self.stocks[0] if self.stocks else "this stock"
        else:
            ticker = min(market_probs, key=lambda t: market_probs[t])
        text = (
            f"AMBIGUOUS CATALYST — {ticker}: New data has emerged that could support or "
            f"undermine the bull thesis. Agents must debate the implications before round 7."
        )
        self._last_category = "ambiguous"
        self._fired_categories.append("ambiguous")
        self.injection_log.append({
            "round":     6,
            "category":  "ambiguous",
            "direction": "neutral",
            "text":      text[:80],
            "impact":    1.0,
        })
        return text

    def _build_round6_single_stock(self, market_probs):
        """
        Single-stock adversarial injection for Round 6.
        Attacks the specific thesis vulnerabilities rather than generic ambiguity.
        The current probability tells us how convicted agents are — if high (>70%),
        hit the thesis hard on its most vulnerable dimensions:
        customer concentration, competitive moat durability, and execution risk.
        """
        ticker = self.stocks[0] if self.stocks else "this stock"
        prob   = market_probs.get(ticker, 0.5) if market_probs else 0.5

        if prob >= 0.80:
            # High conviction — maximum adversarial pressure on the core thesis
            text = (
                f"THESIS STRESS TEST — {ticker}: Devil's advocate round. "
                f"Assume the bull thesis is WRONG. Specifically: "
                f"(1) The company's largest customer announces it is building the same capability in-house, "
                f"eliminating the primary revenue wedge within 18 months. "
                f"(2) A well-capitalized competitor (Broadcom, Marvell, or equivalent) wins the key industry "
                f"standards vote, making {ticker}'s proprietary approach a dead end. "
                f"(3) The CFO or a key founder departs, signaling internal disagreement about strategy. "
                f"Every agent must now argue WHY THE CURRENT PRICE IS WRONG. "
                f"What is the bear case probability and what is the floor price if the thesis breaks?"
            )
            direction = "negative"
        elif prob >= 0.55:
            # Moderate conviction — balanced adversarial challenge
            text = (
                f"CONVICTION CHECK — {ticker}: Before locking in your final position, "
                f"stress-test your thesis on three dimensions: "
                f"(1) EXECUTION RISK — What does the next earnings miss look like and what causes it? "
                f"(2) VALUATION — At current multiples, how many years of perfect execution are already priced in? "
                f"(3) CONCENTRATION — If the top 2 customers reduce orders by 30%, what happens to the thesis? "
                f"Agents who remain bullish must quantify the margin of safety. "
                f"Agents who are bearish must state their specific exit trigger."
            )
            direction = "negative"
        else:
            # Low conviction — give the bull case a final chance
            text = (
                f"BULL CASE PRESSURE TEST — {ticker}: Conviction is low. "
                f"Before the final round, bulls must make their strongest possible case: "
                f"What is the one catalyst that would force the bears to cover? "
                f"What does the stock look like in 18 months if the management team executes perfectly? "
                f"State a specific price target with a specific assumption that drives it. "
                f"Bears must respond: what would change their mind?"
            )
            direction = "neutral"

        self._last_category = "thesis_attack"
        self._fired_categories.append("thesis_attack")
        self._fired_directions.append(direction)
        self.injection_log.append({
            "round":     6,
            "category":  "thesis_attack",
            "direction": direction,
            "text":      text[:80],
            "impact":    1.5,   # higher impact weight — this is the most important injection
        })
        return text

    def check_divergence(self, market_probs):
        """
        If spread between highest and lowest stock probability < 0.15,
        pop and return a reserve injection (convergence prevention).
        Skips the first call (round 1) — spread is always 0 at start because no posts exist.
        Fix #8: enforces category balancing — same category cannot fire consecutively.
        Returns injection string or empty string.
        """
        self._divergence_call_count += 1
        if self._divergence_call_count <= 1:
            return ""
        if len(market_probs) < 2:
            return ""
        probs  = list(market_probs.values())
        spread = max(probs) - min(probs)
        if spread < 0.15 and self._reserve:
            # Prefer an injection from a different category than last fired
            for i, candidate in enumerate(self._reserve):
                if candidate["category"] != self._last_category:
                    inj = self._reserve.pop(i)
                    self._last_category = inj["category"]
                    self._fired_categories.append(inj["category"])
                    self._fired_directions.append(inj.get("direction", "negative"))
                    self.injection_log.append({
                        "round":     self._divergence_call_count,
                        "category":  inj["category"],
                        "direction": inj.get("direction", "negative"),
                        "text":      inj["text"][:80],
                        "impact":    inj.get("strength", 1.0),
                    })
                    print(f"  DIVERGENCE ALERT: spread={spread:.2f} < 0.15 — "
                          f"firing {inj['category']} ({inj.get('direction','?')}) reserve injection.")
                    return inj["text"]
            # All reserve same category as last — pop first anyway
            inj = self._reserve.pop(0)
            self._last_category = inj["category"]
            self._fired_categories.append(inj["category"])
            self._fired_directions.append(inj.get("direction", "negative"))
            self.injection_log.append({
                "round":     self._divergence_call_count,
                "category":  inj["category"],
                "direction": inj.get("direction", "negative"),
                "text":      inj["text"][:80],
                "impact":    inj.get("strength", 1.0),
            })
            print(f"  DIVERGENCE ALERT: spread={spread:.2f} < 0.15 — firing reserve injection.")
            return inj["text"]
        return ""

    def category_coverage_check(self):
        """
        Warn if any expected injection category never fired across the simulation.
        Also report injection direction balance.
        Call at end of simulation to audit injection diversity.
        """
        expected = {"macro", "regulatory", "competitive", "insider", "analyst"}
        fired    = set(self._fired_categories)
        missing  = expected - fired
        if missing:
            print(f"  WARNING: Injection categories never fired: {', '.join(sorted(missing))}")
        else:
            print(f"  Injection coverage OK — all categories fired: {', '.join(sorted(fired & expected))}")
        pos = self._fired_directions.count("positive")
        neg = self._fired_directions.count("negative")
        neu = self._fired_directions.count("neutral")
        total = len(self._fired_directions)
        print(f"  Injection balance: {pos} positive / {neg} negative / {neu} neutral (total {total})")
        if pos < 2:
            print(f"  WARNING: Fewer than 2 positive injections fired — results may be systematically bearish biased.")
        return missing
