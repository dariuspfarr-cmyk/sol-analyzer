"""
Paper Trader — vollautomatischer 24/7 Paper-Trading-Loop.

  • Tradet GENAU die Signale, die der Signal-Bot (sol_analysis_bot) generiert
  • Virtuelles Kapital: $10.000  |  Risiko pro Trade: 1%
  • Liest Entry / SL / TP direkt aus signals.db (keine eigene Analyse)
  • Schreibt Outcome (WIN/LOSS) zurück auf die originale Signal-Row
  • Triggert nach jedem Trade backtest_learner → aktualisierte Gewichte
  • Bot verwendet neue Gewichte beim nächsten Analyse-Lauf → geschlossener Lernkreis
"""

from __future__ import annotations
import csv
import json
import time
import threading
import traceback
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Stop-Event: server.py kann den Loop von außen stoppen/starten
_stop_event = threading.Event()

import learning_engine
import signal_logger
import poi_tracker

# ── Gemeinsame HTTP-Session: Connection-Pooling (keep-alive) + Auto-Retry mit
#    Backoff bei transienten Fehlern (429/5xx). Spart Verbindungsaufbau im 24/7-
#    Loop und überbrückt kurze Netzwerk-/Rate-Limit-Aussetzer. ───────────────────
from requests.adapters import HTTPAdapter
_http = requests.Session()
_http.headers.update({"User-Agent": "SOLAnalyzer/2.0"})
try:
    from urllib3.util.retry import Retry
    _retry = Retry(total=3, connect=3, read=3, backoff_factor=0.6,
                   status_forcelist=(429, 500, 502, 503, 504), raise_on_status=False)
    _adapter = HTTPAdapter(max_retries=_retry, pool_connections=4, pool_maxsize=10)
    _http.mount("https://", _adapter)
    _http.mount("http://", _adapter)
except Exception:
    pass   # Retry optional — Session funktioniert auch ohne

BASE          = Path(__file__).parent
STATE_FILE    = BASE / "state.json"
TRADES_CSV    = BASE / "trades.csv"
TRADES_JSON   = BASE / "trades.json"
ERROR_LOG     = BASE / "error.log"
BINANCE_BASE    = "https://api.binance.com/api/v3"
BINANCE_FUTURES = "https://fapi.binance.com/fapi/v1"
STRATEGY_PARAMS_FILE = BASE / "strategy_params.json"

SYMBOL        = "SOLUSDT"
INTERVAL      = "4h"    # Analyse-Timeframe (Zonen, Indikatoren)
LOOP_INTERVAL = "15m"   # Loop-Takt: SL/TP-Checks + Signal-Pickup pro 15m-Kerze
                        # (vorher 4h → 15m-Signale veralteten vor dem ersten Check)
CANDLES       = 200
INITIAL_BAL   = 10_000.0
RISK_PCT      = 0.01       # 1% Basis-Risiko pro Trade (dynamisch skaliert)
MIN_RR        = 1.8        # Mindest R:R (überschreibbar durch strategy_params.json)
MIN_CONFIDENCE_SCORE = 0.45
ALGO_MIN_SCORE = 70
MAX_SIGNAL_AGE_H = 8.0
POLL_INTERVAL = 30
# Realistischer Entry-Trigger: Position wird nur eröffnet, wenn der AKTUELLE
# Marktpreis gerade in der Entry-Zone des Signals liegt — kein rückwirkendes
# Nachjagen eines bereits weggelaufenen Preises.
# Defaults (überschreibbar via strategy_params.json → vom Optimizer evolvierbar):
ENTRY_ZONE_FRAC  = 0.35  # max. Abweichung (× SL-Distanz) vom Entry in Zonen-Richtung
ENTRY_CHASE_FRAC = 0.15  # max. "Nachlaufen" (× SL-Distanz) Richtung TP über den Entry hinaus
# Realistische Ausführung wie an einer echten Börse:
TAKER_FEE_PCT = 0.05   # Handelsgebühr je Seite in % (Entry + Exit ≈ 0.10% Round-Trip)
SLIPPAGE_PCT  = 0.02   # Slippage bei Market-Order in % (Fill minimal schlechter als Kurs)
MAX_LEVERAGE  = 1.0    # 1.0 = Spot/kein Hebel: Positionswert ≤ freies Kapital
LOT_STEP      = 0.001  # Mindest-Schrittweite der Stückzahl (Lot-Size, wird abgerundet)
PRICE_DECIMALS = 2     # Tick-Size der Preise (0.01) — Fills auf 2 Nachkommastellen
MAX_RISK_MULT        = 2.0   # max. Positions-Skalierung bei sehr starken Signalen
MIN_RISK_MULT        = 0.5   # min. Skalierung (auch bei Drawdown-Schutz)
DRAWDOWN_LOSS_TRIGGER = 3   # Verluste in Folge → halbes Risiko
# Zwangs-Exit ohne SL/TP: TF-abhängig über tf_profiles.max_hold_hours
# (15m: 12h · 4h: 7 Tage · 1d: 30 Tage) — siehe _check_close_one
MIN_SCORE_FLOOR       = 65.0 # Mindest-Composite-Score; schlechtere Signale werden ignoriert
DAILY_LOSS_LIMIT_PCT  = 0.02 # Tägliches Verlust-Limit 2% → kein neuer Trade bis nächsten Tag
DEDUP_HOURS           = 2.0  # Setup+Bias-Duplikat-Sperre: kein erneuter Eintritt innerhalb N Stunden
WIN_STREAK_BONUS_MIN  = 3    # ab N Gewinnen in Folge: Risiko-Bonus
DD_SCALE_MAX_PCT      = 10.0 # bei 10% Drawdown → 50% der normalen Risikogröße
DAILY_PERF_FILE        = BASE / "daily_performance.json"
STRATEGY_RULES_FILE    = BASE / "strategy_rules.json"
ACTIVE_STRATEGY_FILE   = BASE / "active_strategy.json"
MAX_POSITIONS          = 3      # max. gleichzeitig offene Positionen


def _dynamic_score_floor(state: "State") -> float:
    """
    Adaptiver Mindest-Score basierend auf den letzten 20 Trades.
    Gut laufende Phase  → etwas lockerer (mehr Trades einfangen).
    Schlechte Phase     → strenger (weniger Fehlsignale).
    Ohne Daten          → Standard MIN_SCORE_FLOOR.
    """
    recent = [t for t in state.trades[-20:] if isinstance(t.get("pnl"), (int, float))]
    if len(recent) < 10:
        return MIN_SCORE_FLOOR
    wr = sum(1 for t in recent if t["pnl"] > 0) / len(recent)
    if wr > 0.65:
        return max(55.0, MIN_SCORE_FLOOR - 8.0)   # starke Phase → großzügiger
    elif wr < 0.35:
        return min(82.0, MIN_SCORE_FLOOR + 14.0)  # sehr schlechte Phase → viel strenger
    elif wr < 0.45:
        return min(75.0, MIN_SCORE_FLOOR + 7.0)   # schlechte Phase → strenger
    return MIN_SCORE_FLOOR


# ── Strategy Rules (zentral via strategy_knowledge) ──────────────────────────
def _apply_strategy_rules(row: dict, hour: int) -> float:
    """
    Wendet synthetisierte Strategie-Regeln an — zentral über strategy_knowledge
    (inkl. Profil-Filter und Live-Effektivitäts-Gewichtung).
    Gibt gewichteten Score-Modifier zurück.
    """
    try:
        import strategy_knowledge
        mod, _ = strategy_knowledge.evaluate(
            row.get("setup_type", ""), row.get("bias", "neutral"),
            row.get("zone_position", "neutral"), hour,
        )
        return mod
    except Exception:
        return 0.0


def _matched_rule_signatures(row: dict, hour: int) -> list[str]:
    """Signaturen aller Regeln, die für dieses Signal matchen (für Feedback-Loop)."""
    try:
        import strategy_knowledge
        _, sigs = strategy_knowledge.evaluate(
            row.get("setup_type", ""), row.get("bias", "neutral"),
            row.get("zone_position", "neutral"), hour,
        )
        return sigs
    except Exception:
        return []

# ── Browser-Params Cache (strategy_params.json) ───────────────────────────────
_sp_cache: dict = {}
_sp_mtime: float = 0.0

def _load_strategy_params() -> dict:
    global _sp_cache, _sp_mtime
    try:
        mt = STRATEGY_PARAMS_FILE.stat().st_mtime
        if mt != _sp_mtime:
            with open(STRATEGY_PARAMS_FILE, encoding="utf-8") as f:
                _sp_cache = json.load(f)
            _sp_mtime = mt
    except Exception:
        pass
    return _sp_cache


# ── 1D-Trend-Cache (täglich aktualisiert, 4h TTL) ────────────────────────────
_daily_trend_cache: dict = {"bias": "neutral", "ts": 0.0}
_DAILY_CACHE_TTL = 14_400   # 4 Stunden

def _get_daily_trend() -> str:
    """
    Gibt den 1D-Trend von SOL zurück ('bullish'/'bearish'/'neutral').
    EMA20 vs EMA50 auf Tageskerzen. Cached für 4h.
    """
    global _daily_trend_cache
    if time.time() - _daily_trend_cache["ts"] < _DAILY_CACHE_TTL:
        return _daily_trend_cache["bias"]
    try:
        r = _http.get(
            f"{BINANCE_BASE}/klines",
            params={"symbol": SYMBOL, "interval": "1d", "limit": 55},
            timeout=10,
        )
        r.raise_for_status()
        closes = [float(c[4]) for c in r.json()]
        if len(closes) < 50:
            return _daily_trend_cache["bias"]
        ema20 = sum(closes[-20:]) / 20
        ema50 = sum(closes[-50:]) / 50
        bias  = "bullish" if ema20 > ema50 else "bearish"
        _daily_trend_cache = {"bias": bias, "ts": time.time()}
        return bias
    except Exception:
        return _daily_trend_cache["bias"]


# ── 1H-Trend-Cache (EMA9 vs EMA21, 1h TTL) ───────────────────────────────────
_1h_trend_cache: dict = {"bias": "neutral", "ts": 0.0}
_1H_CACHE_TTL = 3_600   # 1 Stunde

def _get_1h_trend() -> str:
    """EMA9 vs EMA21 auf 1H-Kerzen — kurzfristiger Trendfür Confluence-Filter."""
    global _1h_trend_cache
    if time.time() - _1h_trend_cache["ts"] < _1H_CACHE_TTL:
        return _1h_trend_cache["bias"]
    try:
        r = _http.get(
            f"{BINANCE_BASE}/klines",
            params={"symbol": SYMBOL, "interval": "1h", "limit": 25},
            timeout=10,
        )
        r.raise_for_status()
        closes = [float(c[4]) for c in r.json()]
        if len(closes) < 21:
            return _1h_trend_cache["bias"]
        ema9  = sum(closes[-9:])  / 9
        ema21 = sum(closes[-21:]) / 21
        bias  = "bullish" if ema9 > ema21 else "bearish"
        _1h_trend_cache = {"bias": bias, "ts": time.time()}
        return bias
    except Exception:
        return _1h_trend_cache["bias"]


# ── Fear & Greed Cache (alternative.me, 2h TTL) ──────────────────────────────
_fg_cache: dict = {"value": 50, "ts": 0.0}
_FG_CACHE_TTL = 14_400   # 4 Stunden (Fear & Greed aktualisiert nur 1×/Tag)

def _fetch_fear_greed() -> int:
    """Fear & Greed Index 0-100. Cached 2h."""
    global _fg_cache
    if time.time() - _fg_cache["ts"] < _FG_CACHE_TTL:
        return _fg_cache["value"]
    try:
        r = _http.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        r.raise_for_status()
        val = int(r.json()["data"][0]["value"])
        _fg_cache = {"value": val, "ts": time.time()}
        return val
    except Exception:
        return _fg_cache["value"]


# ── Volume-Ratio Cache (aktualisiert bei jedem run_once) ─────────────────────
_vol_cache: dict = {"ratio": 1.0}   # aktuelles Vol / 20-Kerzen-Durchschnitt

# ── ATR + ADX Cache (aktualisiert bei jedem run_once) ────────────────────────
_atr_cache: dict = {"value": 0.0}
_adx_cache: dict = {"value": 25.0}   # 25 = neutral/default

# Reine Indikator-Mathematik in pt_indicators ausgelagert (zustandslos, testbar).
# Namen mit führendem _ beibehalten → alle bisherigen Aufrufstellen unverändert.
from pt_indicators import (
    calc_atr as _calc_atr,
    calc_adx as _calc_adx,
    ema      as _ema,
    calc_rsi as _calc_rsi,
)

_macd_cache: dict = {"macd": 0.0, "signal": 0.0, "hist": 0.0, "hist_prev": 0.0, "trend": "neutral", "cross": "none"}

def _calc_macd(df: pd.DataFrame) -> dict:
    """MACD(12,26,9) auf 4H-Schlusskursen."""
    try:
        closes = list(df["close"].values)
        if len(closes) < 35:
            return _macd_cache
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        min_len = min(len(ema12), len(ema26))
        macd_line = [ema12[-(min_len-i)] - ema26[-(min_len-i)] for i in range(min_len)]
        # align: take last min_len elements of each
        macd_line = [a - b for a, b in zip(ema12[-min_len:], ema26[-min_len:])]
        sig_line  = _ema(macd_line, 9)
        hist      = [m - s for m, s in zip(macd_line[-len(sig_line):], sig_line)]
        if len(hist) < 2:
            return _macd_cache
        h_cur  = hist[-1]
        h_prev = hist[-2]
        cross = "golden" if h_cur > 0 and h_prev <= 0 else ("dead" if h_cur < 0 and h_prev >= 0 else "none")
        return {
            "macd":      round(macd_line[-1], 4),
            "signal":    round(sig_line[-1],  4),
            "hist":      round(h_cur,  4),
            "hist_prev": round(h_prev, 4),
            "trend":     "bullish" if h_cur > 0 else "bearish",
            "cross":     cross,
        }
    except Exception:
        return _macd_cache


_rsi_cache: dict = {"rsi": 50.0, "divergence": "none"}
# _calc_rsi siehe Import aus pt_indicators (oben)

def _detect_rsi_divergence(df: pd.DataFrame) -> str:
    """
    Bearishe Divergenz: Preis macht höheres Hoch, RSI nicht → 'bearish'.
    Bullishe Divergenz: Preis macht tieferes Tief, RSI nicht → 'bullish'.
    Schaut auf letzte 20 Kerzen, sucht 2 Swings.
    """
    try:
        closes = list(df["close"].values[-30:])
        highs  = list(df["high"].values[-30:])
        lows   = list(df["low"].values[-30:])
        if len(closes) < 20:
            return "none"
        # RSI der letzten 30 Kerzen berechnen
        rsi_vals = []
        for i in range(14, len(closes)):
            sub = pd.DataFrame({"close": closes[:i+1]})
            rsi_vals.append(_calc_rsi(sub))
        if len(rsi_vals) < 6:
            return "none"
        # Swing-Highs: Pivot mit Fenster 3
        def pivots_high(arr):
            return [i for i in range(2, len(arr)-2) if arr[i] >= max(arr[i-2:i+3])]
        def pivots_low(arr):
            return [i for i in range(2, len(arr)-2) if arr[i] <= min(arr[i-2:i+3])]
        # Preise relativ zum RSI-Array (offset 14)
        h_arr  = highs[14:]
        l_arr  = lows[14:]
        ph = pivots_high(h_arr)
        pl = pivots_low(l_arr)
        # Bearishe Divergenz: letzten 2 Swing-Highs
        if len(ph) >= 2:
            p1, p2 = ph[-2], ph[-1]
            if h_arr[p2] > h_arr[p1] and rsi_vals[p2] < rsi_vals[p1] - 2:
                return "bearish"
        # Bullishe Divergenz: letzte 2 Swing-Lows
        if len(pl) >= 2:
            p1, p2 = pl[-2], pl[-1]
            if l_arr[p2] < l_arr[p1] and rsi_vals[p2] > rsi_vals[p1] + 2:
                return "bullish"
        return "none"
    except Exception:
        return "none"


_funding_cache: dict = {"rate": 0.0, "ts": 0.0}
_FUNDING_TTL = 14_400   # 4h (Funding settlt nur alle 8h → seltener abfragen)

def _fetch_funding_rate() -> float:
    """
    Binance Perpetual Funding Rate für SOLUSDT.
    Positiv → Longs zahlen Shorts → Markt ist Long-Heavy → bärischer Druck.
    Negativ → Shorts zahlen Longs → Markt ist Short-Heavy → bullischer Druck.
    """
    global _funding_cache
    if time.time() - _funding_cache["ts"] < _FUNDING_TTL:
        return _funding_cache["rate"]
    try:
        r = _http.get(
            f"{BINANCE_FUTURES}/premiumIndex",
            params={"symbol": SYMBOL},
            timeout=8,
        )
        r.raise_for_status()
        rate = float(r.json().get("lastFundingRate", 0.0))
        _funding_cache = {"rate": rate, "ts": time.time()}
        return rate
    except Exception:
        return _funding_cache["rate"]


_oi_cache: dict = {"oi": 0.0, "oi_prev": 0.0, "ts": 0.0}
_OI_TTL = 3_600

def _fetch_open_interest() -> dict:
    """
    Open Interest von Binance Futures.
    OI steigt + Preis steigt = echte Stärke (neue Longs).
    OI fällt  + Preis steigt = Short-Covering (schwächer, Reversal-Risiko).
    """
    global _oi_cache
    if time.time() - _oi_cache["ts"] < _OI_TTL:
        return _oi_cache
    try:
        r = _http.get(
            f"{BINANCE_FUTURES}/openInterest",
            params={"symbol": SYMBOL},
            timeout=8,
        )
        r.raise_for_status()
        oi_new = float(r.json().get("openInterest", 0.0))
        oi_prev = _oi_cache["oi"] or oi_new
        _oi_cache = {"oi": oi_new, "oi_prev": oi_prev, "ts": time.time()}
        return _oi_cache
    except Exception:
        return _oi_cache


def _get_session(hour: int) -> tuple:
    """
    Gibt Trading Session + Quality Multiplier zurück.
    London Open + NY Open sind historisch die besten Zeitfenster.
    """
    if 7 <= hour <= 9:
        return "london_open", 1.25      # Höchste Breakout-Qualität
    if 13 <= hour <= 16:
        return "london_ny_overlap", 1.15  # Zweithöchstes Volumen
    if 17 <= hour <= 19:
        return "new_york", 1.05
    if 10 <= hour <= 12:
        return "london", 1.0
    if 0 <= hour <= 6:
        return "asian", 0.85            # Konsolidierung, mehr False Signals
    return "off_hours", 0.90


_weekly_trend_cache: dict = {"bias": "neutral", "ts": 0.0}
_WEEKLY_TTL = 14_400  # 4h

def _get_weekly_trend() -> str:
    """EMA10 vs EMA20 auf 1W-Kerzen — übergeordneter Struktur-Bias."""
    global _weekly_trend_cache
    if time.time() - _weekly_trend_cache["ts"] < _WEEKLY_TTL:
        return _weekly_trend_cache["bias"]
    try:
        r = _http.get(
            f"{BINANCE_BASE}/klines",
            params={"symbol": SYMBOL, "interval": "1w", "limit": 25},
            timeout=10,
        )
        r.raise_for_status()
        closes = [float(c[4]) for c in r.json()]
        if len(closes) < 20:
            return _weekly_trend_cache["bias"]
        ema10 = sum(closes[-10:]) / 10
        ema20 = sum(closes[-20:]) / 20
        bias  = "bullish" if ema10 > ema20 else "bearish"
        _weekly_trend_cache = {"bias": bias, "ts": time.time()}
        return bias
    except Exception:
        return _weekly_trend_cache["bias"]


_news_cache: dict = {"events": [], "ts": 0.0}
_NEWS_TTL = 6 * 3_600   # 6h

def _fetch_news_events() -> list:
    """Holt hochwertige USD-Events von ForexFactory (wöchentliches JSON)."""
    global _news_cache
    if time.time() - _news_cache["ts"] < _NEWS_TTL:
        return _news_cache["events"]
    try:
        r = _http.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        events = [e for e in r.json() if e.get("impact") == "High" and e.get("country") in ("USD", "US")]
        _news_cache = {"events": events, "ts": time.time()}
        return events
    except Exception:
        return _news_cache["events"]

def _is_news_blocked() -> bool:
    """
    Gibt True zurück wenn ein High-Impact USD-Event innerhalb ±2 Stunden liegt.
    Verhindert Trading in hochvolatilen Nachrichten-Fenstern.
    """
    try:
        events = _fetch_news_events()
        now    = datetime.now(timezone.utc)
        for e in events:
            try:
                date_str = e.get("date", "")
                time_str = e.get("time", "12:00am")
                # ForexFactory gibt Zeit als "2:00pm" etc.
                dt_str = f"{date_str} {time_str}"
                event_dt = datetime.strptime(dt_str, "%Y-%m-%d %I:%M%p").replace(tzinfo=timezone.utc)
                diff_h = abs((now - event_dt).total_seconds()) / 3600
                if diff_h <= 2.0:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _get_portfolio_heat(state) -> float:
    """Gesamtes Risiko aller offenen Positionen als % der Balance."""
    total = 0.0
    for p in state.positions:
        sl_dist = abs(p.get("entry", 0) - p.get("sl", 0))
        if sl_dist > 0:
            total += (sl_dist * p.get("size", 0)) / state.balance
    return round(total, 4)


def _optimal_tp_r(trades: list) -> float:
    """
    Schätzt das optimale R-Vielfache für TP basierend auf historischen MFE-Daten.
    Verwendet 65. Perzentil der MFE/SL-Ratio aus gewonnenen Trades.
    """
    try:
        won = [t for t in trades[-150:] if t.get("pnl", 0) > 0 and t.get("mfe_pct") and t.get("rr")]
        if len(won) < 15:
            return 2.0
        # MFE in R approximieren: mfe_pct / (rr * sl_pct) -- nutze rr als Proxy
        # Vereinfacht: MFE / TP-Distanz approximiert als mfe_pct / (rr * avg_risk_pct)
        mfe_r_vals = sorted([t["mfe_pct"] / max(t["rr"], 0.5) for t in won])
        idx = int(len(mfe_r_vals) * 0.65)
        optimal = mfe_r_vals[idx]
        return round(max(1.5, min(4.0, optimal)), 2)
    except Exception:
        return 2.0


def _detect_liquidity_sweep(df: pd.DataFrame, bias: str) -> str:
    """
    Erkennt Liquiditäts-Sweeps: EQH/EQL-Level wurde mit einem Wick überschritten
    aber der Schlusskurs blieb darunter/darüber.
    Bullisher Sweep: letzter Wick unter Swing-Low, Close darüber → Kaufsignal.
    Bärischer Sweep: letzter Wick über Swing-High, Close darunter → Verkaufssignal.
    """
    try:
        if len(df) < 20:
            return "none"
        last = df.iloc[-1]
        lookback = df.iloc[-20:-1]
        swing_high = lookback["high"].max()
        swing_low  = lookback["low"].min()
        # Bullisher Sweep: Wick unter Swing-Low aber Close darüber
        if last["low"] < swing_low and last["close"] > swing_low:
            return "bullish"
        # Bärischer Sweep: Wick über Swing-High aber Close darunter
        if last["high"] > swing_high and last["close"] < swing_high:
            return "bearish"
        return "none"
    except Exception:
        return "none"


def _kelly_mult(trades: list) -> float:
    """
    Half-Kelly Criterion als Risiko-Multiplikator.
    Braucht mindestens 20 abgeschlossene Trades.
    Gibt Wert in [MIN_RISK_MULT, MAX_RISK_MULT] zurück.
    """
    recent = [t for t in trades[-100:] if isinstance(t.get("pnl"), (int, float))]
    if len(recent) < 20:
        return 1.0
    wins   = [t["pnl"] for t in recent if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in recent if t["pnl"] < 0]
    if not wins or not losses:
        return 1.0
    wr      = len(wins) / len(recent)
    rr      = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
    kelly   = wr - (1 - wr) / rr           # volle Kelly-Fraction
    half_k  = max(0.0, kelly * 0.5)        # Half-Kelly für Sicherheit
    # Normiert: half_k=0→0.5x, 0.083→1.0x, 0.25+→2.0x
    return round(max(MIN_RISK_MULT, min(MAX_RISK_MULT, 0.5 + half_k * 6)), 3)


# ── State ─────────────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.balance:            float          = INITIAL_BAL
        self.positions:          list[dict]     = []
        self.trades:             list[dict]     = []
        self.total_trades:       int            = 0
        self.wins:               int            = 0
        self.losses:             int            = 0
        self.consecutive_losses: int            = 0
        self.consecutive_wins:   int            = 0
        self.peak_balance:       float          = INITIAL_BAL
        self.max_drawdown:       float          = 0.0
        self.evolution_pending:  bool           = False
        self.setup_cooldowns:    dict           = {}   # setup_type → last_loss_ts
        self.daily_pnl:          float          = 0.0  # Tages-PnL für Circuit-Breaker
        self.daily_date:         str            = ""   # "YYYY-MM-DD" des aktuellen Tages
        self.started_at:         str            = datetime.now(timezone.utc).isoformat()

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return self.wins / decided * 100 if decided else 0.0

    @property
    def profit_factor(self) -> float:
        gross_win  = sum(t["pnl"] for t in self.trades if t["pnl"] > 0)
        gross_loss = sum(-t["pnl"] for t in self.trades if t["pnl"] < 0)
        return gross_win / gross_loss if gross_loss > 0 else (9.99 if gross_win > 0 else 0.0)

    @property
    def unrealized_pnl(self) -> Optional[float]:
        if not self.positions:
            return None
        price = _fetch_price()
        if price is None:
            return None
        total = 0.0
        for p in self.positions:
            if p["direction"] == "long":
                total += (price - p["entry"]) * p["size"]
            else:
                total += (p["entry"] - price) * p["size"]
        return round(total, 2)

    def save(self) -> None:
        weights = learning_engine.load_weights()
        data = {
            "balance":            self.balance,
            "peak_balance":       self.peak_balance,
            "max_drawdown":       self.max_drawdown,
            "total_trades":       self.total_trades,
            "wins":               self.wins,
            "losses":             self.losses,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins":   self.consecutive_wins,
            "evolution_pending":  self.evolution_pending,
            "setup_cooldowns":    self.setup_cooldowns,
            "daily_pnl":          self.daily_pnl,
            "daily_date":         self.daily_date,
            "win_rate":           round(self.win_rate, 2),
            "profit_factor":      round(self.profit_factor, 3),
            "positions":          self.positions,
            "started_at":         self.started_at,
            "updated_at":         datetime.now(timezone.utc).isoformat(),
            "signal_weights":     weights,
        }
        existing = {}
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass
        existing.pop("position", None)   # Altes Einzelpositions-Feld entfernen
        existing.update(data)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self.balance            = float(data.get("balance",            INITIAL_BAL))
            self.peak_balance       = float(data.get("peak_balance",       self.balance))
            self.max_drawdown       = float(data.get("max_drawdown",       0.0))
            self.total_trades       = int(data.get("total_trades",         0))
            self.wins               = int(data.get("wins",                 0))
            self.losses             = int(data.get("losses",               0))
            self.consecutive_losses = int(data.get("consecutive_losses",   0))
            self.consecutive_wins   = int(data.get("consecutive_wins",     0))
            self.evolution_pending  = bool(data.get("evolution_pending",   False))
            self.setup_cooldowns    = dict(data.get("setup_cooldowns",     {}))
            self.daily_pnl          = float(data.get("daily_pnl",         0.0))
            self.daily_date         = str(data.get("daily_date",          ""))
            raw = data.get("positions")
            if raw is None:
                old = data.get("position")
                raw = [old] if old else []
            self.positions = raw
            self.started_at         = data.get("started_at", self.started_at)
        except Exception as e:
            _log_error(f"State.load: {e}")
        # Restore trades list from trades.json so profit_factor survives restart
        if TRADES_JSON.exists():
            try:
                with open(TRADES_JSON, encoding="utf-8") as f:
                    self.trades = json.load(f)
            except Exception:
                pass


# ── Binance-Helpers ───────────────────────────────────────────────────────────
def _fetch_ohlcv() -> Optional[pd.DataFrame]:
    try:
        r = _http.get(
            f"{BINANCE_BASE}/klines",
            params={"symbol": SYMBOL, "interval": INTERVAL, "limit": CANDLES},
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "time","open","high","low","close","volume",
            "close_time","quote_vol","trades","taker_buy_base","taker_buy_quote","ignore"
        ])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
        return df[["time","open","high","low","close","volume"]].copy()
    except Exception as e:
        _log_error(f"_fetch_ohlcv: {e}")
        return None


def _fetch_price() -> Optional[float]:
    try:
        r = _http.get(
            f"{BINANCE_BASE}/ticker/price", params={"symbol": SYMBOL}, timeout=8
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


# ── Signal-Selektion ──────────────────────────────────────────────────────────
def _score_signal(row: dict, market_bias: str, hourly_perf: dict,
                  weights: dict, sig_map: dict) -> float:
    """
    Composite-Score für ein Signal-Kandidat.
    Kombiniert: Konfidenz + Backtest-Muster + Markt-Bias + Tagesstunde + Lerngewichte.
    Dynamische Anpassungen nutzen gelernte Win-Rates aus backtest_weights.json.
    """
    import backtest_learner

    # Gelernte Kontext-Performance laden (markt-bias, fear/greed, ADX-Regime)
    _bw_meta: dict = {}
    try:
        if backtest_learner.WEIGHTS_FILE.exists():
            import json as _j
            with open(backtest_learner.WEIGHTS_FILE, encoding="utf-8") as _f:
                _bw_meta = _j.load(_f)
    except Exception:
        pass
    _learned_mkt_perf    = _bw_meta.get("market_bias_performance",   {})
    _learned_fg_perf     = _bw_meta.get("fear_greed_performance",    {})
    _learned_adx_perf    = _bw_meta.get("adx_bucket_performance",    {})
    _learned_atr_perf    = _bw_meta.get("atr_bucket_performance",    {})
    _learned_combo_perf  = _bw_meta.get("trigger_combo_performance",  {})
    _learned_mfe_mae     = _bw_meta.get("mfe_mae_analysis",           {})
    _learned_setup_adx   = _bw_meta.get("setup_adx_performance",      {})
    _learned_tf_perf     = _bw_meta.get("timeframe_performance",      {})
    _learned_mtf_perf    = _bw_meta.get("mtf_alignment_performance",  {})

    src = (row.get("source") or "LIVE").upper()
    if src == "LIVE":
        base = float(row.get("confidence_score") or 0.0) * 100
    else:
        base = float(row.get("algo_score") or 50.0)

    # Backtest pattern score (lernbasiert)
    setup = row.get("setup_type", "Unknown")
    tf    = row.get("timeframe",  "4h")
    bias  = row.get("bias",       "neutral")
    zone  = row.get("zone_position", "neutral")
    hour  = datetime.now(timezone.utc).hour
    bt_score, bt_samples = backtest_learner.get_score(setup, tf, bias, zone, hour)

    # Blend: je mehr Backtest-Daten, desto stärker ihr Einfluss (max 40%)
    blend     = min(bt_samples / 150, 0.40)
    composite = base * (1 - blend) + bt_score * blend

    # Markt-Bias Alignment — gelernte Werte wenn verfügbar, sonst Defaults
    if market_bias != "neutral" and bias != "neutral":
        mkt_data = _learned_mkt_perf.get(market_bias, {})
        if mkt_data.get("samples", 0) >= 8:
            # Gelernte Win-Rate: Abweichung von 50% × 30 = max ±15 Punkte
            mkt_delta = (float(mkt_data["win_rate"]) - 0.5) * 30.0
            if market_bias == bias:
                composite += max(4.0, min(16.0, mkt_delta + 8.0))
            else:
                composite -= max(8.0, min(20.0, abs(mkt_delta) + 10.0))
        else:
            if market_bias == bias:
                composite += 10.0
            else:
                composite -= 15.0  # Kontra-Trend-Strafe (Default)

    # Tagesstunden-Faktor (aus backtest_weights.json hourly_performance)
    hb = str((hour // 3) * 3)
    hp = hourly_perf.get(hb, {})
    if hp.get("samples", 0) >= 5:
        hr_wr = float(hp.get("win_rate", 0.5))
        if hr_wr < 0.35:
            composite -= 20.0   # statistisch schlechte Stunde
        elif hr_wr > 0.65:
            composite += 8.0    # statistisch gute Stunde

    # 1D-Trend Confluence (+8 Agree / -12 Contra)
    daily_trend  = _get_daily_trend()
    signal_bias  = row.get("bias", "neutral")
    if daily_trend != "neutral" and signal_bias != "neutral":
        if daily_trend == signal_bias:
            composite += 8.0
        else:
            composite -= 12.0

    # 1H-Trend Confluence: kurzfristige Bestätigung (+5 / -8)
    h1_trend = _get_1h_trend()
    if h1_trend != "neutral" and signal_bias != "neutral":
        if h1_trend == signal_bias:
            composite += 5.0
        else:
            composite -= 8.0

    # ADX Regime: gelernte Werte aus adx_bucket_performance wenn verfügbar
    adx = _adx_cache.get("value", 25.0)
    adx_bucket = "trending" if adx > 28 else "ranging" if adx < 18 else "moderate"
    adx_data = _learned_adx_perf.get(adx_bucket, {})
    if adx_data.get("samples", 0) >= 8:
        adx_wr_delta = (float(adx_data["win_rate"]) - 0.5) * 28.0  # max ±14 Punkte
        composite += round(adx_wr_delta, 1)
    else:
        if   adx > 35: composite += 8.0
        elif adx > 25: composite += 3.0
        elif adx < 20: composite -= 12.0  # Seitwärtsmarkt = viele Fehlsignale

    # Wochenend-Penalty: niedrigere Liquidität → mehr False Breakouts
    if datetime.now(timezone.utc).weekday() >= 5:
        composite -= 12.0

    # Multi-Signal-Confluence: Bonus für viele bestätigende Trigger
    try:
        triggers = json.loads(row.get("all_triggers") or "[]")
        n = len(triggers)
        if   n >= 4: composite += 15.0
        elif n >= 3: composite += 7.0
        elif n == 1: composite -= 12.0   # Einzelsignal: zu riskant
        elif n == 0: composite -= 20.0

        # Lerngewichte (Bonus für gut trainierte Trigger-Kombinationen)
        w_bonus = sum(weights.get(sig_map.get(t.upper(), ""), 1.0) - 1.0
                      for t in triggers) * 5.0
        composite += w_bonus
    except Exception:
        pass

    # Fear & Greed — gelernte Bucket-Performance wenn verfügbar
    try:
        fg = _fetch_fear_greed()
        fg_bucket = (
            "extreme_fear" if fg < 25 else
            "fear"         if fg < 45 else
            "greed"        if fg > 75 else
            "neutral_fg"
        )
        fg_data = _learned_fg_perf.get(fg_bucket, {})
        if fg_data.get("samples", 0) >= 8:
            fg_wr_delta = (float(fg_data["win_rate"]) - 0.5) * 28.0
            composite += round(fg_wr_delta, 1)
        else:
            if   fg < 20: composite -= 12.0   # Extreme Fear: Panik-Ausverkäufe
            elif fg < 35: composite -=  6.0   # Fear: erhöhte Volatilität
            elif fg > 80: composite -= 10.0   # Extreme Greed: überkauft
            elif fg > 65: composite +=  8.0   # Greed: gesundes Momentum
    except Exception:
        pass

    # Volumen-Bestätigung: starkes Volumen = Überzeugung, schwaches Volumen = False Signal
    vol_ratio = _vol_cache.get("ratio", 1.0)
    if   vol_ratio > 1.8: composite += 10.0   # starkes Volumen → sehr überzeugend
    elif vol_ratio > 1.3: composite +=  5.0   # überdurchschnittlich
    elif vol_ratio < 0.6: composite -=  8.0   # geringes Volumen → Manipulation möglich
    elif vol_ratio < 0.8: composite -=  4.0   # unterdurchschnittlich

    # Signal-Alter-Decay: ältere Signale linear bestrafen (max -18 bei MAX_SIGNAL_AGE_H)
    created_str = row.get("created_at") or row.get("timestamp") or ""
    if created_str:
        try:
            ts = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            composite -= min(18.0, (age_h / MAX_SIGNAL_AGE_H) * 18.0)
        except Exception:
            pass

    # Synthetisierte Strategie-Regeln (aus Erfahrung gelernt)
    rules_mod = _apply_strategy_rules(row, hour)
    if rules_mod != 0.0:
        composite += rules_mod

    # ── Neue Indikatoren ───────────────────────────────────────────────────────

    # MACD Confluence
    macd = _macd_cache
    if macd["cross"] == "golden" and bias == "bullish":
        composite += 12.0   # Golden Cross bestätigt Long
    elif macd["cross"] == "dead" and bias == "bearish":
        composite += 12.0   # Dead Cross bestätigt Short
    elif macd["trend"] == "bullish" and bias == "bullish":
        composite += 5.0
    elif macd["trend"] == "bearish" and bias == "bearish":
        composite += 5.0
    elif macd["trend"] != bias.replace("neutral", ""):
        composite -= 7.0    # MACD widerspricht Signal-Bias

    # RSI Divergenz
    rsi_div = _rsi_cache.get("divergence", "none")
    if rsi_div == "bullish" and bias == "bullish":
        composite += 10.0   # Bullishe Divergenz bestätigt Long
    elif rsi_div == "bearish" and bias == "bearish":
        composite += 10.0   # Bärishe Divergenz bestätigt Short
    elif rsi_div == "bullish" and bias == "bearish":
        composite -= 8.0    # Divergenz widerspricht Bias
    elif rsi_div == "bearish" and bias == "bullish":
        composite -= 8.0

    # Funding Rate (Crypto-spezifisch)
    fr = _funding_cache.get("rate", 0.0)
    if fr > 0.001:         # Stark positiv: Markt ist Long-Heavy → bärischer Druck
        composite -= 8.0 if bias == "bullish" else 3.0
    elif fr > 0.0005:
        composite -= 3.0 if bias == "bullish" else 0.0
    elif fr < -0.001:      # Stark negativ: Short-Heavy → bullischer Druck
        composite -= 8.0 if bias == "bearish" else 3.0
    elif fr < -0.0005:
        composite -= 3.0 if bias == "bearish" else 0.0

    # Open Interest Bestätigung
    oi_data = _oi_cache
    if oi_data["oi"] > 0 and oi_data["oi_prev"] > 0:
        oi_change = (oi_data["oi"] - oi_data["oi_prev"]) / oi_data["oi_prev"]
        # OI steigt + Preis steigt → echte Stärke → Bonus für Long
        if oi_change > 0.01 and bias == "bullish":
            composite += 7.0
        elif oi_change > 0.01 and bias == "bearish":
            composite -= 5.0   # Shorts gegen starken OI-Aufbau
        elif oi_change < -0.01 and bias == "bearish":
            composite += 5.0   # OI fällt + bärisch = Short-Covering läuft
        elif oi_change < -0.01 and bias == "bullish":
            composite -= 4.0

    # Trading Session Qualität
    session, sq = _get_session(hour)
    if sq > 1.0:
        composite *= sq          # London/NY-Open: bis +25% Score
    elif sq < 1.0:
        composite += (sq - 1.0) * 15   # Asian/Off-Hours: bis -15 Punkte

    # Wöchentlicher Trend (Top-Down Analyse)
    weekly = _get_weekly_trend()
    if weekly != "neutral" and bias != "neutral":
        if weekly == bias:
            composite += 6.0    # Alle 3 Timeframes aligned: W+D+Signal
        else:
            composite -= 8.0    # Weekly widerspricht Signal

    # EMA200 Distanz — Overextension-Schutz
    ema200_dist = float(row.get("ema200_dist_pct") or 0.0)
    if ema200_dist > 0:   # Preis über EMA200
        if ema200_dist > 20.0:
            composite -= 12.0   # Stark überkauft → hohes Umkehr-Risiko
        elif ema200_dist > 12.0:
            composite -= 6.0
    elif ema200_dist < -20.0:   # Stark überverkauft
        composite -= 12.0 if bias == "bearish" else 0.0
    elif ema200_dist < -12.0:
        composite -= 6.0 if bias == "bearish" else 0.0

    # Liquiditäts-Sweep Bonus (Einstieg NACH dem Sweep)
    sweep = _rsi_cache.get("sweep", "none")
    if sweep == "bullish" and bias == "bullish":
        composite += 14.0   # Sweep von Tief + Long = hohe Wahrscheinlichkeit
    elif sweep == "bearish" and bias == "bearish":
        composite += 14.0   # Sweep von Hoch + Short = hohe Wahrscheinlichkeit

    # POI-Alignment: Signal nahe einer gelernten Hochwahrscheinlichkeits-Zone
    try:
        entry_price = float(row.get("entry_price") or 0.0)
        if entry_price > 0:
            poi_boost = poi_tracker.get_score_boost(entry_price, bias)
            if poi_boost > 0:
                composite += poi_boost
    except Exception:
        pass

    # ATR-Bucket-Performance: gelernte Volatilitätsregime-Wirkung
    try:
        atr_p = float(row.get("atr_pct") or 0.0)
        if atr_p > 0:
            atr_bkt = "high_vol" if atr_p > 3.0 else "low_vol" if atr_p < 1.0 else "normal_vol"
            atr_data = _learned_atr_perf.get(atr_bkt, {})
            if atr_data.get("samples", 0) >= 5:
                atr_wr_delta = (float(atr_data["win_rate"]) - 0.5) * 20.0  # max ±10 Punkte
                composite += round(atr_wr_delta, 1)
    except Exception:
        pass

    # Trigger-Kombinations-Bonus: gelernte Win-Rate für diese Signalkombination
    try:
        triggers_raw = row.get("all_triggers") or "[]"
        triggers_list = json.loads(triggers_raw) if isinstance(triggers_raw, str) else triggers_raw
        combo_key = ",".join(sorted(str(t).upper() for t in triggers_list)) if triggers_list else "_none"
        combo_data = _learned_combo_perf.get(combo_key, {})
        if combo_data.get("samples", 0) >= 5:
            combo_wr_delta = (float(combo_data["win_rate"]) - 0.5) * 24.0  # max ±12 Punkte
            composite += round(combo_wr_delta, 1)
    except Exception:
        pass

    # MFE/MAE-Qualität: schlechtes Reward/Risk-Profil historisch → Malus
    try:
        mm_data = _learned_mfe_mae.get(setup, {})
        if mm_data.get("samples", 0) >= 5:
            mfe_mae_ratio = float(mm_data.get("mfe_mae_ratio", 1.0))
            if mfe_mae_ratio < 0.8:
                composite -= 10.0   # durchschnittlich schlechtes MFE/MAE-Verhältnis
            elif mfe_mae_ratio > 2.0:
                composite += 6.0    # durchschnittlich gutes MFE/MAE-Verhältnis
    except Exception:
        pass

    # Setup × ADX Kreuz-Performance: spezifische Kombination gelernt
    try:
        adx_bkt_now = "trending" if _adx_cache.get("value", 25.0) > 28 else \
                      "ranging"  if _adx_cache.get("value", 25.0) < 18 else "moderate"
        sa_key = f"{setup}|{adx_bkt_now}"
        sa_data = _learned_setup_adx.get(sa_key, {})
        if sa_data.get("samples", 0) >= 5:
            sa_wr_delta = (float(sa_data["win_rate"]) - 0.5) * 22.0  # max ±11 Punkte
            composite += round(sa_wr_delta, 1)
    except Exception:
        pass

    # Timeframe-Performance: gelernte Win-Rate des Signal-Timeframes
    try:
        tf_data = _learned_tf_perf.get(tf, {})
        if tf_data.get("samples", 0) >= 8:
            tf_wr_delta = (float(tf_data["win_rate"]) - 0.5) * 20.0  # max ±10 Punkte
            composite += round(tf_wr_delta, 1)
    except Exception:
        pass

    # MTF-Alignment: HTF-Bestätigung des Signals (gelernt wenn Daten da, sonst Default)
    try:
        mtf_a = row.get("mtf_alignment")
        if mtf_a is not None:
            mtf_a  = int(mtf_a)
            a_bkt  = "aligned" if mtf_a >= 1 else "contra" if mtf_a <= -1 else "neutral_mtf"
            a_data = _learned_mtf_perf.get(a_bkt, {})
            if a_data.get("samples", 0) >= 8:
                mtf_wr_delta = (float(a_data["win_rate"]) - 0.5) * 26.0  # max ±13 Punkte
                composite += round(mtf_wr_delta, 1)
            else:
                # Default bis genug gelernt: pro HTF ±(6/9) Punkte
                composite += mtf_a * 6.0 if mtf_a > 0 else mtf_a * 9.0
    except Exception:
        pass

    # Bull Run Phase: Score-Modifier für Longs und Shorts je nach Marktphase
    try:
        import bull_run_detector as _brd
        _play = _brd.get_playbook()
        _bias = row.get("bias", "neutral")
        _zone = row.get("zone_position", "neutral")
        if _bias == "bullish":
            composite += float(_play.get("signal_score_boost", 0.0))
            if _zone == "discount":
                composite += float(_play.get("discount_boost", 0.0))
            elif _zone == "premium":
                composite += float(_play.get("premium_penalty", 0.0))
        elif _bias == "bearish":
            # Shorts in Bullenphasen abwerten (nicht blockieren — das macht _get_db_signal)
            composite -= float(_play.get("signal_score_boost", 0.0)) * 0.5
    except Exception:
        pass

    return composite


def _get_db_signal(state: Optional["State"] = None) -> Optional[dict]:
    """
    Holt das beste handelbare Signal aus signals.db.
    Wählt nicht einfach das erste, sondern das Signal mit dem höchsten
    Composite-Score (Konfidenz + Backtest + Markt-Bias + Stunde + Lerngewichte).
    """
    candidates = signal_logger.get_tradeable_signals(max_age_hours=MAX_SIGNAL_AGE_H)
    if not candidates:
        return None

    current_price = _fetch_price()
    # Ohne Live-Preis kein realistischer Entry-Check → diesen Zyklus nicht traden
    if current_price is None or current_price <= 0:
        return None

    # Browser-evolved MIN_RR überschreiben wenn vorhanden
    sp          = _load_strategy_params()
    dynamic_rr  = float(sp.get("rr", MIN_RR))
    dynamic_rr  = max(1.5, min(3.0, dynamic_rr))

    # Entry-Gates aus strategy_params ladbar (evolvierbar), auf sichere Range geclamped
    zone_frac   = max(0.10, min(0.60, float(sp.get("entry_zone_frac",  ENTRY_ZONE_FRAC))))
    chase_frac  = max(0.02, min(0.40, float(sp.get("entry_chase_frac", ENTRY_CHASE_FRAC))))

    # Dynamischer MIN_RR: nach schlechter Phase verschärfen, nach guter Phase lockern
    if state:
        recent_20 = [t for t in state.trades[-20:] if isinstance(t.get("pnl"), (int, float))]
        if len(recent_20) >= 10:
            recent_wr = sum(1 for t in recent_20 if t["pnl"] > 0) / len(recent_20)
            if recent_wr < 0.40:
                dynamic_rr = min(3.0, dynamic_rr + 0.1)   # Schlechte Phase → RR erhöhen
            elif recent_wr > 0.60:
                dynamic_rr = max(1.5, dynamic_rr - 0.1)   # Gute Phase → RR leicht lockern

    # Markt-Kontext für Scoring laden
    market_bias  = "neutral"
    hourly_perf: dict = {}
    try:
        import web_researcher
        market_bias = web_researcher.get_market_bias()
    except Exception:
        pass
    try:
        import json as _json, backtest_learner
        _wf = backtest_learner.WEIGHTS_FILE
        if _wf.exists():
            with open(_wf, encoding="utf-8") as _f:
                _bw = _json.load(_f)
            hourly_perf = _bw.get("hourly_performance", {})
    except Exception:
        pass

    weights = learning_engine.load_weights()
    sig_map = {
        "BOS": "bos", "CHOCH": "choch", "FVG": "fvg", "OB": "order_block",
        "EQH": "eqh", "EQL": "eql", "DISCOUNT": "discount_zone", "PREMIUM": "premium_zone",
    }

    scored: list[tuple[float, dict]] = []

    # Setup-Cooldown-Map: kein Handel für 4h nach 3 Verlusten in Folge mit diesem Setup
    SETUP_COOLDOWN_S = 4 * 3600
    SETUP_LOSS_THRESHOLD = 3
    now_ts = time.time()
    active_cooldowns: set = set()
    if state:
        cooldowns = state.setup_cooldowns
        active_cooldowns = {
            s for s, last_ts in cooldowns.items()
            if not s.endswith("_count")
            and isinstance(last_ts, (int, float))
            and (now_ts - last_ts) < SETUP_COOLDOWN_S
            and cooldowns.get(f"{s}_count", 0) >= SETUP_LOSS_THRESHOLD
        }

    # Dedup-Set: kein erneuter Eintritt in Setup+Bias innerhalb DEDUP_HOURS
    dedup_key_cutoff = now_ts - DEDUP_HOURS * 3600
    recently_traded: set = set()
    open_sig_ids:    set = set()
    open_combos:     set = set()
    if state:
        for t in state.trades[-15:]:
            try:
                closed_ts = datetime.fromisoformat(
                    (t.get("closed_at") or t.get("opened_at") or "")).timestamp()
                if closed_ts >= dedup_key_cutoff:
                    recently_traded.add((t.get("setup_type", ""), t.get("direction", "")))
            except Exception:
                pass
        for pos in state.positions:
            if pos.get("signal_id"):
                open_sig_ids.add(pos["signal_id"])
            open_combos.add((pos.get("setup_type", ""), pos.get("direction", "")))

    for row in candidates:
        src   = (row.get("source") or "LIVE").upper()
        entry = row.get("entry_price", 0.0) or 0.0
        sl    = row.get("sl_price",    0.0) or 0.0
        tp    = row.get("tp_price",    0.0) or 0.0
        if entry <= 0 or sl <= 0 or tp <= 0:
            continue
        sl_dist = abs(entry - sl)
        if sl_dist < 1e-6:
            continue
        rr = abs(tp - entry) / sl_dist
        if rr < dynamic_rr:
            continue

        # Setup-Cooldown-Filter: überspringe Setups mit zu vielen kürzlichen Verlusten
        setup_type = row.get("setup_type", "Unknown")
        if setup_type in active_cooldowns:
            continue

        # Dedup-Filter: kein erneuter Eintritt in gleiches Setup+Richtung innerhalb DEDUP_HOURS
        bias      = row.get("bias", "neutral")
        direction = "long" if bias == "bullish" else "short"
        if (setup_type, direction) in recently_traded:
            continue

        # Offene-Positionen-Filter: keine Duplikate laufender Positionen
        if row.get("id") in open_sig_ids:
            continue
        if (setup_type, direction) in open_combos:
            continue

        # ── Entry-Trigger: Preis muss JETZT in der Entry-Zone liegen ──────────
        # Realistisch wie echtes Trading: nur eröffnen, wenn der aktuelle Markt-
        # preis den Entry gerade signalisiert. Ist der Preis bereits Richtung TP
        # weggelaufen, wird das Signal NICHT mehr nachgejagt (kein rückwirkender
        # Trade). Ist er unter die Zone gefallen, ist das Setup ungültig.
        if bias == "bullish":
            if current_price <= sl:                                continue  # Setup ungültig
            if current_price >= tp:                                continue  # Ziel bereits erreicht
            if current_price > entry + sl_dist * chase_frac:       continue  # hochgelaufen → nicht nachjagen
            if current_price < entry - sl_dist * zone_frac:        continue  # zu tief unter der Zone
        elif bias == "bearish":
            if current_price >= sl:                                continue
            if current_price <= tp:                                continue
            if current_price < entry - sl_dist * chase_frac:       continue  # runtergelaufen → nicht nachjagen
            if current_price > entry + sl_dist * zone_frac:        continue  # zu weit über der Zone
        else:
            continue

        # Contra-HTF Hard-Gate: ≤−2 Alignment = starker HTF-Gegenwind → überspringen
        mtf_a = row.get("mtf_alignment")
        if mtf_a is not None and int(float(mtf_a)) <= -2:
            continue

        # Zone-Gate: nacktes Premium/Discount-Zonen-Setup verliert historisch
        # (~16% WR). Nur traden, wenn die höheren Timeframes die Reversal-Richtung
        # bestätigen (Alignment ≥ +1) — sonst überspringen.
        if setup_type == "Zone":
            if mtf_a is None or int(float(mtf_a)) < 1:
                continue

        # Bull Run Short-Gate: kein Short in early_bull / mid_bull
        try:
            import bull_run_detector as _brd
            if not _brd.get_playbook().get("allow_shorts", True):
                if row.get("bias") == "bearish":
                    continue
        except Exception:
            pass

        # Mindest-Konfidenz
        if src == "LIVE":
            if (row.get("confidence_score") or 0.0) < MIN_CONFIDENCE_SCORE:
                continue
        elif src == "ALGO":
            if (row.get("algo_score") or 0.0) < ALGO_MIN_SCORE:
                continue
            if row.get("routing") == "algo_log":
                continue
        else:
            continue

        score = _score_signal(row, market_bias, hourly_perf, weights, sig_map)
        floor = _dynamic_score_floor(state) if state else MIN_SCORE_FLOOR
        if score < floor:
            continue   # Adaptiver Floor: schlechtere Signale bei schlechter Performance ignorieren
        row["_composite_score"] = round(score, 2)
        scored.append((score, row))

    if not scored:
        return None

    # Bestes Signal zurückgeben — Live-Preis als realistischen Fill mitgeben
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_row = scored[0]
    best_row["_live_price"] = current_price
    if len(scored) > 1:
        print(f"  [Selektion] {len(scored)} Kandidaten bewertet — "
              f"gewählt: {best_row.get('setup_type','')} {best_row.get('bias','')} "
              f"Score={best_score:.1f} (Markt={market_bias})")
    return best_row


# ── Trade-Ausführung ──────────────────────────────────────────────────────────
def _open_trade_from_signal(state: State, sig_row: dict) -> None:
    """Öffnet einen Paper-Trade auf Basis einer Signal-Row aus signals.db."""
    if len(state.positions) >= MAX_POSITIONS:
        return

    # News-Block: kein neuer Trade innerhalb ±2h eines High-Impact Events
    if _is_news_blocked():
        print("  [News-Block] High-Impact Event innerhalb 2h — Trade übersprungen")
        return

    # Portfolio Heat: Risiko bei bestehenden Positionen reduzieren
    heat = _get_portfolio_heat(state)
    heat_scale = 1.0
    if heat > 0.02:      # >2% bereits im Feuer
        heat_scale = 0.5
    elif heat > 0.01:    # >1% bereits im Feuer
        heat_scale = 0.75

    sl        = float(sig_row["sl_price"])
    tp        = float(sig_row["tp_price"])
    bias      = sig_row.get("bias", "neutral")
    direction = "long" if bias == "bullish" else "short"

    # ── Realistischer Fill: Eröffnung zum AKTUELLEN Marktpreis ──────────────────
    # Nicht zum (evtl. veralteten) Signal-Entry. SL/TP bleiben die Zielmarken des
    # Signals; R:R und Positionsgröße werden aus dem echten Fill neu berechnet.
    fill = float(sig_row.get("_live_price") or 0.0)
    if fill <= 0:
        fill = _fetch_price() or 0.0
    if fill <= 0:
        print("  [Entry] Kein Live-Preis verfügbar — Trade übersprungen")
        return

    # Slippage: eine Market-Order füllt minimal schlechter als der Live-Kurs —
    # Long kauft etwas teurer, Short verkauft etwas billiger.
    slip  = fill * SLIPPAGE_PCT / 100.0
    entry = fill + slip if direction == "long" else fill - slip
    entry = round(entry, PRICE_DECIMALS)   # auf Tick-Size runden (0.01)

    sl_dist = abs(entry - sl)
    if sl_dist < 1e-6:
        return

    # Fill-Validierung: Preis darf nicht bereits jenseits SL/TP liegen und das
    # reale R:R muss den Mindestwert noch erfüllen — sonst kein Trade.
    if direction == "long":
        if entry <= sl or entry >= tp:
            return
    else:
        if entry >= sl or entry <= tp:
            return
    rr = round(abs(tp - entry) / sl_dist, 2)
    if rr < MIN_RR:
        print(f"  [Entry] Reales R:R {rr:.2f} < {MIN_RR} nach Live-Fill "
              f"@ ${entry:.2f} — übersprungen")
        return

    # Bull Run Phase: phasenbewusste Konfiguration (TP, Risiko, Hold, Trailing)
    try:
        import bull_run_detector as _brd
        _play          = _brd.get_playbook()
        _br_phase      = _brd.get_phase()
        _br_long_mult  = float(_play.get("long_risk_mult",  1.0))
        _br_short_mult = float(_play.get("short_risk_mult", 1.0))
        _br_tp_mult    = float(_play.get("tp_multiplier",   1.0))
        _br_hold_mult  = float(_play.get("max_hold_mult",   1.0))
        _br_trail_pct  = float(_play.get("trail_pct",       0.0))
        _br_use_trail  = bool(_play.get("trailing_stop",    False))
    except Exception:
        _br_phase, _br_long_mult, _br_short_mult = "unknown", 1.0, 1.0
        _br_tp_mult, _br_hold_mult                = 1.0, 1.0
        _br_trail_pct, _br_use_trail              = 0.0, False

    # Dynamische Positionsgröße: skaliert mit Composite-Score
    comp_score = float(sig_row.get("_composite_score", 70.0))
    risk_mult  = max(MIN_RISK_MULT, min(MAX_RISK_MULT, comp_score / 70.0))

    # #4 Drawdown-Schutz: halbes Risiko nach N Verlusten in Folge
    if state.consecutive_losses >= DRAWDOWN_LOSS_TRIGGER:
        risk_mult *= 0.5
        print(f"  [Drawdown-Schutz] {state.consecutive_losses} Verluste in Folge "
              f"→ Risiko-Multiplikator {risk_mult:.2f}x")

    # ATR-Volatilitäts-Skalierung: in volatilen Märkten kleiner rein
    atr = _atr_cache.get("value", 0.0)
    if atr > 0 and entry > 0:
        atr_pct = atr / entry
        if   atr_pct > 0.06:   risk_mult *= 0.70
        elif atr_pct > 0.04:   risk_mult *= 0.85
        elif atr_pct < 0.015:  risk_mult  = min(MAX_RISK_MULT, risk_mult * 1.15)

    # ADX Regime-Skalierung: in Seitwärtsmärkten kleiner rein
    adx = _adx_cache.get("value", 25.0)
    if   adx < 18:  risk_mult *= 0.60   # starkes Ranging → vorsichtig
    elif adx < 22:  risk_mult *= 0.80
    elif adx > 35:  risk_mult  = min(MAX_RISK_MULT, risk_mult * 1.10)  # starker Trend

    # Kelly Criterion: Half-Kelly basierend auf letzten 100 Trades
    kelly = _kelly_mult(state.trades)
    # Blend: 50% Score-basiert, 50% Kelly
    risk_mult = (risk_mult + kelly) / 2.0
    risk_mult = max(MIN_RISK_MULT, min(MAX_RISK_MULT, risk_mult))

    # Smooth Drawdown Scaling: lineares Risiko-Downscaling bei laufendem Drawdown
    dd_pct = state.max_drawdown
    if dd_pct > 0:
        dd_scale = max(0.5, 1.0 - (dd_pct / DD_SCALE_MAX_PCT) * 0.5)
        risk_mult = round(risk_mult * dd_scale, 3)
        if dd_scale < 0.95:
            print(f"  [DD-Scale] Drawdown {dd_pct:.1f}% → Skalierung {dd_scale:.2f}x")

    # Win-Streak-Bonus: nach N Gewinnen in Folge leicht höheres Risiko (Momentum)
    if state.consecutive_wins >= WIN_STREAK_BONUS_MIN:
        risk_mult = min(MAX_RISK_MULT, risk_mult * 1.10)

    risk_mult = max(MIN_RISK_MULT, min(MAX_RISK_MULT, risk_mult))

    risk_mult *= heat_scale
    risk_mult  = max(MIN_RISK_MULT, min(MAX_RISK_MULT, risk_mult))

    # Bull Run Risiko-Skalierung (nach allen anderen Faktoren, vor finalem risk_usd)
    if direction == "long":
        risk_mult *= _br_long_mult
    else:
        risk_mult *= _br_short_mult
    risk_mult = max(MIN_RISK_MULT, min(MAX_RISK_MULT, risk_mult))

    risk_usd = state.balance * RISK_PCT * risk_mult
    size     = risk_usd / sl_dist
    notional = size * entry

    # ── Kapitalgrenze: nicht mehr Kapital einsetzen als frei verfügbar ──────────
    # (MAX_LEVERAGE = 1.0 → Spot: man kann nur so viel SOL kaufen wie Cash da ist).
    open_notional = sum(pp.get("notional", pp["entry"] * pp["size"])
                        for pp in state.positions)
    free_capital  = max(0.0, state.balance * MAX_LEVERAGE - open_notional)
    if free_capital < 10.0:
        print(f"  [Kapital] Nur ${free_capital:.2f} frei (Rest in offenen Positionen) "
              f"— Trade übersprungen")
        return
    if notional > free_capital:
        # Position auf das verfügbare Kapital begrenzen → reales Risiko sinkt mit
        size     = free_capital / entry
        notional = size * entry
        risk_usd = size * sl_dist
        print(f"  [Kapital] Position auf freies Kapital begrenzt: "
              f"${notional:,.2f} (statt voller Risikogröße)")

    # Lot-Size: Stückzahl auf Börsen-Schrittweite ABrunden (man bekommt nie mehr
    # als die nächste handelbare Einheit) und Kennzahlen konsistent neu berechnen.
    size = int(size / LOT_STEP) * LOT_STEP
    size = round(size, 3)
    if size <= 0:
        print("  [Lot-Size] Position zu klein für handelbare Einheit — übersprungen")
        return
    notional = size * entry
    risk_usd = size * sl_dist

    try:
        triggers = json.loads(sig_row.get("all_triggers") or "[]")
    except Exception:
        triggers = [sig_row.get("setup_type") or "SIGNAL"]
    if not triggers:
        triggers = [sig_row.get("setup_type") or "SIGNAL"]

    # Einheitlicher Score: _composite_score (beinhaltet alle Faktoren)
    score = comp_score

    # MFE-optimiertes TP: nur setzen wenn BESSER als Signal-TP, max. 3.0× R (Cap gegen Überoptimierung)
    MAX_TP_R = 3.0
    optimal_r = _optimal_tp_r(state.trades)
    sl_dist_r = abs(entry - sl)
    if optimal_r > 0 and sl_dist_r > 0:
        optimal_r = min(optimal_r, MAX_TP_R)
        tp_optimized = (round(entry + optimal_r * sl_dist_r, 4)
                        if direction == "long"
                        else round(entry - optimal_r * sl_dist_r, 4))
        if direction == "long" and tp_optimized > tp:
            tp = tp_optimized
        elif direction == "short" and tp_optimized < tp:
            tp = tp_optimized

    # Bull Run TP-Erweiterung: im Bullenmarkt weiter mitlaufen lassen
    if _br_tp_mult > 1.0:
        tp_dist = abs(tp - entry)
        if direction == "long":
            tp = round(entry + tp_dist * _br_tp_mult, 4)
        else:
            tp = round(entry - tp_dist * _br_tp_mult, 4)
        rr = round(abs(tp - entry) / sl_dist, 2)

    # Gematchte Strategie-Regeln festhalten → Feedback nach Trade-Close
    rule_sigs = _matched_rule_signatures(sig_row, datetime.now(timezone.utc).hour)

    # Eingesetztes Kapital (Positionswert / Notional) = Stückzahl × Entry-Preis
    notional = round(notional, 2)
    # Entry-Gebühr (Taker) — wird beim Close zusammen mit der Exit-Gebühr verrechnet
    entry_fee = round(notional * TAKER_FEE_PCT / 100.0, 4)

    state.positions.append({
        "direction":      direction,
        "entry":          entry,
        "sl":             sl,
        "tp":             tp,
        "size":           round(size, 4),
        "notional":       notional,
        "entry_fee":      entry_fee,
        "fee_pct":        TAKER_FEE_PCT,
        "risk_usd":       round(risk_usd, 2),
        "risk_mult":      round(risk_mult, 3),
        "score":          score,
        "composite_score":comp_score,
        "triggers":       triggers,
        "zone":           sig_row.get("zone_position", "neutral"),
        "rr":             rr,
        "opened_at":      datetime.now(timezone.utc).isoformat(),
        "signal_id":      sig_row.get("id"),
        "source":         (sig_row.get("source") or "LIVE").upper(),
        "setup_type":     sig_row.get("setup_type", ""),
        "timeframe":      sig_row.get("timeframe", "4h"),
        "rule_signatures": rule_sigs,
        "created_at":     sig_row.get("created_at") or sig_row.get("timestamp") or "",
        # Bull Run Phase-Kontext (persistiert für Trailing Stop und Hold-Zeit)
        "bull_run_phase": _br_phase,
        "max_hold_mult":  round(_br_hold_mult, 2),
        "trail_pct":      _br_trail_pct,
        "use_trail":      _br_use_trail,
        "trail_sl":       None,   # wird beim ersten Trailing-Update gesetzt
    })

    # Signal als aktiv markieren → update_outcomes() überspringt es
    try:
        signal_logger.mark_paper_trading(sig_row["id"])
    except Exception as e:
        _log_error(f"mark_paper_trading: {e}")

    # ADX-Regime zum Eröffnungszeitpunkt speichern → Regime-Lernen
    try:
        signal_logger.update_signal_adx(sig_row["id"], _adx_cache.get("value", 25.0))
    except Exception:
        pass

    state.save()
    print(f"  📈 PAPER TRADE: {direction.upper()} @ ${entry:.2f} "
          f"(Live ${fill:.2f} +Slippage)  "
          f"SL=${sl:.2f}  TP=${tp:.2f}  R:R={rr:.1f}  Score={score:.0f}  "
          f"Kapital=${notional:,.2f} ({size:.3f} SOL)  "
          f"Gebühr=${entry_fee:.2f}  Risiko={risk_mult:.2f}x ({risk_usd:.2f}$)  "
          f"Setup={sig_row.get('setup_type','')}  Signal-ID={sig_row.get('id')}")


def _check_close_one(state: State, p: dict, df: pd.DataFrame) -> Optional[dict]:
    """Per-Position SL/TP-Check — Trade läuft bis SL oder TP getroffen wird."""

    # Alle Kerzen seit Eröffnung prüfen (nicht nur die letzte)
    try:
        opened_ts = pd.Timestamp(p["opened_at"]).tz_localize("utc") if "+" not in p["opened_at"] else pd.Timestamp(p["opened_at"])
        check_df  = df[df["time"] >= opened_ts]
    except Exception:
        check_df  = df.tail(3)   # Fallback: letzte 3 Kerzen

    if check_df.empty:
        check_df = df.tail(1)

    hit_sl = hit_tp = False
    exit_price    = None
    candles_taken = 0
    mfe_pct       = 0.0
    mae_pct       = 0.0

    # SL/TP — eff_sl wird bei aktivem Trailing angepasst, sl bleibt Original-Referenz
    sl  = float(p["sl"])
    tp  = float(p["tp"])

    # Trailing Stop-Konfiguration (aus Bull Run Phase, persistent im Position-Dict)
    use_trail   = bool(p.get("use_trail", False))
    trail_pct   = float(p.get("trail_pct") or 0.0)
    eff_sl      = float(p.get("trail_sl") or sl)   # gespeichertes Trailing-SL (survives ticks)
    trail_active = use_trail and trail_pct > 0
    trail_saved  = False
    _entry       = float(p["entry"])
    _sl_dist     = abs(_entry - sl)
    # Trailing aktiviert erst nach ≥1R Profit (verhindert frühzeitige Stop-Hunts)
    activate_at  = (_entry + _sl_dist) if p["direction"] == "long" else (_entry - _sl_dist)

    if abs(sl - _entry) < 1e-6:
        return None

    for i, (_, row) in enumerate(check_df.iterrows(), start=1):
        # MFE / MAE für Lernzwecke
        if p["direction"] == "long":
            mfe_pct = max(mfe_pct, (row["high"] - _entry) / _entry * 100)
            mae_pct = max(mae_pct, (_entry - row["low"])  / _entry * 100)
        else:
            mfe_pct = max(mfe_pct, (_entry - row["low"])  / _entry * 100)
            mae_pct = max(mae_pct, (row["high"] - _entry) / _entry * 100)

        # Trailing Stop-Update: SL folgt dem Preis nach ≥1R Profit
        if trail_active:
            if p["direction"] == "long" and row["high"] >= activate_at:
                new_trail = row["high"] * (1.0 - trail_pct)
                if new_trail > eff_sl:
                    eff_sl = new_trail
                    trail_saved = True
            elif p["direction"] == "short" and row["low"] <= activate_at:
                new_trail = row["low"] * (1.0 + trail_pct)
                if new_trail < eff_sl:
                    eff_sl = new_trail
                    trail_saved = True

        # SL / TP prüfen — bei Trailing: eff_sl statt statischem sl.
        # Maximal realistisch:
        #  • Treffen SL UND TP in derselben Kerze, ist die Intrabar-Reihenfolge
        #    unbekannt → konservativ SL ZUERST annehmen (Worst Case, keine
        #    optimistische Verzerrung der Statistik).
        #  • Gap-Fill: öffnet die Kerze bereits jenseits des Stops (Gap über Nacht/
        #    News), wird zum schlechteren OPEN gefüllt, nicht am Stop-Level.
        #    Auf der Eröffnungskerze (i==1) liegt der Open VOR dem Entry → kein Gap.
        op = float(row["open"])
        if p["direction"] == "long":
            sl_hit = row["low"] <= eff_sl
            tp_hit = row["high"] >= tp
            sl_fill = eff_sl if i == 1 else min(eff_sl, op)
            if sl_hit:                                  # SL hat Vorrang (auch bei sl&tp)
                hit_sl, exit_price, candles_taken = True, sl_fill, i
                break
            elif tp_hit:
                hit_tp, exit_price, candles_taken = True, tp, i
                break
        else:
            sl_hit = row["high"] >= eff_sl
            tp_hit = row["low"] <= tp
            sl_fill = eff_sl if i == 1 else max(eff_sl, op)
            if sl_hit:
                hit_sl, exit_price, candles_taken = True, sl_fill, i
                break
            elif tp_hit:
                hit_tp, exit_price, candles_taken = True, tp, i
                break

    # Trailing SL für nächsten Tick persistieren (trade noch offen)
    if trail_saved and not (hit_sl or hit_tp):
        p["trail_sl"] = round(eff_sl, 4)

    hit_time = False
    if not (hit_sl or hit_tp):
        # Zeit-Exit: Position zu lange offen (TF-abhängig) → Zwangs-Exit zum
        # letzten Schlusskurs. Verhindert festsitzende Positionen, die
        # Slots blockieren und kein Lern-Feedback liefern.
        try:
            import tf_profiles
            max_hold_h = float(tf_profiles.get(p.get("timeframe", "4h"))["max_hold_hours"])
            max_hold_h *= float(p.get("max_hold_mult", 1.0))   # Bull Run: länger halten
            opened_dt  = pd.Timestamp(p["opened_at"])
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.tz_localize("utc")
            held_h = (pd.Timestamp.now(tz="utc") - opened_dt).total_seconds() / 3600
            if held_h > max_hold_h:
                hit_time      = True
                exit_price    = float(check_df.iloc[-1]["close"])
                candles_taken = len(check_df)
                print(f"  ⏱️  Zeit-Exit: Position {p.get('setup_type','?')} "
                      f"{held_h:.0f}h offen (Limit {max_hold_h:.0f}h) → Exit @ ${exit_price:.2f}")
        except Exception:
            pass
        if not hit_time:
            return None   # noch offen, weiter warten

    reason = "TP" if hit_tp else ("SL" if hit_sl else "TIME")
    return _finalize_close(state, p, exit_price, reason, candles_taken, mfe_pct, mae_pct)


def _finalize_close(state: State, p: dict, exit_price: float, reason: str,
                    candles_taken: int, mfe_pct: float, mae_pct: float) -> Optional[dict]:
    """
    Verbucht einen Positions-Close: Exit-Slippage, Brutto/Netto-PnL, Gebühren,
    State-Update, Trade-Record, Outcome-Rückschreibung und kompletter Lernzyklus.
    Genutzt vom Candle-Check UND von der Live-Preis-Überwachung. reason ∈ {TP,SL,TIME}.
    """
    sl = float(p["sl"])
    tp = float(p["tp"])

    # Exit-Slippage bei Market-Ausführung (SL / Zeit-Exit) — TP ist eine Limit-
    # Order und füllt exakt am Ziel, daher dort kein Slippage.
    if reason in ("SL", "TIME"):
        eslip      = exit_price * SLIPPAGE_PCT / 100.0
        exit_price = exit_price - eslip if p["direction"] == "long" else exit_price + eslip
    exit_price = round(exit_price, PRICE_DECIMALS)   # auf Tick-Size runden

    # Brutto-PnL aus Kursbewegung
    if p["direction"] == "long":
        gross_pnl = (exit_price - p["entry"]) * p["size"]
    else:
        gross_pnl = (p["entry"] - exit_price) * p["size"]

    # Handelsgebühren: Entry-Gebühr (beim Öffnen festgehalten) + Exit-Gebühr
    fee_pct   = float(p.get("fee_pct", TAKER_FEE_PCT))
    entry_fee = float(p.get("entry_fee", p["entry"] * p["size"] * fee_pct / 100.0))
    exit_fee  = abs(exit_price * p["size"]) * fee_pct / 100.0
    total_fee = round(entry_fee + exit_fee, 4)

    # Netto-PnL = Brutto − Gebühren (so wie bei einem echten Trade)
    pnl     = round(gross_pnl - entry_fee - exit_fee, 4)
    pnl_pct = pnl / (p["entry"] * p["size"]) * 100

    state.balance      = round(state.balance + pnl, 2)
    state.peak_balance = max(state.peak_balance, state.balance)
    dd = (state.peak_balance - state.balance) / state.peak_balance * 100
    state.max_drawdown = max(state.max_drawdown, dd)
    state.total_trades += 1
    won = pnl > 0
    setup_type = p.get("setup_type", "Unknown")

    # Tages-PnL für Circuit-Breaker aktualisieren
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.daily_date != today:
        state.daily_pnl  = 0.0
        state.daily_date = today
    state.daily_pnl = round(state.daily_pnl + pnl, 4)

    if won:
        state.wins               += 1
        state.consecutive_losses  = 0
        state.consecutive_wins   += 1
        # Reset setup-loss counter on win
        if f"{setup_type}_count" in state.setup_cooldowns:
            state.setup_cooldowns[f"{setup_type}_count"] = 0
    else:
        state.losses             += 1
        state.consecutive_losses += 1
        state.consecutive_wins    = 0
        # Track setup-specific losses for cooldown
        count_key = f"{setup_type}_count"
        state.setup_cooldowns[count_key] = state.setup_cooldowns.get(count_key, 0) + 1
        state.setup_cooldowns[setup_type] = time.time()  # timestamp of last loss

    closed_at = datetime.now(timezone.utc).isoformat()

    trade = {
        "id":            state.total_trades,
        "symbol":        SYMBOL,
        "direction":     p["direction"],
        "entry":         p["entry"],
        "sl":            sl,
        "tp":            tp,
        "exit_price":    exit_price,
        "exit_reason":   reason,
        "size":          p["size"],
        "notional":      p.get("notional", round(p["entry"] * p["size"], 2)),
        "fees":          total_fee,
        "gross_pnl":     round(gross_pnl, 4),
        "pnl":           round(pnl, 4),
        "pnl_pct":       round(pnl_pct, 3),
        "balance_after": state.balance,
        "score":         p["score"],
        "triggers":      p["triggers"],
        "zone":          p["zone"],
        "rr":            p["rr"],
        "opened_at":     p["opened_at"],
        "closed_at":     closed_at,
        "signal_id":     p.get("signal_id"),
        "setup_type":    p.get("setup_type", ""),
        "mfe_pct":       round(mfe_pct, 3),
        "mae_pct":       round(mae_pct, 3),
        "candles_taken": candles_taken,
    }

    state.trades.append(trade)
    state.trades = state.trades[-500:]
    state.positions.remove(p)

    # ── 1. Outcome + MFE/MAE auf ORIGINAL Signal-Row zurückschreiben ──────────
    sig_id = p.get("signal_id")
    if sig_id:
        try:
            signal_logger.update_signal_outcome(
                signal_id     = sig_id,
                outcome       = "WIN" if won else "LOSS",
                pnl_pct       = round(pnl_pct, 3),
                exit_price    = exit_price,
                closed_at     = closed_at,
                candles_taken = candles_taken,
                mfe_pct       = round(mfe_pct, 3),
                mae_pct       = round(mae_pct, 3),
            )
        except Exception as e:
            _log_error(f"update_signal_outcome: {e}")
    else:
        _log_error("Kein signal_id in Position — Outcome kann nicht zurückgeschrieben werden")

    # ── 2. Learning Engine: kurzfristige Gewicht-Anpassung ────────────────────
    learning_engine.update_weights(
        p["triggers"], won,
        created_at=p.get("created_at", "") or p.get("opened_at", ""),
    )

    # ── 2b. Strategie-Regel-Feedback: welche Regeln hatten recht? ─────────────
    try:
        import strategy_knowledge
        strategy_knowledge.record_feedback(p.get("rule_signatures", []), won)
    except Exception as e:
        _log_error(f"strategy_knowledge.record_feedback: {e}")

    # ── 3. Vollständiger Lernzyklus: Gewichte + Performance + Thresholds + XGBoost
    #    strategy_evolver orchestriert alle Lernmodule in einem Durchgang.
    #    force=True: immer ausführen, auch wenn < 3 neue Signale seit letztem Lauf.
    try:
        import strategy_evolver
        strategy_evolver.run(force=True)
    except Exception as e:
        _log_error(f"strategy_evolver.run: {e}")

    # Auto-Evolution-Trigger: Browser soll nach jedem 10. Trade re-evolvieren
    if state.total_trades % 10 == 0 and state.total_trades > 0:
        state.evolution_pending = True

    # ── 4. Lokales trades.csv / trades.json fortführen ────────────────────────
    _write_trade(trade)
    _update_daily_perf(state, trade)
    state.save()

    icon = "✅" if won else "❌"
    print(f"  {icon} TRADE GESCHLOSSEN: {p['direction'].upper()} | {reason} | "
          f"Netto-P&L ${pnl:+.2f} ({pnl_pct:+.2f}%) | "
          f"Brutto ${gross_pnl:+.2f} − Gebühr ${total_fee:.2f} | "
          f"Balance: ${state.balance:.2f} | Signal-ID={sig_id}")

    return trade


def _check_close(state: State, df: pd.DataFrame) -> Optional[dict]:
    """Prüft alle offenen Positionen auf SL/TP — gibt letzten geschlossenen Trade zurück."""
    last = None
    for p in list(state.positions):
        result = _check_close_one(state, p, df)
        if result is not None:
            last = result
    return last


def _check_close_live(state: State, price: float) -> bool:
    """
    Live-Preis-Überwachung offener Positionen zwischen den Kerzen-Closes.
    Wird in jedem Poll (alle POLL_INTERVAL Sek.) aufgerufen — schließt eine
    Position SOFORT, sobald der aktuelle Marktpreis SL/TP/Trailing-SL berührt,
    statt bis zum nächsten 15m-Kerzen-Close zu warten (realistisch wie eine echte
    Stop-/Limit-Order). Gibt True zurück, wenn mindestens eine Position geschlossen.
    """
    if not state.positions or price is None or price <= 0:
        return False

    closed_any = False
    for p in list(state.positions):
        try:
            sl   = float(p["sl"])
            tp   = float(p["tp"])
            entry = float(p["entry"])
            long = p["direction"] == "long"
            sl_dist = abs(entry - sl)
            if sl_dist < 1e-6:
                continue

            # Trailing-SL live mitziehen (nach ≥1R Profit), persistent im Dict
            eff_sl = float(p.get("trail_sl") or sl)
            if bool(p.get("use_trail", False)) and float(p.get("trail_pct") or 0.0) > 0:
                trail_pct   = float(p["trail_pct"])
                activate_at = (entry + sl_dist) if long else (entry - sl_dist)
                if long and price >= activate_at:
                    nt = price * (1.0 - trail_pct)
                    if nt > eff_sl:
                        eff_sl = nt
                        p["trail_sl"] = round(eff_sl, 4)
                elif (not long) and price <= activate_at:
                    nt = price * (1.0 + trail_pct)
                    if nt < eff_sl:
                        eff_sl = nt
                        p["trail_sl"] = round(eff_sl, 4)

            # Live-Touch: kein Gap (Preis ist genau jetzt am Level) → Fill am Level.
            # SL hat Vorrang vor TP (konservativ).
            reason = exit_price = None
            if long:
                if   price <= eff_sl: reason, exit_price = "SL", eff_sl
                elif price >= tp:     reason, exit_price = "TP", tp
            else:
                if   price >= eff_sl: reason, exit_price = "SL", eff_sl
                elif price <= tp:     reason, exit_price = "TP", tp

            if reason:
                # MFE/MAE näherungsweise aus dem Move bis zum Live-Exit
                if long:
                    mfe = max(0.0, (price - entry) / entry * 100)
                    mae = max(0.0, (entry - price) / entry * 100)
                else:
                    mfe = max(0.0, (entry - price) / entry * 100)
                    mae = max(0.0, (price - entry) / entry * 100)
                _finalize_close(state, p, exit_price, reason,
                                p.get("candles_taken", 1), mfe, mae)
                closed_any = True
        except Exception as e:
            _log_error(f"_check_close_live: {e}")

    return closed_any


# ── Trade persistieren ────────────────────────────────────────────────────────
def _write_trade(trade: dict) -> None:
    # CSV
    csv_exists = TRADES_CSV.exists()
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=trade.keys())
        if not csv_exists:
            writer.writeheader()
        t = dict(trade)
        t["triggers"] = "|".join(t.get("triggers", []))
        writer.writerow(t)

    # JSON
    all_trades = []
    if TRADES_JSON.exists():
        try:
            with open(TRADES_JSON, encoding="utf-8") as f:
                all_trades = json.load(f)
        except Exception:
            pass
    all_trades.append(trade)
    with open(TRADES_JSON, "w", encoding="utf-8") as f:
        json.dump(all_trades[-1000:], f, indent=2, ensure_ascii=False, default=str)


# ── Tägliche Performance-Zusammenfassung ──────────────────────────────────────
def _update_daily_perf(state: State, trade: dict) -> None:
    """Aktualisiert daily_performance.json mit dem neuen Trade."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    perf: list = []
    if DAILY_PERF_FILE.exists():
        try:
            with open(DAILY_PERF_FILE, encoding="utf-8") as f:
                perf = json.load(f)
        except Exception:
            pass
    # Heutigen Eintrag finden oder neu anlegen
    entry = next((e for e in perf if e.get("date") == today), None)
    if entry is None:
        entry = {"date": today, "trades": 0, "wins": 0, "losses": 0,
                 "pnl": 0.0, "balance_end": state.balance}
        perf.append(entry)
    entry["trades"] += 1
    entry["pnl"]     = round(entry["pnl"] + trade["pnl"], 4)
    entry["balance_end"] = state.balance
    if trade["pnl"] > 0:
        entry["wins"] += 1
    else:
        entry["losses"] += 1
    entry["win_rate"] = round(entry["wins"] / entry["trades"] * 100, 1)
    with open(DAILY_PERF_FILE, "w", encoding="utf-8") as f:
        json.dump(perf[-90:], f, indent=2, ensure_ascii=False)  # max. 90 Tage


# ── Fehler-Logging ────────────────────────────────────────────────────────────
def _log_error(msg: str) -> None:
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}\n"
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(line)
    print(f"  ⚠️  {msg}")


# ── Haupt-Loop ────────────────────────────────────────────────────────────────
def run_once(state: State) -> None:
    """
    Ein einzelner Analyse-Zyklus (läuft auf jedem Kerzen-Close).

    Ablauf:
      1. Frische OHLCV-Daten holen
      2. Offene Signal-Outcomes simulieren (nicht paper-getradete)
      3. Offene Position prüfen (SL/TP getroffen?)
      4. Falls keine Position: bestes Signal aus signals.db holen und traden
    """
    df = _fetch_ohlcv()
    if df is None or len(df) < 30:
        return

    _atr_cache["value"] = _calc_atr(df)   # Volatilität für Positionsgröße
    _adx_cache["value"] = _calc_adx(df)   # Trend-Stärke für Regime-Filter
    _macd_cache.update(_calc_macd(df))
    _rsi_cache["rsi"]        = _calc_rsi(df)
    _rsi_cache["divergence"] = _detect_rsi_divergence(df)
    _rsi_cache["sweep"]      = _detect_liquidity_sweep(df, _1h_trend_cache.get("bias", "neutral"))
    try:
        vol_ma = df["volume"].iloc[-21:-1].mean()
        _vol_cache["ratio"] = round(float(df["volume"].iloc[-1]) / vol_ma, 3) if vol_ma > 0 else 1.0
    except Exception:
        pass

    # Nicht paper-getradete Signale via Kerzen-Simulation auflösen
    try:
        n_resolved = signal_logger.update_outcomes(df)
    except Exception as e:
        _log_error(f"update_outcomes: {e}")
        n_resolved = 0

    # Wenn Simulations-Signale abgeschlossen wurden → Lernzyklus anstoßen
    # (paper-trade-geschlossene Signale triggern bereits force=True in _check_close_one)
    if n_resolved and n_resolved > 0:
        try:
            import strategy_evolver
            strategy_evolver.run()   # ohne force: respektiert MIN_NEW_SIGNALS
        except Exception as e:
            _log_error(f"strategy_evolver (candle): {e}")

    # POI-Lern-Zyklus: neue Zonen erkennen + Outcome bestehender POIs aktualisieren
    try:
        poi_tracker.log_pois(df)
        poi_tracker.update_outcomes(df)
    except Exception as e:
        _log_error(f"poi_tracker: {e}")

    # Offenen Trade prüfen
    _check_close(state, df)

    if len(state.positions) < MAX_POSITIONS:
        # Tages-Verlust-Limit: kein neuer Trade wenn -2% heute erreicht
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.daily_date == today and state.daily_pnl <= -(state.balance * DAILY_LOSS_LIMIT_PCT):
            print(f"  [Circuit Breaker] Tages-Verlust ${state.daily_pnl:.2f} ≤ "
                  f"-{DAILY_LOSS_LIMIT_PCT*100:.0f}% — kein neuer Trade heute")
        else:
            sig_row = _get_db_signal(state)
            if sig_row:
                _open_trade_from_signal(state, sig_row)

    # Kurze Status-Ausgabe
    now_str = datetime.now(timezone.utc).strftime("%H:%M")
    if state.positions:
        pos_parts = [
            f"📊 {p['direction'].upper()} @ ${p['entry']:.2f} SL=${p['sl']:.2f} "
            f"Kapital=${p.get('notional', p['entry']*p['size']):,.0f} "
            f"Score={p['score']:.0f} ({p.get('setup_type','')})"
            for p in state.positions
        ]
        pos_txt = f"[{len(state.positions)} Pos.] " + " | ".join(pos_parts)
    else:
        pos_txt = "— kein offener Trade"
    print(f"  [{now_str} UTC] ${state.balance:.2f} | {pos_txt}")

    # Trail-SL-Updates persistieren (state.save() fehlt wenn kein Trade öffnet/schließt)
    if any(p.get("trail_sl") is not None for p in state.positions):
        state.save()


def _last_closed_candle_ts() -> Optional[str]:
    """
    Gibt den Zeitstempel der letzten GESCHLOSSENEN Kerze zurück.
    Binance liefert die aktuell entstehende Kerze als letzten Eintrag —
    die vorletzte ist die zuletzt abgeschlossene.
    """
    try:
        r = _http.get(
            f"{BINANCE_BASE}/klines",
            params={"symbol": SYMBOL, "interval": LOOP_INTERVAL, "limit": 2},
            timeout=8,
        )
        r.raise_for_status()
        candles = r.json()
        if len(candles) >= 2:
            return str(candles[-2][0])   # Open-Timestamp der letzten geschlossenen Kerze
    except Exception:
        pass
    return None


def run_forever() -> None:
    """
    Startet den Paper-Trading-Loop.
    Läuft run_once() genau dann, wenn eine neue Kerze geschlossen hat —
    synchron zum Chart, nicht zeitbasiert.
    Prüft alle POLL_INTERVAL Sekunden ob eine neue Kerze da ist.
    """
    state = State()
    state.load()
    print(f"\n{'═'*60}")
    print(f"  PAPER TRADER — {SYMBOL} | Balance: ${state.balance:.2f}")
    print(f"  Zeitrahmen: {INTERVAL} | Prüft alle {POLL_INTERVAL}s auf neuen Kerzen-Close")
    print(f"{'═'*60}\n")

    # State sofort persistieren → state.json existiert ab erster Ausführung
    state.save()

    _stop_event.clear()
    last_closed_ts: Optional[str] = None
    _polls_since_save = 0
    _SAVE_EVERY_N_POLLS = max(1, 600 // POLL_INTERVAL)  # ~10 Minuten

    while not _stop_event.is_set():
        try:
            ts = _last_closed_candle_ts()
            if ts is None:
                pass   # Netzwerk-Fehler — nächste Runde
            elif last_closed_ts is None:
                # Erster Start: Basis-Timestamp merken und einmalig analysieren
                last_closed_ts = ts
                print(f"  🕯️  Startpunkt: letzte geschlossene Kerze {ts}")
                run_once(state)
            elif ts != last_closed_ts:
                # Neue Kerze geschlossen → sofort analysieren
                last_closed_ts = ts
                print(f"  🕯️  Neue Kerze geschlossen ({ts}) — analysiere…")
                run_once(state)
            # else: gleiche Kerze läuft noch → nichts tun

            # ── Live-Preis-Überwachung offener Positionen (jeden Poll = ~30s) ──
            # Schließt SL/TP/Trailing SOFORT bei Live-Berührung, statt bis zum
            # nächsten 15m-Kerzen-Close zu warten — wie eine echte Stop-Order.
            if state.positions:
                _live_px = _fetch_price()
                if _live_px:
                    _check_close_live(state, _live_px)   # _finalize_close speichert bei Close

            # Periodisches Speichern alle ~10 Minuten auch ohne Trades
            _polls_since_save += 1
            if _polls_since_save >= _SAVE_EVERY_N_POLLS:
                _polls_since_save = 0
                state.save()
        except KeyboardInterrupt:
            print("\nPaper Trader beendet.")
            state.save()
            break
        except Exception as e:
            _log_error(f"run_forever: {e}\n{traceback.format_exc()}")

        _stop_event.wait(timeout=POLL_INTERVAL)

    print("  Paper Trader gestoppt.")


def is_running() -> bool:
    """True wenn der Paper-Trader-Loop gerade läuft."""
    return not _stop_event.is_set()


def _calc_setup_stats(trades: list) -> dict:
    """Win-Rate + Profit-Factor pro Setup-Typ aus den letzten N Trades."""
    from collections import defaultdict
    agg: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        st = t.get("setup_type") or "Unknown"
        agg[st]["n"]    += 1
        agg[st]["pnl"]  = round(agg[st]["pnl"] + float(t.get("pnl", 0)), 4)
        if float(t.get("pnl", 0)) > 0:
            agg[st]["wins"] += 1
    return {
        st: {
            "n":   d["n"],
            "wr":  round(d["wins"] / d["n"] * 100, 1),
            "pnl": d["pnl"],
        }
        for st, d in agg.items() if d["n"] >= 2
    }


def _get_status_pois(df=None) -> list:
    """Aktive POIs mit gelernten Erfolgsquoten, für das Dashboard."""
    try:
        if df is not None:
            return poi_tracker.get_active_pois(df)
        # Ohne df: rohe Liste ohne Abstandsberechnung
        raw   = poi_tracker._load()
        stats = poi_tracker.get_stats()
        result = []
        for p in raw:
            if p.get("status") != "active":
                continue
            t = p["type"]
            s = stats.get(t, {})
            result.append({
                "type": t, "direction": p["direction"],
                "high": p["high"], "low": p["low"], "midpoint": p["midpoint"],
                "strength": p.get("strength", 1.0),
                "hit_rate":      s.get("hit_rate",      0.0),
                "continue_rate": s.get("continue_rate", 0.0),
                "confidence":    s.get("confidence",    0.0),
                "samples":       s.get("total",         0),
            })
        return result[:25]
    except Exception:
        return []


def _get_poi_stats_summary() -> dict:
    """Zusammenfassung der POI-Statistiken für das Dashboard."""
    try:
        stats = poi_tracker.get_stats()
        return {
            t: {
                "total":         s["total"],
                "hit_rate":      s["hit_rate"],
                "continue_rate": s["continue_rate"],
                "confidence":    s["confidence"],
            }
            for t, s in stats.items()
            if s["total"] >= 3
        }
    except Exception:
        return {}


def get_status() -> dict:
    """Gibt den aktuellen Paper-Trader-Status für das Dashboard zurück."""
    running = is_running()
    s: dict = {}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            pass

    trades: list = []
    if TRADES_JSON.exists():
        try:
            with open(TRADES_JSON, encoding="utf-8") as f:
                trades = json.load(f)
        except Exception:
            pass

    bal = float(s.get("balance", INITIAL_BAL))
    positions = s["positions"] if "positions" in s else ([s.get("position")] if s.get("position") else [])

    # Flag zurücksetzen damit Browser nur einmalig triggert
    if s.get("evolution_pending"):
        try:
            s["evolution_pending"] = False
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    return {
        "active":        True,   # immer True — zeigt Dashboard-Felder an
        "balance":       bal,
        "pnl":           round(bal - INITIAL_BAL, 2),
        "pnl_pct":       round((bal - INITIAL_BAL) / INITIAL_BAL * 100, 2),
        "total_trades":  s.get("total_trades", 0),
        "win_rate":      s.get("win_rate", 0),
        "profit_factor": s.get("profit_factor", 0),
        "max_drawdown":  s.get("max_drawdown", 0),
        "positions":          positions,
        "position":           positions[0] if positions else None,
        "signal_weights":     s.get("signal_weights", {}),
        "recent_trades":      list(reversed(trades))[:10],
        "running":            running,
        "evolution_pending":  s.get("evolution_pending", False),
        "consecutive_losses": s.get("consecutive_losses", 0),
        "daily_trend":        _daily_trend_cache.get("bias", "neutral"),
        "h1_trend":           _1h_trend_cache.get("bias", "neutral"),
        "atr":                round(_atr_cache.get("value", 0.0), 2),
        "adx":                round(_adx_cache.get("value", 25.0), 1),
        "daily_pnl":          s.get("daily_pnl", 0.0),
        "consecutive_wins":   s.get("consecutive_wins", 0),
        "fear_greed":         _fg_cache.get("value", 50),
        "vol_ratio":          round(_vol_cache.get("ratio", 1.0), 2),
        "macd":               _macd_cache,
        "rsi":                round(_rsi_cache.get("rsi", 50.0), 1),
        "rsi_divergence":     _rsi_cache.get("divergence", "none"),
        "funding_rate":       round(_funding_cache.get("rate", 0.0) * 100, 4),
        "open_interest":      round(_oi_cache.get("oi", 0.0), 0),
        "session":            _get_session(datetime.now(timezone.utc).hour)[0],
        "weekly_trend":       _get_weekly_trend(),
        "news_blocked":       False,  # Don't block status calls
        "liquidity_sweep":    _rsi_cache.get("sweep", "none"),
        "poi_zones":          _get_status_pois(),
        "poi_stats":          _get_poi_stats_summary(),
        "balance_history":    [t["balance_after"] for t in trades[-50:] if "balance_after" in t],
        "setup_stats":        _calc_setup_stats(trades[-100:]),
    }




if __name__ == "__main__":
    run_forever()
