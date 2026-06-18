"""
MTF Analyzer — Multi-Timeframe-Scan für präzisere Signale.

Analysiert ALLE Timeframes (15m, 1h, 4h, 1d) in einem Durchgang:
  • pro TF: OHLCV, SMC-Zonen, Trend (Swing-Struktur) und Trigger
  • Cross-TF-Alignment: stimmen die HÖHEREN Timeframes mit einem
    Signal überein? Aligned = präziser, Contra = Fehlsignal-Risiko.

Das Alignment wird:
  • im Algo-Score angewendet (algo_signal_engine liest den Trend-Cache)
  • pro Signal in signals.db gespeichert (Spalte mtf_alignment)
  • von backtest_learner als eigene Lerndimension ausgewertet
  • vom paper_trader im Composite-Score genutzt

Kostenneutral: nur Binance-API (kostenlos), keine zusätzlichen KI-Calls.
"""

from __future__ import annotations
import time
from typing import Optional

import pandas as pd
import requests

BINANCE_BASE = "https://api.binance.com/api/v3"

# Scan-Reihenfolge: HTF zuerst — deren Trends braucht das Alignment der LTFs
TIMEFRAMES = ["1d", "4h", "1h", "15m"]

# Höhere Timeframes je Signal-TF (für Alignment-Berechnung)
HIGHER_TFS = {
    "15m": ["1h", "4h", "1d"],
    "1h":  ["4h", "1d"],
    "4h":  ["1d"],
    "1d":  [],
}

# Kerzenanzahl pro TF (LTF braucht weniger Historie für relevante Struktur)
CANDLE_COUNT = {"15m": 400, "1h": 400, "4h": 500, "1d": 365}

# ── Trend-Cache (vom letzten Scan; von algo_signal_engine gelesen) ────────────
_trend_cache: dict = {"trends": {}, "ts": 0.0}
_TREND_TTL = 1800.0   # 30 min — ein Bot-Lauf bleibt deutlich darunter


def fetch_ohlcv(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    r = requests.get(f"{BINANCE_BASE}/klines",
                     params={"symbol": symbol, "interval": interval, "limit": limit},
                     timeout=15)
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "tbb", "tbq", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    return df[["time", "open", "high", "low", "close", "volume"]].copy()


def _trend_of(df: pd.DataFrame, timeframe: str) -> str:
    """Trend aus Swing-Struktur (TF-kalibriertes Pivot-Fenster)."""
    import signal_engine
    import tf_profiles
    swings = signal_engine._find_swings(df, window=tf_profiles.get(timeframe)["swing_window"])
    return signal_engine._trend_from_swings(swings)


# ── Haupt-Scan ────────────────────────────────────────────────────────────────
def scan(symbol: str = "SOLUSDT") -> dict:
    """
    Scannt alle Timeframes. Gibt pro TF zurück:
      {tf: {"df", "zones", "trend", "triggered", "trigger_reason"}}
    Schreibt zusätzlich den Trend-Cache für algo_signal_engine.
    """
    # Späte Imports: sol_analysis_bot importiert dieses Modul am Dateianfang
    from sol_analysis_bot import calc_smc_zones, should_run_analysis

    results: dict = {}
    trends:  dict = {}

    for tf in TIMEFRAMES:
        try:
            df    = fetch_ohlcv(symbol, tf, CANDLE_COUNT.get(tf, 400))
            zones = calc_smc_zones(df)
            triggered, reason = should_run_analysis(df, zones, interval=tf)

            trend = _trend_of(df, tf)
            trends[tf] = trend
            results[tf] = {
                "df":             df,
                "zones":          zones,
                "trend":          trend,
                "triggered":      triggered,
                "trigger_reason": reason,
            }
        except Exception as e:
            print(f"  [MTF] {tf:>4}: Scan-Fehler - {e}")
            continue
        # Status-Ausgabe getrennt: ein Encoding-Problem der Konsole darf
        # nicht als Scan-Fehler erscheinen
        try:
            tag = "✓" if triggered else "·"
            print(f"  [MTF] {tf:>4}: Trend={trend:<8} {tag} {reason[:60] if reason else ''}")
        except Exception:
            pass

    _trend_cache["trends"] = trends
    _trend_cache["ts"]     = time.time()
    return results


# ── Alignment-Berechnung ──────────────────────────────────────────────────────
def alignment_score(signal_tf: str, signal_bias: str,
                    trends: Optional[dict] = None) -> int:
    """
    Wie gut stimmen die HÖHEREN Timeframes mit dem Signal überein?
    +1 pro übereinstimmendem HTF, −1 pro widersprechendem, 0 bei neutral.
    Bereich: −3 … +3 (15m hat 3 HTFs, 1d hat keinen → immer 0).
    """
    if signal_bias not in ("bullish", "bearish"):
        return 0
    if trends is None:
        trends = get_cached_trends()
    score = 0
    for htf in HIGHER_TFS.get(signal_tf, []):
        t = trends.get(htf, "neutral")
        if t == signal_bias:
            score += 1
        elif t in ("bullish", "bearish"):   # explizit gegenläufig
            score -= 1
    return score


def alignment_modifier(signal_tf: str, signal_bias: str,
                       trends: Optional[dict] = None) -> float:
    """
    Score-Modifier aus dem Alignment: +7 pro bestätigendem HTF,
    −11 pro widersprechendem (Contra-HTF-Trades sind die teuersten Fehler).
    """
    if trends is None:
        trends = get_cached_trends()
    if not trends:
        return 0.0
    mod = 0.0
    for htf in HIGHER_TFS.get(signal_tf, []):
        t = trends.get(htf, "neutral")
        if t == signal_bias:
            mod += 7.0
        elif t in ("bullish", "bearish"):
            mod -= 11.0
    return mod


def get_cached_trends() -> dict:
    """Trends des letzten Scans (leer wenn abgelaufen oder noch kein Scan)."""
    if time.time() - _trend_cache["ts"] > _TREND_TTL:
        return {}
    return dict(_trend_cache["trends"])
