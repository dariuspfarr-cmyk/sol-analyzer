"""
performance_compare — Auswertung der Paper-Trade-Historie.

Liest trades.json und berechnet die wichtigsten Kennzahlen (Win-Rate, Profit
Factor, Ø R:R, Gebühren-Impact, Drawdown). Trennt automatisch in:
  • LEGACY    — Trades VOR den Realismus-Änderungen (ohne Gebühren-/Brutto-Feld)
  • REALISTIC — Trades MIT realistischer Ausführung (Slippage, Gebühren, Cap)

So lässt sich der Effekt der Realismus-Umstellung direkt vergleichen.

Aufruf:  python performance_compare.py
"""

from __future__ import annotations
import json
from pathlib import Path

TRADES_JSON = Path(__file__).parent / "trades.json"


def _metrics(trades: list[dict]) -> dict:
    """Berechnet Kennzahlen für eine Trade-Liste."""
    n = len(trades)
    if n == 0:
        return {"n": 0}

    pnls    = [float(t.get("pnl", 0.0)) for t in trades]
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]
    gross_w = sum(wins)
    gross_l = abs(sum(losses))

    rr_vals = [float(t["rr"]) for t in trades if t.get("rr") not in (None, 0)]
    fees    = [float(t["fees"]) for t in trades if t.get("fees") is not None]
    gross   = [float(t["gross_pnl"]) for t in trades if t.get("gross_pnl") is not None]

    # Drawdown aus balance_after-Sequenz
    bals = [float(t["balance_after"]) for t in trades if "balance_after" in t]
    max_dd = 0.0
    if bals:
        peak = bals[0]
        for b in bals:
            peak = max(peak, b)
            max_dd = max(max_dd, (peak - b) / peak * 100 if peak else 0.0)

    return {
        "n":          n,
        "win_rate":   round(len(wins) / n * 100, 1),
        "wins":       len(wins),
        "losses":     len(losses),
        "net_pnl":    round(sum(pnls), 2),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else float("inf"),
        "avg_win":    round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss":   round(sum(losses) / len(losses), 2) if losses else 0.0,
        "avg_rr":     round(sum(rr_vals) / len(rr_vals), 2) if rr_vals else None,
        # Brutto/Gebühren nur, wenn ALLE Trades der Gruppe das Feld haben
        # (sonst irreführend, weil Legacy-Trades keine Gebühren erfasst haben)
        "total_fees": round(sum(fees), 2) if len(fees) == n else None,
        "gross_pnl":  round(sum(gross), 2) if len(gross) == n else None,
        "max_dd_pct": round(max_dd, 2),
        "by_exit":    _count_by(trades, "exit_reason"),
    }


def _count_by(trades: list[dict], key: str) -> dict:
    out: dict = {}
    for t in trades:
        k = t.get(key, "?")
        out[k] = out.get(k, 0) + 1
    return out


def _fmt(m: dict, title: str) -> str:
    if m.get("n", 0) == 0:
        return f"  {title}: keine Trades\n"
    pf = m["profit_factor"]
    pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
    lines = [
        f"  ── {title} ({m['n']} Trades) ─────────────────────────",
        f"     Win-Rate:       {m['win_rate']}%  ({m['wins']}W / {m['losses']}L)",
        f"     Netto-PnL:      ${m['net_pnl']:+.2f}",
        f"     Profit Factor:  {pf_s}",
        f"     Ø Win / Loss:   ${m['avg_win']:+.2f} / ${m['avg_loss']:+.2f}",
    ]
    if m.get("avg_rr") is not None:
        lines.append(f"     Ø R:R:          {m['avg_rr']}")
    if m.get("total_fees") is not None and m.get("gross_pnl") is not None:
        g = m["gross_pnl"]
        impact = (m["total_fees"] / abs(g) * 100) if g else 0.0
        lines.append(f"     Brutto-PnL:     ${g:+.2f}")
        lines.append(f"     Gebühren:       ${m['total_fees']:.2f}  "
                     f"({impact:.1f}% des Brutto-PnL)")
    lines.append(f"     Max Drawdown:   {m['max_dd_pct']}%")
    if m.get("by_exit"):
        exits = "  ".join(f"{k}:{v}" for k, v in sorted(m["by_exit"].items()))
        lines.append(f"     Exits:          {exits}")
    return "\n".join(lines) + "\n"


def run() -> None:
    if not TRADES_JSON.exists():
        print("Keine trades.json gefunden — der Paper Trader hat noch keine Trades geschlossen.")
        return
    try:
        trades = json.loads(TRADES_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Fehler beim Lesen von trades.json: {e}")
        return
    if not trades:
        print("trades.json ist leer — noch keine geschlossenen Trades.")
        return

    # Realistic = hat das Gebühren-Feld (ab Realismus-Umstellung)
    realistic = [t for t in trades if t.get("fees") is not None]
    legacy    = [t for t in trades if t.get("fees") is None]

    print("\n" + "=" * 60)
    print("  PAPER-TRADER PERFORMANCE  —  Vorher / Nachher")
    print("=" * 60)
    print(_fmt(_metrics(trades),    "GESAMT"))
    print(_fmt(_metrics(legacy),    "LEGACY (vor Realismus-Umstellung)"))
    print(_fmt(_metrics(realistic), "REALISTIC (Slippage + Gebühren + Cap)"))

    # Fazit zum Gebühren-/Slippage-Effekt
    rm = _metrics(realistic)
    if rm.get("n", 0) > 0 and rm.get("total_fees") and rm.get("gross_pnl") is not None:
        diff = rm["gross_pnl"] - rm["net_pnl"]
        print("  ── Effekt realistischer Ausführung ──────────────────")
        print(f"     Brutto → Netto:  ${rm['gross_pnl']:+.2f} → ${rm['net_pnl']:+.2f}")
        print(f"     Reale Kosten:    ${diff:.2f} (Gebühren + Slippage), "
              f"die ein idealisierter Backtest verschwiegen hätte.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    run()
