"""
Streamlit app for visualizing NFL market liquidity.

Run with:
    streamlit run app/streamlit_app.py
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import streamlit as st
import plotly.graph_objects as go
from datetime import datetime, timedelta

from src.db import get_connection, get_active_games, get_snapshots_for_market, get_depth_for_snapshot
from src.config import DB_PATH


st.set_page_config(
    page_title="NFL Liquidity Tracker",
    page_icon="🏈",
    layout="wide"
)

st.title("🏈 Kalshi NFL Liquidity Tracker")


def main():
    # Check if database exists
    if not DB_PATH.exists():
        st.warning("Database not found. Run `python scripts/setup_db.py` first.")
        st.stop()
    
    conn = get_connection()
    
    # Get list of games with data
    cursor = conn.execute("""
        SELECT DISTINCT m.ticker, m.team, g.home_team, g.away_team, g.game_time
        FROM markets m
        JOIN games g ON m.event_ticker = g.event_ticker
        JOIN snapshots s ON s.ticker = m.ticker
        ORDER BY g.game_time
    """)
    markets = cursor.fetchall()
    
    if not markets:
        st.info("No data collected yet. Run the collector first:")
        st.code("python -m src.collector")
        conn.close()
        st.stop()
    
    # Market selector
    market_options = {
        f"{m['away_team']} @ {m['home_team']} ({m['game_time'][:10]})": m['ticker']
        for m in markets
    }
    
    selected_label = st.selectbox("Select Game", options=list(market_options.keys()))
    selected_ticker = market_options[selected_label]
    
    # Get snapshots for selected market
    snapshots = get_snapshots_for_market(selected_ticker, conn=conn)
    
    if not snapshots:
        st.warning("No snapshots for this market yet.")
        conn.close()
        st.stop()
    
    # --- Spread over time ---
    st.subheader("Spread Over Time")
    
    times = [s['timestamp'] for s in snapshots]
    spreads = [s['spread'] for s in snapshots]
    
    fig_spread = go.Figure()
    fig_spread.add_trace(go.Scatter(x=times, y=spreads, mode='lines+markers', name='Spread'))
    fig_spread.update_layout(
        xaxis_title="Time",
        yaxis_title="Spread (cents)",
        height=300
    )
    st.plotly_chart(fig_spread, use_container_width=True)
    
    # --- Total depth over time ---
    st.subheader("Total Depth Over Time")
    
    total_depth = [s['total_bid_depth'] + s['total_ask_depth'] for s in snapshots]
    
    fig_depth = go.Figure()
    fig_depth.add_trace(go.Scatter(x=times, y=total_depth, mode='lines+markers', name='Total Depth'))
    fig_depth.update_layout(
        xaxis_title="Time",
        yaxis_title="Contracts",
        height=300
    )
    st.plotly_chart(fig_depth, use_container_width=True)
    
    # --- Latest orderbook ---
    st.subheader("Latest Orderbook")
    
    latest_snapshot = snapshots[-1]
    depth_levels = get_depth_for_snapshot(latest_snapshot['id'], conn=conn)
    
    bids = [(d['price'], d['quantity']) for d in depth_levels if d['side'] == 'bid']
    asks = [(d['price'], d['quantity']) for d in depth_levels if d['side'] == 'ask']
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.write("**Bids**")
        bids_sorted = sorted(bids, reverse=True)[:10]
        for price, qty in bids_sorted:
            st.write(f"{price}¢: {qty} contracts")
    
    with col2:
        st.write("**Asks**")
        asks_sorted = sorted(asks)[:10]
        for price, qty in asks_sorted:
            st.write(f"{price}¢: {qty} contracts")
    
    # --- Stats ---
    st.subheader("Current Stats")
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Best Bid", f"{latest_snapshot['best_bid']}¢")
    col2.metric("Best Ask", f"{latest_snapshot['best_ask']}¢")
    col3.metric("Spread", f"{latest_snapshot['spread']}¢")
    col4.metric("Open Interest", f"{latest_snapshot['open_interest']:,}" if latest_snapshot['open_interest'] else "N/A")
    
    conn.close()


if __name__ == "__main__":
    main()
