"""
Signal Engine — SMC/ICT Signal-Detektion (präzise Definitionen).

Implementiert exakt:
  BOS       — Kerze SCHLIESST über/unter vorherigen Swing-Punkt
  CHoCH     — erster Bruch des letzten Lower-High (im Downtrend) / Higher-Low (im Uptrend)
  Order Block — LETZTE Gegenfarb-Kerze VOR dem BOS-auslösenden Impuls
  FVG       — 3-Kerzen-Imbalance: C3.low > C1.high (bull) / C3.high < C1.low (bear)
  EQH/EQL   — Liquiditätspools (double tops/bottoms)
  Liq.Sweep — Docht durch EQH/EQL mit Schlusskurs zurück (Stopp-Jagd)
  S/R Flip  — gebrochene Swing-Punkte als Flip-Zonen
  HTF-Trend — Makrotrend auf nächsthöherem Zeitrahmen (benötigt, kein Trade ohne)
  Premium/Discount — 50%-Fibonacci der letzten Swing-Spanne

Entry-Bedingungen (alle müssen erfüllt sein):
  LONG:   HTF bullish + Discount + (Sweep EQL ODER CHoCH) + (OB ODER FVG) = ≥2 Confluences
  SHORT:  HTF bearish + Premium  + (Sweep EQH ODER CHoCH) + (OB ODER FVG) = ≥2 Confluences
"""

from __future__ import annotations

import time
import requests
import pandas as pd
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

BINANCE_BASE = "https://api.binance.com/api/v3"

# HTF-Cache: Trend-Abfrage max. alle 4h neu
_htf_cache: dict[str, tuple[str, float]] = {}   # key → (bias, expires_ts)

# ── Gelernte Signal-Gewichte (aus learning_engine, 60s-Cache) ─────────────────
# Skaliert die Confluence-Punkte relativ zu den Default-Gewichten:
# Faktor = gelerntes Gewicht / Default-Gewicht, begrenzt auf 0.5–1.6.
_lw_cache: dict = {"weights": {}, "ts": 0.0}
_LW_TTL = 60.0

_LW_DEFAULTS = {
    "bos": 1.5, "choch": 1.2, "fvg": 1.0, "order_block": 1.3,
    "eqh": 0.9, "eql": 0.9, "discount_zone": 0.8, "premium_zone": 0.8,
}


def _learned_factor(key: str) -> float:
    """Skalierungsfaktor eines Signal-Typs aus den gelernten Gewichten."""
    if time.time() - _lw_cache["ts"] > _LW_TTL:
        try:
            import learning_engine
            _lw_cache["weights"] = learning_engine.load_weights()
        except Exception:
            _lw_cache["weights"] = {}
        _lw_cache["ts"] = time.time()
    w = _lw_cache["weights"].get(key)
    d = _LW_DEFAULTS.get(key, 1.0)
    if not w or d <= 0:
        return 1.0
    return max(0.5, min(1.6, w / d))

HTF_MAP = {
    "1m": "15m", "5m": "1h", "15m": "4h",
    "30m": "4h", "1h":  "4h", "4h":  "1d",
    "1d":  "1w", "1w":  "1w",
}


# ══ Datenstrukturen ═══════════════════════════════════════════════════════════

@dataclass
class SwingPoint:
    kind:  str    # "high" | "low"
    price: float
    idx:   int

    @property
    def is_high(self) -> bool:
        return self.kind == "high"


@dataclass
class BosEvent:
    direction: str   # "bullish" | "bearish"
    level:     float  # der gebrochene Swing-Punkt
    idx:       int    # Kerzen-Index des Bruchs
    caused_by_close: bool = True  # immer True in unserer Implementierung


@dataclass
class ChochEvent:
    direction: str   # "bullish" | "bearish"
    level:     float  # das gebrochene Lower-High oder Higher-Low
    idx:       int


@dataclass
class OrderBlock:
    direction: str   # "bullish" | "bearish"
    top:       float
    bottom:    float
    idx:       int   # Index der OB-Kerze
    caused_bos: bool = True  # OB, der einen BOS ausgelöst hat

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def size(self) -> float:
        return self.top - self.bottom

    def contains(self, price: float, tolerance: float = 0.003) -> bool:
        return (self.bottom * (1 - tolerance)) <= price <= (self.top * (1 + tolerance))


@dataclass
class FVG:
    direction: str   # "bullish" | "bearish"
    top:       float  # obere Grenze der Imbalance
    bottom:    float  # untere Grenze der Imbalance
    idx:       int    # Index der 3. Kerze

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def size(self) -> float:
        return self.top - self.bottom

    def contains(self, price: float, tolerance: float = 0.003) -> bool:
        return (self.bottom * (1 - tolerance)) <= price <= (self.top * (1 + tolerance))

    def is_filled(self, price: float) -> bool:
        return price <= self.bottom if self.direction == "bullish" else price >= self.top


@dataclass
class LiquiditySweep:
    direction: str   # "bullish" = EQL swept (bullish setup) | "bearish" = EQH swept
    level:     float  # der gekehrte Liquiditätslevel
    idx:       int    # Index der Sweep-Kerze
    candles_ago: int  # wie viele Kerzen her


@dataclass
class SRFlip:
    direction: str   # "support" (ehem. Widerstand) | "resistance" (ehem. Support)
    level:     float
    strength:  int   # 1-3


@dataclass
class EntryConfluence:
    """Alle Confluences für eine Richtung."""
    bias:           str             # "bullish" | "bearish"
    htf_aligned:    bool = False    # HTF-Trend stimmt überein (REQUIRED)
    zone_aligned:   bool = False    # Discount (bull) oder Premium (bear) (REQUIRED)
    liq_sweep:      Optional[LiquiditySweep] = None   # Liquiditäts-Sweep
    choch:          Optional[ChochEvent]     = None   # CHoCH
    order_block:    Optional[OrderBlock]     = None   # OB im Bereich
    fvg:            Optional[FVG]            = None   # FVG im Bereich
    sr_flip:        Optional[SRFlip]         = None   # S/R Flip im Bereich
    bos_confirms:   Optional[BosEvent]       = None   # bestätigender BOS

    @property
    def confluence_count(self) -> int:
        """Anzahl der optionalen Confluences (exkl. Required)."""
        return sum([
            self.liq_sweep   is not None,
            self.choch       is not None,
            self.order_block is not None,
            self.fvg         is not None,
            self.sr_flip     is not None,
        ])

    @property
    def is_valid(self) -> bool:
        """Valid = beide Required + mindestens 2 optionale Confluences."""
        return self.htf_aligned and self.zone_aligned and self.confluence_count >= 2

    @property
    def score(self) -> int:
        """
        Score 0-100. Nur gültige Setups bekommen > 50.
        Confluence-Punkte werden mit den GELERNTEN Signal-Gewichten skaliert
        (learning_engine) — Signale die historisch gewinnen, zählen mehr.
        """
        if not self.htf_aligned or not self.zone_aligned:
            return 0
        base = 40  # htf + zone = Basis
        # Jede Confluence addiert Punkte, gewichtet nach Qualität × gelerntem Gewicht
        if self.liq_sweep:
            # Recency decay: voller Bonus bei ≤3 Kerzen, linear abfallend bis Kerze 20
            age    = getattr(self.liq_sweep, "candles_ago", 1) or 1
            recency = max(0.35, 1.0 - (age - 1) / 20.0)
            sweep_key = "eql" if self.bias == "bullish" else "eqh"
            base  += round(20 * recency * _learned_factor(sweep_key))
        if self.choch:        base += round(15 * _learned_factor("choch"))
        if self.order_block:  base += round(15 * _learned_factor("order_block"))
        if self.fvg:          base += round(8  * _learned_factor("fvg"))
        if self.sr_flip:      base += 5
        if self.bos_confirms: base += round(7  * _learned_factor("bos"))
        return min(100, base)

    @property
    def triggers(self) -> list[str]:
        t = []
        if self.bos_confirms: t.append("BOS")
        if self.choch:        t.append("CHoCH")
        if self.liq_sweep:    t.append("SWEEP")
        if self.order_block:  t.append("OB")
        if self.fvg:          t.append("FVG")
        if self.sr_flip:      t.append("SR_FLIP")
        if self.zone_aligned:
            t.append("DISCOUNT" if self.bias == "bullish" else "PREMIUM")
        return t


@dataclass
class SignalResult:
    timestamp:   datetime
    bias:        str              # "bullish" | "bearish" | "neutral"
    score:       int              # 0-100
    triggers:    list[str]
    zone:        str              # "premium" | "discount" | "neutral"
    entry:       float
    sl:          float
    tp:          float
    rr:          float
    htf_bias:    str = "neutral"  # Makrotrend
    confluence:  Optional[EntryConfluence] = None

    @property
    def is_valid(self) -> bool:
        return (self.score >= 55 and self.rr >= 2.0
                and self.bias != "neutral"
                and self.confluence is not None
                and self.confluence.is_valid)


# ══ Swing-Struktur ════════════════════════════════════════════════════════════

def _find_swings(df: pd.DataFrame, window: int = 5) -> list[SwingPoint]:
    """
    Findet Pivot-Highs und Pivot-Lows.
    Pivot-High: höchster Punkt im Fenster links und rechts.
    Pivot-Low:  tiefster Punkt im Fenster links und rechts.
    """
    highs = df["high"].values
    lows  = df["low"].values
    n     = len(df)
    swings: list[SwingPoint] = []

    for i in range(window, n - window):
        if highs[i] == max(highs[i - window: i + window + 1]):
            swings.append(SwingPoint("high", highs[i], i))
        if lows[i] == min(lows[i - window: i + window + 1]):
            swings.append(SwingPoint("low", lows[i], i))

    swings.sort(key=lambda s: s.idx)
    return swings


def _trend_from_swings(swings: list[SwingPoint]) -> str:
    """
    Bestimmt Trend aus Swing-Sequenz.
    Bullish: Higher Highs + Higher Lows.
    Bearish: Lower Highs + Lower Lows.
    Sonst: neutral.
    """
    highs = [s for s in swings if s.is_high][-4:]
    lows  = [s for s in swings if not s.is_high][-4:]

    if len(highs) < 2 or len(lows) < 2:
        return "neutral"

    hh = all(highs[i].price < highs[i + 1].price for i in range(len(highs) - 1))
    hl = all(lows[i].price  < lows[i + 1].price  for i in range(len(lows)  - 1))
    lh = all(highs[i].price > highs[i + 1].price for i in range(len(highs) - 1))
    ll = all(lows[i].price  > lows[i + 1].price  for i in range(len(lows)  - 1))

    if hh and hl:
        return "bullish"
    if lh and ll:
        return "bearish"
    return "neutral"


# ══ BOS ══════════════════════════════════════════════════════════════════════

def detect_bos(df: pd.DataFrame, swings: list[SwingPoint],
               lookback: int = 100) -> list[BosEvent]:
    """
    BOS = Kerze SCHLIESST über einem vorherigen Swing-High (bullish BOS)
         oder UNTER einem vorherigen Swing-Low (bearish BOS).
    Nur die letzten `lookback` Kerzen werden geprüft.
    """
    closes = df["close"].values
    n      = len(df)
    start  = max(0, n - lookback)
    events: list[BosEvent] = []

    for i in range(start, n):
        close = closes[i]
        # Swing-Punkte die VOR dieser Kerze liegen
        prev_highs = [s for s in swings if s.is_high and s.idx < i - 2]
        prev_lows  = [s for s in swings if not s.is_high and s.idx < i - 2]

        if prev_highs:
            last_sh = max(prev_highs[-4:], key=lambda s: s.price)
            if close > last_sh.price:
                # Kein Duplikat auf demselben Swing-Level
                if not any(abs(e.level - last_sh.price) / last_sh.price < 0.001
                           and e.direction == "bullish" for e in events):
                    events.append(BosEvent("bullish", last_sh.price, i))

        if prev_lows:
            last_sl = min(prev_lows[-4:], key=lambda s: s.price)
            if close < last_sl.price:
                if not any(abs(e.level - last_sl.price) / last_sl.price < 0.001
                           and e.direction == "bearish" for e in events):
                    events.append(BosEvent("bearish", last_sl.price, i))

    return events


# ══ CHoCH ═════════════════════════════════════════════════════════════════════

def detect_choch(df: pd.DataFrame, swings: list[SwingPoint],
                 trend: str, lookback: int = 100) -> list[ChochEvent]:
    """
    CHoCH = erster Bruch gegen den aktuellen Trend:
    - Im Downtrend (lower highs + lower lows): Schlusskurs über das letzte Lower High
    - Im Uptrend   (higher highs + higher lows): Schlusskurs unter das letzte Higher Low
    """
    closes = df["close"].values
    n      = len(df)
    start  = max(0, n - lookback)
    events: list[ChochEvent] = []

    for i in range(start, n):
        close = closes[i]
        prev_swings = [s for s in swings if s.idx < i - 1]

        if trend == "bearish":
            # Lower Highs der Abwärtsbewegung finden
            highs = [s for s in prev_swings if s.is_high]
            if len(highs) >= 2:
                # Nur Highs die tatsächlich lower sind als das vorherige
                lower_highs = [h for j, h in enumerate(highs[1:], 1)
                               if h.price < highs[j - 1].price]
                if lower_highs:
                    last_lh = lower_highs[-1]
                    if close > last_lh.price:
                        if not any(abs(e.level - last_lh.price) / last_lh.price < 0.001
                                   for e in events):
                            events.append(ChochEvent("bullish", last_lh.price, i))

        elif trend == "bullish":
            # Higher Lows der Aufwärtsbewegung finden
            lows = [s for s in prev_swings if not s.is_high]
            if len(lows) >= 2:
                higher_lows = [l for j, l in enumerate(lows[1:], 1)
                               if l.price > lows[j - 1].price]
                if higher_lows:
                    last_hl = higher_lows[-1]
                    if close < last_hl.price:
                        if not any(abs(e.level - last_hl.price) / last_hl.price < 0.001
                                   for e in events):
                            events.append(ChochEvent("bearish", last_hl.price, i))

    return events


# ══ Order Blocks ══════════════════════════════════════════════════════════════

def detect_order_blocks(df: pd.DataFrame,
                        bos_events: list[BosEvent]) -> list[OrderBlock]:
    """
    Order Block = LETZTE Gegenfarb-Kerze direkt vor dem BOS-auslösenden Impuls.

    Bullish OB: letzte bearishe Kerze (close < open) vor einem Bullish BOS.
    Bearish OB: letzte bullishe Kerze (close > open) vor einem Bearish BOS.

    'Vor dem Impuls' = die Kerzen zwischen OB und BOS-Kerze sind der Impuls.
    """
    opens  = df["open"].values
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    obs: list[OrderBlock] = []
    price  = closes[-1]

    for bos in bos_events:
        impulse_start = max(0, bos.idx - 30)

        if bos.direction == "bullish":
            # Suche letzten bearishen Kerze vor dem Impuls
            for i in range(bos.idx - 1, impulse_start - 1, -1):
                if closes[i] < opens[i]:  # bearishe Kerze
                    ob = OrderBlock("bullish", highs[i], lows[i], i, caused_bos=True)
                    # Nicht als "used" markieren wenn Preis schon tief darunter
                    if price >= ob.bottom * 0.95:
                        obs.append(ob)
                    break

        elif bos.direction == "bearish":
            for i in range(bos.idx - 1, impulse_start - 1, -1):
                if closes[i] > opens[i]:  # bullishe Kerze
                    ob = OrderBlock("bearish", highs[i], lows[i], i, caused_bos=True)
                    if price <= ob.top * 1.05:
                        obs.append(ob)
                    break

    # Deduplizieren (mehrere BOS können auf denselben OB zeigen)
    unique: list[OrderBlock] = []
    for ob in obs:
        if not any(abs(ob.midpoint - u.midpoint) / ob.midpoint < 0.005 for u in unique):
            unique.append(ob)

    return unique[-6:]


# ══ FVG ═══════════════════════════════════════════════════════════════════════

def detect_fvg(df: pd.DataFrame, lookback: int = 200) -> list[FVG]:
    """
    Fair Value Gap — 3-Kerzen-Imbalance:

    Bullish FVG:  Kerze[i].low > Kerze[i-2].high
                  → Imbalance zwischen C1.high (bottom) und C3.low (top)
                  → Preis neigt dazu, zurückzukommen um Imbalance zu füllen
                  → FVG wirkt als Support

    Bearish FVG:  Kerze[i].high < Kerze[i-2].low
                  → Imbalance zwischen C3.high (top) und C1.low (bottom)
                  → FVG wirkt als Widerstand

    Nur ungefüllte FVGs werden zurückgegeben.
    """
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    n      = len(df)
    start  = max(2, n - lookback)
    price  = closes[-1]
    fvgs: list[FVG] = []

    for i in range(start, n):
        # Bullish FVG: C3.low > C1.high
        c1_high = highs[i - 2]
        c3_low  = lows[i]
        if c3_low > c1_high:
            fvg = FVG("bullish", top=c3_low, bottom=c1_high, idx=i)
            # Noch nicht gefüllt wenn Preis über der Untergrenze
            if not fvg.is_filled(price):
                fvgs.append(fvg)

        # Bearish FVG: C3.high < C1.low
        c1_low  = lows[i - 2]
        c3_high = highs[i]
        if c3_high < c1_low:
            fvg = FVG("bearish", top=c1_low, bottom=c3_high, idx=i)
            if not fvg.is_filled(price):
                fvgs.append(fvg)

    return fvgs[-10:]


# ══ Equal Highs / Equal Lows ═════════════════════════════════════════════════

def detect_eqh_eql(df: pd.DataFrame,
                   swings: list[SwingPoint],
                   tolerance: float = 0.002,
                   lookback: int = 300) -> tuple[list[float], list[float]]:
    """
    EQH: ≥2 Swing-Highs innerhalb `tolerance` voneinander → Liquiditätspool oben.
    EQL: ≥2 Swing-Lows innerhalb `tolerance` voneinander → Liquiditätspool unten.
    Stärkere Cluster (3+ Berührungen) werden bevorzugt.
    """
    # Swings der letzten `lookback` Kerzen verwenden
    min_idx = max(0, len(df) - lookback)
    lookback_swings = [s for s in swings if s.idx >= min_idx]
    pivot_highs = [s.price for s in lookback_swings if s.is_high]
    pivot_lows  = [s.price for s in lookback_swings if not s.is_high]

    eqh: list[float] = []
    eql: list[float] = []

    for levels, out in [(pivot_highs, eqh), (pivot_lows, eql)]:
        for i, p in enumerate(levels):
            cluster = [q for q in levels if abs(q - p) / max(p, 0.001) < tolerance]
            if len(cluster) >= 2:
                mid = sum(cluster) / len(cluster)
                if not any(abs(mid - e) / max(mid, 0.001) < tolerance for e in out):
                    out.append(mid)

    return sorted(eqh, reverse=True)[:5], sorted(eql)[:5]


# ══ Liquidity Sweep ═══════════════════════════════════════════════════════════

def detect_liquidity_sweeps(df: pd.DataFrame,
                            eqh_levels: list[float],
                            eql_levels: list[float],
                            lookback: int = 25) -> list[LiquiditySweep]:
    """
    Liquidity Sweep = Docht (Wick) durch EQH/EQL mit Schlusskurs zurück.

    Bullish Sweep (EQL swept):
      - Low der Kerze < EQL-Level (wick geht drunter)
      - Aber: Close der Kerze > EQL-Level (schließt zurück über dem Level)
      → Stop-Loss-Jagd nach unten → Reversal nach oben

    Bearish Sweep (EQH swept):
      - High der Kerze > EQH-Level (wick geht drüber)
      - Aber: Close der Kerze < EQH-Level (schließt zurück unter dem Level)
      → Stop-Loss-Jagd nach oben → Reversal nach unten
    """
    n      = len(df)
    start  = max(0, n - lookback)
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    sweeps: list[LiquiditySweep] = []

    for i in range(start, n):
        # Bullish sweep: wick unter EQL, close drüber
        for lvl in eql_levels:
            if lows[i] < lvl and closes[i] > lvl:
                sweeps.append(LiquiditySweep(
                    "bullish", lvl, i, candles_ago=n - 1 - i
                ))

        # Bearish sweep: wick über EQH, close drunter
        for lvl in eqh_levels:
            if highs[i] > lvl and closes[i] < lvl:
                sweeps.append(LiquiditySweep(
                    "bearish", lvl, i, candles_ago=n - 1 - i
                ))

    # Nur die jüngsten, max. 20 Kerzen alt
    return [s for s in sweeps if s.candles_ago <= 20]


# ══ Support & Resistance Flip-Zonen ══════════════════════════════════════════

def detect_sr_flips(bos_events: list[BosEvent]) -> list[SRFlip]:
    """
    S/R Flip: Ein gebrochener Swing-Punkt wechselt seine Rolle.
    - Bullisher BOS bricht Swing-High → dieses High wird zur neuen Support-Zone
    - Bearisher BOS bricht Swing-Low  → dieses Low wird zur neuen Resistance-Zone
    """
    flips: list[SRFlip] = []
    for bos in bos_events:
        if bos.direction == "bullish":
            flips.append(SRFlip("support", bos.level, strength=2))
        else:
            flips.append(SRFlip("resistance", bos.level, strength=2))
    return flips[-8:]


# ══ HTF-Trend ════════════════════════════════════════════════════════════════

def get_htf_bias(symbol: str, current_interval: str) -> str:
    """
    Holt den Trend-Bias auf dem nächsthöheren Zeitrahmen.
    Ergebnis: "bullish" | "bearish" | "neutral"
    Gecacht für 4 Stunden.
    """
    htf = HTF_MAP.get(current_interval, "1d")
    cache_key = f"{symbol}_{htf}"
    now_ts = time.time()

    if cache_key in _htf_cache:
        bias, expires = _htf_cache[cache_key]
        if now_ts < expires:
            return bias

    try:
        r = requests.get(
            f"{BINANCE_BASE}/klines",
            params={"symbol": symbol, "interval": htf, "limit": 300},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
        htf_df = pd.DataFrame(raw, columns=[
            "time","open","high","low","close","volume",
            "close_time","quote_vol","trades","tbb","tbq","ignore"
        ])
        for col in ["open","high","low","close"]:
            htf_df[col] = htf_df[col].astype(float)

        htf_swings = _find_swings(htf_df, window=4)
        bias = _trend_from_swings(htf_swings)

        # 4 Stunden cachen
        _htf_cache[cache_key] = (bias, now_ts + 4 * 3600)
        return bias
    except Exception as e:
        # Fallback: letzten bekannten Wert verwenden wenn nicht älter als 12h
        if cache_key in _htf_cache:
            stale_bias, _ = _htf_cache[cache_key]
            print(f"  [HTF] API-Fehler ({e}) → nutze gespeicherten Bias: {stale_bias}")
            return stale_bias
        return "neutral"


# ══ Premium / Discount ════════════════════════════════════════════════════════

def get_zone_position(price: float, zones: dict) -> str:
    """
    Premium: Preis über 50% der letzten Swing-Spanne → Short-Bereich.
    Discount: Preis unter 50% → Long-Bereich.
    """
    eq    = zones.get("equilibrium", price)
    p_bot = zones.get("premium_bottom", eq * 1.001)
    d_top = zones.get("discount_top",   eq * 0.999)

    if price > p_bot:
        return "premium"
    if price < d_top:
        return "discount"
    return "neutral"


# ══ Entry / SL / TP ══════════════════════════════════════════════════════════

def _calc_entry_sl_tp(price: float, bias: str,
                      confluence: EntryConfluence,
                      zones: dict,
                      timeframe: str = "4h") -> tuple[float, float, float]:
    """
    Präzise Entry / SL / TP Berechnung nach SMC-Regeln — TF-/ATR-kalibriert:

    LONG:
      Entry: beim bullishen OB (top) oder FVG (bottom) — näher am Preis
      SL:    unterhalb des Sweep-Lows / OB-Bottom — Puffer = 0.4× ATR
             (statt fixer 0.3%, die auf 15m zu weit und auf 1d zu eng sind)
      TP:    nächstes EQH oder letzter Swing-High; mindestens TF-Mindest-RR

    SHORT: Spiegelbildlich.
    Clamps sind pro Timeframe kalibriert und nie enger als 1.2× ATR
    (SL im Stop-Hunt-Bereich = vermeidbare Verluste).
    """
    import tf_profiles
    atr  = float(zones.get("atr") or 0.0)
    buf  = tf_profiles.sl_buffer(timeframe, price, atr)
    min_sl_dist, min_tp_dist = tf_profiles.sl_tp_clamps(timeframe, price, atr)
    rr_floor = tf_profiles.min_rr(timeframe)

    entry = price

    if bias == "bullish":
        # Entry: bestes Niveau aus OB oder FVG
        ob  = confluence.order_block
        fvg = confluence.fvg
        if ob and ob.contains(price):
            entry = ob.top        # Einstieg am OB-Top
        elif fvg and fvg.contains(price):
            entry = fvg.bottom    # Einstieg am FVG-Bottom

        # SL: unter Sweep-Low oder unter OB-Bottom (ATR-Puffer)
        if confluence.liq_sweep:
            sl = confluence.liq_sweep.level - buf
        elif ob:
            sl = ob.bottom - buf
        else:
            dz = zones.get("demand_zones", [])
            sl = (dz[0][0] - buf) if dz else price - min_sl_dist

        # Clamp ZUERST: SL nie enger als TF-Minimum / 1.2× ATR
        sl = min(sl, entry - min_sl_dist)

        # TP aus dem FINALEN SL ableiten — RR-Floor bleibt garantiert
        tp_target = zones.get("weak_high", price + min_tp_dist * 2)
        tp = max(tp_target, entry + (entry - sl) * rr_floor, entry + min_tp_dist)

    elif bias == "bearish":
        ob  = confluence.order_block
        fvg = confluence.fvg
        if ob and ob.contains(price):
            entry = ob.bottom
        elif fvg and fvg.contains(price):
            entry = fvg.top

        if confluence.liq_sweep:
            sl = confluence.liq_sweep.level + buf
        elif ob:
            sl = ob.top + buf
        else:
            sl = zones.get("weak_high", price + min_sl_dist) + buf

        # Clamp ZUERST: SL nie enger als TF-Minimum / 1.2× ATR
        sl = max(sl, entry + min_sl_dist)

        # TP aus dem FINALEN SL ableiten — RR-Floor bleibt garantiert
        dz = zones.get("demand_zones", [])
        tp_target = (dz[0][0] + buf * 0.5) if dz else price - min_tp_dist * 2
        tp = min(tp_target, entry - (sl - entry) * rr_floor, entry - min_tp_dist)

    else:
        return price, price - min_sl_dist, price + min_tp_dist * 2

    return round(entry, 4), round(sl, 4), round(tp, 4)


# ══ Haupt-Analyse ═════════════════════════════════════════════════════════════

def analyze(df: pd.DataFrame, zones: dict,
            timeframe: str = "4h",
            weights: Optional[dict] = None,
            symbol: str = "SOLUSDT") -> SignalResult:
    """
    Vollständige SMC/ICT-Analyse. Gibt ein SignalResult zurück.

    Entry-Regeln (exakt nach Spec):
    LONG:  HTF bullish + Discount + (Sweep EQL ODER CHoCH bullish)
           + (Bullish OB im Bereich ODER Bullish FVG im Bereich)
           → mindestens 2 optionale Confluences

    SHORT: HTF bearish + Premium + (Sweep EQH ODER CHoCH bearish)
           + (Bearish OB im Bereich ODER Bearish FVG im Bereich)
           → mindestens 2 optionale Confluences

    Kein Trade wenn HTF unklar (neutral).
    """
    price = float(zones["price_now"])
    now   = datetime.now(timezone.utc)

    # ── TF-Profil: Parameter auf den Timeframe kalibriert ─────────
    import tf_profiles
    prof    = tf_profiles.get(timeframe)
    atr_pct = float(zones.get("atr_pct") or 0.0)

    # ── Schritt 1: Basis-Signale ──────────────────────────────────
    swings   = _find_swings(df, window=prof["swing_window"])
    ltf_trend = _trend_from_swings(swings)
    htf_bias = get_htf_bias(symbol, timeframe)

    bos_events   = detect_bos(df, swings)
    choch_events = detect_choch(df, swings, ltf_trend)
    obs          = detect_order_blocks(df, bos_events)
    fvgs         = detect_fvg(df)
    eqh_lv, eql_lv = detect_eqh_eql(
        df, swings, tolerance=tf_profiles.eqh_tolerance(timeframe, atr_pct)
    )
    sweeps       = detect_liquidity_sweeps(df, eqh_lv, eql_lv)
    sr_flips     = detect_sr_flips(bos_events[-6:])
    zone_pos     = get_zone_position(price, zones)

    # ── Schritt 2: Confluence für Long und Short aufbauen ─────────
    bull_conf = _build_confluence("bullish", price, htf_bias, zone_pos,
                                  sweeps, choch_events, obs, fvgs,
                                  sr_flips, bos_events)
    bear_conf = _build_confluence("bearish", price, htf_bias, zone_pos,
                                  sweeps, choch_events, obs, fvgs,
                                  sr_flips, bos_events)

    # ── Schritt 3: Besten Bias wählen ────────────────────────────
    if bull_conf.is_valid and bear_conf.is_valid:
        # Beide valid → höheren Score nehmen
        confluence = bull_conf if bull_conf.score >= bear_conf.score else bear_conf
        bias = confluence.bias
    elif bull_conf.is_valid:
        confluence, bias = bull_conf, "bullish"
    elif bear_conf.is_valid:
        confluence, bias = bear_conf, "bearish"
    else:
        # Kein valides Setup → kein Trade
        best = bull_conf if bull_conf.score >= bear_conf.score else bear_conf
        return SignalResult(
            timestamp=now, bias="neutral", score=max(bull_conf.score, bear_conf.score),
            triggers=[], zone=zone_pos,
            entry=price, sl=price * 0.97, tp=price * 1.03, rr=0.0,
            htf_bias=htf_bias, confluence=best,
        )

    # ── Schritt 4: Entry / SL / TP berechnen (TF-/ATR-kalibriert) ─
    entry, sl, tp = _calc_entry_sl_tp(price, bias, confluence, zones, timeframe)
    rr = abs(tp - entry) / max(abs(entry - sl), 0.001)

    # Mindest-RR pro Timeframe (LTF = mehr Rauschen → strenger)
    if rr < tf_profiles.min_rr(timeframe):
        return SignalResult(
            timestamp=now, bias="neutral", score=confluence.score,
            triggers=confluence.triggers, zone=zone_pos,
            entry=entry, sl=sl, tp=tp, rr=round(rr, 2),
            htf_bias=htf_bias, confluence=confluence,
        )

    return SignalResult(
        timestamp=now, bias=bias, score=confluence.score,
        triggers=confluence.triggers, zone=zone_pos,
        entry=entry, sl=sl, tp=tp, rr=round(rr, 2),
        htf_bias=htf_bias, confluence=confluence,
    )


def _build_confluence(bias: str, price: float,
                      htf_bias: str, zone_pos: str,
                      sweeps: list[LiquiditySweep],
                      choch_events: list[ChochEvent],
                      obs: list[OrderBlock],
                      fvgs: list[FVG],
                      sr_flips: list[SRFlip],
                      bos_events: list[BosEvent]) -> EntryConfluence:
    """Baut die EntryConfluence für eine Richtung auf."""
    c = EntryConfluence(bias=bias)

    # Required: HTF-Trend
    if bias == "bullish":
        c.htf_aligned = htf_bias in ("bullish", "neutral")   # neutral = kein harter Ausschluss
    else:
        c.htf_aligned = htf_bias in ("bearish", "neutral")

    # Required: Zone
    if bias == "bullish":
        c.zone_aligned = zone_pos == "discount"
    else:
        c.zone_aligned = zone_pos == "premium"

    # Optional: Liquidity Sweep (stärkste Confluence)
    relevant_sweeps = [s for s in sweeps if s.direction == bias]
    if relevant_sweeps:
        c.liq_sweep = min(relevant_sweeps, key=lambda s: s.candles_ago)

    # Optional: CHoCH
    relevant_choch = [e for e in choch_events if e.direction == bias]
    if relevant_choch:
        c.choch = relevant_choch[-1]

    # Optional: Order Block im Bereich
    relevant_obs = [ob for ob in obs if ob.direction == bias and ob.contains(price)]
    if relevant_obs:
        c.order_block = min(relevant_obs, key=lambda ob: abs(ob.midpoint - price))

    # Optional: FVG im Bereich
    relevant_fvgs = [fvg for fvg in fvgs
                     if fvg.direction == bias and fvg.contains(price)]
    if relevant_fvgs:
        c.fvg = min(relevant_fvgs, key=lambda fvg: abs(fvg.midpoint - price))

    # Optional: S/R Flip
    if bias == "bullish":
        flips = [f for f in sr_flips if f.direction == "support"
                 and abs(f.level - price) / price < 0.01]
    else:
        flips = [f for f in sr_flips if f.direction == "resistance"
                 and abs(f.level - price) / price < 0.01]
    if flips:
        c.sr_flip = flips[0]

    # Bestätigender BOS
    relevant_bos = [b for b in bos_events if b.direction == bias]
    if relevant_bos:
        c.bos_confirms = relevant_bos[-1]

    return c
