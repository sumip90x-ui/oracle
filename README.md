# ORACLE — Stock Screener + Investment Simulation Engine

ORACLE is a two-part system:

1. **Oracle Screener** — scans your Fidelity portfolio CSV and scores every stock against AMD/MU/SNDK runner DNA (beaten down + revenue inflecting + EPS turning + high short interest). Uses Haiku AI triage to pick the best 5-6 candidates.

2. **Oracle Think Tank** — 29 legendary investors (Buffett, Munger, Lynch, Marks, Burry + 24 more) analyze those candidates across 6 composite analyst panels and deliver scored verdicts: STRONG_BUY / BUY / WATCH / PASS.

3. **Oracle Simulation Dashboard** — agent-based market simulation. Stocks compete in a prediction market across 8 rounds with bullish/bearish agents debating. Neo4j graph backend. Real-time SSE dashboard at `localhost:5050` with Piper TTS voice announcements.

---

## Quick Install

```bash
git clone https://github.com/sumip90x-ui/oracle.git
cd oracle
./install.sh
```

That's it. The installer handles Python deps, Neo4j, Piper TTS voice model, and config.

---

## Requirements

- Python 3.10+
- Neo4j 5.x (installer handles this)
- OpenRouter API key (get one free at openrouter.ai)
- Fidelity portfolio CSV export

---

## Usage

### Screener + Think Tank (terminal launcher)
```bash
bash ~/ORACLE/engine/oracle_think_tank_launch.sh
```

Options:
- `1` — Scan Fidelity CSV for top runner candidates
- `2` — Run Think Tank on specific tickers you type
- `3` — Full pipeline: scan → auto-analyze top picks
- `4` — Choose a specific CSV file
- `5` — Alpaca drawdown candidates (biggest losers today → Think Tank)

### Simulation Dashboard
```bash
bash ~/ORACLE/web/start.sh
# Open http://localhost:5050
```

---

## Config

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
nano .env
```

Required:
```
OPENROUTER_API_KEY=your_key_here
NEO4J_PASSWORD=miroshark2026
```

---

## Directory Structure

```
ORACLE/
  engine/          # Screener + Think Tank
  sim/             # Simulation engine (agents, markets, graph)
  web/             # Flask dashboard + SSE + voice
  data/            # yfinance data layer
  voice/           # Piper TTS model (downloaded by installer)
  cache/           # yfinance cache (24hr TTL)
  sims/            # Saved simulation results
  tests/           # Unit tests
```
