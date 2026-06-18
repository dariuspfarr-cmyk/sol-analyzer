"""
Terminal Dashboard — zeigt Paper-Trader-Status mit Rich.

Aufruf:  python dashboard.py
         (aktualisiert automatisch alle 5 Sekunden)
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text
    from rich.live import Live
    from rich.layout import Layout
    RICH = True
except ImportError:
    RICH = False

import paper_trader
import learning_engine

REFRESH = 5   # Sekunden

console = Console() if RICH else None


def _col(val: float, good_positive: bool = True) -> str:
    if val > 0:
        return "[green]" if good_positive else "[red]"
    if val < 0:
        return "[red]" if good_positive else "[green]"
    return "[dim]"


def _fmt_pnl(val: float) -> str:
    c = _col(val)
    return f"{c}{val:+.2f}[/]"


def build_display(status: dict) -> str:
    if not RICH:
        return _plain_display(status)

    if not status.get("active"):
        return "\n[yellow]Paper Trader nicht aktiv. Starte mit:[/]\n  [bold]python paper_trader.py[/]\n"

    bal         = status.get("balance", 10000)
    pnl         = status.get("pnl", 0)
    pnl_pct     = status.get("pnl_pct", 0)
    total       = status.get("total_trades", 0)
    wr          = status.get("win_rate", 0)
    pf          = status.get("profit_factor", 0)
    dd          = status.get("max_drawdown", 0)
    pos         = status.get("position")
    weights     = status.get("signal_weights", {})
    recent      = status.get("recent_trades", [])

    # ── Header ────────────────────────────────────────────────────
    lines = [
        f"[bold white]SOL/USDT Paper Trader[/]  "
        f"[dim]{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}[/]"
    ]

    # ── Account Stats ─────────────────────────────────────────────
    lines.append(f"\n[bold]Account[/]")
    lines.append(f"  Balance:       [bold]${bal:,.2f}[/]")
    lines.append(f"  P&L:           {_fmt_pnl(pnl)} ({_fmt_pnl(pnl_pct)}%)")
    lines.append(f"  Trades:        {total}  |  Win-Rate: [bold]{wr:.1f}%[/]  |  PF: {pf:.2f}  |  Max DD: [red]{dd:.1f}%[/]")

    # ── Offene Position ───────────────────────────────────────────
    lines.append(f"\n[bold]Offene Position[/]")
    if pos:
        d   = pos["direction"].upper()
        col = "[green]" if d == "LONG" else "[red]"
        lines.append(f"  {col}{d}[/]  Entry: ${pos['entry']:.2f}  SL: ${pos['sl']:.2f}  TP: ${pos['tp']:.2f}")
        lines.append(f"  Score: {pos['score']}  Triggers: {', '.join(pos.get('triggers', []))}")
        try:
            upnl = paper_trader.State()
            upnl.position = pos
            up = upnl.unrealized_pnl
            if up is not None:
                lines.append(f"  Unrealized P&L: {_fmt_pnl(up)}")
        except Exception:
            pass
    else:
        lines.append("  [dim]— kein offener Trade[/dim]")

    # ── Letzte 10 Trades ─────────────────────────────────────────
    lines.append(f"\n[bold]Letzte {min(10, len(recent))} Trades[/]")
    if recent:
        for t in recent[:10]:
            d    = t.get("direction","?").upper()
            res  = t.get("exit_reason","?")
            pnl2 = t.get("pnl", 0)
            p    = t.get("pnl_pct", 0)
            c    = "[green]" if pnl2 > 0 else "[red]"
            dt   = (t.get("closed_at") or "")[:16].replace("T"," ")
            lines.append(f"  {dt}  {'LONG ' if d=='LONG' else 'SHORT'} {res:<2}  {c}{pnl2:+.2f}$ ({p:+.2f}%)[/]")
    else:
        lines.append("  [dim]Noch keine geschlossenen Trades[/]")

    # ── Signal-Gewichte ───────────────────────────────────────────
    lines.append(f"\n[bold]Signal-Gewichte (live)[/]")
    for item in learning_engine.get_weight_summary():
        trend = item["trend"]
        delta = item["delta"]
        col   = "[green]" if delta > 0.05 else "[red]" if delta < -0.05 else "[dim]"
        lines.append(f"  {item['signal']:<16} {item['weight']:.3f}  {col}{trend} {delta:+.3f}[/]")

    return "\n".join(lines)


def _plain_display(status: dict) -> str:
    if not status.get("active"):
        return "Paper Trader nicht aktiv. Starte mit: python paper_trader.py"
    lines = [
        "=" * 50,
        f"  PAPER TRADER  —  {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}",
        "=" * 50,
        f"  Balance:    ${status.get('balance', 10000):,.2f}",
        f"  P&L:        {status.get('pnl', 0):+.2f}$ ({status.get('pnl_pct', 0):+.2f}%)",
        f"  Trades:     {status.get('total_trades', 0)}  WR: {status.get('win_rate', 0):.1f}%",
        f"  Max DD:     {status.get('max_drawdown', 0):.1f}%",
        "",
    ]
    pos = status.get("position")
    if pos:
        lines.append(f"  OFFEN: {pos['direction'].upper()} @ ${pos['entry']:.2f}  SL=${pos['sl']:.2f}  TP=${pos['tp']:.2f}")
    else:
        lines.append("  Kein offener Trade")
    lines.append("")
    for t in status.get("recent_trades", [])[:5]:
        pnl = t.get("pnl", 0)
        lines.append(f"  {t['direction'].upper()[:5]} {t['exit_reason']}  ${pnl:+.2f}")
    return "\n".join(lines)


def run() -> None:
    if RICH:
        with Live(console=console, refresh_per_second=0.2, screen=True) as live:
            while True:
                status = paper_trader.get_status()
                live.update(Panel(build_display(status),
                                  title="[bold white]SOL/USDT Paper Trader[/]",
                                  border_style="dim"))
                time.sleep(REFRESH)
    else:
        while True:
            status = paper_trader.get_status()
            print("\033[2J\033[H")   # clear screen
            print(build_display(status))
            time.sleep(REFRESH)


if __name__ == "__main__":
    run()
