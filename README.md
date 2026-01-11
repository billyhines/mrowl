# Mr Owl's Liquidity Tracker

A Python application that tracks and visualizes liquidity in Kalshi NFL prediction markets over time. Helps bettors understand when and how liquidity becomes available—from early week through game day.

## Overview

This tool collects orderbook snapshots from Kalshi's NFL betting markets, stores them in SQLite, and provides a Streamlit dashboard for visualization. Key features include:

- **Real-time orderbook collection** with adaptive polling frequency
- **Depth heatmap visualization** showing liquidity at every price level over time
- **Historical tracking** of spreads, depth, and open interest
- **Automatic market discovery** for active NFL games

## Project Structure

```
mrowl/
├── app/
│   └── streamlit_app.py      # Visualization dashboard
├── src/
│   ├── __init__.py
│   ├── config.py             # Configuration constants
│   ├── kalshi_api.py         # Kalshi API wrapper
│   ├── db.py                 # Database operations
│   └── collector.py          # Data collection script
├── data/
│   └── nfl_liquidity.db      # SQLite database (created at runtime)
├── requirements.txt
└── README.md
```

## Installation

```bash
# Clone the repository
git clone https://github.com/billyhines/mrowl.git
cd mrowl

# Install dependencies
pip install -r requirements.txt
```

### Dependencies

- `requests` - HTTP client for Kalshi API
- `streamlit` - Web dashboard framework
- `plotly` - Interactive visualizations
- `python-dotenv` - Configuration management

## Quick Start

### 1. Initialize the Database

```bash
python -c "from src.db import init_db; init_db()"
```

### 2. Run the Data Collector

```bash
# Single collection pass
python -m src.collector --once

# Continuous collection (for cron or persistent process)
python -m src.collector
```

### 3. Launch the Dashboard

```bash
streamlit run app/streamlit_app.py
```

## How It Works

### Market Structure

Kalshi organizes NFL markets in a hierarchy:

```
Series (KXNFLGAME)
└── Event (KXNFLGAME-26JAN10GBCHI)     ← One per game
    ├── Market (KXNFLGAME-26JAN10GBCHI-GB)   ← "GB wins"
    └── Market (KXNFLGAME-26JAN10GBCHI-CHI)  ← "CHI wins"
```

Since the two markets per game are mirrors (GB YES ≈ CHI NO), we only track one market per game.

### Orderbook Translation

Kalshi orderbooks show YES and NO bids separately. We convert these to a traditional bid/ask format:

- **Bids** = YES bids (people wanting to buy the outcome)
- **Asks** = Inverted NO bids (100¢ - NO bid price)

### Polling Strategy

| Time to Game | Polling Frequency |
|--------------|-------------------|
| > 24 hours   | Hourly            |
| < 24 hours   | Every 15 minutes  |
| Game started | Stop polling      |

## API Reference

The Kalshi API requires no authentication for read-only market data.

### Key Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `GET /markets?series_ticker=KXNFLGAME` | List active NFL markets |
| `GET /markets/{ticker}/orderbook` | Full orderbook depth |
| `GET /markets/{ticker}` | Market metadata (open interest, volume) |
| `GET /series/{series}/events/{event}/candlesticks` | Historical OHLC data |

### Rate Limits

- **Basic tier:** 20 requests/second (sufficient for this use case)

## Database Schema

```sql
-- Games being tracked
games (event_ticker, home_team, away_team, game_time)

-- One market per game
markets (ticker, event_ticker, team)

-- Point-in-time snapshots
snapshots (ticker, timestamp, best_bid, best_ask, mid, spread, 
           total_bid_depth, total_ask_depth, open_interest)

-- Full orderbook depth per snapshot
depth_levels (snapshot_id, side, price, quantity)

-- Historical candlestick data
candles (ticker, end_time, interval_minutes, OHLC, volume, open_interest)
```

## Configuration

Edit `src/config.py` to customize:

```python
# Paths
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "nfl_liquidity.db"

# API
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
NFL_SERIES_TICKER = "KXNFLGAME"

# Polling intervals (minutes)
POLL_INTERVAL_FAR = 60      # >24 hours to game
POLL_INTERVAL_NEAR = 15     # <24 hours to game

# Threshold for switching to frequent polling
NEAR_GAME_THRESHOLD_HOURS = 24
```

## Visualization Features

The Streamlit dashboard provides:

- **Market selector** - Choose which game to analyze
- **Depth heatmap** - Visual representation of orderbook depth over time
  - Blue = bids below mid price
  - Red = asks above mid price
  - White line = mid price movement
- **Liquidity metrics** - Spread, total depth, open interest trends
- **Time range selection** - Filter data by timeframe

## Example Usage

### Programmatic Access

```python
from src.kalshi_api import get_nfl_markets, get_orderbook, build_unified_orderbook
from src.collector import collect_snapshot

# Get all active NFL markets
markets = get_nfl_markets(status='open')

# Fetch orderbook for a specific market
ticker = "KXNFLGAME-26JAN10GBCHI-GB"
raw_book = get_orderbook(ticker)
book = build_unified_orderbook(raw_book['yes'], raw_book['no'])

# Collect a full snapshot (with metadata)
snapshot = collect_snapshot(ticker)
print(f"Mid: {snapshot['mid']}¢, Spread: {snapshot['spread']}¢")
```

### Query Historical Data

```python
from src.db import get_connection, get_snapshots_for_market

conn = get_connection()
snapshots = get_snapshots_for_market("KXNFLGAME-26JAN10GBCHI-GB", conn=conn)
for snap in snapshots:
    print(f"{snap['timestamp']}: mid={snap['mid']}, spread={snap['spread']}")
```

## Resources

- [Kalshi API Documentation](https://trading-api.readme.io/reference/getting-started)
- [Streamlit Documentation](https://docs.streamlit.io/)
