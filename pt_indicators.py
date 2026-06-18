"""
pt_indicators — reine technische Indikator-Mathematik für den Paper Trader.

Bewusst zustandslos (keine Modul-Caches, keine Netzwerk-/Datei-Zugriffe), damit
diese Funktionen isoliert testbar und aus paper_trader.py herausgelöst sind.
Identisches Verhalten wie zuvor — nur an einen sauberen Ort verschoben.
"""

from __future__ import annotations
import pandas as pd


def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    """14-Period Average True Range."""
    try:
        h = df["high"].values; l = df["low"].values; c = df["close"].values
        tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
              for i in range(1, len(df))]
        return sum(tr[-period:]) / period if len(tr) >= period else 0.0
    except Exception:
        return 0.0


def calc_adx(df: pd.DataFrame, period: int = 14) -> float:
    """
    Wilder ADX — misst Trend-Stärke (0-100).
    < 20 = Seitwärtsmarkt, 20-35 = moderater Trend, > 35 = starker Trend.
    """
    try:
        h = df["high"].values; l = df["low"].values; c = df["close"].values
        n = len(df)
        if n < period * 3:
            return 25.0

        plus_dm  = [max(h[i]-h[i-1], 0) if (h[i]-h[i-1]) > (l[i-1]-l[i]) else 0 for i in range(1, n)]
        minus_dm = [max(l[i-1]-l[i], 0) if (l[i-1]-l[i]) > (h[i]-h[i-1]) else 0 for i in range(1, n)]
        tr_vals  = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, n)]

        # Wilder Smoothing: TR/+DM/-DM → initiale Summe, dann rollierend
        def wilder_sum(vals: list, p: int) -> list:
            s = [sum(vals[:p])]
            for v in vals[p:]:
                s.append(s[-1] - s[-1] / p + v)
            return s

        atr_w = wilder_sum(tr_vals, period)
        pdm_w = wilder_sum(plus_dm,  period)
        mdm_w = wilder_sum(minus_dm, period)

        pdi = [100 * pdm_w[i] / atr_w[i] if atr_w[i] > 0 else 0 for i in range(len(atr_w))]
        mdi = [100 * mdm_w[i] / atr_w[i] if atr_w[i] > 0 else 0 for i in range(len(atr_w))]
        dx  = [100 * abs(pdi[i]-mdi[i]) / (pdi[i]+mdi[i]) if (pdi[i]+mdi[i]) > 0 else 0
               for i in range(len(pdi))]

        # ADX = EMA des DX: Initialwert = Durchschnitt (nicht Summe!)
        if len(dx) < period * 2:
            return 25.0
        adx = [sum(dx[:period]) / period]
        for d in dx[period:]:
            adx.append((adx[-1] * (period - 1) + d) / period)

        return round(adx[-1], 2) if adx else 25.0
    except Exception:
        return 25.0


def ema(values, period: int):
    """Exponential Moving Average helper."""
    if len(values) < period:
        return values
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(result[-1] * (1 - k) + v * k)
    return result


def calc_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """RSI(14)."""
    try:
        closes = list(df["close"].values)
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]
        avg_g  = sum(gains[-period:])  / period
        avg_l  = sum(losses[-period:]) / period
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return round(100 - 100 / (1 + rs), 2)
    except Exception:
        return 50.0
