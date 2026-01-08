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
from datetime import datetime, timezone
from typing import Optional

from .config import NFL_SERIES_TICKER
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


def parse_event_ticker(event_ticker: str) -> dict:
    """
    Parse event ticker to extract game info.
    
    Example: KXNFLGAME-26JAN10GBCHI
    Returns: {'date_str': '26JAN10', 'away': 'GB', 'home': 'CHI'}
    """
    # Format: KXNFLGAME-{YYMMMDD}{AWAY}{HOME}
    parts = event_ticker.split('-')
    if len(parts) != 2:
        raise ValueError(f"Unexpected event ticker format: {event_ticker}")
    
    suffix = parts[1]  # e.g., "26JAN10GBCHI"
    
    # Date is first 7 chars (YYMMMDD), rest is teams
    date_str = suffix[:7]
    teams_str = suffix[7:]
    
    # Teams are 2-3 chars each, but we need to figure out where to split
    # Common NFL abbreviations are 2-3 chars
    # For now, assume 2-3 char codes and try to split reasonably
    # This is fragile - may need refinement based on actual data
    
    # Most NFL teams use 2-3 letter codes
    # Let's assume away team is first 2-3 chars based on total length
    if len(teams_str) == 4:
        away, home = teams_str[:2], teams_str[2:]
    elif len(teams_str) == 5:
        # Could be 2+3 or 3+2, need heuristic
        # For now assume 2+3 (more common)
        away, home = teams_str[:2], teams_str[2:]
    elif len(teams_str) == 6:
        away, home = teams_str[:3], teams_str[3:]
    else:
        # Fallback
        away, home = teams_str[:len(teams_str)//2], teams_str[len(teams_str)//2:]
    
    return {
        'date_str': date_str,
        'away': away,
        'home': home
    }


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
        
        result.append({
            'event_ticker': event_ticker,
            'market_ticker': market['ticker'],
            'team': market['ticker'].split('-')[-1],
            'home_team': parsed['home'],
            'away_team': parsed['away'],
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
