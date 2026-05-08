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
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

load_dotenv()

WINDOW_DAYS = 7  # Binance Futures API max window per request

console = Console()


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


def pnl_color(value: float) -> str:
    return "green" if value >= 0 else "red"


def fetch_1h_volume(exchange, symbol: str) -> tuple[float, float]:
    """Sum base and quote volume across the last 60 one-minute candles (rolling 1 h)."""
    try:
        binance_symbol = symbol.replace('/', '').replace(':USDT', '')
        klines = exchange.fapiPublicGetKlines({
            'symbol':   binance_symbol,
            'interval': '1m',
            'limit':    60,
        })
        base_vol  = sum(float(k[5]) for k in klines)
        quote_vol = sum(float(k[7]) for k in klines)
        return base_vol, quote_vol
    except Exception:
        return 0.0, 0.0


def fetch_volume_snapshot(exchange, symbols: list[str]) -> list[dict]:
    """Fetch live 24h ticker + rolling 1h volume for each symbol."""
    rows = []
    for symbol in symbols:
        try:
            t                       = exchange.fetch_ticker(symbol)
            vol_1h_base, vol_1h_usd = fetch_1h_volume(exchange, symbol)
            rows.append({
                "symbol":       symbol,
                "last":         float(t.get("last") or 0),
                "base_volume":  float(t.get("baseVolume") or 0),
                "quote_volume": float(t.get("quoteVolume") or 0),
                "change_pct":   float(t.get("percentage") or 0),
                "vol_1h_base":  vol_1h_base,
                "vol_1h_usd":   vol_1h_usd,
                "timestamp":    t.get("timestamp"),
            })
        except Exception:
            pass
    return rows


def main():
    api_key    = os.getenv("EXCHANGE_API_KEY", "")
    api_secret = os.getenv("EXCHANGE_API_SECRET", "")

    if not api_key or not api_secret:
        console.print("[bold red]ERROR:[/] Set EXCHANGE_API_KEY and EXCHANGE_API_SECRET environment variables.")
        return

    exchange = ccxt.binanceusdm({
        "apiKey": api_key,
        "secret": api_secret,
        "options": {"defaultType": "future"},
    })

    # ── Leverage map ───────────────────────────────────────────────────────────
    leverage_map = fetch_leverage_map(exchange)

    # ── Live trading volume snapshot ───────────────────────────────────────────
    snapshot_symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    snapshot_time    = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    volume_rows      = fetch_volume_snapshot(exchange, snapshot_symbols)

    vol_table = Table(
        title=f"Live Market Volume  —  {snapshot_time}",
        box=box.ROUNDED,
        header_style="bold cyan",
        title_style="bold white on dark_blue",
        padding=(0, 1),
    )
    vol_table.add_column("Symbol",           style="bold white", min_width=16)
    vol_table.add_column("Last Price",       justify="right",    min_width=14)
    vol_table.add_column("24h Change",       justify="right",    min_width=11)
    vol_table.add_column("24h Vol (Base)",   justify="right",    min_width=18)
    vol_table.add_column("24h Vol (USDT)",   justify="right",    min_width=18)
    vol_table.add_column("1h Vol (USDT)",    justify="right",    min_width=18, style="bold magenta")

    for row in volume_rows:
        chg_color = pnl_color(row["change_pct"])
        vol_table.add_row(
            row["symbol"],
            f"{row['last']:,.2f}",
            Text(f"{row['change_pct']:+.2f}%", style=f"bold {chg_color}"),
            f"{row['base_volume']:,.4f}",
            f"{row['quote_volume']:,.2f}",
            f"{row['vol_1h_usd']:,.2f}",
        )

    console.print()
    console.print(vol_table)

    # ── Open positions ─────────────────────────────────────────────────────────
    positions      = exchange.fetch_positions()
    open_positions = [p for p in positions if float(p["contracts"] or 0) != 0]

    pos_table = Table(
        title="Open Positions",
        box=box.ROUNDED,
        header_style="bold cyan",
        show_lines=False,
        title_style="bold white on dark_blue",
        padding=(0, 1),
    )
    pos_table.add_column("Symbol",         style="bold white",  min_width=16)
    pos_table.add_column("Side",           justify="center",    min_width=6)
    pos_table.add_column("Leverage",       justify="right",     min_width=9)
    pos_table.add_column("Size",           justify="right",     min_width=10)
    pos_table.add_column("Entry Price",    justify="right",     min_width=12)
    pos_table.add_column("Mark Price",     justify="right",     min_width=12)
    pos_table.add_column("Unrealized P/L", justify="right",     min_width=16)
    pos_table.add_column("ROI %",          justify="right",     min_width=8)

    if open_positions:
        for p in open_positions:
            raw_sym     = str(p['symbol'] or '').replace('/', '').replace(':USDT', '')
            leverage    = leverage_map.get(raw_sym) or int(p.get('leverage') or 1)
            notional    = float(p['entryPrice'] or 0) * abs(float(p['contracts'] or 0))
            init_margin = notional / leverage
            upnl        = float(p['unrealizedPnl'] or 0)
            roi         = (upnl / init_margin * 100) if init_margin else 0
            color       = pnl_color(upnl)
            side        = str(p['side'])
            side_text   = Text(side.upper(), style="green" if side == "long" else "red")

            pos_table.add_row(
                str(p['symbol']),
                side_text,
                f"{leverage}x",
                f"{float(p['contracts'] or 0):.4f}",
                f"{float(p['entryPrice'] or 0):,.2f}",
                f"{float(p['markPrice'] or 0):,.2f}",
                Text(f"{upnl:+,.2f} USDT", style=f"bold {color}"),
                Text(f"{roi:+.2f}%", style=f"bold {color}"),
            )
    else:
        pos_table.add_row("[dim]No open positions.[/dim]", "", "", "", "", "", "", "")

    console.print()
    console.print(pos_table)

    # ── Trade history (last 3 months) ─────────────────────────────────────────
    symbols_to_check = list({p['symbol'] for p in open_positions} | {'BTC/USDT:USDT'})

    all_trades = []
    for symbol in symbols_to_check:
        trades = fetch_trades_3months(exchange, symbol)
        all_trades.extend(trades)

    all_trades.sort(key=lambda t: t['timestamp'])

    trade_table = Table(
        title=f"Trade History — Last 3 Months  ({len(all_trades)} trades)",
        box=box.ROUNDED,
        header_style="bold cyan",
        show_lines=False,
        title_style="bold white on dark_blue",
        padding=(0, 1),
    )
    trade_table.add_column("Date (UTC)",    style="dim",         min_width=20)
    trade_table.add_column("Symbol",        style="bold white",  min_width=16)
    trade_table.add_column("Side",          justify="center",    min_width=5)
    trade_table.add_column("Amount",        justify="right",     min_width=10)
    trade_table.add_column("Price",         justify="right",     min_width=12)
    trade_table.add_column("Cost (USDT)",   justify="right",     min_width=14)
    trade_table.add_column("Fee (USDT)",    justify="right",     min_width=12)

    total_buy_cost  = 0.0
    total_sell_cost = 0.0
    total_fees      = 0.0

    if all_trades:
        for t in all_trades:
            date     = datetime.fromtimestamp(t['timestamp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            fee_cost = t['fee']['cost'] if t.get('fee') else 0
            total_fees += fee_cost
            side     = t['side']
            if side == 'buy':
                total_buy_cost += t['cost']
            else:
                total_sell_cost += t['cost']
            side_text = Text(side.upper(), style="green" if side == "buy" else "red")
            trade_table.add_row(
                date,
                t['symbol'],
                side_text,
                f"{t['amount']:.4f}",
                f"{t['price']:,.2f}",
                f"{t['cost']:,.2f}",
                f"{fee_cost:.4f}",
            )
    else:
        trade_table.add_row("[dim]No trades found.[/dim]", "", "", "", "", "", "")

    console.print()
    console.print(trade_table)

    # ── Summary panel ─────────────────────────────────────────────────────────
    if all_trades:
        summary_lines = Text()
        summary_lines.append(f"  Total Buy Volume:   ", style="bold white")
        summary_lines.append(f"{total_buy_cost:>14,.2f} USDT\n", style="bold green")
        summary_lines.append(f"  Total Sell Volume:  ", style="bold white")
        summary_lines.append(f"{total_sell_cost:>14,.2f} USDT\n", style="bold red")
        summary_lines.append(f"  Total Fees Paid:    ", style="bold white")
        summary_lines.append(f"{total_fees:>14,.4f} USDT\n", style="bold yellow")
        console.print(Panel(summary_lines, title="[bold cyan]Trade Summary[/bold cyan]", box=box.ROUNDED, expand=False))

    # ── Per-position ROI panels ────────────────────────────────────────────────
    for p in open_positions:
        raw_sym     = str(p['symbol'] or '').replace('/', '').replace(':USDT', '')
        leverage    = leverage_map.get(raw_sym) or int(p.get('leverage') or 1)
        notional    = float(p['entryPrice'] or 0) * abs(float(p['contracts'] or 0))
        init_margin = notional / leverage
        upnl        = float(p['unrealizedPnl'] or 0)
        roi         = (upnl / init_margin * 100) if init_margin else 0
        color       = pnl_color(upnl)

        details = Text()
        details.append(f"  Leverage:         ", style="bold white")
        details.append(f"{leverage}x\n", style="bold yellow")
        details.append(f"  Notional Value:   ", style="bold white")
        details.append(f"{notional:>14,.2f} USDT\n")
        details.append(f"  Initial Margin:   ", style="bold white")
        details.append(f"{init_margin:>14,.2f} USDT\n")
        details.append(f"  Unrealized P/L:   ", style="bold white")
        details.append(f"{upnl:>+14,.2f} USDT\n", style=f"bold {color}")
        details.append(f"  ROI:              ", style="bold white")
        details.append(f"{roi:>+13.2f}%", style=f"bold {color}")

        console.print(Panel(
            details,
            title=f"[bold cyan]Position Summary — {p['symbol']}[/bold cyan]",
            box=box.ROUNDED,
            expand=False,
        ))

    console.print()


if __name__ == "__main__":
    main()
