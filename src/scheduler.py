"""
Adaptive scheduler for Kalshi NFL liquidity collection.

Polls each game at a frequency based on its state:
- Far (>24h to kickoff): every 60 minutes
- Near (<24h, pregame): every 15 minutes  
- Live (in progress): every 1 minute
- Ended: stop polling

Usage:
    python -m src.scheduler          # Run continuously
    python -m src.scheduler --once   # Single pass, then exit (for testing)

This replaces cron-based scheduling with intelligent per-game intervals.
"""

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import (
    POLL_INTERVAL_FAR,
    POLL_INTERVAL_NEAR,
    POLL_INTERVAL_LIVE,
    NEAR_GAME_THRESHOLD_HOURS,
    GAME_DURATION_HOURS,
)
from .collector import discover_markets, collect_snapshot
from .db import (
    get_connection, init_db, insert_game, insert_market,
    insert_snapshot, insert_depth_levels
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Polling intervals (in seconds)
INTERVAL_FAR = POLL_INTERVAL_FAR * 60       # 60 min -> 3600s
INTERVAL_NEAR = POLL_INTERVAL_NEAR * 60     # 15 min -> 900s
INTERVAL_LIVE = POLL_INTERVAL_LIVE * 60     # 1 min -> 60s


class GameState:
    """Tracks polling state for a single game."""
    
    def __init__(
        self,
        event_ticker: str,
        market_ticker: str,
        home_team: str,
        away_team: str,
        game_time: datetime,  # Market close time / game END (UTC)
    ):
        self.event_ticker = event_ticker
        self.market_ticker = market_ticker
        self.home_team = home_team
        self.away_team = away_team
        self.game_time = game_time
        self.next_poll = datetime.now(timezone.utc)
    
    def get_game_start(self) -> datetime:
        """Estimated kickoff time (game_end minus typical game duration)."""
        return self.game_time - timedelta(hours=GAME_DURATION_HOURS)
    
    def get_status(self, now: Optional[datetime] = None) -> str:
        """
        Determine current game status.
        
        Returns: 'far', 'near', or 'live'
        
        Note: We don't track 'ended' here - games are removed from tracking
        when Kalshi removes them from the API (in refresh_games).
        """
        now = now or datetime.now(timezone.utc)
        game_start = self.get_game_start()
        
        if now >= game_start:
            return 'live'
        elif now >= game_start - timedelta(hours=NEAR_GAME_THRESHOLD_HOURS):
            return 'near'
        else:
            return 'far'
    
    def get_interval(self, now: Optional[datetime] = None) -> int:
        """Get polling interval in seconds based on current status."""
        status = self.get_status(now)
        intervals = {
            'far': INTERVAL_FAR,
            'near': INTERVAL_NEAR,
            'live': INTERVAL_LIVE,
        }
        return intervals[status]
    
    def update_next_poll(self, now: Optional[datetime] = None) -> None:
        """Set next_poll based on current status."""
        now = now or datetime.now(timezone.utc)
        interval = self.get_interval(now)
        self.next_poll = now + timedelta(seconds=interval)
    
    def __str__(self) -> str:
        status = self.get_status()
        kickoff = self.get_game_start().strftime('%a %I:%M%p')
        return f"{self.away_team}@{self.home_team} [kickoff={kickoff}, status={status}]"


class Scheduler:
    """
    Manages polling schedule for multiple games.
    
    Maintains a dict of GameState objects and sleeps until
    the next game needs to be polled.
    """
    
    def __init__(self):
        self.games: dict[str, GameState] = {}
        self.conn = None
    
    def refresh_games(self) -> None:
        """
        Discover active games from API and update internal state.
        
        - Adds new games
        - Removes games that are no longer in API (ended/settled)
        - Preserves next_poll for existing games
        """
        try:
            discovered = discover_markets()
        except Exception as e:
            logger.error(f"Failed to discover markets: {e}")
            return
        
        discovered_tickers = set()
        
        for game in discovered:
            event_ticker = game['event_ticker']
            discovered_tickers.add(event_ticker)
            
            # Parse game_time - it comes as ISO string from API
            game_time_str = game['game_time']
            if game_time_str:
                # Handle various ISO formats
                if game_time_str.endswith('Z'):
                    game_time = datetime.fromisoformat(game_time_str.replace('Z', '+00:00'))
                else:
                    game_time = datetime.fromisoformat(game_time_str)
                    if game_time.tzinfo is None:
                        game_time = game_time.replace(tzinfo=timezone.utc)
            else:
                logger.warning(f"No game_time for {event_ticker}, skipping")
                continue
            
            if event_ticker not in self.games:
                # New game - add it
                state = GameState(
                    event_ticker=event_ticker,
                    market_ticker=game['market_ticker'],
                    home_team=game['home_team'],
                    away_team=game['away_team'],
                    game_time=game_time,
                )
                # Set initial next_poll based on current status
                state.update_next_poll()
                self.games[event_ticker] = state
                logger.info(f"Added new game: {state}")
                
                # Ensure game is in database
                self._ensure_in_db(game)
        
        # Remove games no longer in API
        for ticker in list(self.games.keys()):
            if ticker not in discovered_tickers:
                logger.info(f"Removing completed game: {self.games[ticker]}")
                del self.games[ticker]
    
    def _ensure_in_db(self, game: dict) -> None:
        """Make sure game and market exist in database."""
        if self.conn is None:
            self.conn = get_connection()
        
        insert_game(
            event_ticker=game['event_ticker'],
            home_team=game['home_team'],
            away_team=game['away_team'],
            game_time=game['game_time'],
            conn=self.conn
        )
        insert_market(
            ticker=game['market_ticker'],
            event_ticker=game['event_ticker'],
            team=game['team'],
            conn=self.conn
        )
    
    def collect_game(self, game: GameState) -> bool:
        """
        Collect a snapshot for a single game.
        
        Returns True if successful, False otherwise.
        """
        if self.conn is None:
            self.conn = get_connection()
        
        try:
            snapshot = collect_snapshot(game.market_ticker)
            
            snapshot_id = insert_snapshot(
                ticker=game.market_ticker,
                timestamp=snapshot['timestamp'],
                best_bid=snapshot['best_bid'],
                best_ask=snapshot['best_ask'],
                mid=snapshot['mid'],
                spread=snapshot['spread'],
                total_bid_depth=snapshot['total_bid_depth'],
                total_ask_depth=snapshot['total_ask_depth'],
                open_interest=snapshot['open_interest'],
                conn=self.conn
            )
            
            insert_depth_levels(
                snapshot_id=snapshot_id,
                bids=snapshot['bids'],
                asks=snapshot['asks'],
                conn=self.conn
            )
            
            status = game.get_status()
            interval = game.get_interval()
            logger.info(
                f"[{status}] {game.away_team}@{game.home_team}: "
                f"bid={snapshot['best_bid']} ask={snapshot['best_ask']} "
                f"spread={snapshot['spread']} (next in {interval}s)"
            )
            return True
            
        except Exception as e:
            logger.error(f"Failed to collect {game.market_ticker}: {e}")
            return False
    
    def get_next_game(self) -> Optional[GameState]:
        """Get the game that needs polling soonest."""
        if not self.games:
            return None
        
        return min(self.games.values(), key=lambda g: g.next_poll)
    
    def run_once(self) -> None:
        """Single collection pass - poll all games due now."""
        now = datetime.now(timezone.utc)
        
        for game in self.games.values():
            if game.get_status() == 'ended':
                continue
            
            if game.next_poll <= now:
                self.collect_game(game)
                game.update_next_poll(now)
    
    def run_forever(self) -> None:
        """
        Main loop - runs continuously, sleeping between polls.
        
        Refreshes game list every 15 minutes to catch new games.
        """
        init_db()
        last_refresh = datetime.min.replace(tzinfo=timezone.utc)
        refresh_interval = timedelta(minutes=15)
        
        logger.info("Starting adaptive scheduler...")
        
        while True:
            now = datetime.now(timezone.utc)
            
            # Refresh game list periodically
            if now - last_refresh > refresh_interval:
                logger.info("Refreshing game list...")
                self.refresh_games()
                last_refresh = now
                
                if not self.games:
                    logger.info("No active games. Sleeping 15 minutes...")
                    time.sleep(900)
                    continue
            
            # Poll any games that are due
            for game in list(self.games.values()):
                if game.next_poll <= now:
                    self.collect_game(game)
                    game.update_next_poll(now)
            
            # Sleep until next game is due
            next_game = self.get_next_game()
            if next_game is None:
                logger.info("No active games remaining. Sleeping 15 minutes...")
                time.sleep(900)
                continue
            
            sleep_seconds = (next_game.next_poll - datetime.now(timezone.utc)).total_seconds()
            sleep_seconds = max(1, sleep_seconds)  # At least 1 second
            
            # Log status
            live_count = sum(1 for g in self.games.values() if g.get_status() == 'live')
            near_count = sum(1 for g in self.games.values() if g.get_status() == 'near')
            far_count = sum(1 for g in self.games.values() if g.get_status() == 'far')
            
            logger.debug(
                f"Games: {live_count} live, {near_count} near, {far_count} far. "
                f"Next poll in {sleep_seconds:.0f}s ({next_game.away_team}@{next_game.home_team})"
            )
            
            time.sleep(sleep_seconds)


def main():
    parser = argparse.ArgumentParser(description='Adaptive Kalshi NFL data collector')
    parser.add_argument('--once', action='store_true', help='Single pass, then exit')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    scheduler = Scheduler()
    
    # Initial game discovery
    init_db()
    scheduler.refresh_games()
    
    if not scheduler.games:
        logger.warning("No active games found!")
        return
    
    logger.info(f"Tracking {len(scheduler.games)} games:")
    for game in scheduler.games.values():
        logger.info(f"  {game}")
    
    if args.once:
        scheduler.run_once()
    else:
        scheduler.run_forever()


if __name__ == '__main__':
    main()