"""
Configuration for the Kalshi NFL liquidity tracker.
"""

from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "nfl_liquidity.db"

# Kalshi API
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
NFL_SERIES_TICKER = "KXNFLGAME"

# Polling intervals (in minutes)
POLL_INTERVAL_FAR = 60       # >24 hours to game
POLL_INTERVAL_NEAR = 15      # <24 hours to game, pregame
POLL_INTERVAL_LIVE = 1       # Game in progress

# Thresholds
NEAR_GAME_THRESHOLD_HOURS = 24   # Switch from far->near polling
GAME_DURATION_HOURS = 3.5        # How long after kickoff to consider game "ended"