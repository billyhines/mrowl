"""
Main data collection script.

Run this via cron to collect orderbook snapshots.
Designed to be idempotent - safe to run multiple times.

Usage:
    python -m src.collector          # Collect all active markets
    python -m src.collector --once   # Single pass, then exit
"""

import argparse
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .config import NFL_SERIES_TICKERS
from .kalshi_api import get_nfl_markets, get_orderbook, get_market_info, build_unified_orderbook
from .db import (
    get_connection, init_db, insert_game, insert_market,
    insert_snapshot, insert_depth_levels
)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# NFL team abbreviations (for parsing)
NFL_TEAMS = {
    'ARI', 'ATL', 'BAL', 'BUF', 'CAR', 'CHI', 'CIN', 'CLE',
    'DAL', 'DEN', 'DET', 'GB', 'HOU', 'IND', 'JAX', 'KC',
    'LAC', 'LAR', 'LA', 'LV', 'MIA', 'MIN', 'NE', 'NO',
    'NYG', 'NYJ', 'PHI', 'PIT', 'SEA', 'SF', 'TB', 'TEN', 'WAS'
}


def parse_event_ticker(event_ticker: str) -> dict:
    """
    Parse event ticker to extract game info.
    
    Handles multiple formats:
    - Regular: KXNFLGAME-26JAN10GBCHI -> {'away': 'GB', 'home': 'CHI', 'date_str': '26JAN10'}
    - Championship: KXNFLAFCCHAMP-25 -> {'away': None, 'home': None, 'date_str': '25'}
    
    Returns: {'date_str': str, 'away': str|None, 'home': str|None}
    """
    parts = event_ticker.split('-')
    if len(parts) != 2:
        raise ValueError(f"Unexpected event ticker format: {event_ticker}")
    
    series = parts[0]
    suffix = parts[1]
    
    # Championship format: just a year/id like "25"
    if len(suffix) <= 4 or not any(c.isalpha() for c in suffix[2:]):
        return {
            'date_str': suffix,
            'away': None,
            'home': None,
            'is_championship': True
        }
    
    # Regular game format: YYMMMDDAWAYHOME like "26JAN10GBCHI"
    # Date is first 7 chars (YYMMMDD), rest is teams
    date_str = suffix[:7]
    teams_str = suffix[7:]
    
    # Try to intelligently split teams using known abbreviations
    away, home = split_team_codes(teams_str)
    
    return {
        'date_str': date_str,
        'away': away,
        'home': home,
        'is_championship': False
    }


def split_team_codes(teams_str: str) -> tuple[str, str]:
    """
    Split concatenated team codes like 'GBCHI' into ('GB', 'CHI').
    
    Uses known NFL team abbreviations for accurate splitting.
    """
    # Try all possible split points
    for i in range(2, len(teams_str) - 1):
        away = teams_str[:i]
        home = teams_str[i:]
        if away in NFL_TEAMS and home in NFL_TEAMS:
            return away, home
    
    # Fallback: split in half
    mid = len(teams_str) // 2
    return teams_str[:mid], teams_str[mid:]


def extract_teams_from_market(market: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Extract team names from market metadata.
    
    Uses title, subtitle, or other fields to determine teams.
    """
    # Try to get from title (e.g., "Bills vs Chiefs" or "Buffalo Bills")
    title = market.get('title', '')
    subtitle = market.get('subtitle', '')
    
    # The ticker often ends with the team code
    ticker = market.get('ticker', '')
    team_from_ticker = ticker.split('-')[-1] if '-' in ticker else None
    
    # For championship games, we might need to look at event-level data
    # or use the yes/no contract titles
    
    # Try to extract from title patterns like "Team X to win"
    # or "AFC Championship: Team X vs Team Y"
    
    # Return what we can find
    return team_from_ticker, None  # Returns (team_this_market_is_for, opponent)


def discover_markets() -> list[dict]:
    """
    Fetch active NFL markets and organize by game.
    
    Returns list of dicts with game info and the market to track.
    We only track one market per game (they're mirrors).
    """
    markets = get_nfl_markets(status='open')
    
    # Group by event_ticker
    games = {}
    for m in markets:
        event = m['event_ticker']
        if event not in games:
            games[event] = []
        games[event].append(m)
    
    result = []
    for event_ticker, event_markets in games.items():
        # Pick first market (arbitrary - they're mirrors)
        market = event_markets[0]
        
        try:
            parsed = parse_event_ticker(event_ticker)
        except ValueError as e:
            logger.warning(f"Skipping unparseable event: {e}")
            continue
        
        # For championship games, try to get team info from market metadata
        if parsed.get('is_championship') or not parsed.get('away'):
            # Get team code from the market ticker (last segment)
            ticker_parts = market['ticker'].split('-')
            team = ticker_parts[-1] if len(ticker_parts) > 1 else 'UNK'
            
            # Use yes_sub_title for full team name (e.g., "New England")
            # This is the team this market is FOR
            team_name = market.get('yes_sub_title', team)
            
            # Log what we have for debugging
            logger.debug(f"Championship market: ticker={market['ticker']}, team={team_name}")
            
            # If we have both markets, we can get both team names
            if len(event_markets) == 2:
                # Get team names from yes_sub_title of each market
                team_names = [m.get('yes_sub_title', m['ticker'].split('-')[-1]) for m in event_markets]
                away_team = team_names[0]
                home_team = team_names[1]
            else:
                away_team = team_name
                home_team = 'TBD'
        else:
            away_team = parsed['away']
            home_team = parsed['home']
            team = market['ticker'].split('-')[-1]
        
        result.append({
            'event_ticker': event_ticker,
            'market_ticker': market['ticker'],
            'team': team if not parsed.get('is_championship') else market.get('yes_sub_title', team),
            'home_team': home_team,
            'away_team': away_team,
            'game_time': market.get('expected_expiration_time'),
            'market': market
        })
    
    return result


def collect_snapshot(ticker: str) -> dict:
    """
    Collect a full snapshot of market state.
    
    Returns dict ready to be inserted into database.
    """
    timestamp = datetime.now(timezone.utc)
    
    # Get orderbook
    raw_book = get_orderbook(ticker)
    book = build_unified_orderbook(raw_book['yes'], raw_book['no'])
    
    # Calculate metrics
    sorted_bids = sorted(book['bids'].items(), reverse=True)
    sorted_asks = sorted(book['asks'].items())
    
    best_bid = sorted_bids[0][0] if sorted_bids else None
    best_ask = sorted_asks[0][0] if sorted_asks else None
    
    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else None
    spread = best_ask - best_bid if (best_bid and best_ask) else None
    
    # Get open interest from market info
    try:
        market_info = get_market_info(ticker)
        open_interest = market_info.get('open_interest')
    except Exception as e:
        logger.warning(f"Failed to get market info for {ticker}: {e}")
        open_interest = None
    
    return {
        'timestamp': timestamp,
        'best_bid': best_bid,
        'best_ask': best_ask,
        'mid': mid,
        'spread': spread,
        'bids': book['bids'],
        'asks': book['asks'],
        'total_bid_depth': sum(book['bids'].values()),
        'total_ask_depth': sum(book['asks'].values()),
        'open_interest': open_interest
    }


def run_collection() -> None:
    """
    Main collection routine.
    
    1. Discover active NFL markets
    2. For each game, ensure it's in the database
    3. Collect and store a snapshot
    """
    logger.info("Starting collection run")
    
    # Ensure database exists
    init_db()
    
    # Discover markets
    try:
        games = discover_markets()
        logger.info(f"Found {len(games)} active games")
    except Exception as e:
        logger.error(f"Failed to discover markets: {e}")
        return
    
    conn = get_connection()
    
    for game in games:
        try:
            # Ensure game and market are in DB
            insert_game(
                event_ticker=game['event_ticker'],
                home_team=game['home_team'],
                away_team=game['away_team'],
                game_time=game['game_time'],
                conn=conn
            )
            insert_market(
                ticker=game['market_ticker'],
                event_ticker=game['event_ticker'],
                team=game['team'],
                conn=conn
            )
            
            # Collect snapshot
            snapshot = collect_snapshot(game['market_ticker'])
            
            # Store snapshot
            snapshot_id = insert_snapshot(
                ticker=game['market_ticker'],
                timestamp=snapshot['timestamp'],
                best_bid=snapshot['best_bid'],
                best_ask=snapshot['best_ask'],
                mid=snapshot['mid'],
                spread=snapshot['spread'],
                total_bid_depth=snapshot['total_bid_depth'],
                total_ask_depth=snapshot['total_ask_depth'],
                open_interest=snapshot['open_interest'],
                conn=conn
            )
            
            # Store depth levels
            insert_depth_levels(
                snapshot_id=snapshot_id,
                bids=snapshot['bids'],
                asks=snapshot['asks'],
                conn=conn
            )
            
            logger.info(
                f"Collected {game['market_ticker']}: "
                f"bid={snapshot['best_bid']} ask={snapshot['best_ask']} "
                f"spread={snapshot['spread']} depth={snapshot['total_bid_depth']+snapshot['total_ask_depth']}"
            )
            
        except Exception as e:
            logger.error(f"Failed to collect {game['market_ticker']}: {e}")
            continue
    
    conn.close()
    logger.info("Collection run complete")


def main():
    parser = argparse.ArgumentParser(description='Collect Kalshi NFL market data')
    parser.add_argument('--once', action='store_true', help='Run once and exit')
    args = parser.parse_args()
    
    # For now, always run once (cron handles scheduling)
    run_collection()


if __name__ == '__main__':
    main()