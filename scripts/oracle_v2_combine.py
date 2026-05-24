#!/usr/bin/env python3
"""
oracle_v2_combine.py — Combine Claude's verdict with MiroFish simulation result

Usage:
  python3 oracle_v2_combine.py --ticker SNOW
  python3 oracle_v2_combine.py --ticker SNOW --claude-rating BUY --claude-conviction 7 --market-price 0.72
"""

import argparse
import datetime
import json
from pathlib import Path


def combine_signals(ticker, claude_rating, claude_conviction, market_price,
                    agent_count=0, contradictions_found=0):
    if contradictions_found > 2:
        claude_weight, sim_weight = 0.25, 0.75
        weight_note = f"Agents found {contradictions_found} contradictions — simulation weighted higher"
    elif contradictions_found > 0:
        claude_weight, sim_weight = 0.35, 0.65
        weight_note = f"Agents found {contradictions_found} contradictions — slight simulation premium"
    else:
        claude_weight, sim_weight = 0.40, 0.60
        weight_note = "No contradictions found — standard weighting"

    rating_map = {
        "STRONG_BUY": 0.95, "STRONG BUY": 0.95,
        "BUY": 0.80, "INVESTIGATE": 0.70, "WATCH": 0.60, "HOLD": 0.55,
        "PASS": 0.30, "WARN": 0.20, "ELIMINATE": 0.05,
    }
    claude_numeric = rating_map.get(claude_rating.upper(), 0.50)
    conviction_scale = claude_conviction / 10
    claude_adjusted = 0.50 + (claude_numeric - 0.50) * conviction_scale
    combined = (claude_adjusted * claude_weight) + (market_price * sim_weight)

    if combined >= 0.75:
        signal, position, color = "STRONG BUY", "5-7%", "🟢🟢"
    elif combined >= 0.62:
        signal, position, color = "BUY", "3-5%", "🟢"
    elif combined >= 0.52:
        signal, position, color = "WATCH", "1-2%", "🟡"
    elif combined >= 0.40:
        signal, position, color = "PASS", "0%", "🔴"
    else:
        signal, position, color = "ELIMINATE", "0% (exit if held)", "🔴🔴"

    claude_bull = claude_numeric >= 0.65
    sim_bull = market_price >= 0.55
    agreement = claude_bull == sim_bull
    confidence = min(0.95, combined + (0.08 if agreement else -0.08))

    return {
        "ticker": ticker,
        "final_signal": signal,
        "position_size": position,
        "color": color,
        "combined_score": round(combined, 3),
        "confidence": round(confidence * 100, 1),
        "claude_rating": claude_rating,
        "claude_conviction": claude_conviction,
        "claude_numeric": round(claude_adjusted, 3),
        "simulation_price": round(market_price, 3),
        "claude_weight": claude_weight,
        "sim_weight": sim_weight,
        "agreement": agreement,
        "weight_note": weight_note,
        "contradictions_found": contradictions_found,
    }


def print_report(result):
    today = datetime.date.today().isoformat()
    agree_str = "✓ Claude and simulation AGREE" if result["agreement"] else "✗ Claude and simulation DIVERGE"
    print(f"""
{'='*60}
ORACLE V2 COMBINED SIGNAL: {result['ticker']}
{'='*60}
Date: {today}

{result['color']} FINAL SIGNAL: {result['final_signal']}
   Position Size: {result['position_size']}
   Confidence:    {result['confidence']}%

SIGNAL BREAKDOWN:
  Claude Analysis:    {result['claude_rating']} ({result['claude_conviction']}/10)
                      Numeric: {result['claude_numeric']:.2f} | Weight: {result['claude_weight']*100:.0f}%

  Simulation Result:  Market Price: {result['simulation_price']:.3f}
                      Weight: {result['sim_weight']*100:.0f}%

  Combined Score:     {result['combined_score']:.3f}

  Agreement:          {agree_str}
  Weighting Note:     {result['weight_note']}
{'='*60}
""")


def main():
    parser = argparse.ArgumentParser(description="Combine Claude verdict with MiroFish simulation result")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--claude-rating", default="WATCH")
    parser.add_argument("--claude-conviction", type=int, default=5)
    parser.add_argument("--market-price", type=float, default=0.5)
    parser.add_argument("--contradictions", type=int, default=0)
    parser.add_argument("--agents", type=int, default=10)
    args = parser.parse_args()

    result = combine_signals(
        ticker=args.ticker.upper(),
        claude_rating=args.claude_rating,
        claude_conviction=args.claude_conviction,
        market_price=args.market_price,
        agent_count=args.agents,
        contradictions_found=args.contradictions,
    )
    print_report(result)

    output_dir = Path.home() / "ORACLE" / "reports" / "v2"
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    result_path = output_dir / f"{args.ticker.upper()}_{today}_v2_signal.json"
    result_path.write_text(json.dumps(result, indent=2))
    print(f"Signal saved: {result_path}")


if __name__ == "__main__":
    main()
