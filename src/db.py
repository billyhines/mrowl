"""
Database operations for the liquidity tracker.

Uses SQLite for simplicity. All functions take an optional connection
parameter to support transactions; if not provided, they create their own.
"""

import sqlite3
from datetime import datetime
from typing import Optional
from .config import DB_PATH


def get_connection() -> sqlite3.Connection:
    """Get a database connection with row factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: Optional[sqlite3.Connection] = None) -> None:
    """
    Create all tables if they don't exist.
    
    Call this once at startup or via scripts/setup_db.py
    """
    close_conn = conn is None
    conn = conn or get_connection()
    
    conn.executescript("""
        -- Games we're tracking (one per matchup)
        CREATE TABLE IF NOT EXISTS games (
            event_ticker TEXT PRIMARY KEY,
            home_team TEXT,
            away_team TEXT,
            game_time TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Track one market per game (away team's "win" market)
        CREATE TABLE IF NOT EXISTS markets (
            ticker TEXT PRIMARY KEY,
            event_ticker TEXT,
            team TEXT,
            FOREIGN KEY (event_ticker) REFERENCES games(event_ticker)
        );

        -- Point-in-time snapshots
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            timestamp TIMESTAMP,
            best_bid INTEGER,
            best_ask INTEGER,
            mid REAL,
            spread INTEGER,
            total_bid_depth INTEGER,
            total_ask_depth INTEGER,
            open_interest INTEGER,
            FOREIGN KEY (ticker) REFERENCES markets(ticker)
        );

        -- Full orderbook depth (for heatmap reconstruction)
        CREATE TABLE IF NOT EXISTS depth_levels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER,
            side TEXT,
            price INTEGER,
            quantity INTEGER,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
        );

        -- Hourly candlestick data (backfilled from API)
        CREATE TABLE IF NOT EXISTS candles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            end_time TIMESTAMP,
            interval_minutes INTEGER,
            open_price INTEGER,
            high_price INTEGER,
            low_price INTEGER,
            close_price INTEGER,
            volume INTEGER,
            open_interest INTEGER,
            FOREIGN KEY (ticker) REFERENCES markets(ticker)
        );

        -- Indexes for common queries
        CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_time 
            ON snapshots(ticker, timestamp);
        CREATE INDEX IF NOT EXISTS idx_depth_snapshot 
            ON depth_levels(snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_candles_ticker_time 
            ON candles(ticker, end_time);
    """)
    
    conn.commit()
    if close_conn:
        conn.close()


def insert_game(
    event_ticker: str,
    home_team: str,
    away_team: str,
    game_time: datetime,
    conn: Optional[sqlite3.Connection] = None
) -> None:
    """Insert a new game, ignore if already exists."""
    close_conn = conn is None
    conn = conn or get_connection()
    
    conn.execute("""
        INSERT OR IGNORE INTO games (event_ticker, home_team, away_team, game_time)
        VALUES (?, ?, ?, ?)
    """, (event_ticker, home_team, away_team, game_time))
    
    conn.commit()
    if close_conn:
        conn.close()


def insert_market(
    ticker: str,
    event_ticker: str,
    team: str,
    conn: Optional[sqlite3.Connection] = None
) -> None:
    """Insert a new market, ignore if already exists."""
    close_conn = conn is None
    conn = conn or get_connection()
    
    conn.execute("""
        INSERT OR IGNORE INTO markets (ticker, event_ticker, team)
        VALUES (?, ?, ?)
    """, (ticker, event_ticker, team))
    
    conn.commit()
    if close_conn:
        conn.close()


def insert_snapshot(
    ticker: str,
    timestamp: datetime,
    best_bid: Optional[int],
    best_ask: Optional[int],
    mid: Optional[float],
    spread: Optional[int],
    total_bid_depth: int,
    total_ask_depth: int,
    open_interest: Optional[int],
    conn: Optional[sqlite3.Connection] = None
) -> int:
    """
    Insert a snapshot and return its ID.
    
    Use the returned ID to insert depth_levels.
    """
    close_conn = conn is None
    conn = conn or get_connection()
    
    cursor = conn.execute("""
        INSERT INTO snapshots 
        (ticker, timestamp, best_bid, best_ask, mid, spread, 
         total_bid_depth, total_ask_depth, open_interest)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ticker, timestamp, best_bid, best_ask, mid, spread,
          total_bid_depth, total_ask_depth, open_interest))
    
    snapshot_id = cursor.lastrowid
    conn.commit()
    
    if close_conn:
        conn.close()
    
    return snapshot_id


def insert_depth_levels(
    snapshot_id: int,
    bids: dict[int, int],
    asks: dict[int, int],
    conn: Optional[sqlite3.Connection] = None
) -> None:
    """Insert all depth levels for a snapshot."""
    close_conn = conn is None
    conn = conn or get_connection()
    
    rows = []
    for price, qty in bids.items():
        rows.append((snapshot_id, 'bid', price, qty))
    for price, qty in asks.items():
        rows.append((snapshot_id, 'ask', price, qty))
    
    conn.executemany("""
        INSERT INTO depth_levels (snapshot_id, side, price, quantity)
        VALUES (?, ?, ?, ?)
    """, rows)
    
    conn.commit()
    if close_conn:
        conn.close()


def get_active_games(conn: Optional[sqlite3.Connection] = None) -> list[sqlite3.Row]:
    """Get games that haven't happened yet."""
    close_conn = conn is None
    conn = conn or get_connection()
    
    cursor = conn.execute("""
        SELECT * FROM games 
        WHERE game_time > datetime('now')
        ORDER BY game_time
    """)
    
    rows = cursor.fetchall()
    if close_conn:
        conn.close()
    
    return rows


def get_snapshots_for_market(
    ticker: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    conn: Optional[sqlite3.Connection] = None
) -> list[sqlite3.Row]:
    """Get all snapshots for a market, optionally filtered by time range."""
    close_conn = conn is None
    conn = conn or get_connection()
    
    query = "SELECT * FROM snapshots WHERE ticker = ?"
    params = [ticker]
    
    if start_time:
        query += " AND timestamp >= ?"
        params.append(start_time)
    if end_time:
        query += " AND timestamp <= ?"
        params.append(end_time)
    
    query += " ORDER BY timestamp"
    
    cursor = conn.execute(query, params)
    rows = cursor.fetchall()
    
    if close_conn:
        conn.close()
    
    return rows


def get_depth_for_snapshot(
    snapshot_id: int,
    conn: Optional[sqlite3.Connection] = None
) -> list[sqlite3.Row]:
    """Get all depth levels for a snapshot."""
    close_conn = conn is None
    conn = conn or get_connection()
    
    cursor = conn.execute("""
        SELECT * FROM depth_levels 
        WHERE snapshot_id = ?
        ORDER BY side, price
    """, (snapshot_id,))
    
    rows = cursor.fetchall()
    if close_conn:
        conn.close()
    
    return rows
