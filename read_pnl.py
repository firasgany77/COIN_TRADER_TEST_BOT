"""
Live Binance Futures dashboard — auto-refreshes every 3 seconds.
Displays open positions, live market volume, and 3-month trade history.
API round-trip latency for each Binance request is shown in the header.

Credentials:
  EXCHANGE_API_KEY    — Binance API key
  EXCHANGE_API_SECRET — Binance API secret
"""

import os
import time
import ccxt
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich import box

load_dotenv()

WINDOW_DAYS           = 7
REFRESH_SECS          = 3   # fast-data refresh interval
TRADE_REFRESH_SECS    = 60  # trade history re-fetch interval

console = Console()


# ── Helpers ────────────────────────────────────────────────────────────────────

def pnl_color(value: float) -> str:
    return "green" if value >= 0 else "red"


def fetch_leverage_map(exchange) -> dict:
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


def fetch_trades_3months(exchange, symbol: str) -> list:
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
                symbol, since=since, params={"endTime": end_time}
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


def fetch_1h_volume(exchange, symbol: str) -> tuple[float, float]:
    """Base and quote volume of the current open 1H candle."""
    try:
        binance_symbol = symbol.replace('/', '').replace(':USDT', '')
        klines = exchange.fapiPublicGetKlines({
            'symbol': binance_symbol, 'interval': '1h', 'limit': 1,
        })
        return float(klines[0][5]), float(klines[0][7])
    except Exception:
        return 0.0, 0.0


def fetch_volume_snapshot(exchange, symbols: list[str]) -> list[dict]:
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
            })
        except Exception:
            pass
    return rows


# ── Renderable builders ────────────────────────────────────────────────────────

def build_header(ts: str, latencies: dict, next_refresh_in: float) -> Panel:
    t = Text()
    t.append("  Updated: ", style="bold white")
    t.append(f"{ts}   ", style="cyan")

    t.append("Latency — ", style="bold white")
    for label, ms in latencies.items():
        color = "green" if ms < 200 else "yellow" if ms < 500 else "red"
        t.append(f"{label}: ", style="bold white")
        t.append(f"{ms} ms   ", style=f"bold {color}")

    t.append("Next refresh in: ", style="bold white")
    t.append(f"{next_refresh_in:.1f}s", style="bold cyan")

    return Panel(t, box=box.ROUNDED, style="on grey11", padding=(0, 1))


def build_vol_table(rows: list[dict]) -> Table:
    tbl = Table(
        title="Live Market Volume",
        box=box.ROUNDED, header_style="bold cyan",
        title_style="bold white on dark_blue", padding=(0, 1),
    )
    tbl.add_column("Symbol",          style="bold white", min_width=16)
    tbl.add_column("Last Price",      justify="right",    min_width=14)
    tbl.add_column("24h Change",      justify="right",    min_width=11)
    tbl.add_column("24h Vol (Base)",  justify="right",    min_width=18)
    tbl.add_column("24h Vol (USDT)",  justify="right",    min_width=18)
    tbl.add_column("1h Vol (USDT)",   justify="right",    min_width=18, style="bold magenta")
    tbl.add_column("1h Vol (BTC)",    justify="right",    min_width=16, style="bold magenta")

    for row in rows:
        chg_color = pnl_color(row["change_pct"])
        tbl.add_row(
            row["symbol"],
            f"{row['last']:,.2f}",
            Text(f"{row['change_pct']:+.2f}%", style=f"bold {chg_color}"),
            f"{row['base_volume']:,.4f}",
            f"{row['quote_volume']:,.2f}",
            f"{row['vol_1h_usd']:,.2f}",
            f"{row['vol_1h_base']:,.4f}",
        )
    return tbl


def build_pos_table(open_positions: list, leverage_map: dict) -> Table:
    tbl = Table(
        title="Open Positions",
        box=box.ROUNDED, header_style="bold cyan", show_lines=False,
        title_style="bold white on dark_blue", padding=(0, 1),
    )
    tbl.add_column("Symbol",         style="bold white", min_width=16)
    tbl.add_column("Side",           justify="center",   min_width=6)
    tbl.add_column("Leverage",       justify="right",    min_width=9)
    tbl.add_column("Size",           justify="right",    min_width=10)
    tbl.add_column("Entry Price",    justify="right",    min_width=12)
    tbl.add_column("Mark Price",     justify="right",    min_width=12)
    tbl.add_column("Unrealized P/L", justify="right",    min_width=16)
    tbl.add_column("ROI %",          justify="right",    min_width=8)

    if open_positions:
        for p in open_positions:
            raw_sym     = str(p['symbol'] or '').replace('/', '').replace(':USDT', '')
            leverage    = leverage_map.get(raw_sym) or int(p.get('leverage') or 1)
            notional    = float(p['entryPrice'] or 0) * abs(float(p['contracts'] or 0))
            init_margin = notional / leverage if leverage else 0
            upnl        = float(p['unrealizedPnl'] or 0)
            roi         = (upnl / init_margin * 100) if init_margin else 0
            color       = pnl_color(upnl)
            side        = str(p['side'])
            tbl.add_row(
                str(p['symbol']),
                Text(side.upper(), style="green" if side == "long" else "red"),
                f"{leverage}x",
                f"{float(p['contracts'] or 0):.4f}",
                f"{float(p['entryPrice'] or 0):,.2f}",
                f"{float(p['markPrice'] or 0):,.2f}",
                Text(f"{upnl:+,.2f} USDT", style=f"bold {color}"),
                Text(f"{roi:+.2f}%",        style=f"bold {color}"),
            )
    else:
        tbl.add_row("[dim]No open positions.[/dim]", "", "", "", "", "", "", "")
    return tbl


def build_trade_table(all_trades: list, last_fetched: str) -> tuple[Table, float, float, float]:
    tbl = Table(
        title=f"Trade History — Last 3 Months  ({len(all_trades)} trades)  [dim]cached at {last_fetched}[/dim]",
        box=box.ROUNDED, header_style="bold cyan", show_lines=False,
        title_style="bold white on dark_blue", padding=(0, 1),
    )
    tbl.add_column("Date (UTC)",   style="dim",        min_width=20)
    tbl.add_column("Symbol",       style="bold white", min_width=16)
    tbl.add_column("Side",         justify="center",   min_width=5)
    tbl.add_column("Amount",       justify="right",    min_width=10)
    tbl.add_column("Price",        justify="right",    min_width=12)
    tbl.add_column("Cost (USDT)",  justify="right",    min_width=14)
    tbl.add_column("Fee (USDT)",   justify="right",    min_width=12)

    total_buy = total_sell = total_fees = 0.0
    for t in all_trades:
        date     = datetime.fromtimestamp(t['timestamp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        fee_cost = t['fee']['cost'] if t.get('fee') else 0
        total_fees += fee_cost
        side     = t['side']
        if side == 'buy':
            total_buy += t['cost']
        else:
            total_sell += t['cost']
        tbl.add_row(
            date, t['symbol'],
            Text(side.upper(), style="green" if side == "buy" else "red"),
            f"{t['amount']:.4f}",
            f"{t['price']:,.2f}",
            f"{t['cost']:,.2f}",
            f"{fee_cost:.4f}",
        )
    return tbl, total_buy, total_sell, total_fees


def build_summary_panel(total_buy: float, total_sell: float, total_fees: float) -> Panel:
    t = Text()
    t.append("  Total Buy Volume:   ", style="bold white")
    t.append(f"{total_buy:>14,.2f} USDT\n", style="bold green")
    t.append("  Total Sell Volume:  ", style="bold white")
    t.append(f"{total_sell:>14,.2f} USDT\n", style="bold red")
    t.append("  Total Fees Paid:    ", style="bold white")
    t.append(f"{total_fees:>14,.4f} USDT\n", style="bold yellow")
    return Panel(t, title="[bold cyan]Trade Summary[/bold cyan]", box=box.ROUNDED, expand=False)


def build_roi_panels(open_positions: list, leverage_map: dict) -> list:
    panels = []
    for p in open_positions:
        raw_sym     = str(p['symbol'] or '').replace('/', '').replace(':USDT', '')
        leverage    = leverage_map.get(raw_sym) or int(p.get('leverage') or 1)
        notional    = float(p['entryPrice'] or 0) * abs(float(p['contracts'] or 0))
        init_margin = notional / leverage if leverage else 0
        upnl        = float(p['unrealizedPnl'] or 0)
        roi         = (upnl / init_margin * 100) if init_margin else 0
        color       = pnl_color(upnl)
        t = Text()
        t.append("  Leverage:         ", style="bold white"); t.append(f"{leverage}x\n",               style="bold yellow")
        t.append("  Notional Value:   ", style="bold white"); t.append(f"{notional:>14,.2f} USDT\n")
        t.append("  Initial Margin:   ", style="bold white"); t.append(f"{init_margin:>14,.2f} USDT\n")
        t.append("  Unrealized P/L:   ", style="bold white"); t.append(f"{upnl:>+14,.2f} USDT\n",      style=f"bold {color}")
        t.append("  ROI:              ", style="bold white"); t.append(f"{roi:>+13.2f}%",               style=f"bold {color}")
        panels.append(Panel(t, title=f"[bold cyan]Position Summary — {p['symbol']}[/bold cyan]",
                            box=box.ROUNDED, expand=False))
    return panels


# ── Main loop ──────────────────────────────────────────────────────────────────

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

    snapshot_symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT']

    # Cached state
    leverage_map        = {}
    all_trades          = []
    trade_last_fetched  = "never"
    last_trade_time     = 0.0
    total_buy = total_sell = total_fees = 0.0

    with Live(console=console, screen=True, refresh_per_second=2) as live:
        while True:
            loop_start  = time.perf_counter()
            latencies   = {}
            now_ts      = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

            # ── Leverage map (refresh every cycle, lightweight) ────────────────
            t0 = time.perf_counter()
            leverage_map = fetch_leverage_map(exchange)
            latencies["leverage"] = round((time.perf_counter() - t0) * 1000)

            # ── Positions ─────────────────────────────────────────────────────
            t0 = time.perf_counter()
            positions      = exchange.fetch_positions()
            latencies["positions"] = round((time.perf_counter() - t0) * 1000)
            open_positions = [p for p in positions if float(p["contracts"] or 0) != 0]

            # ── Volume snapshot ────────────────────────────────────────────────
            t0 = time.perf_counter()
            volume_rows = fetch_volume_snapshot(exchange, snapshot_symbols)
            latencies["volume"] = round((time.perf_counter() - t0) * 1000)

            # ── Trade history (cached, re-fetched every TRADE_REFRESH_SECS) ───
            if time.perf_counter() - last_trade_time > TRADE_REFRESH_SECS:
                trade_symbols = list({str(p['symbol']) for p in open_positions if p['symbol']} | {'BTC/USDT:USDT'})
                t0 = time.perf_counter()
                all_trades = []
                for sym in trade_symbols:
                    all_trades.extend(fetch_trades_3months(exchange, sym))
                all_trades.sort(key=lambda t: t['timestamp'])
                latencies["trades"] = round((time.perf_counter() - t0) * 1000)
                last_trade_time    = time.perf_counter()
                trade_last_fetched = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
                total_buy = total_sell = total_fees = 0.0
                for t in all_trades:
                    fee = t['fee']['cost'] if t.get('fee') else 0
                    total_fees += fee
                    if t['side'] == 'buy':
                        total_buy += t['cost']
                    else:
                        total_sell += t['cost']

            elapsed    = time.perf_counter() - loop_start
            latencies["total"] = round(elapsed * 1000)
            next_in    = max(0.0, REFRESH_SECS - elapsed)

            # ── Build display ──────────────────────────────────────────────────
            trade_tbl, _, _, _ = build_trade_table(all_trades, trade_last_fetched)

            renderables = [
                build_header(now_ts, latencies, next_in),
                "",
                build_vol_table(volume_rows),
                "",
                build_pos_table(open_positions, leverage_map),
                "",
                trade_tbl,
            ]
            if all_trades:
                renderables += ["", build_summary_panel(total_buy, total_sell, total_fees)]
            renderables += [""] + build_roi_panels(open_positions, leverage_map)

            live.update(Group(*renderables))

            time.sleep(max(0.0, REFRESH_SECS - (time.perf_counter() - loop_start)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Dashboard stopped.[/bold yellow]")
