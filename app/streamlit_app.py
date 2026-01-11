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
import numpy as np
from datetime import datetime, timedelta

from src.db import get_connection, get_active_games, get_snapshots_for_market, get_depth_for_snapshot
from src.config import DB_PATH


st.set_page_config(
    page_title="NFL Liquidity Tracker",
    page_icon="🏈",
    layout="wide"
)

st.title("🏈 Kalshi NFL Liquidity Tracker")


def signed_log(x):
    """Apply log transform preserving sign: sign(x) * log1p(|x|)"""
    return np.sign(x) * np.log1p(np.abs(x))


def build_depth_heatmap(snapshots, conn):
    """Build depth heatmap data from snapshots."""
    snapshot_ids = [s['id'] for s in snapshots]
    times = [s['timestamp'] for s in snapshots]
    mids = [s['mid'] for s in snapshots]
    
    # Get all depth data to determine price range
    all_depths = []
    for snap_id in snapshot_ids:
        depths = get_depth_for_snapshot(snap_id, conn=conn)
        all_depths.extend(depths)
    
    if not all_depths:
        return None, None, None, None
    
    all_prices = [d['price'] for d in all_depths]
    min_price = max(1, min(all_prices) - 5)
    max_price = min(99, max(all_prices) + 5)
    price_range = list(range(min_price, max_price + 1))
    
    # Build matrices
    bid_matrix = np.zeros((len(price_range), len(snapshots)))
    ask_matrix = np.zeros((len(price_range), len(snapshots)))
    
    for col_idx, snap_id in enumerate(snapshot_ids):
        depths = get_depth_for_snapshot(snap_id, conn=conn)
        for d in depths:
            price = d['price']
            if price < min_price or price > max_price:
                continue
            row_idx = price - min_price
            if d['side'] == 'bid':
                bid_matrix[row_idx, col_idx] = d['quantity']
            else:
                ask_matrix[row_idx, col_idx] = d['quantity']
    
    # Combine: bids below mid (negative), asks above mid (positive)
    combined_matrix = np.zeros_like(bid_matrix)
    for col_idx, mid in enumerate(mids):
        if mid is None:
            continue
        for row_idx, price in enumerate(price_range):
            if price < mid:
                combined_matrix[row_idx, col_idx] = -bid_matrix[row_idx, col_idx]
            else:
                combined_matrix[row_idx, col_idx] = ask_matrix[row_idx, col_idx]
    
    # Apply signed log transform for better visualization
    combined_matrix = signed_log(combined_matrix)
    
    return combined_matrix, times, mids, price_range


def build_depth_chart(depth_levels, best_bid, best_ask):
    """Build a cumulative depth chart visualization."""
    bids = {d['price']: d['quantity'] for d in depth_levels if d['side'] == 'bid'}
    asks = {d['price']: d['quantity'] for d in depth_levels if d['side'] == 'ask'}
    
    if not bids and not asks:
        return None
    
    # Sort bids descending (best bid first), asks ascending (best ask first)
    bid_prices = sorted(bids.keys(), reverse=True)
    ask_prices = sorted(asks.keys())
    
    # Calculate cumulative depth
    bid_cumulative = []
    cumsum = 0
    for p in bid_prices:
        cumsum += bids[p]
        bid_cumulative.append(cumsum)
    
    ask_cumulative = []
    cumsum = 0
    for p in ask_prices:
        cumsum += asks[p]
        ask_cumulative.append(cumsum)
    
    fig = go.Figure()
    
    # Bids - filled area (green)
    if bid_prices:
        fig.add_trace(go.Scatter(
            x=bid_prices,
            y=bid_cumulative,
            fill='tozeroy',
            fillcolor='rgba(0, 200, 83, 0.3)',
            line=dict(color='rgb(0, 200, 83)', width=2),
            name='Bids',
            hovertemplate='Price: %{x}¢<br>Cumulative: %{y} contracts<extra></extra>'
        ))
    
    # Asks - filled area (red)
    if ask_prices:
        fig.add_trace(go.Scatter(
            x=ask_prices,
            y=ask_cumulative,
            fill='tozeroy',
            fillcolor='rgba(255, 82, 82, 0.3)',
            line=dict(color='rgb(255, 82, 82)', width=2),
            name='Asks',
            hovertemplate='Price: %{x}¢<br>Cumulative: %{y} contracts<extra></extra>'
        ))
    
    # Add vertical lines for best bid/ask
    if best_bid:
        fig.add_vline(x=best_bid, line_dash="dash", line_color="green", 
                      annotation_text=f"Bid {best_bid}¢", annotation_position="top left")
    if best_ask:
        fig.add_vline(x=best_ask, line_dash="dash", line_color="red",
                      annotation_text=f"Ask {best_ask}¢", annotation_position="top right")
    
    fig.update_layout(
        xaxis_title="Price (cents)",
        yaxis_title="Cumulative Depth (contracts)",
        height=350,
        xaxis=dict(ticksuffix='¢'),
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode='x unified'
    )
    
    return fig


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
        st.info("No data collected yet. Run the collector first: `python -m src.collector`")
        conn.close()
        st.stop()
    
    # Market selector
    market_options = {
        f"{m['away_team']} @ {m['home_team']} ({m['game_time'][:10]})": m['ticker']
        for m in markets
    }
    
    selected_label = st.selectbox("Select Game", options=list(market_options.keys()))
    selected_ticker = market_options[selected_label]
    
    # Get snapshots for this market
    snapshots = get_snapshots_for_market(selected_ticker, conn=conn)
    
    if not snapshots:
        st.warning("No snapshots for this market yet.")
        conn.close()
        st.stop()
    
    # --- Current Stats ---
    st.subheader("Current Stats")
    latest_snapshot = snapshots[-1]
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Best Bid", f"{latest_snapshot['best_bid']}¢")
    col2.metric("Best Ask", f"{latest_snapshot['best_ask']}¢")
    col3.metric("Spread", f"{latest_snapshot['spread']}¢")
    col4.metric("Open Interest", f"{latest_snapshot['open_interest']:,}" if latest_snapshot['open_interest'] else "N/A")
    
    # --- Depth Heatmap ---
    st.subheader("📊 Depth Heatmap")
    
    with st.spinner("Building heatmap..."):
        combined_matrix, times, mids, price_range = build_depth_heatmap(snapshots, conn)
    
    if combined_matrix is not None:
        fig_heatmap = go.Figure()
        
        # Create colorbar tick values and labels (in original scale)
        # Use nice round numbers that span the likely range
        tick_values_original = [-1000000, -100000, -10000, -1000, -100, 0, 100, 1000, 10000, 100000, 1000000]
        tick_values_log = [float(signed_log(v)) for v in tick_values_original]
        tick_labels = ['-1M', '-100K', '-10K', '-1K', '-100', '0', '100', '1K', '10K', '100K', '1M']
        
        # Filter to values within our data range
        z_min, z_max = combined_matrix.min(), combined_matrix.max()
        filtered = [(v, l) for v, l in zip(tick_values_log, tick_labels) if z_min <= v <= z_max]
        if filtered:
            tick_values_log, tick_labels = zip(*filtered)
        
        # Add heatmap
        fig_heatmap.add_trace(go.Heatmap(
            z=combined_matrix,
            x=times,
            y=price_range,
            colorscale=[
                [0, 'rgb(0, 0, 139)'],       # Dark blue (large bids)
                [0.3, 'rgb(100, 149, 237)'], # Light blue (small bids)
                [0.5, 'rgb(255, 255, 255)'], # White (no depth)
                [0.7, 'rgb(255, 99, 71)'],   # Light red (small asks)
                [1, 'rgb(139, 0, 0)'],       # Dark red (large asks)
            ],
            zmid=0,
            showscale=True,
            colorbar=dict(
                title="Depth",
                tickvals=list(tick_values_log),
                ticktext=list(tick_labels)
            )
        ))
        
        # Add mid price line
        fig_heatmap.add_trace(go.Scatter(
            x=times,
            y=mids,
            mode='lines',
            line=dict(color='black', width=2),
            name='Mid Price'
        ))
        
        fig_heatmap.update_layout(
            xaxis_title="Time",
            yaxis_title="Price (cents)",
            height=500,
            yaxis=dict(ticksuffix='¢')
        )
        
        st.plotly_chart(fig_heatmap, use_container_width=True)
        st.caption("🔵 Blue = bid depth below mid | 🔴 Red = ask depth above mid | ⬛ Black line = mid price")
    else:
        st.warning("No depth data available for heatmap.")
    
    # --- Current Depth Chart ---
    st.subheader("📈 Current Depth Chart")
    
    depth_levels = get_depth_for_snapshot(latest_snapshot['id'], conn=conn)
    fig_depth_chart = build_depth_chart(depth_levels, latest_snapshot['best_bid'], latest_snapshot['best_ask'])
    
    if fig_depth_chart:
        st.plotly_chart(fig_depth_chart, use_container_width=True)
        
        # Summary stats below the chart
        bids = [(d['price'], d['quantity']) for d in depth_levels if d['side'] == 'bid']
        asks = [(d['price'], d['quantity']) for d in depth_levels if d['side'] == 'ask']
        total_bid_depth = sum(q for _, q in bids)
        total_ask_depth = sum(q for _, q in asks)
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Bid Depth", f"{total_bid_depth:,} contracts")
        col2.metric("Total Ask Depth", f"{total_ask_depth:,} contracts")
        col3.metric("Bid/Ask Ratio", f"{total_bid_depth/total_ask_depth:.2f}" if total_ask_depth > 0 else "N/A")
    else:
        st.warning("No depth data available.")
    
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
    
    # --- Data info ---
    st.divider()
    st.caption(f"📈 {len(snapshots)} snapshots | First: {snapshots[0]['timestamp']} | Last: {snapshots[-1]['timestamp']}")
    
    conn.close()


if __name__ == "__main__":
    main()