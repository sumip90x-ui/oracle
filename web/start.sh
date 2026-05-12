#!/bin/bash
cd ~/ORACLE/web
python3 app.py &
sleep 2
xdg-open http://localhost:5050 2>/dev/null || open http://localhost:5050 2>/dev/null
echo "ORACLE Web UI running at http://localhost:5050"
