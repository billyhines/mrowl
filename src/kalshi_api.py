"""
Kalshi API wrapper.

Handles all communication with the Kalshi API.
No authentication required for read-only market data.
"""

import requests
from typing import Optional
from .config import KALSHI_BASE_URL, NFL_SERIES_TICKER


def get_nfl_markets(status: str = 'open') -> list[dict]:
    """
    Get all NFL game markets.
    
    Args:
        status: Filter by market status ('open', 'closed', etc.)
    
    Returns:
        List of market dicts from Kalshi API
    """
    url = f"{KALSHI_BASE_URL}/markets"
    params = {'series_ticker': NFL_SERIES_TICKER, 'status': status}
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()['markets']


def get_orderbook(ticker: str) -> dict:
    """
    Fetch raw orderbook for a market.
    
    Returns:
        {'yes': [[price, qty], ...], 'no': [[price, qty], ...]}
    """
    url = f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()['orderbook']


def get_market_info(ticker: str) -> dict:
    """Fetch market metadata (open interest, volume, etc.)."""
    url = f"{KALSHI_BASE_URL}/markets/{ticker}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()['market']


def build_unified_orderbook(yes_book: list, no_book: list) -> dict:
    """
    Convert Kalshi's YES/NO bids into a standard bid/ask orderbook.
    
    In binary prediction markets:
    - YES BID at X¢ = someone wants to buy "yes" at X
    - NO BID at Y¢ = effectively a YES ASK at (100-Y)¢
    
    Returns:
        {'bids': {price: qty, ...}, 'asks': {price: qty, ...}}
    """
    bids = {price: qty for price, qty in yes_book}
    asks = {(100 - price): qty for price, qty in no_book}
    return {'bids': bids, 'asks': asks}


def get_candlesticks(
    series: str,
    event: str,
    start_ts: int,
    end_ts: int,
    interval: int = 60
) -> dict:
    """
    Fetch historical candlestick data.
    
    Args:
        series: Series ticker (e.g., 'KXNFLGAME')
        event: Event ticker (e.g., 'KXNFLGAME-26JAN10GBCHI')
        start_ts: Unix timestamp for start of range
        end_ts: Unix timestamp for end of range
        interval: Candle interval in minutes (1, 60, or 1440)
    
    Returns:
        Candlestick data from API
    """
    url = f"{KALSHI_BASE_URL}/series/{series}/events/{event}/candlesticks"
    params = {
        'start_ts': start_ts,
        'end_ts': end_ts,
        'period_interval': interval
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()
