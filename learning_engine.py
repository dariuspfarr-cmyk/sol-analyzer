"""
Learning Engine — passt Signal-Gewichte nach jedem Paper-Trade an.

Nach jedem geschlossenen Trade:
  • welche Signale waren beim Entry aktiv?
  • Trade gewonnen → Gewichte leicht erhöhen
  • Trade verloren → Gewichte leicht senken
  • Gewichte werden in state.json persistiert (zusammen mit Paper-Trader-State)

Nutzt backtest_learner.compute_score als Referenz-Scoring.
"""

from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Optional

STATE_FILE = Path(__file__).parent / "state.json"

# Lernrate: wie stark ein einzelner Trade die Gewichte bewegt (0-1)
LEARN_RATE   = 0.05
WEIGHT_MIN   = 0.2
WEIGHT_MAX   = 3.0

DEFAULT_WEIGHTS = {
    "bos":           1.5,
    "choch":         1.2,
    "fvg":           1.0,
    "order_block":   1.3,
    "eqh":           0.9,
    "eql":           0.9,
    "discount_zone": 0.8,
    "premium_zone":  0.8,
}


def load_weights() -> dict:
    """Lädt aktuelle Signal-Gewichte aus state.json, Fallback auf Defaults."""
    try:
        state = _load_state()
        w = state.get("signal_weights", {})
        if w:
            merged = dict(DEFAULT_WEIGHTS)
            merged.update(w)
            return merged
    except Exception:
        pass
    return dict(DEFAULT_WEIGHTS)


def update_weights(active_signals: list[str], won: bool,
                   created_at: str = "") -> dict:
    """
    Aktualisiert Gewichte basierend auf dem Trade-Ergebnis.
    active_signals: Liste der aktiven Signal-Namen beim Entry (z. B. ["BOS", "FVG", "OB"])
    won: True = Trade gewonnen, False = verloren.
    created_at: ISO-Timestamp des Signals; ältere Signale haben geringeren Lerneinfluss
                (Halbwertszeit 7 Tage → nach 7 Tagen wirkt nur noch 50% der Lernrate).
    Gibt die neuen Gewichte zurück.
    """
    from datetime import datetime, timezone

    weights   = load_weights()
    direction = 1.0 if won else -1.0

    # Zeitlicher Decay: Einfluss sinkt exponentiell mit Signalalter
    age_hours   = 0.0
    if created_at:
        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 3600)
        except Exception:
            pass
    # Halbwertszeit 7 Tage (168 h): exp(-ln2 * age / 168)
    age_factor = math.exp(-math.log(2) * age_hours / 168.0)

    # Warm-up-Dämpfung: bei wenig Gesamt-Trades die Lernrate reduzieren, damit
    # ein einzelnes Ergebnis die Gewichte nicht überproportional verzerrt
    # (Schutz gegen Overfitting auf statistisches Rauschen). Voll ab ~20 Trades.
    total_trades = 0
    try:
        total_trades = int(_load_state().get("total_trades", 0))
    except Exception:
        pass
    WARMUP_N = 20
    warmup   = max(0.25, min(1.0, total_trades / WARMUP_N)) if WARMUP_N else 1.0

    signal_map = {
        "BOS":       "bos",
        "CHOCH":     "choch",   # uppercase weil sig.upper() aufgerufen wird
        "FVG":       "fvg",
        "OB":        "order_block",
        "EQH":       "eqh",
        "EQL":       "eql",
        "DISCOUNT":  "discount_zone",
        "PREMIUM":   "premium_zone",
    }

    for sig in active_signals:
        key = signal_map.get(sig.upper())
        if key and key in weights:
            delta = LEARN_RATE * warmup * direction * weights[key] * age_factor
            weights[key] = round(
                max(WEIGHT_MIN, min(WEIGHT_MAX, weights[key] + delta)), 4
            )

    _save_weights(weights)
    return weights


def get_weight_summary() -> list[dict]:
    """Gibt sortierte Gewicht-Liste für das Dashboard zurück."""
    w = load_weights()
    defaults = DEFAULT_WEIGHTS
    result = []
    for k, v in sorted(w.items(), key=lambda x: -x[1]):
        delta = round(v - defaults.get(k, v), 4)
        result.append({"signal": k, "weight": v, "delta": delta,
                        "trend": "↑" if delta > 0.05 else "↓" if delta < -0.05 else "→"})
    return result


# ── Interne Hilfsfunktionen ───────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_weights(weights: dict) -> None:
    state = _load_state()
    state["signal_weights"] = weights
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
