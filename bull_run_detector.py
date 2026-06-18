"""
Bull Run Detector — klassifiziert die aktuelle SOL/BTC Marktphase.

Kostenlos via Binance (kein API-Key nötig). Cache: 4h TTL.

Phases:
  early_bull   → Frischer Ausbruch aus Bear/Accumulation, bestes Long-Setup
  mid_bull     → Etablierter Aufwärtstrend, Dips kaufen
  late_bull    → Überdehnt, Trailing Stops wichtig, Vorsicht
  distribution → Mögliche Trendwende, beide Richtungen möglich
  bear         → Abwärtstrend, nur selektive Longs, Shorts bevorzugen
  unknown      → Nicht genug Daten

Klassifizierung basiert auf:
  - Wochenkurs vs. 20W-EMA / 40W-EMA (Golden/Death Cross)
  - Preis vs. 200-Tage-MA
  - ATH-Distanz (innerhalb 15% = spät)
  - Wöchentlicher RSI (70+ = überdehnt)
  - Higher Highs / Higher Lows Struktur (letzte 12 Wochen)
"""

from __future__ import annotations
import json
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

CACHE_FILE   = Path(__file__).parent / "bull_run_cache.json"
CACHE_TTL    = 4 * 3600   # 4 Stunden

BINANCE_BASE = "https://api.binance.com/api/v3"
SYMBOL       = "SOLUSDT"
HEADERS      = {"User-Agent": "SOLAnalyzer/2.0"}

# ── Phase-Konstanten ──────────────────────────────────────────────────────────
EARLY_BULL   = "early_bull"
MID_BULL     = "mid_bull"
LATE_BULL    = "late_bull"
DISTRIBUTION = "distribution"
BEAR         = "bear"
UNKNOWN      = "unknown"

# ── Bull Run Playbook: vollständiges Regelwerk pro Marktphase ─────────────────
#
# Jede Phase definiert exakt wie die Bots handeln sollen:
#   long_bias        → bevorzuge Long-Signale
#   allow_shorts     → erlaube Short-Trades (False = nur Longs)
#   long_risk_mult   → Multiplikator auf Basis-Risiko für Longs
#   short_risk_mult  → Multiplikator auf Basis-Risiko für Shorts
#   tp_multiplier    → TP-Ziel wird um diesen Faktor erweitert (weiter mitlaufen)
#   trailing_stop    → aktiviert Trailing Stop für laufende Positionen
#   trail_pct        → Trailing-Abstand in % des Preises (0.04 = 4%)
#   max_hold_mult    → Haltezeit-Multiplikator (1.0 = Standard, 2.0 = doppelt lang)
#   priority_setups  → welche Signal-Typen in dieser Phase am wertvollsten sind
#   discount_boost   → Extra-Punkte wenn Entry in Discount-Zone
#   premium_penalty  → Straf-Punkte wenn Entry in Premium-Zone
#   signal_score_boost → Genereller Score-Bonus für Long-Signale
#
PHASE_PLAYBOOK: dict[str, dict] = {
    EARLY_BULL: {
        "name":        "Frühes Bullenmarkt",
        "description": "Strukturbruch aus Bear/Accumulation — bestes Setup für Longs. "
                       "BOS und CHoCH nach oben sind hochwertig. Shorts komplett meiden. "
                       "Aggressiv in Demand Zones einsteigen. TP-Ziele verdoppeln — "
                       "in early bull laufen Moves oft weit über normale Erwartungen.",
        # Bias / Richtung
        "long_bias":         True,
        "allow_shorts":      False,
        # Positions-Sizing
        "long_risk_mult":    1.5,     # 50% mehr Risiko — starke Überzeugung
        "short_risk_mult":   0.0,     # Shorts gesperrt
        # Exits
        "tp_multiplier":     2.0,     # TP-Ziele verdoppelt
        "trailing_stop":     False,   # Noch kein Trailing nötig (Trend frisch)
        "trail_pct":         0.0,
        "max_hold_mult":     2.5,     # 2.5× länger halten
        # Signal-Filterung
        "priority_setups":   ["BOS", "CHOCH", "CHoCH"],
        "discount_boost":    18.0,    # Stark: Demand-Zone-Entries im early bull = Gold
        "premium_penalty":   -15.0,  # Nicht in Premium kaufen
        "signal_score_boost": 25.0,  # Alle Longs deutlich attraktiver
    },
    MID_BULL: {
        "name":        "Etablierter Bullenmarkt",
        "description": "Starker Aufwärtstrend — Dips in FVG und Demand Zones kaufen. "
                       "Shorts nur in Ausnahmefällen (vollständig blockiert). "
                       "Trailing Stop aktiviert — Gewinne mitlaufen lassen. "
                       "TP-Ziele 60% weiter als normal. Haltezeiten verdoppeln.",
        "long_bias":         True,
        "allow_shorts":      False,
        "long_risk_mult":    1.3,
        "short_risk_mult":   0.0,
        "tp_multiplier":     1.6,
        "trailing_stop":     True,    # Trailing Stop aktiv
        "trail_pct":         0.04,    # 4% Trailing-Abstand
        "max_hold_mult":     2.0,
        "priority_setups":   ["FVG", "Zone", "CHOCH", "CHoCH"],
        "discount_boost":    14.0,
        "premium_penalty":   -12.0,
        "signal_score_boost": 18.0,
    },
    LATE_BULL: {
        "name":        "Später Bullenmarkt",
        "description": "Markt überdehnt, RSI hoch, ATH in Reichweite. "
                       "Longs weiterhin möglich aber mit engerem Trailing Stop. "
                       "Shorts jetzt erlaubt bei klaren Distribution-Zeichen. "
                       "Kein Kauf mehr in Premium. TP normal, nicht erweitern. "
                       "Trailing Stop schützt aufgebaute Gewinne.",
        "long_bias":         True,
        "allow_shorts":      True,    # Shorts jetzt erlaubt
        "long_risk_mult":    1.0,     # Normales Risiko
        "short_risk_mult":   0.5,     # Shorts mit halbem Risiko
        "tp_multiplier":     1.2,     # Leichte TP-Erweiterung
        "trailing_stop":     True,
        "trail_pct":         0.03,    # Etwas enger: 3%
        "max_hold_mult":     1.3,
        "priority_setups":   ["EQH", "Zone", "BOS"],
        "discount_boost":    8.0,
        "premium_penalty":   -18.0,  # Stark: kein Kauf in Premium
        "signal_score_boost": 6.0,
    },
    DISTRIBUTION: {
        "name":        "Distribution / Topping",
        "description": "Mögliche Trendwende. Longs und Shorts gleichberechtigt. "
                       "Engere SL, kleinere Ziele. Trailing Stop schützt Gewinne. "
                       "Kein erhöhtes Risiko. EQH-Signale (Equal Highs) besonders "
                       "wertvoll als Hinweis auf Liquiditätsjagd vor Reversal.",
        "long_bias":         False,
        "allow_shorts":      True,
        "long_risk_mult":    0.8,
        "short_risk_mult":   0.8,
        "tp_multiplier":     1.0,
        "trailing_stop":     True,
        "trail_pct":         0.025,   # 2.5% — eng wegen Volatilität
        "max_hold_mult":     0.8,     # Kürzer halten
        "priority_setups":   ["EQH", "BOS", "CHOCH", "CHoCH"],
        "discount_boost":    0.0,
        "premium_penalty":   -8.0,
        "signal_score_boost": 0.0,
    },
    BEAR: {
        "name":        "Bärenmarkt",
        "description": "Abwärtstrend dominiert. Shorts bevorzugen, Longs nur selektiv. "
                       "Keine erhöhte Position-Größe für Longs. Demand Zones können "
                       "brechen — nicht blind kaufen. EQH-Setups (Equal Highs) als "
                       "Short-Trigger bevorzugen. Haltezeiten kürzer.",
        "long_bias":         False,
        "allow_shorts":      True,
        "long_risk_mult":    0.6,     # Weniger Risiko für Longs im Bear
        "short_risk_mult":   1.2,     # Mehr Risiko für Shorts erlaubt
        "tp_multiplier":     1.0,
        "trailing_stop":     False,
        "trail_pct":         0.0,
        "max_hold_mult":     0.7,     # Kürzer halten
        "priority_setups":   ["EQH", "BOS"],
        "discount_boost":    -5.0,   # Demand Zones brechen im Bear
        "premium_penalty":   -5.0,
        "signal_score_boost": -12.0, # Alle Longs deutlich abgewertet
    },
    UNKNOWN: {
        "name":        "Unbekannte Phase",
        "description": "Nicht genug Daten für Phasen-Klassifizierung. "
                       "Standard-Verhalten, keine Anpassungen.",
        "long_bias":         False,
        "allow_shorts":      True,
        "long_risk_mult":    1.0,
        "short_risk_mult":   1.0,
        "tp_multiplier":     1.0,
        "trailing_stop":     False,
        "trail_pct":         0.0,
        "max_hold_mult":     1.0,
        "priority_setups":   [],
        "discount_boost":    0.0,
        "premium_penalty":   0.0,
        "signal_score_boost": 0.0,
    },
}

# Anzeige-Labels für Terminal/Caption
PHASE_LABELS: dict[str, str] = {
    EARLY_BULL:   "🐂 Frühes Bullenmarkt",
    MID_BULL:     "🚀 Bullenmarkt",
    LATE_BULL:    "⚠️  Später Bullenmarkt",
    DISTRIBUTION: "📊 Distribution",
    BEAR:         "🐻 Bärenmarkt",
    UNKNOWN:      "❓ Unbekannt",
}


# ── Binance-Daten ─────────────────────────────────────────────────────────────
def _fetch_candles(interval: str, limit: int) -> pd.DataFrame:
    r = requests.get(
        f"{BINANCE_BASE}/klines",
        params={"symbol": SYMBOL, "interval": interval, "limit": limit},
        headers=HEADERS, timeout=15,
    )
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=[
        "time","open","high","low","close","volume",
        "close_time","qav","num_trades","taker_buy_base","taker_buy_quote","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    return df


def _ema(series: pd.Series, period: int) -> float:
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])


def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean().iloc[-1]
    loss  = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
    if loss == 0:
        return 100.0
    return round(100 - (100 / (1 + gain / loss)), 1)


# ── Phasen-Erkennung ──────────────────────────────────────────────────────────
def detect() -> dict:
    """
    Erkennt die aktuelle SOL-Marktphase anhand technischer Metriken.
    Gibt {phase, confidence, score, metrics, playbook, phase_label, detected_at} zurück.
    """
    # Daten holen: 60 Wochenkerzen + 210 Tageskerzen
    df_w = _fetch_candles("1w", 60)
    df_d = _fetch_candles("1d", 210)

    close_w = df_w["close"]
    high_w  = df_w["high"]
    low_w   = df_w["low"]
    close_d = df_d["close"]

    price   = float(close_w.iloc[-1])

    # ── Metriken ──────────────────────────────────────────────────────────────
    ema20w = _ema(close_w, 20)     # 20-Wochen-EMA (~5 Monate)
    ema40w = _ema(close_w, 40)     # 40-Wochen-EMA (~10 Monate, "Jahres-EMA")
    ma200d = float(close_d.rolling(200).mean().iloc[-1])
    rsi_w  = _rsi(close_w, 14)

    ath         = float(high_w.max())
    ath_dist    = (price / ath - 1) * 100   # z. B. -30.0 = 30% unter ATH
    low_52w     = float(low_w.tail(52).min())
    above_52w_x = price / max(low_52w, 0.01)  # z. B. 2.1 = 2.1× über 52W-Low

    # Higher Highs + Higher Lows (letzte 12 Wochen → bullische Struktur)
    h12 = high_w.tail(12).values
    l12 = low_w.tail(12).values
    n_hh = sum(1 for i in range(1, len(h12)) if h12[i] > h12[i-1])
    n_hl = sum(1 for i in range(1, len(l12)) if l12[i] > l12[i-1])
    hh_hl = (n_hh + n_hl) / (2 * 11)   # 0..1

    metrics = {
        "price":              round(price, 2),
        "ema20w":             round(ema20w, 2),
        "ema40w":             round(ema40w, 2),
        "ma200d":             round(ma200d, 2),
        "rsi_weekly":         rsi_w,
        "ath":                round(ath, 2),
        "ath_dist_pct":       round(ath_dist, 1),
        "above_52w_low_x":    round(above_52w_x, 2),
        "hh_hl_score":        round(hh_hl, 2),
        "price_vs_ema20w_pct":round((price / ema20w - 1) * 100, 1),
        "price_vs_ma200d_pct":round((price / ma200d - 1) * 100, 1),
    }

    # ── Score-Berechnung: +Punkte = bullish ───────────────────────────────────
    score = 0

    # 1. Trendstruktur (HH + HL auf Wochenbasis)
    if   hh_hl >= 0.70: score += 3
    elif hh_hl >= 0.50: score += 1
    elif hh_hl <= 0.30: score -= 2

    # 2. Preis vs. EMAs (Golden/Death Cross)
    if price > ema20w:
        score += 2
        if ema20w > ema40w:   score += 2   # Golden Cross Weekly
    else:
        score -= 2
        if ema20w < ema40w:   score -= 2   # Death Cross Weekly

    # 3. Preis vs. 200-Tage-MA
    if   price > ma200d * 1.05: score += 2
    elif price > ma200d:        score += 1
    elif price < ma200d * 0.95: score -= 2
    else:                       score -= 1

    # 4. Wöchentlicher RSI
    if   rsi_w >= 70:  score += 0    # Überkauft → kein zusätzlicher Bullish-Punkt
    elif rsi_w >= 55:  score += 1
    elif rsi_w >= 40:  score += 0
    elif rsi_w <  30:  score -= 2

    # ── Phasen-Bestimmung ─────────────────────────────────────────────────────
    near_ath  = ath_dist >= -15.0   # innerhalb 15% vom ATH
    very_near = ath_dist >= -5.0    # innerhalb 5% (fast ATH)

    if score >= 7:
        # Starker Bullenmarkt
        if rsi_w >= 75 or very_near:
            phase      = LATE_BULL
            confidence = round(min(0.90, 0.60 + (max(rsi_w, 70) - 70) / 60), 2)
        else:
            phase      = MID_BULL
            confidence = round(min(0.88, 0.55 + score / 22), 2)
    elif score >= 4:
        if above_52w_x >= 1.8 and not near_ath:
            phase      = EARLY_BULL
            confidence = 0.65
        else:
            phase      = MID_BULL
            confidence = round(min(0.75, 0.50 + score / 18), 2)
    elif score >= 1:
        if rsi_w >= 65 and near_ath:
            phase      = DISTRIBUTION
            confidence = 0.55
        else:
            phase      = EARLY_BULL
            confidence = 0.50
    elif score >= -2:
        if rsi_w >= 65:
            phase      = DISTRIBUTION
            confidence = 0.60
        else:
            phase      = UNKNOWN
            confidence = 0.30
    else:
        phase      = BEAR
        confidence = round(min(0.90, 0.50 + abs(score) / 14), 2)

    return {
        "phase":       phase,
        "phase_label": PHASE_LABELS.get(phase, phase),
        "confidence":  confidence,
        "score":       score,
        "metrics":     metrics,
        "playbook":    PHASE_PLAYBOOK[phase],
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Öffentliche API ───────────────────────────────────────────────────────────
def get_cached() -> dict:
    """Gibt gecachte Phase zurück (4h TTL) oder erkennt neu."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                c = json.load(f)
            age = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(c["detected_at"])).total_seconds()
            if age < CACHE_TTL:
                return c
        except Exception:
            pass
    try:
        result = detect()
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        return result
    except Exception as e:
        return {
            "phase":       UNKNOWN,
            "phase_label": PHASE_LABELS[UNKNOWN],
            "confidence":  0.0,
            "score":       0,
            "metrics":     {},
            "playbook":    PHASE_PLAYBOOK[UNKNOWN],
            "error":       str(e),
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }


def get_phase() -> str:
    """Gibt die aktuelle Marktphase zurück (String-Konstante)."""
    return get_cached().get("phase", UNKNOWN)


def get_playbook() -> dict:
    """Gibt das vollständige Playbook der aktuellen Phase zurück."""
    c = get_cached()
    return c.get("playbook", PHASE_PLAYBOOK[UNKNOWN])


def get_phase_label() -> str:
    """Gibt den lesbaren Phase-Label zurück (mit Emoji)."""
    return get_cached().get("phase_label", PHASE_LABELS[UNKNOWN])


if __name__ == "__main__":
    print("\n  [Bull Run Detector] Analysiere Marktphase…")
    c = detect()
    m = c["metrics"]
    p = c["playbook"]
    print(f"\n  Phase:      {c['phase_label']}  (Konfidenz: {c['confidence']:.0%})")
    print(f"  Score:      {c['score']}")
    print(f"  Preis:      ${m['price']:,.2f}")
    print(f"  EMA20W:     ${m['ema20w']:,.2f}  ({m['price_vs_ema20w_pct']:+.1f}%)")
    print(f"  MA200D:     ${m['ma200d']:,.2f}  ({m['price_vs_ma200d_pct']:+.1f}%)")
    print(f"  RSI(W14):   {m['rsi_weekly']}")
    print(f"  ATH:        ${m['ath']:,.2f}  ({m['ath_dist_pct']:+.1f}% vom ATH)")
    print(f"  HH/HL:      {m['hh_hl_score']:.2f}")
    print(f"\n  Playbook:   {p['name']}")
    print(f"  Strategie:  {p['description'][:80]}…")
    print(f"\n  Longs:      risk×{p['long_risk_mult']}  TP×{p['tp_multiplier']}"
          f"  hold×{p['max_hold_mult']}  shorts={p['allow_shorts']}")
    print(f"  Trailing:   {'Aktiv' if p['trailing_stop'] else 'Inaktiv'}"
          f"  ({p['trail_pct']*100:.1f}%)")
    print(f"  Score-Boost Longs:  {p['signal_score_boost']:+.0f} Punkte")
    print(f"  Discount-Bonus:     {p['discount_boost']:+.0f} Punkte")
