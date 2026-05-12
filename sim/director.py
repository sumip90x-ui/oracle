#!/usr/bin/env python3
"""
ORACLE Phase 2 — director.py
Director injections that stress-test agent conviction across rounds.
"""


class Director:
    """
    Injects curated narrative shocks at specific rounds to force agents to reveal
    genuine conviction. Round 6 dynamically targets the lowest-probability stock.
    """

    INJECTIONS = {
        3: (
            "MACRO SHOCK: Federal Reserve signals 75bps rate hike. Risk assets selling off. "
            "Nasdaq down 4%. Which of your holdings do you hold through a 20% market drawdown "
            "and which do you cut first?"
        ),
        4: (
            "FDA WATCH: A competitor in a similar rare disease indication received a Complete "
            "Response Letter citing manufacturing concerns. Regulatory environment is tightening. "
            "How does this change your biotech conviction?"
        ),
        5: (
            "COMPETITIVE THREAT: A major hyperscaler just announced native data and AI capabilities "
            "shipping Q3, bundled into existing enterprise agreements at no additional cost. "
            "Which software or data company in this batch is most exposed?"
        ),
        7: "",  # Free round — no injection
    }

    RESERVE = [
        (
            "INSIDER SELLING: Form 4 filings show C-suite selling shares at current prices "
            "across the sector. Which position concerns you most and why?"
        ),
        (
            "ANALYST DOWNGRADE: A bulge bracket firm cuts price targets citing valuation "
            "compression. Which stock in this batch is most exposed to multiple compression?"
        ),
        (
            "EARNINGS WHISPER: Channel checks suggest next earnings may disappoint consensus "
            "for high-multiple names. Which stock carries the most execution risk?"
        ),
    ]

    def __init__(self, stocks):
        self.stocks  = list(stocks)
        self._reserve = list(self.RESERVE)
        self._divergence_call_count = 0  # tracks calls to skip round-1 false positive

    def get_injection(self, round_num, market_probs):
        """
        Return the injection string for this round.
        Round 6 dynamically targets the lowest-probability ticker.
        """
        if round_num == 6:
            return self._build_round6_injection(market_probs)
        return self.INJECTIONS.get(round_num, "")

    def _build_round6_injection(self, market_probs):
        """Build dynamic injection targeting the lowest-conviction stock."""
        if not market_probs:
            ticker = self.stocks[0] if self.stocks else "this stock"
        else:
            ticker = min(market_probs, key=lambda t: market_probs[t])
        return (
            f"AMBIGUOUS CATALYST — {ticker}: New data has emerged that could support or "
            f"undermine the bull thesis. Agents must debate the implications before round 7."
        )

    def check_divergence(self, market_probs):
        """
        If spread between highest and lowest stock probability < 0.15,
        pop and return a reserve injection (convergence prevention).
        Skips the first call (round 1) — spread is always 0 at start because no posts exist.
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
            inj = self._reserve.pop(0)
            print(f"  DIVERGENCE ALERT: spread={spread:.2f} < 0.15 — firing reserve injection.")
            return inj
        return ""
