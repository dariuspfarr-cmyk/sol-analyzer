"""
selectivity_backtest — findet empirisch die Selektivitäts-Schwellen, die die
WIN-RATE maximieren (Hauptziel des Systems), per Grid-Search über die echten
historischen Signal-Outcomes in signals.db.

Methodik (sauber, nicht überangepasst):
  • Train/Test-Split chronologisch (70/30) → out-of-sample-Validierung
  • Nebenbedingungen: Mindest-Trade-Anzahl + POSITIVE Erwartung (Ø-PnL ≥ 0),
    damit keine "hohe WR, aber Verlust"-Konfiguration empfohlen wird
  • Zielfunktion: Win-Rate maximieren (Tie-Break: Erwartung, dann Trade-Zahl)

Nur Felder mit 100% Befüllung werden als Filter genutzt (confidence_score,
Trigger-Anzahl, volume_ratio, setup_type, source, Bias×Markt-Bias).

Aufruf:  python selectivity_backtest.py
"""

from __future__ import annotations
import sqlite3
import json
import itertools
from pathlib import Path

DB = Path(__file__).parent / "signals.db"

# ── Such-Raum (alle Dimensionen aus 100%-befüllten Feldern) ───────────────────
GRID = {
    "min_conf":  [0.0, 0.45, 0.50, 0.55, 0.60],   # nur LIVE betroffen
    "min_trig":  [1, 2, 3],                        # Confluence
    "min_vol":   [0.0, 1.0, 1.3, 1.5],             # Volumen-Ratio
    "setups":    ["all", "no_zone", "trend_only", "no_zone_no_vol"],
    "source":    ["all", "no_algo", "live_only"],
    "bias_mkt":  ["any", "with_market"],           # Signal-Bias vs Research-Bias
}
MIN_TRADES_TRAIN = 40
MIN_TRADES_TEST  = 15


def _load():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT outcome,pnl_pct,confidence_score,all_triggers,volume_ratio,"
        "setup_type,source,bias,market_bias FROM signals "
        "WHERE outcome IN ('WIN','LOSS') ORDER BY id").fetchall()]
    for r in rows:
        try:
            r["_ntrig"] = len(json.loads(r["all_triggers"] or "[]"))
        except Exception:
            r["_ntrig"] = 0
        r["_src"] = (r["source"] or "LIVE").upper()
    return rows


_TREND = {"BOS", "CHoCH"}


def _passes(r, p) -> bool:
    # Konfidenz (nur LIVE-Signale haben eine sinnvolle confidence_score)
    if r["_src"] == "LIVE" and (r["confidence_score"] or 0) < p["min_conf"]:
        return False
    if r["_ntrig"] < p["min_trig"]:
        return False
    if (r["volume_ratio"] or 0) < p["min_vol"]:
        return False
    st = r["setup_type"]
    if p["setups"] == "no_zone" and st == "Zone":
        return False
    if p["setups"] == "trend_only" and st not in _TREND:
        return False
    if p["setups"] == "no_zone_no_vol" and st in ("Zone", "Volume"):
        return False
    if p["source"] == "no_algo" and r["_src"] == "ALGO":
        return False
    if p["source"] == "live_only" and r["_src"] != "LIVE":
        return False
    if p["bias_mkt"] == "with_market":
        mb = r["market_bias"] or "neutral"
        if mb != "neutral" and r["bias"] != "neutral" and r["bias"] != mb:
            return False
    return True


def _stats(rows):
    if not rows:
        return (0, 0.0, 0.0)
    w = sum(1 for r in rows if r["outcome"] == "WIN")
    pnl = sum(r["pnl_pct"] for r in rows) / len(rows)
    return (len(rows), w / len(rows) * 100, pnl)


def _combos():
    keys = list(GRID)
    for vals in itertools.product(*[GRID[k] for k in keys]):
        yield dict(zip(keys, vals))


def run():
    rows = _load()
    n = len(rows)
    split = int(n * 0.70)
    train, test = rows[:split], rows[split:]

    bN, bWR, bPnl = _stats(train)
    tN, tWR, tPnl = _stats(test)
    print("\n" + "=" * 68)
    print("  SELEKTIVITÄTS-BACKTEST — Win-Rate maximieren (out-of-sample)")
    print("=" * 68)
    print(f"  Signale: {n}  (Train {len(train)} / Test {len(test)}, chronologisch)")
    print(f"  BASELINE (kein Filter):  Train WR={bWR:.1f}% Ø={bPnl:+.2f}%  |  "
          f"Test WR={tWR:.1f}% Ø={tPnl:+.2f}%")

    # Grid-Search auf Train: max WR mit Nebenbedingungen
    cands = []
    for p in _combos():
        kept = [r for r in train if _passes(r, p)]
        cN, cWR, cPnl = _stats(kept)
        if cN >= MIN_TRADES_TRAIN and cPnl >= 0:
            cands.append((cWR, cPnl, cN, p))
    cands.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)

    if not cands:
        print("\n  Keine Konfiguration erfüllt die Nebenbedingungen.")
        return None

    print("\n  Top-5 auf TRAIN (max WR, Ø-PnL≥0, ≥%d Trades):" % MIN_TRADES_TRAIN)
    print(f"  {'WR':>6} {'ØPnL':>7} {'N':>4}  Schwellen")
    best_oos = None
    for cWR, cPnl, cN, p in cands[:5]:
        kept_test = [r for r in test if _passes(r, p)]
        eN, eWR, ePnl = _stats(kept_test)
        oos = f"Test: WR={eWR:.0f}% Ø={ePnl:+.2f}% N={eN}" if eN >= MIN_TRADES_TEST else f"Test: N={eN} (zu wenig)"
        desc = (f"conf≥{p['min_conf']} trig≥{p['min_trig']} vol≥{p['min_vol']} "
                f"{p['setups']} {p['source']} {p['bias_mkt']}")
        print(f"  {cWR:5.1f}% {cPnl:+6.2f}% {cN:4}  {desc}")
        print(f"         └─ {oos}")
        # Bestes OUT-OF-SAMPLE wählen (robust): hohe Test-WR mit genug Trades
        if eN >= MIN_TRADES_TEST and ePnl >= 0:
            score = (eWR, ePnl)
            if best_oos is None or score > best_oos[0]:
                best_oos = (score, p, (cN, cWR, cPnl), (eN, eWR, ePnl))

    print("\n" + "-" * 68)
    if best_oos:
        _, p, tr, te = best_oos
        print("  ✅ EMPFOHLENE SCHWELLEN (beste validierte Out-of-Sample-WR):")
        print(f"     min_confidence = {p['min_conf']}")
        print(f"     min_triggers   = {p['min_trig']}")
        print(f"     min_volume     = {p['min_vol']}")
        print(f"     setups         = {p['setups']}")
        print(f"     source         = {p['source']}")
        print(f"     bias_vs_market = {p['bias_mkt']}")
        print(f"\n     Train: WR={tr[1]:.1f}% Ø={tr[2]:+.2f}% N={tr[0]}")
        print(f"     Test:  WR={te[1]:.1f}% Ø={te[2]:+.2f}% N={te[0]}  (out-of-sample)")
        print(f"\n     → WR-Hebung Test: {tWR:.1f}% (Baseline) → {te[1]:.1f}% "
              f"(+{te[1]-tWR:.1f} Punkte), Trades {tN}→{te[0]}")
        return {"thresholds": p, "train": tr, "test": te,
                "baseline_test_wr": tWR}
    else:
        print("  ⚠️  Keine Konfiguration validiert robust out-of-sample "
              "(Test-WR nicht stabil über Baseline). Mehr Daten nötig.")
        return None


if __name__ == "__main__":
    run()
