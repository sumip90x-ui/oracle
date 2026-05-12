#!/usr/bin/env python3
"""
ORACLE Phase 2 — markets.py
Prediction markets that track agent conviction across rounds.
"""


class PredictionMarket:
    FLOOR   = 0.15
    CEILING = 0.85

    def __init__(self, market_id, question, market_type, tickers):
        self.market_id   = market_id
        self.question    = question
        self.market_type = market_type   # "individual" | "head_to_head" | "forced_choice"
        self.tickers     = tickers       # list of relevant tickers
        self.probability = 0.50
        self.prob_history = []           # one entry per round after update

    def update_from_graph(self, driver, run_id, round_num, stocks):
        """
        Query Neo4j for this round's bullish/bearish stances on our tickers.
        delta per edge = conviction * 0.03 (capped; round_weight stored on edge but not used here),
        clamped to FLOOR/CEILING.
        For head_to_head: bullish on tickers[0] pushes up, bearish on tickers[0] pushes down;
                          bullish on tickers[1] pushes down, bearish on tickers[1] pushes up.
        """
        if driver is None:
            self.prob_history.append(self.probability)
            return

        try:
            with driver.session() as s:
                # Bullish edges this round for our tickers
                bull_rows = s.run("""
                    MATCH (ag:Agent {run_id: $run_id})-[r:BULLISH_ON]->(st:Stock {run_id: $run_id})
                    WHERE st.ticker IN $tickers AND r.round_num = $round_num
                    RETURN st.ticker AS ticker, r.conviction AS conviction, r.round_weight AS rw
                """, run_id=run_id, tickers=self.tickers, round_num=round_num).data()

                bear_rows = s.run("""
                    MATCH (ag:Agent {run_id: $run_id})-[r:BEARISH_ON]->(st:Stock {run_id: $run_id})
                    WHERE st.ticker IN $tickers AND r.round_num = $round_num
                    RETURN st.ticker AS ticker, r.conviction AS conviction, r.round_weight AS rw
                """, run_id=run_id, tickers=self.tickers, round_num=round_num).data()

            delta = 0.0

            if self.market_type == "individual":
                for r in bull_rows:
                    delta += float(r["conviction"]) * 0.03
                for r in bear_rows:
                    delta -= float(r["conviction"]) * 0.03

            elif self.market_type == "head_to_head":
                # probability = chance tickers[0] beats tickers[1]
                t0, t1 = self.tickers[0], self.tickers[1]
                for r in bull_rows:
                    sign = 1.0 if r["ticker"] == t0 else -1.0
                    delta += sign * float(r["conviction"]) * 0.03
                for r in bear_rows:
                    sign = -1.0 if r["ticker"] == t0 else 1.0
                    delta += sign * float(r["conviction"]) * 0.03

            elif self.market_type == "forced_choice":
                # probability = chance the batch as a whole has a clear winner (bullish consensus)
                for r in bull_rows:
                    delta += float(r["conviction"]) * 0.015
                for r in bear_rows:
                    delta -= float(r["conviction"]) * 0.015

            self.probability = max(self.FLOOR, min(self.CEILING, self.probability + delta))

        except Exception as e:
            print(f"  WARNING: market update error ({self.market_id}): {e}")

        self.prob_history.append(self.probability)

    def velocity(self):
        """Slope of probability over last 3 rounds. Positive = rising."""
        if len(self.prob_history) < 2:
            return 0.0
        recent = self.prob_history[-3:]
        if len(recent) == 1:
            return 0.0
        return (recent[-1] - recent[0]) / max(1, len(recent) - 1)

    def __repr__(self):
        return f"<Market {self.market_id} p={self.probability:.2f} v={self.velocity():+.3f}>"


# ── Factory ────────────────────────────────────────────────────────────────────

def build_markets(stocks, fundamentals=None):
    """
    Build 10 prediction markets:
      6 individual  — TICKER achieves 50%+ return in 12 months
      3 head-to-head — sector pairings
      1 forced-choice — which one stock wins over 12 months
    """
    fundamentals = fundamentals or {}
    markets = []

    # 6 individual markets
    for ticker in stocks:
        markets.append(PredictionMarket(
            market_id   = f"indiv_{ticker}",
            question    = f"{ticker} achieves 50%+ return in 12 months",
            market_type = "individual",
            tickers     = [ticker],
        ))

    # Detect sector groupings for head-to-head pairs
    _BIOTECH = {"biotechnology", "healthcare", "biopharmaceuticals", "pharmaceuticals"}
    _SAAS    = {"software", "software—application", "software - application",
                "software infrastructure", "software—infrastructure"}
    _DATA_AI = {"SNOW", "PLTR", "DDOG", "MDB"}

    biotech_group = []
    saas_group    = []
    data_ai_group = []
    remainder     = []

    for ticker in stocks:
        f   = fundamentals.get(ticker, {})
        sec = (f.get("sector") or "").lower().strip()
        if any(b in sec for b in _BIOTECH):
            biotech_group.append(ticker)
        elif ticker.upper() in _DATA_AI:
            data_ai_group.append(ticker)
        elif any(s in sec for s in _SAAS):
            saas_group.append(ticker)
        else:
            remainder.append(ticker)

    def _pair(group, fallback):
        if len(group) >= 2:
            return group[:2]
        extra = [t for t in fallback if t not in group]
        return (group + extra)[:2]

    biotech_pair  = _pair(biotech_group, stocks)
    saas_pair     = _pair(saas_group,    [t for t in stocks if t not in biotech_pair])
    data_ai_pair  = _pair(
        data_ai_group,
        [t for t in stocks if t not in biotech_pair and t not in saas_pair]
    )

    # Only add head-to-head markets if we have 2 stocks in each pair
    if len(biotech_pair) == 2:
        markets.append(PredictionMarket(
            market_id   = "hth_biotech",
            question    = f"{biotech_pair[0]} outperforms {biotech_pair[1]} over 12 months",
            market_type = "head_to_head",
            tickers     = biotech_pair,
        ))
    if len(saas_pair) == 2:
        markets.append(PredictionMarket(
            market_id   = "hth_saas",
            question    = f"{saas_pair[0]} outperforms {saas_pair[1]} over 12 months",
            market_type = "head_to_head",
            tickers     = saas_pair,
        ))
    if len(data_ai_pair) == 2:
        markets.append(PredictionMarket(
            market_id   = "hth_data_ai",
            question    = f"{data_ai_pair[0]} outperforms {data_ai_pair[1]} over 12 months",
            market_type = "head_to_head",
            tickers     = data_ai_pair,
        ))

    # 1 forced-choice
    markets.append(PredictionMarket(
        market_id   = "forced_choice",
        question    = f"Hold ONE of {'/'.join(stocks)} for 12 months — which wins?",
        market_type = "forced_choice",
        tickers     = list(stocks),
    ))

    return markets
