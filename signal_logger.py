"""
Signal Logger — persistente Erfassung und automatisches Outcome-Tracking.

Jedes SMC-Signal wird in signals.db (SQLite) gespeichert.
Nach jedem Kerzen-Close werden offene Signale automatisch aufgelöst:
  - TP getroffen vor SL → WIN
  - SL getroffen vor TP → LOSS
  - Nach 50 Kerzen nichts → EXPIRED
"""

import os
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

DB_PATH  = Path(__file__).parent / "signals.db"
_CONN    = None   # lazily initialized, shared within a process

# ── Hilfsdaten ────────────────────────────────────────────────────────────────
_SETUP_KEYWORDS = {
    "BOS":   ["BOS"],
    "CHoCH": ["CHOCH", "CHoCH"],
    "EQH":   ["EQUAL HIGH"],
    "EQL":   ["EQUAL LOW"],
    "Zone":  ["PREMIUM", "DISCOUNT"],
    "Volume":["VOLUME SPIKE"],
}

_BIAS_KEYWORDS = {
    "bullish": ["BULLISCH", "DISCOUNT"],  # discount = potential bullish setup
    "bearish": ["BÄRISCH",  "PREMIUM"],   # premium  = potential bearish setup
}


# ── Datenbank ─────────────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _init_db(_CONN)
        _migrate_db(_CONN)   # safe — adds missing columns if needed
    return _CONN


def _migrate_db(conn: sqlite3.Connection) -> None:
    """Fügt neue Spalten hinzu ohne bestehende Daten zu zerstören (safe migration)."""
    new_cols = {
        "source":           "TEXT DEFAULT 'LIVE'",  # 'LIVE' | 'BACKTEST' | 'ALGO'
        "algo_score":       "REAL",                  # 0-100 Score vom algo_signal_engine
        "routing":          "TEXT",                  # 'algo' | 'ai' | 'skip'
        "paper_traded":     "INTEGER DEFAULT 0",     # 0=nein  1=aktiv  2=abgeschlossen
        "paper_exit_price": "REAL",                  # tatsächlicher Exit-Preis (Paper Trader)
        "mfe_pct":          "REAL",                  # Max Favorable Excursion in % (bestes P&L während Trade)
        "mae_pct":          "REAL",                  # Max Adverse Excursion in % (schlechtestes P&L während Trade)
        "atr_pct":          "REAL",                  # ATR / Preis * 100 zum Signalzeitpunkt (Volatilitätskontext)
        "ema200_dist_pct":  "REAL",                  # Abstand EMA200 in % (Trendkontext)
        "market_bias":      "TEXT DEFAULT 'neutral'", # Research-Marktbias zur Signal-Zeit
        "fear_greed":       "INTEGER DEFAULT 50",     # Fear & Greed Wert (0-100) zur Signal-Zeit
        "adx_at_signal":    "REAL DEFAULT NULL",      # ADX-Wert zum Signal-/Trade-Eröffnungszeitpunkt
        "mtf_alignment":    "INTEGER DEFAULT NULL",   # Cross-TF-Alignment (−3…+3): HTFs einig mit Signal?
    }
    for col, definition in new_cols.items():
        try:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass   # Spalte existiert bereits


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id                INTEGER  PRIMARY KEY AUTOINCREMENT,
            timestamp         TEXT     NOT NULL,          -- ISO 8601 UTC
            timeframe         TEXT,                        -- "4h", "30m", …
            setup_type        TEXT,                        -- BOS/CHoCH/EQH/EQL/Zone/Volume
            all_triggers      TEXT,                        -- JSON-Array aller Trigger-Texte
            bias              TEXT,                        -- "bullish" | "bearish"
            entry_price       REAL,
            sl_price          REAL,
            tp_price          REAL,
            risk_pct          REAL,                        -- SL-Distanz in %
            reward_pct        REAL,                        -- TP-Distanz in %
            confidence        TEXT,                        -- "low" | "medium" | "high"
            confidence_score  REAL,                        -- numerischer Score 0.0…1.0
            api_model_used    TEXT,                        -- "haiku"/"sonnet"/"local_model"/"skipped"
            tokens_used       INTEGER  DEFAULT 0,
            cost_usd          REAL     DEFAULT 0.0,
            volume_ratio      REAL,                        -- vol / vol_ma20
            zone_position     TEXT,                        -- "premium"|"discount"|"neutral"
            time_of_day       INTEGER,                     -- Stunde 0-23
            day_of_week       INTEGER,                     -- 0=Mo … 6=So
            entry_candle_ts   TEXT,                        -- Timestamp der Einstiegskerze
            -- Outcome (wird nachträglich gefüllt)
            outcome           TEXT     DEFAULT NULL,       -- WIN/LOSS/EXPIRED
            pnl_pct           REAL     DEFAULT NULL,
            outcome_time      TEXT     DEFAULT NULL,
            candles_to_outcome INTEGER DEFAULT NULL,
            trigger_reason    TEXT                         -- Originaltext von Layer 1
        );

        CREATE INDEX IF NOT EXISTS idx_outcome   ON signals (outcome);
        CREATE INDEX IF NOT EXISTS idx_timestamp ON signals (timestamp);
        CREATE INDEX IF NOT EXISTS idx_setup     ON signals (setup_type);
        CREATE INDEX IF NOT EXISTS idx_paper     ON signals (paper_traded);
        CREATE INDEX IF NOT EXISTS idx_timeframe ON signals (timeframe);
        -- Composite für get_tradeable_signals (offen + nicht paper-getradet)
        CREATE INDEX IF NOT EXISTS idx_open_paper ON signals (outcome, paper_traded);
    """)
    conn.commit()


# ── Feature-Extraktion ────────────────────────────────────────────────────────
def _parse_trigger(trigger_reason: str) -> tuple[str, str, list[str]]:
    """Gibt (primary_setup_type, bias, alle_trigger_labels) zurück."""
    upper   = trigger_reason.upper()
    found   = []

    for stype, kws in _SETUP_KEYWORDS.items():
        if any(kw.upper() in upper for kw in kws):
            found.append(stype)

    primary = found[0] if found else "Unknown"

    bias = "neutral"
    for b, kws in _BIAS_KEYWORDS.items():
        if any(kw.upper() in upper for kw in kws):
            bias = b
            break

    # Fallback für sonst richtungslose Liquiditäts-Setups (häufig auf 4H/1D):
    # SMC-Reversal-Logik — Liquidität wird abgeholt, dann dreht der Markt.
    #   EQL (Equal Lows, Liquidität UNTER Markt) → Sweep + Reversal nach OBEN  = bullish
    #   EQH (Equal Highs, Liquidität ÜBER Markt) → Sweep + Reversal nach UNTEN = bearish
    # Explizite BOS/CHoCH-Richtung (oben) behält immer Vorrang.
    if bias == "neutral":
        if "EQUAL LOW" in upper:
            bias = "bullish"
        elif "EQUAL HIGH" in upper:
            bias = "bearish"

    return primary, bias, found


def _calc_confidence(found_triggers: list[str], volume_ratio: float,
                     cfg: Optional[dict] = None) -> tuple[str, float]:
    """Berechnet Konfidenz-Level und numerischen Score."""
    import config as _cfg
    weights = _cfg.load()

    score = 0.0
    for t in found_triggers:
        key = f"{t}_WEIGHT"
        score += weights.get(key, 1.0)

    # Volumen-Bonus
    if volume_ratio > 3.0:
        score += 0.5
    elif volume_ratio > 2.0:
        score += 0.25

    # Normalisieren auf 0…1 (max theoretisch ~6)
    norm = min(score / 6.0, 1.0)

    if norm >= 0.55:
        level = "high"
    elif norm >= 0.30:
        level = "medium"
    else:
        level = "low"

    return level, round(norm, 4)


def _derive_sl_tp(
    zones: dict, bias: str, timeframe: str = "4h", atr_pct: float = 0.0
) -> tuple[float, float, float, float, float]:
    """
    Leitet entry, SL und TP aus den SMC-Zonen ab — TF-kalibriert mit RR-Floor.
    Gibt (entry, sl, tp, risk_pct, reward_pct) zurück.

    Vorher: Flat-3%-Clamp für alle TFs → Ø R:R 1.10, viele EXPIRED.
    Jetzt:  tf_profiles-Clamps + RR-Floor garantiert (z. B. 2.2× auf 15m).
    """
    import tf_profiles
    entry   = zones["price_now"]
    atr     = entry * atr_pct / 100.0   # % → absolut (0 wenn nicht bekannt)

    buf            = tf_profiles.sl_buffer(timeframe, entry, atr)
    min_sl, min_tp = tf_profiles.sl_tp_clamps(timeframe, entry, atr)
    rr_floor       = tf_profiles.min_rr(timeframe)

    if bias == "bullish":
        dz       = zones.get("demand_zones", [])
        # Guard: zone_bot muss unter Entry liegen; sonst Fallback auf min_sl
        _zb      = float(dz[0][0]) if dz else None
        zone_bot = _zb if (_zb is not None and _zb < entry) else (entry - min_sl)
        # SL: unterhalb Zone-Boden mit Puffer, mindestens min_sl unter Entry
        sl = min(zone_bot - buf, entry - min_sl)

        # TP: Weak High als primäres Ziel; RR-Floor garantiert
        wh        = float(zones.get("weak_high") or 0.0)
        rr_min_tp = entry + max(min_tp, (entry - sl) * rr_floor)
        tp        = max(wh if wh > entry else 0.0, rr_min_tp)
    else:
        wh      = float(zones.get("weak_high") or 0.0)
        # SL: oberhalb Weak High mit Puffer, mindestens min_sl über Entry
        sl_cand = (wh + buf) if (wh and wh > entry) else (entry + min_sl)
        sl      = max(sl_cand, entry + min_sl)

        # TP: Demand-Zone-Boden als primäres Ziel; RR-Floor garantiert
        dz      = zones.get("demand_zones", [])
        dz_tgt  = float(dz[0][0]) if dz else 0.0
        rr_min_tp = entry - max(min_tp, (sl - entry) * rr_floor)
        # Nehme das weiter entfernte Ziel (besseres R:R)
        if dz_tgt and dz_tgt < entry:
            tp = min(dz_tgt, rr_min_tp)
        else:
            tp = rr_min_tp

    risk_pct   = abs(entry - sl) / entry * 100
    reward_pct = abs(tp - entry) / entry * 100
    return entry, sl, tp, round(risk_pct, 3), round(reward_pct, 3)


# ── Öffentliche API ───────────────────────────────────────────────────────────
def log_signal(
    zones:           dict,
    df:              pd.DataFrame,
    trigger_reason:  str,
    api_model_used:  str,
    tokens_used:     int,
    cost_usd:        float,
    timeframe:       str,
    atr_pct:         float = 0.0,
    ema200_dist_pct: float = 0.0,
    mtf_alignment:   Optional[int] = None,
) -> int:
    """
    Speichert ein neues Signal in der Datenbank.
    Gibt die neue Signal-ID zurück.
    """
    ts  = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc)

    setup_type, bias, all_triggers = _parse_trigger(trigger_reason)

    # Volumen-Ratio
    vol   = df["volume"].values
    vol_ratio = float(vol[-1] / vol[-21:-1].mean()) if len(vol) >= 21 else 1.0

    # Entry / SL / TP
    entry, sl, tp, risk_pct, reward_pct = _derive_sl_tp(zones, bias, timeframe=timeframe, atr_pct=atr_pct)

    # Konfidenz
    conf_level, conf_score = _calc_confidence(all_triggers, vol_ratio)

    # Zone-Position
    zones.get("equilibrium", entry)
    p_bot = zones.get("premium_bottom", entry * 1.05)
    d_top = zones.get("discount_top",   entry * 0.95)
    if entry >= p_bot:
        zone_pos = "premium"
    elif entry <= d_top:
        zone_pos = "discount"
    else:
        zone_pos = "neutral"

    entry_candle_ts = df["time"].iloc[-1].isoformat() if not df.empty else ts

    # Markt-Kontext aus Research-Cache (nicht blockierend)
    mkt_bias = "neutral"
    fg_val   = 50
    try:
        import web_researcher as _wr
        mkt_bias = _wr.get_market_bias()
        fg_val   = _wr.get_fear_greed()
    except Exception:
        pass

    conn = _conn()
    cur  = conn.execute(
        """INSERT INTO signals
           (timestamp, timeframe, setup_type, all_triggers, bias,
            entry_price, sl_price, tp_price, risk_pct, reward_pct,
            confidence, confidence_score, api_model_used, tokens_used, cost_usd,
            volume_ratio, zone_position, time_of_day, day_of_week,
            entry_candle_ts, trigger_reason,
            atr_pct, ema200_dist_pct, market_bias, fear_greed, mtf_alignment)
           VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?,?)""",
        (ts, timeframe, setup_type, json.dumps(all_triggers), bias,
         entry, sl, tp, risk_pct, reward_pct,
         conf_level, conf_score, api_model_used, tokens_used, cost_usd,
         round(vol_ratio, 3), zone_pos, now.hour, now.weekday(),
         entry_candle_ts, trigger_reason,
         round(atr_pct, 4), round(ema200_dist_pct, 4), mkt_bias, fg_val,
         mtf_alignment)
    )
    conn.commit()
    sig_id = cur.lastrowid
    print(f"  📝 Signal #{sig_id} gespeichert: {setup_type} {bias.upper()} "
          f"@ ${entry:.2f}  SL ${sl:.2f}  TP ${tp:.2f}  Konfidenz: {conf_level}")
    return sig_id


def _fetch_tf_candles(timeframe: str, limit: int = 300) -> Optional[pd.DataFrame]:
    """Holt OHLCV-Kerzen eines Timeframes von Binance (für TF-genaue Auflösung)."""
    import requests
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": os.getenv("SYMBOL", "SOLUSDT"),
                    "interval": timeframe, "limit": limit},
            timeout=12,
        )
        r.raise_for_status()
        df = pd.DataFrame(r.json(), columns=[
            "time", "open", "high", "low", "close", "volume",
            "close_time", "qv", "n", "tbb", "tbq", "ig"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
        return df[["time", "open", "high", "low", "close", "volume"]]
    except Exception:
        return None


def update_outcomes(df: pd.DataFrame, interval_hours: int = 4,
                    tf_dfs: Optional[dict] = None) -> int:
    """
    Prüft alle offenen Signale auf Win / Loss / Expired.
    Jedes Signal wird gegen die Kerzen SEINES EIGENEN Timeframes geprüft —
    ein 15m-Signal gegen 15m-Kerzen, ein 1d-Signal gegen Tageskerzen.
    (Vorher liefen alle gegen den übergebenen df → ungenau und falsche
    Expiry-Horizonte für Nicht-4h-Signale.)
    tf_dfs: optionale vorgeladene Kerzen {timeframe: df} (z. B. vom MTF-Scan).
    Gibt die Anzahl aufgelöster Signale zurück.
    """
    conn         = _conn()
    # paper_traded=1 → wird vom Paper Trader live überwacht, hier nicht simulieren
    open_signals = conn.execute(
        "SELECT * FROM signals WHERE outcome IS NULL AND (paper_traded IS NULL OR paper_traded < 1)"
    ).fetchall()

    if not open_signals:
        return 0

    # Kerzen-Cache pro Timeframe: vorgeladene nutzen, fehlende lazy holen
    candle_cache: dict[str, Optional[pd.DataFrame]] = dict(tf_dfs or {})

    def _df_for(tf: str) -> pd.DataFrame:
        if tf not in candle_cache:
            candle_cache[tf] = _fetch_tf_candles(tf)
        d = candle_cache[tf]
        return d if d is not None and not d.empty else df

    resolved = 0
    for sig in open_signals:
        try:
            entry_ts = pd.Timestamp(sig["entry_candle_ts"], tz="utc")
        except Exception:
            entry_ts = pd.Timestamp(sig["timestamp"], tz="utc")

        sig_df = _df_for(sig["timeframe"] or "4h")
        future = sig_df[sig_df["time"] > entry_ts].copy()
        if future.empty:
            continue

        n_future      = len(future)
        entry         = sig["entry_price"]
        sl            = sig["sl_price"]
        tp            = sig["tp_price"]
        bias          = sig["bias"]
        outcome       = None
        pnl_pct       = None
        candles_taken = None

        for i, (_, row) in enumerate(future.iterrows(), start=1):
            if bias == "bullish":
                if row["low"] <= sl:
                    outcome       = "LOSS"
                    pnl_pct       = round((sl - entry) / entry * 100, 3)
                    candles_taken = i
                    break
                if row["high"] >= tp:
                    outcome       = "WIN"
                    pnl_pct       = round((tp - entry) / entry * 100, 3)
                    candles_taken = i
                    break
            else:  # bearish
                if row["high"] >= sl:
                    outcome       = "LOSS"
                    pnl_pct       = round((entry - sl) / entry * 100, 3)  # sl>entry → negativ = Verlust
                    candles_taken = i
                    break
                if row["low"] <= tp:
                    outcome       = "WIN"
                    pnl_pct       = round((entry - tp) / entry * 100, 3)
                    candles_taken = i
                    break

        # Nach 50 Kerzen ablaufen lassen
        if outcome is None and n_future >= 50:
            outcome       = "EXPIRED"
            pnl_pct       = 0.0
            candles_taken = 50

        if outcome:
            conn.execute(
                """UPDATE signals
                   SET outcome=?, pnl_pct=?, outcome_time=?, candles_to_outcome=?
                   WHERE id=?""",
                (outcome, pnl_pct, datetime.now(timezone.utc).isoformat(),
                 candles_taken, sig["id"])
            )
            resolved += 1

    conn.commit()
    if resolved:
        print(f"  🔄 {resolved} Signal(e) aufgelöst.")
    return resolved


def get_all_signals(include_open: bool = True, limit: int | None = None) -> list[dict]:
    """
    Gibt Signale als Liste von Dicts zurück.
      • limit=None  → alle Zeilen, aufsteigend nach id (Default, für Training/Analyse).
      • limit=N     → die N NEUESTEN Zeilen (absteigend nach id) — vermeidet das
                      Laden der gesamten Tabelle für Dashboard-Abfragen.
    """
    query = "SELECT * FROM signals"
    if not include_open:
        query += " WHERE outcome IS NOT NULL"
    if limit is not None:
        rows = _conn().execute(query + " ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]          # neueste zuerst
    rows = _conn().execute(query + " ORDER BY id").fetchall()
    return [dict(r) for r in rows]              # aufsteigend (unverändert)


def log_backtest_signal(
    zones:          dict,
    df_window:      pd.DataFrame,
    trigger_reason: str,
    timeframe:      str,
    candle_ts,                      # pd.Timestamp der historischen Kerze
    outcome:        str,            # WIN | LOSS | EXPIRED (sofort bekannt im Backtest)
    pnl_pct:        float,
    candles_taken:  int,
) -> int:
    """
    Speichert ein Backtest-Signal mit sofort bekanntem Outcome.
    Wird NUR vom Backtester aufgerufen — kein API-Call.
    """
    import json as _json

    if isinstance(candle_ts, pd.Timestamp):
        ts_str = candle_ts.isoformat()
    else:
        ts_str = str(candle_ts)

    setup_type, bias, all_triggers = _parse_trigger(trigger_reason)
    vol   = df_window["volume"].values
    vr    = float(vol[-1] / vol[-21:-1].mean()) if len(vol) >= 21 else 1.0
    conf_level, conf_score = _calc_confidence(all_triggers, vr)
    entry, sl, tp, risk_pct, reward_pct = _derive_sl_tp(zones, bias, timeframe=timeframe)

    zones.get("equilibrium", entry)
    p_bot = zones.get("premium_bottom", entry * 1.05)
    d_top = zones.get("discount_top",   entry * 0.95)
    if entry >= p_bot:
        zone_pos = "premium"
    elif entry <= d_top:
        zone_pos = "discount"
    else:
        zone_pos = "neutral"

    if isinstance(candle_ts, pd.Timestamp):
        hour_val = candle_ts.hour
        dow_val  = candle_ts.dayofweek
    else:
        hour_val, dow_val = 12, 0

    conn = _conn()
    cur  = conn.execute(
        """INSERT INTO signals
           (timestamp, timeframe, setup_type, all_triggers, bias,
            entry_price, sl_price, tp_price, risk_pct, reward_pct,
            confidence, confidence_score, api_model_used, tokens_used, cost_usd,
            volume_ratio, zone_position, time_of_day, day_of_week,
            entry_candle_ts, trigger_reason,
            outcome, pnl_pct, outcome_time, candles_to_outcome, source)
           VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?,?)""",
        (ts_str, timeframe, setup_type, _json.dumps(all_triggers), bias,
         entry, sl, tp, risk_pct, reward_pct,
         conf_level, conf_score, "BACKTEST", 0, 0.0,
         round(vr, 3), zone_pos, hour_val, dow_val,
         ts_str, trigger_reason,
         outcome, pnl_pct, ts_str, candles_taken, "BACKTEST")
    )
    conn.commit()
    return cur.lastrowid


def log_paper_trade(
    direction:    str,       # "long" | "short"
    entry_price:  float,
    sl_price:     float,
    tp_price:     float,
    exit_price:   float,
    outcome:      str,       # "WIN" | "LOSS"
    pnl_pct:      float,
    triggers:     list[str],
    zone_pos:     str,
    score:        float,
    timeframe:    str,
    opened_at:    str,       # ISO timestamp
    closed_at:    str,       # ISO timestamp
) -> int:
    """
    Loggt einen abgeschlossenen Paper-Trade in signals.db.
    Verwendet die exakten Werte aus dem Paper Trader (keine Ableitung aus Zonen).
    source = 'PAPER' — damit strategy_evolver und XGBoost-Trainer davon lernen.
    """
    bias   = "bullish" if direction == "long" else "bearish"
    reason = " | ".join(triggers) if triggers else "PAPER"

    setup_map = {"BOS": "BOS", "CHoCH": "CHoCH", "FVG": "Zone",
                 "OB": "Zone", "SWEEP": "CHoCH", "EQH": "EQH", "EQL": "EQL"}
    setup_type = next((setup_map[t] for t in triggers if t in setup_map), "Zone")
    all_trig   = json.dumps(triggers)

    risk_pct   = abs(entry_price - sl_price) / entry_price * 100
    reward_pct = abs(tp_price - entry_price) / entry_price * 100

    try:
        opened_dt  = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        hour_val   = opened_dt.hour
        dow_val    = opened_dt.weekday()
    except Exception:
        hour_val, dow_val = 12, 0

    conn = _conn()
    cur  = conn.execute(
        """INSERT INTO signals
           (timestamp, timeframe, setup_type, all_triggers, bias,
            entry_price, sl_price, tp_price, risk_pct, reward_pct,
            confidence, confidence_score, api_model_used, tokens_used, cost_usd,
            volume_ratio, zone_position, time_of_day, day_of_week,
            entry_candle_ts, trigger_reason,
            outcome, pnl_pct, outcome_time, candles_to_outcome,
            source, algo_score, routing)
           VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?,?, ?,?)""",
        (opened_at, timeframe, setup_type, all_trig, bias,
         entry_price, sl_price, tp_price,
         round(risk_pct, 3), round(reward_pct, 3),
         "medium", round(score / 100, 4), "PAPER", 0, 0.0,
         1.0, zone_pos, hour_val, dow_val,
         opened_at, reason,
         outcome, round(pnl_pct, 3), closed_at, 0,
         "PAPER", float(score), "paper")
    )
    conn.commit()
    return cur.lastrowid


def log_autoki_signal(
    direction:  str,           # 'long' | 'short'
    entry:      float,
    sl:         float,
    tp:         float,
    rsi:        float,
    label:      str  = 'AUTO_KI',
    conf:       float = 0.5,   # KI-Konfidenz 0.0–1.0
    timeframe:  str  = '4h',
) -> int:
    """
    Speichert ein Auto-KI-Signal aus dem Live-Chart (JavaScript-Engine).
    Wird über POST /api/signals/submit aufgerufen.
    source='LIVE', routing='autoki' — Paper Trader liest es wie Bot-Signale.
    """
    now  = datetime.now(timezone.utc)
    ts   = now.isoformat()
    bias = "bullish" if direction == "long" else "bearish"

    sl_dist    = abs(entry - sl)
    risk_pct   = sl_dist / entry * 100 if entry > 0 else 0
    reward_pct = abs(tp - entry) / entry * 100 if entry > 0 else 0

    # Konfidenz-Level aus KI-Score
    conf_score = max(0.0, min(1.0, float(conf or 0.5)))
    if conf_score >= 0.60:
        conf_level = "high"
    elif conf_score >= 0.40:
        conf_level = "medium"
    else:
        conf_level = "low"

    setup_map = {
        "BREAK": "BOS", "BREAKOUT": "BOS",
        "BOUNCE": "Zone", "REVERSAL": "CHoCH",
    }
    setup_type = setup_map.get((label or "").upper().split()[0], "Zone")
    trigger    = f"AUTO_KI {label} RSI={rsi:.1f}"

    conn = _conn()
    cur  = conn.execute(
        """INSERT INTO signals
           (timestamp, timeframe, setup_type, all_triggers, bias,
            entry_price, sl_price, tp_price, risk_pct, reward_pct,
            confidence, confidence_score, api_model_used, tokens_used, cost_usd,
            volume_ratio, zone_position, time_of_day, day_of_week,
            entry_candle_ts, trigger_reason,
            source, routing)
           VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?, ?,?)""",
        (ts, timeframe, setup_type,
         json.dumps([f"RSI_{int(rsi)}", setup_type, "AUTO_KI"]), bias,
         round(entry, 4), round(sl, 4), round(tp, 4),
         round(risk_pct, 3), round(reward_pct, 3),
         conf_level, round(conf_score, 4), "AUTO_KI", 0, 0.0,
         1.0, "neutral", now.hour, now.weekday(),
         ts, trigger,
         "LIVE", "autoki")
    )
    conn.commit()
    sig_id = cur.lastrowid
    print(f"  📡 Auto-KI Signal #{sig_id}: {bias.upper()} @ ${entry:.2f}  "
          f"SL=${sl:.2f}  TP=${tp:.2f}  RSI={rsi:.1f}  Konfidenz={conf_level}")
    return sig_id


def get_tradeable_signals(max_age_hours: float = 8.0) -> list[dict]:
    """
    Gibt alle frischen, ungehandelten Signale zurück, die für den Paper Trader
    in Frage kommen — geordnet nach Score (beste zuerst).
    """
    conn = _conn()
    rows = conn.execute(
        """SELECT * FROM signals
           WHERE outcome IS NULL
             AND (paper_traded IS NULL OR paper_traded = 0)
             AND source IN ('LIVE', 'ALGO')
             AND (routing IS NULL OR routing != 'algo_log')
             AND entry_price IS NOT NULL
             AND sl_price    IS NOT NULL
             AND tp_price    IS NOT NULL
             AND bias        IS NOT NULL
             AND bias        != 'neutral'
           ORDER BY COALESCE(algo_score, confidence_score * 100, 0) DESC, id DESC"""
    ).fetchall()

    now    = datetime.now(timezone.utc)
    result = []
    for row in rows:
        d = dict(row)
        try:
            ts  = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
            age = (now - ts).total_seconds() / 3600
            # TF-spezifisches Höchstalter: 15m-Signale veralten in 2h,
            # 1d-Signale bleiben 48h gültig (Fallback: max_age_hours)
            try:
                import tf_profiles
                limit_h = float(tf_profiles.get(d.get("timeframe") or "4h")
                                .get("signal_max_age_h", max_age_hours))
            except Exception:
                limit_h = max_age_hours
            if age > limit_h:
                continue
        except Exception as e:
            print(f"  [Warnung] Ungültiger Timestamp für Signal #{d.get('id')}: {e}")
            continue
        result.append(d)
    return result


def mark_paper_trading(signal_id: int) -> None:
    """Markiert ein Signal als aktiv paper-getradet (verhindert Doppeleinstieg)."""
    conn = _conn()
    conn.execute("UPDATE signals SET paper_traded=1 WHERE id=?", (signal_id,))
    conn.commit()


def update_signal_adx(signal_id: int, adx_value: float) -> None:
    """Speichert den ADX-Wert zum Trade-Eröffnungszeitpunkt für späteres Regime-Lernen."""
    try:
        conn = _conn()
        conn.execute("UPDATE signals SET adx_at_signal=? WHERE id=?",
                     (round(float(adx_value), 2), signal_id))
        conn.commit()
    except Exception:
        pass


def update_signal_outcome(
    signal_id:     int,
    outcome:       str,    # "WIN" | "LOSS"
    pnl_pct:       float,
    exit_price:    float,
    closed_at:     str,    # ISO timestamp
    candles_taken: int   = 0,
    mfe_pct:       float = 0.0,   # Max Favorable Excursion in %
    mae_pct:       float = 0.0,   # Max Adverse Excursion in %
) -> None:
    """
    Schreibt das Trade-Ergebnis zurück auf die originale Signal-Row.
    Wird vom Paper Trader aufgerufen, wenn SL oder TP getroffen wurde.
    MFE/MAE liefern Kontext für zukünftige SL/TP-Optimierung.
    paper_traded=2 → abgeschlossen, nicht mehr aktiv.
    """
    conn = _conn()
    conn.execute(
        """UPDATE signals
           SET outcome=?, pnl_pct=?, outcome_time=?, candles_to_outcome=?,
               paper_traded=2, paper_exit_price=?,
               mfe_pct=?, mae_pct=?
           WHERE id=?""",
        (outcome, round(pnl_pct, 3), closed_at, candles_taken,
         round(exit_price, 4),
         round(mfe_pct, 3), round(mae_pct, 3),
         signal_id)
    )
    conn.commit()


def count() -> dict[str, int]:
    """Gibt Anzahl Signale nach Status zurück."""
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    wins  = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome='WIN'").fetchone()[0]
    loss  = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome='LOSS'").fetchone()[0]
    exp   = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome='EXPIRED'").fetchone()[0]
    pend  = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome IS NULL").fetchone()[0]
    return {"total": total, "win": wins, "loss": loss, "expired": exp, "open": pend}
