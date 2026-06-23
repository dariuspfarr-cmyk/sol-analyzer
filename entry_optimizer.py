#!/usr/bin/env python3
"""
entry_optimizer.py — Der Bot lernt, WO er den Entry platziert.

Statt am Markt zu chasen, lernt der Bot aus der MAE (Max Adverse Excursion) der
GEWINNER, wie tief der Markt nach einem Signal typischerweise zurücksetzt, bevor
er Richtung TP dreht. Ein Limit-Entry auf diesem Pullback-Level wird mit hoher
Wahrscheinlichkeit gefüllt (der Markt kehrt dorthin zurück → "Order abholen") UND
verbessert den Einstiegspreis (kleinere SL-Distanz → besseres R:R).

Pro Kontext (setup_type, timeframe) wird gelernt:
  pullback_frac : Median der Gewinner-MAE / SL-Distanz — wie tief der Pullback
  fill_rate     : Anteil ALLER geschlossenen Signale, deren MAE ≥ pullback_frac
                  ist — also wie oft der Markt diese Tiefe wirklich erreicht
                  (= geschätzte Füll-Wahrscheinlichkeit des Limit-Entries)
  samples       : Anzahl Gewinner im Kontext

Anwendung (suggest_entry): nur wenn genug Daten UND hohe Füll-Quote → Entry auf
das gelernte Pullback-Level legen; sonst Trigger-Preis unverändert (Markt-Fill).
Dadurch ist das Modul im Kaltstart (wenig Daten) ein No-Op und das Verhalten
bleibt exakt wie bisher; mit wachsender Historie greift der gelernte Pullback.
"""
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

MODEL_FILE = Path(__file__).parent / "entry_optimizer.json"

MIN_SAMPLES       = 8     # Mindest-Gewinner je Kontext, sonst kein gelernter Pullback
MIN_FILL_RATE     = 0.55  # Mindest-Füllquote, sonst Markt-Entry (kein Pullback-Limit)
PULLBACK_PCTL     = 0.50  # Perzentil der Gewinner-MAE als Pullback-Tiefe (Median)
MAX_PULLBACK_FRAC = 0.60  # Sicherheits-Cap: nie tiefer als 60 % der SL-Distanz

_cache: dict | None = None
_mtime: float = 0.0


def _frac(mae_pct, risk_pct) -> float | None:
    """MAE als Bruchteil der SL-Distanz (0..1), oder None bei ungültigen Werten."""
    try:
        mae_pct = float(mae_pct); risk_pct = float(risk_pct)
    except (TypeError, ValueError):
        return None
    if risk_pct <= 0:
        return None
    return max(0.0, min(1.0, mae_pct / risk_pct))


def update() -> dict:
    """Lernt das Pullback-Modell aus geschlossenen Signalen und speichert es."""
    import signal_logger
    sigs = signal_logger.get_all_signals(include_open=False)

    by_ctx: dict[tuple, dict] = {}
    for s in sigs:
        f = _frac(s.get("mae_pct"), s.get("risk_pct"))
        if f is None:
            continue
        ctx = by_ctx.setdefault((s.get("setup_type", "?"), s.get("timeframe", "?")),
                                {"win": [], "all": []})
        ctx["all"].append(f)
        if s.get("outcome") == "WIN":
            ctx["win"].append(f)

    contexts: dict[str, dict] = {}
    for (st, tf), d in by_ctx.items():
        wins = sorted(d["win"])
        if len(wins) < MIN_SAMPLES:
            continue
        idx       = min(len(wins) - 1, int(len(wins) * PULLBACK_PCTL))
        pull      = min(MAX_PULLBACK_FRAC, wins[idx])
        reached   = sum(1 for f in d["all"] if f >= pull)
        fill_rate = reached / max(1, len(d["all"]))
        contexts[f"{st}|{tf}"] = {
            "pullback_frac":    round(pull, 3),
            "fill_rate":        round(fill_rate, 3),
            "samples":          len(wins),
            "avg_win_mae_frac": round(statistics.mean(wins), 3),
        }

    out = {
        "updated":       datetime.now(timezone.utc).isoformat(),
        "min_samples":   MIN_SAMPLES,
        "min_fill_rate": MIN_FILL_RATE,
        "contexts":      contexts,
    }
    MODEL_FILE.write_text(json.dumps(out, indent=2))
    return out


def load() -> dict:
    """Lädt das gelernte Modell (gecacht, mtime-invalidiert)."""
    global _cache, _mtime
    try:
        mt = MODEL_FILE.stat().st_mtime
    except OSError:
        return {"contexts": {}}
    if _cache is None or mt != _mtime:
        try:
            _cache = json.loads(MODEL_FILE.read_text())
            _mtime = mt
        except Exception:
            _cache = {"contexts": {}}
    return _cache


def suggest_entry(trigger: float, sl: float, direction: str,
                  setup_type: str, timeframe: str) -> dict:
    """
    Liefert den gelernten Pullback-Entry — oder den Trigger-Preis als Fallback.

    Rückgabe: {entry, pullback_frac, fill_rate, samples, source}
      source = 'learned'  → entry auf gelerntes Pullback-Level gelegt
      source = 'default'  → zu wenig Daten/zu niedrige Füllquote → Trigger unverändert
    """
    res = {"entry": round(float(trigger), 4), "pullback_frac": 0.0,
           "fill_rate": None, "samples": 0, "source": "default"}
    try:
        m = load().get("contexts", {}).get(f"{setup_type}|{timeframe}")
        if not m or m["samples"] < MIN_SAMPLES or m["fill_rate"] < MIN_FILL_RATE:
            return res
        sl_dist = abs(float(trigger) - float(sl))
        if sl_dist <= 0:
            return res
        offset = m["pullback_frac"] * sl_dist
        entry  = trigger - offset if direction == "long" else trigger + offset
        res.update(entry=round(entry, 4), pullback_frac=m["pullback_frac"],
                   fill_rate=m["fill_rate"], samples=m["samples"], source="learned")
    except Exception:
        pass
    return res


if __name__ == "__main__":
    model = update()
    print(f"[entry_optimizer] {len(model['contexts'])} Kontexte gelernt "
          f"(min_samples={MIN_SAMPLES}, min_fill_rate={MIN_FILL_RATE}):")
    for k, v in sorted(model["contexts"].items()):
        print(f"  {k:16} pullback={v['pullback_frac']:.2f}×SL  "
              f"fill={v['fill_rate']*100:.0f}%  N={v['samples']}")
