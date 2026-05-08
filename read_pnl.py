"""
Reads current unrealized P/L for all open Binance Futures positions,
and prints all trades from the past 3 months.

Credentials are read from environment variables:
  EXCHANGE_API_KEY    — Binance API key
  EXCHANGE_API_SECRET — Binance API secret
"""

import os
import ccxt
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

load_dotenv()

WINDOW_DAYS = 7  # Binance Futures API max window per request

def fetch_trades_3months(exchange, symbol):
    """Paginate through 3 months of trade history in 7-day windows."""
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=90)
    trades = []

    window_start = start
    while window_start < now:
        window_end = min(window_start + timedelta(days=WINDOW_DAYS), now)
        since    = int(window_start.timestamp() * 1000)
        end_time = int(window_end.timestamp() * 1000)
        try:
            batch = exchange.fetch_my_trades(
                symbol, since=since,
                params={"endTime": end_time}
            )
            trades.extend(batch)
        except Exception:
            pass
        window_start = window_end

    # deduplicate by trade id
    seen = set()
    unique = []
    for t in trades:
        if t['id'] not in seen:
            seen.add(t['id'])
            unique.append(t)

    unique.sort(key=lambda t: t['timestamp'])
    return unique


def main():
    api_key    = os.getenv("EXCHANGE_API_KEY", "")
    api_secret = os.getenv("EXCHANGE_API_SECRET", "")

    if not api_key or not api_secret:
        print("ERROR: Set EXCHANGE_API_KEY and EXCHANGE_API_SECRET environment variables.")
        return

    exchange = ccxt.binanceusdm({
        "apiKey": api_key,
        "secret": api_secret,
        "options": {"defaultType": "future"},
    })

    # ── Open positions ─────────────────────────────────────────────────────────
    positions = exchange.fetch_positions()
    open_positions = [p for p in positions if float(p["contracts"]) != 0]

    if open_positions:
        print("=== Open Positions ===")
        print(f"{'Symbol':<16} {'Side':<6} {'Size':>10} {'Entry':>12} {'Mark':>12} {'Unrealized P/L':>16} {'ROI%':>8}")
        print("-" * 86)
        for p in open_positions:
            leverage = p.get('leverage') or 1
            initial_margin = (p['entryPrice'] * abs(p['contracts'])) / leverage
            roi = (p['unrealizedPnl'] / initial_margin * 100) if initial_margin else 0
            print(
                f"{p['symbol']:<16} "
                f"{p['side']:<6} "
                f"{p['contracts']:>10} "
                f"{p['entryPrice']:>12.4f} "
                f"{p['markPrice']:>12.4f} "
                f"{p['unrealizedPnl']:>+16.4f} "
                f"{roi:>+7.2f}%"
            )
    else:
        print("No open positions.")

    # ── Trade history (last 3 months) ─────────────────────────────────────────
    # Collect unique symbols to query: open positions + BTC/USDT:USDT
    symbols_to_check = list({p['symbol'] for p in open_positions} | {'BTC/USDT:USDT'})

    all_trades = []
    for symbol in symbols_to_check:
        print(f"\nFetching trades for {symbol}...")
        trades = fetch_trades_3months(exchange, symbol)
        all_trades.extend(trades)

    all_trades.sort(key=lambda t: t['timestamp'])

    print(f"\n=== Trade History — last 3 months ({len(all_trades)} trades) ===")
    if not all_trades:
        print("No trades found.")
        return

    print(f"{'Date':<22} {'Symbol':<16} {'Side':<5} {'Amount':>10} {'Price':>12} {'Cost (USDT)':>14} {'Fee':>10}")
    print("-" * 95)
    for t in all_trades:
        date     = datetime.fromtimestamp(t['timestamp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        fee_cost = t['fee']['cost'] if t.get('fee') else 0
        print(
            f"{date:<22} "
            f"{t['symbol']:<16} "
            f"{t['side']:<5} "
            f"{t['amount']:>10.4f} "
            f"{t['price']:>12.2f} "
            f"{t['cost']:>14.2f} "
            f"{fee_cost:>10.4f}"
        )

if __name__ == "__main__":
    main()
