#!/bin/bash
# ORACLE Simulator Launcher
# Starts the web dashboard if not already running, then opens browser

PORT=5050

# Check if already running
if curl -s http://localhost:$PORT/api/status > /dev/null 2>&1; then
    echo "ORACLE Sim dashboard already running at http://localhost:$PORT"
else
    echo "Starting ORACLE Simulator dashboard..."
    cd ~/ORACLE/web
    python3 app.py &
    SIM_PID=$!
    echo "PID: $SIM_PID"

    # Wait for it to be ready (max 10s)
    for i in $(seq 1 10); do
        sleep 1
        if curl -s http://localhost:$PORT/api/status > /dev/null 2>&1; then
            echo "Dashboard ready."
            break
        fi
        echo "Waiting... ($i)"
    done
fi

# Open browser
xdg-open http://localhost:$PORT 2>/dev/null || open http://localhost:$PORT 2>/dev/null || \
    echo "Open your browser to: http://localhost:$PORT"

echo ""
echo "ORACLE Simulator running at http://localhost:$PORT"
echo "Press Ctrl+C to stop the dashboard when done."
wait
