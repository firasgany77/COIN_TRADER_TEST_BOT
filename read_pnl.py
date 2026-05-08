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


def fetch_leverage_map(exchange):
    """Return {symbol: leverage} derived from notional / initialMargin (v3 endpoint)."""
    try:
        risks = exchange.fapiPrivateV3GetPositionRisk()
        result = {}
        for r in risks:
            notional = float(r.get('notional', 0))
            margin   = float(r.get('initialMargin', 0))
            if margin > 0 and notional > 0:
                result[r['symbol']] = round(notional / margin)
        return result
    except Exception:
        return {}


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

    seen, unique = set(), []
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

    # ── Leverage map ───────────────────────────────────────────────────────────
    leverage_map = fetch_leverage_map(exchange)

    # ── Open positions ─────────────────────────────────────────────────────────
    positions     = exchange.fetch_positions()
    open_positions = [p for p in positions if float(p["contracts"] or 0) != 0]

    print("=== Open Positions ===")
    if open_positions:
        print(f"{'Symbol':<16} {'Side':<6} {'Leverage':>9} {'Size':>10} {'Entry':>12} "
              f"{'Mark':>12} {'Unrealized P/L':>16} {'ROI%':>8}")
        print("-" * 97)
        for p in open_positions:
            # strip ':USDT' suffix to match Binance symbol format (e.g. BTCUSDT)
            raw_sym     = str(p['symbol'] or '').replace('/', '').replace(':USDT', '')
            leverage    = leverage_map.get(raw_sym) or int(p.get('leverage') or 1)
            notional    = float(p['entryPrice'] or 0) * abs(float(p['contracts'] or 0))
            init_margin = notional / leverage
            roi         = (float(p['unrealizedPnl'] or 0) / init_margin * 100) if init_margin else 0
            print(
                f"{str(p['symbol']):<16} "
                f"{str(p['side']):<6} "
                f"{leverage:>8}x "
                f"{float(p['contracts'] or 0):>10} "
                f"{float(p['entryPrice'] or 0):>12.2f} "
                f"{float(p['markPrice'] or 0):>12.2f} "
                f"{float(p['unrealizedPnl'] or 0):>+16.2f} "
                f"{roi:>+7.2f}%"
            )
    else:
        print("No open positions.")

    # ── Trade history (last 3 months) ─────────────────────────────────────────
    symbols_to_check = list({p['symbol'] for p in open_positions} | {'BTC/USDT:USDT'})

    all_trades = []
    for symbol in symbols_to_check:
        trades = fetch_trades_3months(exchange, symbol)
        all_trades.extend(trades)

    all_trades.sort(key=lambda t: t['timestamp'])

    print(f"\n=== Trade History — last 3 months ({len(all_trades)} trades) ===")
    if not all_trades:
        print("No trades found.")
        return

    print(f"{'Date':<22} {'Symbol':<16} {'Side':<5} {'Amount':>10} {'Price':>12} {'Cost (USDT)':>14} {'Fee (USDT)':>12}")
    print("-" * 97)

    total_buy_cost  = 0
    total_sell_cost = 0
    total_fees      = 0

    for t in all_trades:
        date     = datetime.fromtimestamp(t['timestamp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        fee_cost = t['fee']['cost'] if t.get('fee') else 0
        total_fees += fee_cost
        if t['side'] == 'buy':
            total_buy_cost += t['cost']
        else:
            total_sell_cost += t['cost']
        print(
            f"{date:<22} "
            f"{t['symbol']:<16} "
            f"{t['side']:<5} "
            f"{t['amount']:>10.4f} "
            f"{t['price']:>12.2f} "
            f"{t['cost']:>14.2f} "
            f"{fee_cost:>12.4f}"
        )

    print("-" * 97)
    print(f"{'Total Buy Cost:':<50} {total_buy_cost:>14.2f}")
    print(f"{'Total Sell Cost:':<50} {total_sell_cost:>14.2f}")
    print(f"{'Total Fees Paid:':<50} {total_fees:>14.4f}")

    # ROI summary using trade cost as initial margin base
    for p in open_positions:
        raw_sym     = str(p['symbol'] or '').replace('/', '').replace(':USDT', '')
        leverage    = leverage_map.get(raw_sym) or int(p.get('leverage') or 1)
        notional    = float(p['entryPrice'] or 0) * abs(float(p['contracts'] or 0))
        init_margin = notional / leverage
        roi         = (float(p['unrealizedPnl'] or 0) / init_margin * 100) if init_margin else 0
        print(f"\n{'Position:':<20} {p['symbol']}")
        print(f"{'Leverage:':<20} {leverage}x")
        print(f"{'Notional Value:':<20} {notional:,.2f} USDT")
        print(f"{'Initial Margin:':<20} {init_margin:,.2f} USDT")
        print(f"{'Unrealized P/L:':<20} {p['unrealizedPnl']:+,.2f} USDT")
        print(f"{'ROI%:':<20} {roi:+.2f}%")


if __name__ == "__main__":
    main()
