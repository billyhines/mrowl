#!/usr/bin/env python3
"""
One-time database setup script.

Creates the SQLite database and all tables.

Usage:
    python scripts/setup_db.py
"""

import sys
from pathlib import Path

# Add project root to path so we can import src
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.db import init_db, DB_PATH
from src.config import DATA_DIR


def main():
    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    print(f"Initializing database at {DB_PATH}")
    init_db()
    print("Database initialized successfully!")
    
    # Verify tables exist
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    print(f"Tables created: {', '.join(tables)}")


if __name__ == '__main__':
    main()
