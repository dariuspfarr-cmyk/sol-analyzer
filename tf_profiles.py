"""
TF Profiles — Timeframe-spezifische Präzisions-Parameter für alle Bots.

Problem: Fixe Parameter (Swing-Fenster 5, EQH-Toleranz 0.2%, SL-Puffer 0.3%,
SL/TP-Clamps 3%) passen nicht gleichzeitig für 15m-Charts (ATR ~0.3%) und
1d-Charts (ATR ~4%). Auf 15m sind 3% SL-Distanz absurd weit, auf 1d viel
zu eng — beides kostet Präzision.

Lösung: Pro Timeframe kalibrierte Basis-Parameter, zusätzlich ATR-adaptiv
skaliert. Genutzt von signal_engine, sol_analysis_bot und paper_trader.
"""

from __future__ import annotations

# ── Basis-Profile pro Timeframe ───────────────────────────────────────────────
# swing_window:  Pivot-Fenster (kleiner = empfindlicher; LTF braucht weniger,
#                HTF mehr Kerzen für signifikante Swings)
# eqh_tol:       Equal-Highs/Lows-Toleranz (relativ) — Basis, ATR-adaptiv erhöht
# sl_buffer_pct: SL-Puffer unter/über Sweep/OB-Level — Basis, ATR überschreibt
# min_sl_pct:    minimale SL-Distanz vom Entry (Clamp) — vorher fix 3% überall
# max_tp_pct:    minimale TP-Distanz (Clamp-Gegenstück)
# max_risk_pct:  MAXIMALE SL-Distanz vom Entry — Setups mit weiterem strukturellem
#                Stop sind nicht handelbar (Preis erreicht SL/TP nie im Tracking-
#                Fenster → Signal läuft ab statt zu schließen). Verhindert z. B.
#                1d-EQH-Signale mit 24% Stop, die ewig offen bleiben.
# min_rr:        Mindest-RR für gültiges Setup auf diesem TF (LTF = mehr Rauschen
#                → höhere Anforderung)
# choch_win:     Fenster für CHoCH-Trigger-Erkennung
# vol_mult:      Volumen-Spike-Multiplikator (LTF rauschiger → höher)
# bos_recency:   BOS-Trigger nur wenn Bruch innerhalb der letzten N Kerzen
# signal_max_age_h: max. Signal-Alter für Paper-Trade-Einstieg (LTF-Signale
#                veralten schnell; 1d-Signale bleiben lange gültig)
# max_hold_hours: Zwangs-Exit nach N Stunden ohne SL/TP (verhindert
#                festsitzende Positionen, die Slots und Lernen blockieren)
PROFILES: dict[str, dict] = {
    "1m":  {"swing_window": 3, "eqh_tol": 0.0010, "sl_buffer_pct": 0.0015,
            "min_sl_pct": 0.004, "min_tp_pct": 0.006, "max_risk_pct": 0.025,
            "min_rr": 2.5,
            "choch_win": 14, "vol_mult": 2.8, "bos_recency": 3,
            "signal_max_age_h": 0.5, "max_hold_hours": 4},
    "5m":  {"swing_window": 4, "eqh_tol": 0.0012, "sl_buffer_pct": 0.0020,
            "min_sl_pct": 0.005, "min_tp_pct": 0.008, "max_risk_pct": 0.030,
            "min_rr": 2.3,
            "choch_win": 12, "vol_mult": 2.6, "bos_recency": 3,
            "signal_max_age_h": 1.0, "max_hold_hours": 8},
    "15m": {"swing_window": 4, "eqh_tol": 0.0015, "sl_buffer_pct": 0.0025,
            "min_sl_pct": 0.008, "min_tp_pct": 0.012, "max_risk_pct": 0.040,
            "min_rr": 2.2,
            "choch_win": 12, "vol_mult": 2.4, "bos_recency": 3,
            "signal_max_age_h": 2.0, "max_hold_hours": 12},
    "30m": {"swing_window": 5, "eqh_tol": 0.0018, "sl_buffer_pct": 0.0030,
            "min_sl_pct": 0.010, "min_tp_pct": 0.016, "max_risk_pct": 0.050,
            "min_rr": 2.1,
            "choch_win": 11, "vol_mult": 2.2, "bos_recency": 3,
            "signal_max_age_h": 4.0, "max_hold_hours": 24},
    "1h":  {"swing_window": 5, "eqh_tol": 0.0020, "sl_buffer_pct": 0.0035,
            "min_sl_pct": 0.013, "min_tp_pct": 0.020, "max_risk_pct": 0.060,
            "min_rr": 2.0,
            "choch_win": 10, "vol_mult": 2.1, "bos_recency": 3,
            "signal_max_age_h": 6.0, "max_hold_hours": 48},
    "4h":  {"swing_window": 5, "eqh_tol": 0.0025, "sl_buffer_pct": 0.0045,
            "min_sl_pct": 0.030, "min_tp_pct": 0.030, "max_risk_pct": 0.090,
            "min_rr": 2.0,
            "choch_win": 10, "vol_mult": 2.0, "bos_recency": 2,
            "signal_max_age_h": 8.0, "max_hold_hours": 168},
    "1d":  {"swing_window": 6, "eqh_tol": 0.0040, "sl_buffer_pct": 0.0080,
            "min_sl_pct": 0.050, "min_tp_pct": 0.050, "max_risk_pct": 0.120,
            "min_rr": 1.8,
            "choch_win": 8,  "vol_mult": 1.8, "bos_recency": 2,
            "signal_max_age_h": 48.0, "max_hold_hours": 720},
    "1w":  {"swing_window": 6, "eqh_tol": 0.0060, "sl_buffer_pct": 0.0120,
            "min_sl_pct": 0.080, "min_tp_pct": 0.080, "max_risk_pct": 0.200,
            "min_rr": 1.6,
            "choch_win": 8,  "vol_mult": 1.6, "bos_recency": 2,
            "signal_max_age_h": 168.0, "max_hold_hours": 2160},
}

_DEFAULT_TF = "4h"


def get(timeframe: str) -> dict:
    """Profil für einen Timeframe — Fallback auf 4h bei unbekanntem TF."""
    return dict(PROFILES.get(timeframe, PROFILES[_DEFAULT_TF]))


# ── ATR-adaptive Ableitungen ──────────────────────────────────────────────────
def eqh_tolerance(timeframe: str, atr_pct: float = 0.0) -> float:
    """
    Equal-Highs/Lows-Toleranz: Basis-Toleranz des TF, bei hoher Volatilität
    proportional zur ATR erweitert (Cluster streuen breiter bei hoher Vola).
    atr_pct: ATR in Prozent des Preises (z. B. 2.5 für 2.5%).
    """
    base = get(timeframe)["eqh_tol"]
    if atr_pct > 0:
        # 15% der ATR als Toleranz, aber nie unter Basis und max 3× Basis
        adaptive = (atr_pct / 100.0) * 0.15
        return max(base, min(base * 3.0, adaptive))
    return base


def sl_buffer(timeframe: str, price: float, atr: float = 0.0) -> float:
    """
    SL-Puffer in ABSOLUTEN Preiseinheiten unter/über dem Schutzlevel.
    ATR-basiert (0.4× ATR) wenn verfügbar, sonst TF-Basis-Prozent.
    """
    base_abs = price * get(timeframe)["sl_buffer_pct"]
    if atr > 0:
        return max(base_abs, atr * 0.4)
    return base_abs


def sl_tp_clamps(timeframe: str, price: float, atr: float = 0.0) -> tuple[float, float]:
    """
    (min_sl_dist, min_tp_dist) in absoluten Preiseinheiten.
    Ersetzt die alten fixen 3%-Clamps: TF-kalibriert und ATR-bewusst
    (SL nie enger als 1.2× ATR — sonst Stop-Hunt-Opfer).
    """
    p = get(timeframe)
    min_sl = price * p["min_sl_pct"]
    min_tp = price * p["min_tp_pct"]
    if atr > 0:
        min_sl = max(min_sl, atr * 1.2)
        min_tp = max(min_tp, atr * 1.8)
    return min_sl, min_tp


def min_rr(timeframe: str) -> float:
    """Mindest-RR für ein gültiges Setup auf diesem Timeframe."""
    return get(timeframe)["min_rr"]


def max_risk_pct(timeframe: str) -> float:
    """
    Maximale handelbare SL-Distanz (Anteil vom Entry) für diesen Timeframe.
    Setups mit weiterem strukturellem Stop sind nicht handelbar — der Preis
    erreicht SL/TP nie im Tracking-Fenster und das Signal läuft nur ab.
    Unbekannte TFs nutzen das 4h-Profil (get-Fallback); 0.12 nur falls der
    Schlüssel im Profil fehlt.
    """
    return get(timeframe).get("max_risk_pct", 0.12)
