#!/bin/bash
# ORACLE Install Script
# Tested on Ubuntu 22.04 / Linux Mint 21+ / Debian 12
# Run once: ./install.sh

set -e

ORACLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
RESET="\033[0m"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         ORACLE INSTALL SCRIPT            ║"
echo "║   Screener + Think Tank + Sim Dashboard  ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. Python deps ────────────────────────────────────────────────────────────
echo -e "${BOLD}[1/5] Installing Python dependencies...${RESET}"
pip install -r "$ORACLE_DIR/requirements.txt" --break-system-packages --quiet 2>/dev/null \
  || pip install -r "$ORACLE_DIR/requirements.txt" --quiet
echo -e "${GREEN}  ✓ Python deps installed${RESET}"

# ── 2. Neo4j ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[2/5] Checking Neo4j...${RESET}"

if command -v neo4j &>/dev/null; then
    echo -e "${GREEN}  ✓ Neo4j already installed${RESET}"
else
    echo "  Installing Neo4j 5.x..."
    # Add Neo4j repo
    wget -q -O /tmp/neo4j.gpg https://debian.neo4j.com/neotechnology.gpg.key
    sudo gpg --dearmor -o /usr/share/keyrings/neo4j.gpg /tmp/neo4j.gpg 2>/dev/null || true
    echo "deb [signed-by=/usr/share/keyrings/neo4j.gpg] https://debian.neo4j.com stable 5" \
        | sudo tee /etc/apt/sources.list.d/neo4j.list > /dev/null
    sudo apt-get update -q
    sudo apt-get install -y neo4j
    echo -e "${GREEN}  ✓ Neo4j installed${RESET}"
fi

# Set password
echo "  Setting Neo4j password..."
NEO4J_HOME=$(dirname $(dirname $(which neo4j)) 2>/dev/null || echo "$HOME/Documents/neo4j")
if [ -d "$NEO4J_HOME" ]; then
    "$NEO4J_HOME/bin/neo4j-admin" dbms set-initial-password miroshark2026 2>/dev/null || true
fi
echo -e "${GREEN}  ✓ Neo4j password set (miroshark2026)${RESET}"

# ── 3. Piper TTS voice model ──────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[3/5] Downloading Piper TTS voice model...${RESET}"
VOICE_DIR="$ORACLE_DIR/voice"
mkdir -p "$VOICE_DIR"

VOICE_MODEL="$VOICE_DIR/en_US-lessac-high.onnx"
if [ -f "$VOICE_MODEL" ]; then
    echo -e "${GREEN}  ✓ Voice model already present${RESET}"
else
    echo "  Downloading en_US-lessac-high (~109MB)..."
    wget -q --show-progress \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx" \
        -O "$VOICE_MODEL"
    wget -q \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx.json" \
        -O "$VOICE_DIR/en_US-lessac-high.onnx.json"
    echo -e "${GREEN}  ✓ Voice model downloaded${RESET}"
fi

# ── 4. .env config ────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[4/5] Configuring environment...${RESET}"

ENV_FILE="$ORACLE_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cp "$ORACLE_DIR/.env.example" "$ENV_FILE"
    echo -e "${YELLOW}  ⚠ Created .env from template${RESET}"
    echo -e "${YELLOW}  → Edit $ENV_FILE and add your OPENROUTER_API_KEY${RESET}"
else
    echo -e "${GREEN}  ✓ .env already exists${RESET}"
fi

# Check if key is set
if grep -q "your_openrouter_key_here" "$ENV_FILE"; then
    echo -e "${YELLOW}  ⚠ OPENROUTER_API_KEY not set — edit .env before running${RESET}"
fi

# ── 5. Portfolio CSV location ─────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[5/5] Setting up portfolio CSV path...${RESET}"
mkdir -p "$ORACLE_DIR/portfolio_csv"
echo -e "${GREEN}  ✓ Place your Fidelity CSV in: $ORACLE_DIR/portfolio_csv/${RESET}"
echo "    Or in ~/Downloads/ — ORACLE auto-detects the latest one"

# ── Make scripts executable ───────────────────────────────────────────────────
chmod +x "$ORACLE_DIR/engine/oracle_think_tank_launch.sh"
chmod +x "$ORACLE_DIR/web/start.sh"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║              INSTALL COMPLETE            ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo -e "  ${BOLD}Next steps:${RESET}"
echo ""
echo "  1. Add your OpenRouter API key:"
echo "     nano $ENV_FILE"
echo ""
echo "  2. Place your Fidelity CSV in:"
echo "     $ORACLE_DIR/portfolio_csv/"
echo ""
echo "  3. Start Neo4j:"
echo "     neo4j start  (or ~/Documents/neo4j/bin/neo4j start)"
echo ""
echo "  4. Run the screener + Think Tank:"
echo "     bash $ORACLE_DIR/engine/oracle_think_tank_launch.sh"
echo ""
echo "  5. Run the simulation dashboard:"
echo "     bash $ORACLE_DIR/web/start.sh"
echo "     Open: http://localhost:5050"
echo ""
