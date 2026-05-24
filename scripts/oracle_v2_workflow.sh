#!/bin/bash
# ORACLE V2 — Complete workflow in one command
# Usage:
#   ./oracle_v2_workflow.sh SNOW
#   ./oracle_v2_workflow.sh SNOW --file /tmp/snow_analysis.txt
#   ./oracle_v2_workflow.sh SNOW --mode platform_compounder

set -e

TICKER="${1:-}"
SCRIPTS="$HOME/ORACLE/scripts"
SEEDS="$HOME/ORACLE/mirofish_seeds"

if [ -z "$TICKER" ]; then
    echo "Usage: $0 TICKER [--file FILE] [--mode MODE]"
    echo "Example: $0 SNOW"
    echo "Example: $0 AEM --mode commodity_producer"
    exit 1
fi

TICKER=$(echo "$TICKER" | tr '[:lower:]' '[:upper:]')
shift

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║       ORACLE V2 WORKFLOW: $TICKER"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "STEP 1: FORMAT CLAUDE'S ANALYSIS AS MIROFISH SEED"
echo "-----------------------------------------------"

if [[ "$*" == *"--file"* ]]; then
    python3 "$SCRIPTS/oracle_v2_seed_formatter.py" --ticker "$TICKER" $@
else
    echo "Ready to receive Claude's analysis."
    echo "Paste the full output from Claude, then press Ctrl+D:"
    echo ""
    python3 "$SCRIPTS/oracle_v2_seed_formatter.py" --ticker "$TICKER" --interactive $@
fi

TODAY=$(date +%Y-%m-%d)
SEED_FILE="$SEEDS/${TICKER}_${TODAY}_seed.md"
PROMPT_FILE="$SEEDS/${TICKER}_${TODAY}_prompt.txt"

echo ""
echo "STEP 2: LOAD INTO MIROFISH"
echo "-----------------------------------------------"
echo "Files ready:"
echo "  Seed:   $SEED_FILE"
echo "  Prompt: $PROMPT_FILE"
echo ""
echo "Now do this:"
echo "  1. Open MiroFish: http://localhost:3000"
echo "  2. Create new simulation"
echo "  3. Upload seed file or paste its contents"
echo "  4. Paste the prompt from the prompt file"
echo "  5. Run the simulation"
echo "  6. Note the final prediction market price (0.00 to 1.00)"
echo ""
read -p "Press ENTER when simulation is complete..."

echo ""
echo "STEP 3: COMBINE RESULTS"
echo "-----------------------------------------------"
echo ""
read -p "Claude's rating (BUY/WATCH/PASS/ELIMINATE etc): " CLAUDE_RATING
read -p "Claude's conviction 1-10: " CLAUDE_CONVICTION
read -p "MiroFish final market price (0.00 to 1.00): " MARKET_PRICE
read -p "Contradictions agents found (0 if none): " CONTRADICTIONS

echo ""
python3 "$SCRIPTS/oracle_v2_combine.py" \
    --ticker "$TICKER" \
    --claude-rating "$CLAUDE_RATING" \
    --claude-conviction "$CLAUDE_CONVICTION" \
    --market-price "$MARKET_PRICE" \
    --contradictions "$CONTRADICTIONS"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║           ORACLE V2 COMPLETE                 ║"
echo "╚══════════════════════════════════════════════╝"
echo "Reports saved to: ~/ORACLE/reports/v2/"
