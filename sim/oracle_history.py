#!/usr/bin/env python3
"""
ORACLE Phase 2 — oracle_history.py
SQLite run history for simulation results.
"""

import os
import sqlite3
import datetime

DEFAULT_DB = os.path.expanduser("~/ORACLE/oracle_history.db")


def init_db(db_path=None):
    path = db_path or DEFAULT_DB
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sim_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            signal TEXT,
            composite REAL,
            probability REAL,
            velocity REAL,
            rounds_completed INTEGER,
            injection_seed INTEGER DEFAULT 0,
            timestamp TEXT,
            UNIQUE(run_id, ticker, injection_seed)
        )
    """)
    conn.commit()
    conn.close()


def record_run(run_id, rankings, injection_seed=0, db_path=None):
    path = db_path or DEFAULT_DB
    init_db(path)
    conn = sqlite3.connect(path)
    ts = datetime.datetime.now().isoformat()
    for r in rankings:
        conn.execute("""
            INSERT OR REPLACE INTO sim_runs
                (run_id, ticker, signal, composite, probability, velocity,
                 rounds_completed, injection_seed, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            r["ticker"],
            r.get("signal"),
            r.get("composite"),
            r.get("probability"),
            r.get("velocity"),
            r.get("rounds_completed"),
            injection_seed,
            ts,
        ))
    conn.commit()
    conn.close()


def get_ticker_history(ticker, db_path=None):
    path = db_path or DEFAULT_DB
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM sim_runs WHERE ticker = ? ORDER BY timestamp DESC",
        (ticker,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_run_signals(run_id, db_path=None):
    path = db_path or DEFAULT_DB
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM sim_runs WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
