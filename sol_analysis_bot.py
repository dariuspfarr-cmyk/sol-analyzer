#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║   SOLUSDT Daily SMC Analysis Bot  🤖📊                  ║
║   Binance API  →  Claude AI  →  Terminal                ║
╚══════════════════════════════════════════════════════════╝
"""

import os, json, requests, math, io
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
from PIL import Image, ImageDraw, ImageFont

import cost_tracker
import signal_logger
import local_filter_model
import performance_analyzer
import threshold_optimizer
import backtester
import smart_router

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SYMBOL          = os.getenv("SYMBOL", "SOLUSDT")
INTERVAL        = os.getenv("INTERVAL", "4h")   # Kerzenintervall für Chart
CANDLES         = int(os.getenv("CANDLES", "500"))  # Anzahl Kerzen

BINANCE_BASE    = "https://api.binance.com/api/v3"
HAIKU_MODEL     = "claude-haiku-4-5-20251001"
SONNET_MODEL    = "claude-sonnet-4-20250514"   # keep existing model unchanged
ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"

# ── 1. DATEN HOLEN ────────────────────────────────────────────
def fetch_ohlcv(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    url = f"{BINANCE_BASE}/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    raw = r.json()
    df = pd.DataFrame(raw, columns=[
        "time","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_buy_base",
        "taker_buy_quote","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df[["time","open","high","low","close","volume"]].copy()
    return df


def fetch_ticker(symbol: str) -> dict:
    r = requests.get(f"{BINANCE_BASE}/ticker/24hr", params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    return r.json()


# ── 2. SMC-ZONEN BERECHNEN ────────────────────────────────────
def calc_smc_zones(df: pd.DataFrame) -> dict:
    """Berechnet SMC-relevante Zonen aus OHLCV-Daten."""
    close  = df["close"].values
    high   = df["high"].values
    low    = df["low"].values
    n      = len(df)

    import config as _cfg
    _c = _cfg.load()
    pivot_lb = int(_c.get("PIVOT_LB", 5))

    # Browser-evolved pivot überschreibt config wenn vorhanden und sinnvoll
    try:
        _sp_file = Path(__file__).parent / "strategy_params.json"
        if _sp_file.exists():
            import json as _j
            _sp = _j.loads(_sp_file.read_text(encoding="utf-8"))
            _sp_pivot = int(_sp.get("pivot", 0))
            if 3 <= _sp_pivot <= 15:
                pivot_lb = _sp_pivot
    except Exception:
        pass

    # Swing Highs / Lows (Pivot-Methode, Fenster aus config)
    pivot_highs, pivot_lows = [], []
    for i in range(pivot_lb, n - pivot_lb):
        if high[i] == max(high[i-pivot_lb:i+pivot_lb+1]):
            pivot_highs.append((i, high[i]))
        if low[i] == min(low[i-pivot_lb:i+pivot_lb+1]):
            pivot_lows.append((i, low[i]))

    recent_highs = sorted(pivot_highs[-6:], key=lambda x: x[1], reverse=True)
    recent_lows  = sorted(pivot_lows[-6:],  key=lambda x: x[1])

    price_now = close[-1]

    # Höchstes und tiefstes Swing im gesamten Zeitraum
    swing_high_val = max(high)
    swing_low_val  = min(low)

    # Equilibrium (50% des gesamten Ranges)
    equilibrium = (swing_high_val + swing_low_val) / 2

    # Premium / Discount
    premium_zone_top    = swing_high_val
    premium_zone_bottom = swing_high_val - (swing_high_val - equilibrium) * 0.25
    discount_zone_top   = swing_low_val  + (equilibrium - swing_low_val) * 0.25
    discount_zone_bottom = swing_low_val

    # Letzte Struktur: BOS-Niveaus (letzter gebrochener Swing)
    last_bos = None
    for i, val in reversed(recent_highs):
        if val < price_now:
            last_bos = val
            break
    if last_bos is None and recent_highs:
        last_bos = recent_highs[-1][1]

    # Order Block Zonen (letzte 3 Demand-Zonen: Tiefs der letzten Aufwärtsimpulse)
    demand_zones = []
    for i in range(n - 2, max(n - 60, 5), -1):
        if close[i] > close[i-1] * 1.005:  # Kerze mit >0.5% Anstieg
            demand_zones.append((low[i-1], close[i-1]))
            if len(demand_zones) == 3:
                break

    # CHoCH-Niveau (letzter Tief vor dem Impuls)
    choch_level = min(low[-30:]) if len(df) >= 30 else min(low)

    # EQL (Equal Lows) – ähnliche Tiefs
    eql_candidates = [l for _, l in recent_lows if abs(l - recent_lows[0][1]) / recent_lows[0][1] < 0.01]
    eql_level = sum(eql_candidates) / len(eql_candidates) if eql_candidates else recent_lows[0][1] if recent_lows else None

    # Weak High (höchstes der letzten 20 Kerzen, das noch nicht retestet)
    weak_high = max(high[-20:])

    # ── ATR (Average True Range, 14 Perioden) ─────────────────────────────────
    tr_list = [
        max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
        for i in range(1, n)
    ]
    atr     = sum(tr_list[-14:]) / min(14, len(tr_list)) if tr_list else 0.0
    atr_pct = atr / price_now * 100 if price_now > 0 else 0.0

    # ── EMA200 Abstand ────────────────────────────────────────────────────────
    ema200 = None
    if n >= 200:
        ema200_vals = [float(close[0])]
        k = 2 / 201
        for i in range(1, n):
            ema200_vals.append(close[i] * k + ema200_vals[-1] * (1 - k))
        ema200 = ema200_vals[-1]
    ema200_dist_pct = (price_now - ema200) / ema200 * 100 if ema200 else 0.0

    return {
        "price_now":         price_now,
        "swing_high":        swing_high_val,
        "swing_low":         swing_low_val,
        "equilibrium":       equilibrium,
        "premium_top":       premium_zone_top,
        "premium_bottom":    premium_zone_bottom,
        "discount_top":      discount_zone_top,
        "discount_bottom":   discount_zone_bottom,
        "weak_high":         weak_high,
        "last_bos":          last_bos,
        "demand_zones":      demand_zones,
        "choch_level":       choch_level,
        "eql_level":         eql_level,
        "pivot_highs":       pivot_highs,
        "pivot_lows":        pivot_lows,
        "n":                 n,
        "atr":               round(atr, 4),
        "atr_pct":           round(atr_pct, 4),
        "ema200":            round(ema200, 4) if ema200 else None,
        "ema200_dist_pct":   round(ema200_dist_pct, 4),
    }


# ── 3. CHART ZEICHNEN ─────────────────────────────────────────
def draw_chart(df: pd.DataFrame, zones: dict, symbol: str,
               interval: str | None = None) -> bytes:
    """Erzeugt einen annotierten Candlestick-Chart als PNG-Bytes."""
    fig, (ax, ax_vol) = plt.subplots(
        2, 1, figsize=(13, 6.5),
        gridspec_kw={"height_ratios": [5, 1]},
        facecolor="#0d1117"
    )
    ax.set_facecolor("#0d1117")
    ax_vol.set_facecolor("#0d1117")

    n = len(df)

    # ── Kerzen ────────────────────────────────────────────────
    for i, row in enumerate(df.itertuples()):
        color = "#26a69a" if row.close >= row.open else "#ef5350"
        ax.plot([i, i], [row.low, row.high], color=color, linewidth=0.7, zorder=2)
        ax.add_patch(plt.Rectangle(
            (i - 0.35, min(row.open, row.close)),
            0.7, abs(row.close - row.open) or 0.01,
            color=color, zorder=3
        ))

    # ── Volume ────────────────────────────────────────────────
    for i, row in enumerate(df.itertuples()):
        color = "#26a69a33" if row.close >= row.open else "#ef535033"
        ax_vol.bar(i, row.volume, color=color, width=0.8)

    # ── Zonen ─────────────────────────────────────────────────
    price_range = zones["swing_high"] - zones["swing_low"]
    y_pad = price_range * 0.04

    # Labels werden rechts neben dem Chart platziert (kein Overlap mit Kerzen)
    def _rlabel(y: float, text: str, color: str, va: str = "bottom") -> None:
        ax.text(n + 0.8, y, text, color=color, fontsize=7.5, fontweight="bold",
                va=va, ha="left", zorder=6, clip_on=False,
                bbox=dict(boxstyle="round,pad=0.15", fc="#0d1117",
                          ec=color, alpha=0.9, lw=0.8))

    def zone_rect(y_bot: float, y_top: float, color: str, alpha: float = 0.14) -> None:
        ax.axhspan(y_bot, y_top, color=color, alpha=alpha, zorder=1)
        ax.axhline(y_top, color=color, linewidth=0.9, linestyle="--", alpha=0.6, zorder=2)
        ax.axhline(y_bot, color=color, linewidth=0.6, linestyle="--", alpha=0.4, zorder=2)

    # Supply / Premium
    zone_rect(zones["premium_bottom"], zones["premium_top"], "#ff5252", alpha=0.14)
    _rlabel(zones["premium_top"], "SUPPLY", "#ff5252")

    # Equilibrium
    eq = zones["equilibrium"]
    ax.axhline(eq, color="#777777", linewidth=0.9, linestyle=":", alpha=0.7, zorder=2)
    _rlabel(eq, "EQ", "#777777")

    # Demand Zones
    dz_colors = ["#2196f3", "#1976d2", "#1565c0"]
    for idx, (dz_low, dz_high) in enumerate(zones["demand_zones"]):
        zone_rect(dz_low, dz_high, dz_colors[idx], alpha=0.18)
    if zones["demand_zones"]:
        _rlabel(zones["demand_zones"][0][0], "DEMAND", dz_colors[0], va="top")

    # Discount Zone
    zone_rect(zones["discount_bottom"], zones["discount_top"], "#4caf50", alpha=0.10)
    _rlabel(zones["discount_bottom"], "DISCOUNT", "#4caf50", va="top")

    # Weak High
    wh = zones["weak_high"]
    ax.axhline(wh, color="#ff9800", linewidth=1.2, linestyle="-.", alpha=0.85, zorder=2)
    _rlabel(wh, "WEAK HI", "#ff9800")

    # BOS
    if zones["last_bos"]:
        ax.axhline(zones["last_bos"], color="#00e676", linewidth=1.1, linestyle="--", alpha=0.75)
        _rlabel(zones["last_bos"], "BOS", "#00e676")

    # CHoCH
    ax.axhline(zones["choch_level"], color="#00bcd4", linewidth=1.0, linestyle=":", alpha=0.65)
    _rlabel(zones["choch_level"], "CHoCH", "#00bcd4", va="top")

    # EQL
    if zones["eql_level"]:
        ax.axhline(zones["eql_level"], color="#ce93d8", linewidth=0.8, linestyle=":", alpha=0.55)
        _rlabel(zones["eql_level"], "EQL", "#ce93d8", va="top")

    # Aktueller Preis
    p = zones["price_now"]
    ax.axhline(p, color="#ffffff", linewidth=0.8, linestyle="-", alpha=0.4)
    ax.text(n - 1, p, f"  ${p:,.2f}", color="#ffffff", fontsize=9,
            fontweight="bold", va="center", ha="left", zorder=6,
            bbox=dict(boxstyle="round,pad=0.25", fc="#1565c0", ec="#2196f3", alpha=0.95, lw=1))

    # ── Pivot-Punkte ──────────────────────────────────────────
    for idx, val in zones["pivot_highs"][-6:]:
        ax.plot(idx, val, "v", color="#ff525268", markersize=4, zorder=4)
    for idx, val in zones["pivot_lows"][-6:]:
        ax.plot(idx, val, "^", color="#26a69a68", markersize=4, zorder=4)

    # ── X-Achse: Datum-Labels ─────────────────────────────────
    step = max(1, n // 10)
    tick_pos    = list(range(0, n, step))
    tick_labels = [df["time"].iloc[i].strftime("%d.%m %H:%M") for i in tick_pos]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_labels, rotation=25, ha="right",
                       fontsize=6.5, color="#666666")
    ax_vol.set_xticks([])

    # ── Styling ───────────────────────────────────────────────
    for a in [ax, ax_vol]:
        a.tick_params(colors="#555555", labelsize=7)
        for spine in a.spines.values():
            spine.set_color("#1a1a1a")

    ax.set_xlim(-1, n + 13)   # Platz rechts für Labels
    ax.set_ylim(zones["swing_low"] - y_pad * 2, zones["swing_high"] + y_pad * 3)
    ax.grid(True, color="#141414", linewidth=0.4, alpha=0.9)
    ax_vol.grid(True, color="#141414", linewidth=0.4, alpha=0.9)
    ax_vol.set_ylabel("Vol", color="#444444", fontsize=7)

    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    ax.set_title(
        f"{symbol}  ·  {interval or INTERVAL}  ·  SMC  ·  {now_str}",
        color="#cccccc", fontsize=10.5, fontweight="bold", pad=8
    )

    plt.tight_layout(h_pad=0.3, pad=1.2)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor="#0d1117")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── 4a. LAYER 1 — Algorithmic Pre-Filter ─────────────────────
def should_run_analysis(df: pd.DataFrame, zones: dict,
                        interval: str | None = None) -> tuple[bool, str]:
    """
    Pure-Python pre-filter: returns (True, reason) when at least one
    SMC condition fires, so we only hit the Claude API when necessary.
    No network calls. Zero cost.
    interval: Timeframe für die TF-Profil-Skalierung (Default: globales INTERVAL).
    """
    tf = interval or INTERVAL
    close  = df["close"].values
    high   = df["high"].values
    low    = df["low"].values
    opens  = df["open"].values
    vol    = df["volume"].values
    n      = len(df)
    price  = zones["price_now"]
    ph     = zones["pivot_highs"]   # list of (idx, price)
    pl     = zones["pivot_lows"]

    import config as _cfg
    import tf_profiles
    _c        = _cfg.load()
    vol_mult  = float(_c.get("VOLUME_SPIKE_MULTIPLIER", 2.0))
    eqh_tol   = float(_c.get("EQH_TOLERANCE", 0.0015))
    choch_win = int(_c.get("CHOCH_WINDOW", 10))

    # ── TF-Kalibrierung: config-Werte (vom Optimizer gelernt) relativ zum
    #    4h-Baseline-Profil auf den aktiven Timeframe skalieren ──────────────
    prof   = tf_profiles.get(tf)
    base   = tf_profiles.get("4h")
    eqh_tol   = eqh_tol  * (prof["eqh_tol"]  / base["eqh_tol"])
    vol_mult  = vol_mult * (prof["vol_mult"] / base["vol_mult"])
    choch_win = max(6, round(choch_win * (prof["choch_win"] / base["choch_win"])))
    # ATR-adaptiv: bei hoher Volatilität streuen Equal Highs breiter
    eqh_tol   = max(eqh_tol, tf_profiles.eqh_tolerance(tf, zones.get("atr_pct", 0.0)))
    bos_recency = int(prof["bos_recency"])

    # Letzte GESCHLOSSENE Kerze (letzte Zeile ist die laufende, unfertige Kerze)
    closed_c = close[-2] if n >= 2 else close[-1]

    triggers: list[str] = []

    # 1. BOS — letzte geschlossene Kerze SCHLIESST über Swing-High / unter Swing-Low.
    #    Kein Docht-Fakeout (price → closed_c) und nur FRISCHE Brüche:
    #    mindestens eine der vorherigen Kerzen muss noch diesseits des Levels liegen.
    if ph and n >= bos_recency + 3:
        last_sh = max(h for _, h in ph[-3:])
        prev_closes = close[-2 - bos_recency:-2]
        if closed_c > last_sh and any(c <= last_sh for c in prev_closes):
            triggers.append(f"BOS BULLISCH (Close ${closed_c:.2f} > ${last_sh:.2f})")
    if pl and n >= bos_recency + 3:
        last_sl = min(l for _, l in pl[-3:])
        prev_closes = close[-2 - bos_recency:-2]
        if closed_c < last_sl and any(c >= last_sl for c in prev_closes):
            triggers.append(f"BOS BÄRISCH (Close ${closed_c:.2f} < ${last_sl:.2f})")

    # 2. CHoCH — Gegenbruch nach Trend-Leg (auf Schlusskurs-Basis)
    if n >= choch_win:
        h_slice = high[-choch_win:-1]
        l_slice = low[-choch_win:-1]
        was_down = all(h_slice[i] >= h_slice[i + 1] for i in range(3))
        was_up   = all(l_slice[i] <= l_slice[i + 1] for i in range(3))
        if was_down and closed_c > max(h_slice[-3:]):
            triggers.append("CHoCH BULLISCH (Break nach Abwärtsleg)")
        if was_up and closed_c < min(l_slice[-3:]):
            triggers.append("CHoCH BÄRISCH (Break nach Aufwärtsleg)")

    # 3. Equal Highs / Equal Lows — within configurable tolerance
    if len(ph) >= 2:
        vals = [h for _, h in ph[-5:]]
        if any(abs(vals[i] - vals[j]) / vals[i] < eqh_tol
               for i in range(len(vals)) for j in range(i + 1, len(vals))):
            triggers.append(f"EQUAL HIGHS ~${vals[-1]:.2f}")
    if len(pl) >= 2:
        vals = [l for _, l in pl[-5:]]
        if any(abs(vals[i] - vals[j]) / vals[i] < eqh_tol
               for i in range(len(vals)) for j in range(i + 1, len(vals))):
            triggers.append(f"EQUAL LOWS ~${vals[-1]:.2f}")

    # 4. Premium / Discount Zone — NUR mit Rejection-Bestätigung.
    #    Das nackte Zone-Setup hatte historisch ~25% WR; eine Ablehnungs-Kerze
    #    (langer Gegendocht oder Umkehr-Close) filtert die Fehlsignale.
    if n >= 2:
        c_open, c_close = opens[-2], close[-2]
        c_high, c_low   = high[-2], low[-2]
        c_range = max(c_high - c_low, 1e-9)
        lower_wick = (min(c_open, c_close) - c_low)  / c_range
        upper_wick = (c_high - max(c_open, c_close)) / c_range

        if price < zones["discount_top"]:
            # Bullishe Rejection: langer unterer Docht ODER bullisher Umkehr-Close
            if lower_wick >= 0.40 or c_close > c_open:
                triggers.append(
                    f"DISCOUNT ZONE + REJECTION (${price:.2f} ≤ ${zones['discount_top']:.2f})"
                )
        elif price > zones["premium_bottom"]:
            if upper_wick >= 0.40 or c_close < c_open:
                triggers.append(
                    f"PREMIUM ZONE + REJECTION (${price:.2f} ≥ ${zones['premium_bottom']:.2f})"
                )

    # 5. Volume spike — current candle > vol_mult × 20-period average
    if n >= 21:
        avg_vol = float(vol[-21:-1].mean())
        if vol[-1] > avg_vol * vol_mult:
            triggers.append(f"VOLUME SPIKE ({vol[-1]:,.0f} > {vol_mult:.1f}× Ø {avg_vol:,.0f})")

    if triggers:
        return True, " | ".join(triggers)
    return False, ""


# ── 4b. LAYER 2 — Haiku Pre-Check ────────────────────────────
def haiku_precheck(zones: dict, ticker: dict) -> bool:
    """
    Cheap sanity check via claude-haiku before calling Sonnet.
    System prompt ≤ 50 tokens. User message: compact SMC summary.
    Returns True  → proceed to Sonnet.
    Returns False → abort (Haiku said NO).
    """
    if not ANTHROPIC_API_KEY:
        return True     # no key → don't gate on Haiku

    price    = zones["price_now"]
    eq       = zones["equilibrium"]
    chg      = float(ticker.get("priceChangePercent", 0))
    zone_tag = "PREMIUM" if price > eq else "DISCOUNT"
    bos_lvl  = f"${zones['last_bos']:.2f}" if zones["last_bos"] else "n/a"

    user_msg = (
        f"SOL ${price:.2f} | 24h {chg:+.1f}% | {zone_tag} | "
        f"EQ ${eq:.2f} | WH ${zones['weak_high']:.2f} | "
        f"BOS {bos_lvl} | CHoCH ${zones['choch_level']:.2f}\n"
        "High-probability SMC setup right now? YES or NO only."
    )

    headers = {
        "x-api-key":          ANTHROPIC_API_KEY,
        "anthropic-version":  "2023-06-01",
        "content-type":       "application/json",
    }
    payload = {
        "model":      HAIKU_MODEL,
        "max_tokens": 5,
        "system":     "SMC crypto analyst. Reply YES or NO only.",
        "messages":   [{"role": "user", "content": user_msg}],
    }
    r = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=15)
    r.raise_for_status()
    data  = r.json()
    usage = data.get("usage", {})

    cost_tracker.log_call(
        model         = HAIKU_MODEL,
        input_tokens  = usage.get("input_tokens",  0),
        output_tokens = usage.get("output_tokens", 0),
    )

    answer = data["content"][0]["text"].strip().upper()
    return answer.startswith("YES")


# ── 4. CLAUDE AI ANALYSE ──────────────────────────────────────
def get_ai_analysis(zones: dict, ticker: dict, symbol: str) -> str:
    """
    Ruft die Claude API für eine SMC-Textanalyse auf.

    LAYER 3 — Prompt Caching:
    The static role/format instructions are placed in the system prompt
    with cache_control so repeated calls reuse cached tokens (~90 % cheaper).
    Only the dynamic market data (user message) is sent fresh each time.
    Analysis content and output format are UNCHANGED from original.
    """
    if not ANTHROPIC_API_KEY:
        return "⚠️ ANTHROPIC_API_KEY nicht gesetzt – AI-Analyse übersprungen."

    # ── Static part → cached (role + output format instructions) ──────
    _SYSTEM = (
        "Du bist ein professioneller Krypto-Trader mit Fokus auf "
        "Smart Money Concepts (SMC) / ICT.\n\n"
        "Erstelle eine prägnante Tagesanalyse auf Deutsch mit diesen 5 Punkten:\n"
        "1. 📍 Marktstruktur (bullisch/bärisch/neutral + Begründung)\n"
        "2. 🎯 Key Levels die heute wichtig sind\n"
        "3. 🐂 Bullisches Szenario (Einstieg, Ziel, Invalidierung)\n"
        "4. 🐻 Bärisches Szenario (Einstieg, Ziel, Invalidierung)\n"
        "5. ⚡ Bias des Tages (1 klare Meinung)\n\n"
        "Sei präzise, konkret mit Preisniveaus. Kein Blabla."
    )

    # ── Dynamic part → sent fresh every call (only market numbers) ────
    price   = zones["price_now"]
    eq      = zones["equilibrium"]
    wh      = zones["weak_high"]
    sh      = zones["swing_high"]
    sl      = zones["swing_low"]
    chg_24h = float(ticker.get("priceChangePercent", 0))
    vol_24h = float(ticker.get("quoteVolume", 0))
    dz      = zones["demand_zones"]
    bos_str = f"${zones['last_bos']:,.2f}" if zones["last_bos"] else "n/a"

    user_msg = (
        f"Analysiere {symbol} auf Basis dieser aktuellen Marktdaten:\n\n"
        f"PREISDATEN:\n"
        f"- Aktueller Preis: ${price:,.2f}\n"
        f"- 24h Veränderung: {chg_24h:+.2f}%\n"
        f"- 24h Volumen: ${vol_24h:,.0f}\n\n"
        f"SMC-ZONEN:\n"
        f"- Swing High: ${sh:,.2f}\n"
        f"- Swing Low:  ${sl:,.2f}\n"
        f"- Equilibrium: ${eq:,.2f}\n"
        f"- Weak High: ${wh:,.2f}\n"
        f"- Premium Zone: ${zones['premium_bottom']:,.2f} – ${zones['premium_top']:,.2f}\n"
        f"- Discount Zone: ${zones['discount_bottom']:,.2f} – ${zones['discount_top']:,.2f}\n"
        f"- Demand Zone 1: ${dz[0][0]:,.2f} – ${dz[0][1]:,.2f}\n"
        f"- Demand Zone 2: ${dz[1][0]:,.2f} – ${dz[1][1]:,.2f}\n"
        f"- Demand Zone 3: ${dz[2][0]:,.2f} – ${dz[2][1]:,.2f}\n"
        f"- CHoCH Level: ${zones['choch_level']:,.2f}\n"
        f"- Letztes BOS: {bos_str}"
    )

    headers = {
        "x-api-key":          ANTHROPIC_API_KEY,
        "anthropic-version":  "2023-06-01",
        "anthropic-beta":     "prompt-caching-2024-07-31",   # Layer 3: enable caching
        "content-type":       "application/json",
    }
    payload = {
        "model":      SONNET_MODEL,
        "max_tokens": 1000,
        # Layer 3: system prompt with cache_control
        "system": [
            {
                "type": "text",
                "text": _SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user_msg}],
    }

    r = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data  = r.json()
    usage = data.get("usage", {})

    # Layer 3: log actual token usage including cache hits
    call_cost = cost_tracker.log_call(
        model                = SONNET_MODEL,
        input_tokens         = usage.get("input_tokens",              0),
        output_tokens        = usage.get("output_tokens",             0),
        cached_input_tokens  = usage.get("cache_read_input_tokens",   0),
        cache_write_tokens   = usage.get("cache_creation_input_tokens", 0),
    )
    # Expose for INTEGRATION 4 (signal update)
    global _last_tokens_used, _last_cost_usd
    _last_tokens_used = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    _last_cost_usd    = call_cost

    return data["content"][0]["text"]


# ── 5. KONSOLEN-AUSGABE ───────────────────────────────────────
import re as _re

_CHARTS_DIR = Path(__file__).parent / "charts"


def _strip_html(text: str) -> str:
    """Entfernt einfache HTML-Tags für die Terminal-Ausgabe."""
    return _re.sub(r"<[^>]+>", "", text)


def output_chart(image_bytes: bytes, caption: str):
    """Speichert den Chart als PNG-Datei und gibt die Kurzinfo im Terminal aus."""
    _CHARTS_DIR.mkdir(exist_ok=True)
    fname = _CHARTS_DIR / f"chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    fname.write_bytes(image_bytes)
    print("\n" + "─" * 60)
    print(_strip_html(caption))
    print(f"🖼️  Chart gespeichert: {fname}")
    print("─" * 60)


def output_message(text: str):
    """Gibt die vollständige Analyse im Terminal aus."""
    print("\n" + "═" * 60)
    print(_strip_html(text))
    print("═" * 60 + "\n")


# ── 5b. SELF-LEARNING HELPERS ─────────────────────────────────
# Laufende Signal-ID und Token-Tracking für den aktuellen main()-Aufruf
_current_signal_id: int = -1
_last_tokens_used:  int = 0
_last_cost_usd:     float = 0.0

_WEEKLY_STAMP = Path(__file__).parent / ".last_weekly_run"


def _smart_layer2(zones: dict, ticker: dict,
                   df, trigger_reason: str,
                   interval: str | None = None) -> tuple[str, bool]:
    """
    INTEGRATION: Ersetzt haiku_precheck() durch intelligente Auswahl.
    Verwendet lokales Modell wenn verfügbar und genau genug, sonst Haiku.
    """
    if local_filter_model.is_active():
        result = local_filter_model.predict(zones, df, trigger_reason,
                                            interval or INTERVAL)
        cost_tracker.log_call(model="local_model", input_tokens=0, output_tokens=0)
        return "local_model", result
    else:
        result = haiku_precheck(zones, ticker)
        return HAIKU_MODEL, result


def _update_signal_model(sig_id: int, model: str,
                          tokens: int, cost: float) -> None:
    """Aktualisiert ein gespeichertes Signal mit dem tatsächlich verwendeten Modell."""
    if sig_id < 0:
        return
    try:
        conn = signal_logger._conn()
        conn.execute(
            "UPDATE signals SET api_model_used=?, tokens_used=?, cost_usd=? WHERE id=?",
            (model, tokens, cost, sig_id)
        )
        conn.commit()
    except Exception:
        pass


_DAILY_STAMP = Path(__file__).parent / ".last_daily_summary"


def _run_weekly_if_due() -> None:
    """Führt wöchentliche Analyse/Optimierung aus wenn > 6 Tage vergangen."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    if now.weekday() != 6:   # nur sonntags
        return
    if _WEEKLY_STAMP.exists():
        try:
            last = datetime.fromisoformat(_WEEKLY_STAMP.read_text().strip())
            if (now - last) < timedelta(hours=20):
                return
        except Exception:
            pass
    print(f"\n  ═══ SONNTÄGLICHE OPTIMIERUNG ═══")
    performance_analyzer.run()
    threshold_optimizer.run()
    try:
        import backtest_learner as _bl
        _bl.run()
    except Exception as e:
        print(f"  ⚠️  backtest_learner: {e}")
    try:
        smart_router.weekly_report()
    except Exception as e:
        print(f"  ⚠️  routing_report: {e}")
    local_filter_model.train_if_ready()
    _WEEKLY_STAMP.write_text(now.isoformat())
    print(f"  ═══ Optimierung abgeschlossen ═══\n")


def _send_daily_summary_if_due() -> None:
    """Gibt tägliche Zusammenfassung im Terminal aus (08:00 MEZ ≈ 07:00 UTC)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    if now.hour not in (6, 7):    # 07:00-08:00 UTC = 08:00-09:00 MEZ
        return
    if _DAILY_STAMP.exists():
        try:
            last = datetime.fromisoformat(_DAILY_STAMP.read_text().strip())
            if (now - last) < timedelta(hours=20):
                return
        except Exception:
            pass
    try:
        smart_router.daily_summary(SYMBOL)
        _DAILY_STAMP.write_text(now.isoformat())
        print(f"  📅 Tages-Zusammenfassung ausgegeben.")
    except Exception as e:
        print(f"  ⚠️  Daily-Summary: {e}")


# ── 6. MAIN ───────────────────────────────────────────────────
def _fill_demand_zones(zones: dict) -> None:
    """Füllt demand_zones auf 3 Einträge auf (Fallback-Zonen unter dem Preis)."""
    p = zones["price_now"]
    while len(zones["demand_zones"]) < 3:
        offset = len(zones["demand_zones"]) * p * 0.015
        zones["demand_zones"].append((p - offset - p * 0.01, p - offset))


def _set_signal_meta(sig_id: int, mtf_alignment: int) -> None:
    """Schreibt das MTF-Alignment nachträglich auf ein geloggtes Signal."""
    if not sig_id or sig_id < 0:
        return
    try:
        conn = signal_logger._conn()
        conn.execute("UPDATE signals SET mtf_alignment=? WHERE id=?",
                     (int(mtf_alignment), sig_id))
        conn.commit()
    except Exception:
        pass


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starte Multi-Timeframe-Analyse für {SYMBOL}...")

    print("  → Lade Ticker-Daten...")
    ticker = fetch_ticker(SYMBOL)

    # ── MULTI-TIMEFRAME-SCAN: alle Charts in einem Durchgang ─────────
    import mtf_analyzer
    print("  → Multi-Timeframe-Scan (1d → 4h → 1h → 15m)...")
    mtf = mtf_analyzer.scan(SYMBOL)
    if not mtf:
        print("  ✗ MTF-Scan fehlgeschlagen — keine Daten.")
        return
    trends = {tf: r["trend"] for tf, r in mtf.items()}

    # ── INTEGRATION 1: Offene Signale auflösen — jedes Signal gegen die
    #    Kerzen seines eigenen Timeframes (Daten liegen aus dem Scan vor) ─
    df_4h = mtf.get("4h", {}).get("df")
    if df_4h is not None:
        signal_logger.update_outcomes(
            df_4h, tf_dfs={tf: r["df"] for tf, r in mtf.items()}
        )

    # ── INTEGRATION 2: Backtest (wenn DB < 200 oder Montag 02:00) ────
    if backtester.should_run_now():
        backtester.run()

    # ── INTEGRATION 3: Wöchentliche Optimierung (sonntags) ───────────
    _run_weekly_if_due()

    # ── Tages-Zusammenfassung 08:00 MEZ ──────────────────────────────
    _send_daily_summary_if_due()

    # ── Getriggerte Timeframes einsammeln (HTF zuerst) ───────────────
    triggered_tfs = [tf for tf in mtf_analyzer.TIMEFRAMES
                     if mtf.get(tf, {}).get("triggered")]
    if not triggered_tfs:
        print("  ✗ Kein SMC-Trigger auf keinem Timeframe – API übersprungen.")
        print(f"  💰 Kosten diese Ausführung: $0.00000  |  Monat: ${cost_tracker.get_monthly_total():.4f}")
        return

    print(f"  ✓ Trigger auf {len(triggered_tfs)} Timeframe(s): {', '.join(triggered_tfs)}")

    # Budget-Schutz: maximal EIN KI-Call pro Lauf — der erste (= höchste) TF,
    # den der Router auf 'ai' routet. Alle weiteren laufen als Algo.
    ai_used = False
    import algo_signal_engine as _ae

    for tf in triggered_tfs:
        r      = mtf[tf]
        zones  = r["zones"]
        df     = r["df"]
        reason = r["trigger_reason"]
        _fill_demand_zones(zones)

        # MTF-Alignment dieses Signals (HTF-Trends vs. Signal-Bias)
        _, sig_bias, _ = signal_logger._parse_trigger(reason)
        align = mtf_analyzer.alignment_score(tf, sig_bias, trends)
        align_str = f"+{align}" if align > 0 else str(align)
        print(f"\n  ── {tf}: {reason[:70]}")
        print(f"     MTF-Alignment: {align_str} (HTF-Trends: "
              + ", ".join(f"{k}={v}" for k, v in trends.items() if k != tf) + ")")

        # ── SMART ROUTER pro Timeframe ───────────────────────────────
        routing, algo_score, samples, win_rate_pct = smart_router.route(
            zones, df, reason, tf
        )

        if routing == "skip":
            print(f"     ✗ Router: Score {algo_score} zu niedrig – übersprungen.")
            continue

        if routing == "algo" or ai_used:
            if routing == "ai" and ai_used:
                print(f"     ↓ KI-Budget verbraucht → Algo-Pfad für {tf}")
            result = _ae.process_signal(zones, df, reason, tf)
            if result.get("signal_id"):
                _set_signal_meta(result["signal_id"], align)
            continue

        # ── routing == "ai" → KI-Pipeline (einmal pro Lauf) ──────────
        ai_used = True
        _current_signal_id = signal_logger.log_signal(
            zones=zones, df=df, trigger_reason=reason,
            api_model_used="pending", tokens_used=0, cost_usd=0.0,
            timeframe=tf,
            atr_pct=zones.get("atr_pct", 0.0),
            ema200_dist_pct=zones.get("ema200_dist_pct", 0.0),
            mtf_alignment=align,
        )
        try:
            _c = signal_logger._conn()
            _c.execute("UPDATE signals SET algo_score=?, routing=? WHERE id=?",
                       (float(algo_score), "ai", _current_signal_id))
            _c.commit()
        except Exception:
            pass

        # Chart zeichnen
        print("     → Zeichne Chart...")
        chart_bytes = draw_chart(df, zones, SYMBOL, interval=tf)

        # ── LAYER 2: Lokales Modell oder Haiku ───────────────────────
        layer2_model, proceed = _smart_layer2(zones, ticker, df, reason, interval=tf)
        if not proceed:
            _update_signal_model(_current_signal_id, layer2_model, 0, 0.0)
            print(f"     ✗ Layer 2 ({layer2_model}): kein Setup – Sonnet übersprungen.")
            now_str   = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
            chg       = float(ticker.get("priceChangePercent", 0))
            chg_emoji = "📈" if chg >= 0 else "📉"
            caption   = (
                f"<b>🤖 {SYMBOL} SMC Scan {tf}</b>  <i>({now_str})</i>\n\n"
                f"{chg_emoji} ${zones['price_now']:,.2f}  ({chg:+.2f}%)\n"
                f"⚖️ EQ: ${zones['equilibrium']:,.2f}  |  Trigger: {reason}\n"
                f"<i>Keine hochwertige Setup-Bestätigung – Analyse entfällt.</i>"
            )
            output_chart(chart_bytes, caption)
            continue

        print(f"     ✓ Layer 2 ({layer2_model}): HIGH-PROBABILITY Setup bestätigt")
        # Paper Trader liest das Signal beim nächsten Kerzen-Close aus signals.db

        # AI Analyse
        print("     → Hole AI-Analyse (Sonnet + Prompt Caching)...")
        analysis_text = get_ai_analysis(zones, ticker, SYMBOL)

        # ── INTEGRATION 4: Signal mit echten Kosten aktualisieren ────
        _update_signal_model(_current_signal_id, SONNET_MODEL,
                             _last_tokens_used, _last_cost_usd)

        # Ausgabe: Chart mit Kurzinfo
        now_str  = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        chg      = float(ticker.get("priceChangePercent", 0))
        chg_emoji = "📈" if chg >= 0 else "📉"

        # Bull Run Phase (gecacht, kein Extra-API-Call)
        try:
            import bull_run_detector as _brd
            _br_label = _brd.get_phase_label()
        except Exception:
            _br_label = ""

        caption = (
            f"<b>🤖 {SYMBOL} SMC Analyse {tf}</b>\n"
            f"<b>{now_str}</b>\n\n"
            f"{chg_emoji} Preis: <b>${zones['price_now']:,.2f}</b>  ({chg:+.2f}%)\n"
            f"🔀 MTF-Alignment: {align_str}\n"
            + (f"📊 Marktphase: {_br_label}\n" if _br_label else "")
            + f"⚖️ EQ: ${zones['equilibrium']:,.2f}\n"
            f"⚡ Weak High: ${zones['weak_high']:,.2f}\n"
            f"🔴 Premium: ${zones['premium_bottom']:,.2f}–${zones['premium_top']:,.2f}\n"
            f"🔵 DZ1: ${zones['demand_zones'][0][0]:,.2f}–${zones['demand_zones'][0][1]:,.2f}"
        )

        print("     → Gebe Chart aus...")
        output_chart(chart_bytes, caption)

        full_msg = (
            f"<b>📊 {SYMBOL} – Vollständige Analyse {tf} ({now_str})</b>\n\n"
            + analysis_text
        )
        print("     → Gebe Analyse-Text aus...")
        output_message(full_msg)

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ✅ Multi-TF-Analyse abgeschlossen "
          f"({len(triggered_tfs)} TF(s) verarbeitet).")


if __name__ == "__main__":
    main()
