#!/bin/bash
clear
echo "╔══════════════════════════════════════════╗"
echo "║           ORACLE THINK TANK              ║"
echo "║     Find the next AMD before it runs     ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "What do you want to do?"
echo ""
echo "  [1] Scan my Fidelity CSV for top candidates"
echo "      (reads your portfolio, scores every stock,"
echo "       shows you the best runner candidates)"
echo ""
echo "  [2] Analyze specific tickers with the Think Tank"
echo "      (you type tickers, 29 investors analyze them)"
echo ""
echo "  [3] Full pipeline: Scan CSV -> Think Tank"
echo "      (finds top candidates then auto-analyzes them)"
echo ""
echo "  [4] Choose a specific CSV file to use"
echo "      (pick from saved portfolios by date)"
echo ""
echo "  [5] Alpaca drawdown candidates -> Think Tank"
echo "      (finds stocks with biggest $ loss today across"
echo "       all Fidelity accounts — same as today's buy list)"
echo ""
echo "  [q] Quit"
echo ""
read -rp "Choice: " CHOICE

case "$CHOICE" in

  # ── OPTION 1: Just scan the CSV ─────────────────────────────
  1)
    clear
    echo "Scanning your Fidelity portfolio for runner candidates..."
    echo "(Scoring every stock against AMD/MU/SNDK runner DNA)"
    echo ""
    python3 /home/sumith/ORACLE/engine/oracle_runner_screener.py --no-seed
    echo ""
    echo "Press Enter to close..."
    read -r
    ;;

  # ── OPTION 2: Think Tank on specific tickers ────────────────
  2)
    clear
    echo "╔══════════════════════════════════════════╗"
    echo "║           THINK TANK ANALYSIS            ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    echo "Type the tickers you want to analyze:"
    echo "Example: INSM BBIO ZETA SNOW PLTR SMCI  (auto-filled from screener in option 3)"
    echo ""
    read -rp "Tickers: " STOCKS

    if [ -z "$STOCKS" ]; then
      echo "No tickers entered. Exiting."
      sleep 2
      exit 0
    fi

    echo ""
    echo "How deep should the analysis go?"
    echo ""
    echo "  [1] Fast   - haiku AI,  ~\$0.12, ~3 min   (good for exploring)"
    echo "  [2] Full   - sonnet AI, ~\$0.70, ~8 min   (recommended)"
    echo "  [3] Max    - all 29 investors separately, ~\$4, ~15 min"
    echo ""
    read -rp "Choice (press Enter for Full): " MODE

    echo ""
    echo "Running Think Tank on: $STOCKS"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    case "$MODE" in
      1) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS --fast ;;
      3) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS --deep ;;
      *) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS ;;
    esac

    echo ""
    echo "Press Enter to close..."
    read -r
    ;;

  # ── OPTION 3: Full pipeline ──────────────────────────────────
  3)
    clear
    echo "╔══════════════════════════════════════════╗"
    echo "║       FULL PIPELINE: SCAN + ANALYZE      ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    echo "Step 1: Scanning your Fidelity portfolio..."
    echo "(Scoring 480 stocks against AMD/MU/SNDK runner DNA)"
    echo ""

    # Run screener, stream live output and capture for parsing
    TMPFILE=$(mktemp /tmp/oracle_screen_XXXX.txt)
    python3 /home/sumith/ORACLE/engine/oracle_runner_screener.py --screen-only 2>/dev/null | tee "$TMPFILE"
    SCREEN_OUTPUT=$(cat "$TMPFILE")
    rm -f "$TMPFILE"

    if [ -z "$SCREEN_OUTPUT" ]; then
      echo "ERROR: Could not run screener. Check that ~/portfolio.csv exists."
      echo ""
      echo "Press Enter to close..."
      read -r
      exit 1
    fi

    # Print the full screening results
    echo "$SCREEN_OUTPUT"
    echo ""

    # Extract tickers from Haiku triage output line (most reliable)
    # Screener prints: "  Think Tank candidates (triage order): ['INSM', 'BBIO', ...]"
    TOP=$(echo "$SCREEN_OUTPUT" | grep "Think Tank candidates" | grep -oP "[A-Z]{2,6}" | tr '\n' ' ' | xargs)

    # Fallback: if triage line not found, use Python directly with live data
    if [ -z "$TOP" ]; then
        TOP=$(python3 -c "
import sys; sys.path.insert(0, '/home/sumith/ORACLE/engine')
import oracle_runner_screener as s
live = s.load_cache()
holdings = s.parse_fidelity_csv(s.CSV_PATH)
results = s.run_screen(holdings, live, top_n=15)
picks = s.get_thinktank_candidates(results, live_data_map=live, max_stocks=6)
print(' '.join(picks))
" 2>/dev/null)
    fi

    # Build a compact screener summary for Think Tank context
    SCREENER_TABLE=$(echo "$SCREEN_OUTPUT" | grep -A50 "SYM" | head -20)

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Step 2: Think Tank Analysis"
    echo ""

    if [ -n "$TOP" ]; then
      echo "Top candidates from scan: $TOP"
      echo "Use these? [Enter = yes, or type different tickers]"
    else
      echo "Could not auto-extract tickers. Type them manually:"
    fi

    read -rp "Tickers: " OVERRIDE
    STOCKS="${OVERRIDE:-$TOP}"

    if [ -z "$STOCKS" ]; then
      echo "No tickers. Exiting."
      sleep 2
      exit 0
    fi

    echo ""
    echo "How deep?"
    echo "  [1] Fast (~\$0.12, ~3min)   [2] Full (~\$0.70, ~8min, recommended)   [3] Max (~\$4)"
    read -rp "Choice (press Enter for Full): " MODE

    echo ""
    echo "Running Think Tank on: $STOCKS"
    echo "(Screener DNA scores included as context)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    case "$MODE" in
      1) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS --fast \
           --screener-context "$SCREENER_TABLE" ;;
      3) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS --deep \
           --screener-context "$SCREENER_TABLE" ;;
      *) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS \
           --screener-context "$SCREENER_TABLE" ;;
    esac

    echo ""
    echo "Press Enter to close..."
    read -r
    ;;

  # ── OPTION 4: Choose a specific CSV file ────────────────────
  4)
    clear
    echo "╔══════════════════════════════════════════╗"
    echo "║         CHOOSE A PORTFOLIO CSV           ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    echo "Available CSVs (newest first):"
    echo ""

    # Find all CSVs in known locations
    CSV_DIR="$HOME/ORACLE/portfolio_csv"
    DL_DIR="$HOME/Downloads"
    CURRENT="$HOME/portfolio.csv"

    mapfile -t CSV_FILES < <(
      { ls -t "$CSV_DIR"/*.csv 2>/dev/null; ls -t "$DL_DIR"/Portfolio_Positions_*.csv 2>/dev/null; } | head -20
    )

    if [ ${#CSV_FILES[@]} -eq 0 ]; then
      echo "  No CSV files found in ~/ORACLE/portfolio_csv/ or ~/Downloads/"
      echo ""
      echo "  Currently active: ~/portfolio.csv"
      echo ""
      echo "Press Enter to go back..."
      read -r
      exec "$0"
    fi

    # Display numbered list
    for i in "${!CSV_FILES[@]}"; do
      fname=$(basename "${CSV_FILES[$i]}")
      fdate=$(stat -c %y "${CSV_FILES[$i]}" 2>/dev/null | cut -d' ' -f1)
      active=""
      if [ "${CSV_FILES[$i]}" -ef "$CURRENT" ] 2>/dev/null; then
        active=" ← ACTIVE"
      fi
      echo "  [$((i+1))] $fname  ($fdate)$active"
    done

    echo ""
    echo "  [0] Use currently active ~/portfolio.csv"
    echo ""
    read -rp "Choice: " CSV_CHOICE

    if [ "$CSV_CHOICE" = "0" ] || [ -z "$CSV_CHOICE" ]; then
      SELECTED_CSV=""
      echo "  Using active portfolio.csv"
    elif [[ "$CSV_CHOICE" =~ ^[0-9]+$ ]] && [ "$CSV_CHOICE" -ge 1 ] && [ "$CSV_CHOICE" -le "${#CSV_FILES[@]}" ]; then
      SELECTED_CSV="${CSV_FILES[$((CSV_CHOICE-1))]}"
      echo "  Selected: $(basename "$SELECTED_CSV")"
    else
      echo "  Invalid choice."
      sleep 2
      exec "$0"
    fi

    echo ""
    echo "Now what?"
    echo "  [1] Scan this CSV for top candidates"
    echo "  [3] Full pipeline: Scan this CSV -> Think Tank"
    echo ""
    read -rp "Choice: " NEXT

    CSV_FLAG=""
    [ -n "$SELECTED_CSV" ] && CSV_FLAG="--csv \"$SELECTED_CSV\""

    case "$NEXT" in
      1)
        eval python3 /home/sumith/oracle_runner_screener.py --no-seed $CSV_FLAG
        echo ""
        echo "Press Enter to close..."
        read -r
        ;;
      3)
        TMPFILE=$(mktemp /tmp/oracle_screen_XXXX.txt)
        eval python3 /home/sumith/oracle_runner_screener.py --screen-only $CSV_FLAG 2>/dev/null | tee "$TMPFILE"
        SCREEN_OUTPUT=$(cat "$TMPFILE")
        rm -f "$TMPFILE"

        TOP=$(echo "$SCREEN_OUTPUT" | grep "Think Tank candidates" | grep -oP "[A-Z]{2,6}" | tr '\n' ' ' | xargs)
        SCREENER_TABLE=$(echo "$SCREEN_OUTPUT" | grep -A50 "SYM" | head -20)

        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        if [ -n "$TOP" ]; then
          echo "Top candidates: $TOP"
          echo "Use these? [Enter = yes, or type different tickers]"
        else
          echo "Type tickers to analyze:"
        fi
        read -rp "Tickers: " OVERRIDE
        STOCKS="${OVERRIDE:-$TOP}"

        echo ""
        echo "  [1] Fast (~\$0.12, ~3min)   [2] Full (~\$0.70, ~8min)   [3] Max (~\$4)"
        read -rp "Choice (press Enter for Full): " MODE

        case "$MODE" in
          1) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS --fast --screener-context "$SCREENER_TABLE" ;;
          3) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS --deep --screener-context "$SCREENER_TABLE" ;;
          *) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS --screener-context "$SCREENER_TABLE" ;;
        esac

        echo ""
        echo "Press Enter to close..."
        read -r
        ;;
      *)
        echo "Going back..."
        sleep 1
        exec "$0"
        ;;
    esac
    ;;

  # ── OPTION 5: Alpaca drawdown candidates ────────────────────
  5)
    clear
    echo "╔══════════════════════════════════════════╗"
    echo "║    ALPACA DRAWDOWN -> THINK TANK         ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    echo "Finding stocks with biggest dollar loss today across all accounts..."
    echo ""

    # Run the drawdown scanner
    TMPFILE=$(mktemp /tmp/oracle_drawdown_XXXX.txt)
    python3 /home/sumith/ORACLE/engine/alpaca_drawdown_candidates.py 2>/dev/null | tee "$TMPFILE"
    SCAN_OUTPUT=$(cat "$TMPFILE")
    rm -f "$TMPFILE"

    if [ -z "$SCAN_OUTPUT" ]; then
      echo "ERROR: Could not scan CSV. Make sure your Fidelity CSV is in ~/Downloads/"
      echo ""
      echo "Press Enter to close..."
      read -r
      exit 1
    fi

    # Extract tickers from output line
    TOP=$(echo "$SCAN_OUTPUT" | grep "Think Tank candidates" | grep -oP "[A-Z]{2,6}" | tr '\n' ' ' | xargs)

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Top drawdown candidates: $TOP"
    echo ""
    echo "How many to analyze? (default 6, max 10)"
    read -rp "Count (press Enter for 6): " COUNT
    COUNT="${COUNT:-6}"

    # Re-run with limited count
    TOP=$(python3 /home/sumith/ORACLE/engine/alpaca_drawdown_candidates.py --json --top "$COUNT" 2>/dev/null \
          | python3 -c "import sys,json; d=json.load(sys.stdin); print(' '.join(x['sym'] for x in d))")

    echo ""
    echo "Use these tickers? [Enter = yes, or type different tickers]"
    echo "  $TOP"
    echo ""
    read -rp "Tickers: " OVERRIDE
    STOCKS="${OVERRIDE:-$TOP}"

    if [ -z "$STOCKS" ]; then
      echo "No tickers. Exiting."
      sleep 2
      exit 0
    fi

    echo ""
    echo "How deep?"
    echo "  [1] Fast (~\$0.12, ~3min)   [2] Full (~\$0.70, ~8min, recommended)   [3] Max (~\$4)"
    read -rp "Choice (press Enter for Full): " MODE

    echo ""
    echo "Running Think Tank on: $STOCKS"
    echo "(Drawdown methodology — biggest losers today across all accounts)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    case "$MODE" in
      1) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS --fast ;;
      3) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS --deep ;;
      *) python3 /home/sumith/ORACLE/engine/oracle_think_tank.py --stocks $STOCKS ;;
    esac

    echo ""
    echo "Press Enter to close..."
    read -r
    ;;

  q|Q)
    exit 0
    ;;

  *)
    echo "Invalid choice. Closing."
    sleep 2
    exit 0
    ;;
esac
