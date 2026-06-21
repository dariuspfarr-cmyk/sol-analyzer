"""
trend_detector — erkennt den aktuellen Trend UND TRENDWECHSEL aus dem gesamten
verfügbaren Wissen des Systems:
  • Daily- / 1H- / Weekly-Trend (paper_trader-Caches)
  • ADX-Regime (Trendstärke vs. Range)
  • Bull-Run-Phase (bull_run_detector)

Die Lern-Bots nutzen Trendwechsel, um regime-spezifisches Lernen zurückzusetzen
und sich schnell an die neue Marktphase anzupassen, statt im alten Regime
hängenzubleiben (z. B. „nur Long" aus einer Bullenphase, während der Markt schon
gedreht hat).

Zustände:  "up" (klarer Aufwärtstrend) · "down" (Abwärtstrend) · "range"
Persistiert den letzten Zustand in trend_state.json; check_change() meldet Wechsel.
"""
from __future__ import annotations
import json
import time
from pathlib import Path

STATE_FILE = Path(__file__).parent / "trend_state.json"


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def current_trend() -> dict:
    """
    Vereint alle Trend-Signale zu einem robusten Zustand + Score.
    Score: bullish=+1 / bearish=-1 / neutral=0, gewichtet (Daily & Weekly stärker).
    """
    import paper_trader as pt
    daily  = _safe(pt._get_daily_trend,  "neutral")
    h1     = _safe(pt._get_1h_trend,     "neutral")
    weekly = _safe(pt._get_weekly_trend, "neutral")
    adx    = float(_safe(lambda: pt._adx_cache.get("value", 25.0), 25.0))
    phase  = _safe(lambda: __import__("bull_run_detector").get_phase(), "unknown")

    def v(t):
        return 1 if t == "bullish" else -1 if t == "bearish" else 0
    score = v(daily) * 2 + v(h1) * 1 + v(weekly) * 2   # −5 … +5

    if adx < 18:
        state = "range"            # zu schwach für einen Trend
    elif score >= 2:
        state = "up"
    elif score <= -2:
        state = "down"
    else:
        state = "range"
    return {"state": state, "score": score, "daily": daily, "h1": h1,
            "weekly": weekly, "adx": round(adx, 1), "phase": phase}


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        pass


def check_change() -> dict:
    """
    Vergleicht den aktuellen Trend mit dem zuletzt gespeicherten Zustand.
    Gibt {changed, direction_flip, from, to, ...} zurück und persistiert den neuen
    Zustand. direction_flip=True nur bei up↔down (der lernrelevante Wechsel).
    """
    cur  = current_trend()
    prev = _load()
    prev_state = prev.get("state")
    changed = prev_state is not None and prev_state != cur["state"]
    flip = changed and {prev_state, cur["state"]} == {"up", "down"}
    _save({**cur, "ts": time.time(),
           "prev_state": prev_state, "changed_at": time.time() if changed else prev.get("changed_at")})
    return {"changed": changed, "direction_flip": flip,
            "from": prev_state, "to": cur["state"], **cur}


if __name__ == "__main__":
    info = check_change()
    print("══ TREND-DETEKTOR ══")
    print(f"  Zustand:   {info['to']}  (Score {info['score']}, ADX {info['adx']})")
    print(f"  Daily={info['daily']} · 1H={info['h1']} · Weekly={info['weekly']} "
          f"· Phase={info['phase']}")
    if info["changed"]:
        print(f"  ⚠️  TRENDWECHSEL: {info['from']} → {info['to']}"
              + ("  (Richtungs-Flip!)" if info["direction_flip"] else ""))
    else:
        print("  Kein Trendwechsel seit letztem Check.")
