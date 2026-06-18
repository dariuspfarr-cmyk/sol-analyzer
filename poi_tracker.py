"""
Point-of-Interest (POI) Tracker

Erkennt Hochwahrscheinlichkeits-Rückkehrzonen und lernt deren Trefferquoten.
Gespeichert in poi_log.json.

POI-Typen & was sie bedeuten:
  FVG_BULL   — Bullishes Fair Value Gap: Preis übersprung Zone → kommt zurück zum Füllen
  FVG_BEAR   — Bärisches Fair Value Gap
  OB_DEMAND  — Order Block Demand: letzte Bären-Kerze vor starkem Aufwärts-Impuls
  OB_SUPPLY  — Order Block Supply: letzte Bullen-Kerze vor starkem Abwärts-Impuls
  BOS_BULL   — Gebrochenes Swing-High → Retest dieses Niveaus
  BOS_BEAR   — Gebrochenes Swing-Low → Retest
  EQL_ZONE   — Equal Lows: Liquiditäts-Pool unter Cluster von ähnlichen Tiefs
  EQH_ZONE   — Equal Highs: Liquiditäts-Pool über Cluster von ähnlichen Hochs

Lern-Zyklus:
  1. detect_pois(df)     → aktuelle POIs aus Kerzendaten
  2. log_pois(df)        → neue POIs ins poi_log.json schreiben
  3. update_outcomes(df) → prüfen ob aktive POIs getroffen wurden + Outcome setzen
  4. get_stats()         → Trefferquoten pro Typ berechnen
  5. get_active_pois(df) → angereicherte POI-Liste für Chart/Scoring

"continue_rate" = Wahrscheinlichkeit dass Preis nach dem Retest in POI-Richtung weiterzieht
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

BASE     = Path(__file__).parent
POI_FILE = BASE / "poi_log.json"

MAX_POIS         = 600
MAX_AGE_CANDLES  = 80    # POI läuft ab wenn nach N Kerzen kein Hit
MIN_FVG_SIZE_ATR = 0.25  # FVG muss mind. 25% eines ATR groß sein


# ── Persistenz ────────────────────────────────────────────────────────────────

def _load() -> list:
    if POI_FILE.exists():
        try:
            return json.loads(POI_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save(pois: list) -> None:
    POI_FILE.write_text(
        json.dumps(pois[-MAX_POIS:], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _atr14(df: pd.DataFrame) -> float:
    n = len(df)
    if n < 2:
        return 1.0
    trs = [
        max(
            float(df["high"].iloc[i]) - float(df["low"].iloc[i]),
            abs(float(df["high"].iloc[i]) - float(df["close"].iloc[i-1])),
            abs(float(df["low"].iloc[i])  - float(df["close"].iloc[i-1])),
        )
        for i in range(1, n)
    ]
    return sum(trs[-14:]) / min(14, len(trs)) if trs else 1.0


def _dedup(pois: list) -> list:
    """Entfernt nahezu identische POIs (gleicher Typ, Midpoint < 0.5% Abstand)."""
    seen:  list = []
    out:   list = []
    for p in pois:
        mp = p["midpoint"]
        duplicate = any(
            p["type"] == s["type"] and abs(mp - s["midpoint"]) / max(mp, 1) < 0.005
            for s in seen
        )
        if not duplicate:
            seen.append(p)
            out.append(p)
    return out


# ── POI-Erkennung ─────────────────────────────────────────────────────────────

def detect_pois(df: pd.DataFrame, lookback: int = 60) -> list:
    """
    Erkennt aktive POIs aus den letzten `lookback` Kerzen.
    Gibt nicht-gespeicherte Liste zurück (kein Schreiben).
    """
    pois: list = []
    n = len(df)
    if n < 5:
        return pois

    closes = df["close"].values.astype(float)
    highs  = df["high"].values.astype(float)
    lows   = df["low"].values.astype(float)
    opens  = df["open"].values.astype(float)
    atr    = _atr14(df)
    start  = max(1, n - lookback)

    for i in range(start, n - 1):
        ts = str(df.index[i])

        # ── Bullishes FVG ─────────────────────────────────────────────────────
        if i > 0:
            ph = highs[i-1]
            nl = lows[i+1]
            gap = nl - ph
            if gap >= atr * MIN_FVG_SIZE_ATR:
                mid    = (ph + nl) / 2
                filled = any(lows[j] <= mid for j in range(i+2, n))
                pois.append({
                    "type": "FVG_BULL", "direction": "bullish",
                    "high": round(nl, 4), "low": round(ph, 4),
                    "midpoint": round(mid, 4), "candle_idx": int(i),
                    "ts": ts, "filled": filled,
                    "strength": round(gap / atr, 2),
                })

        # ── Bärisches FVG ─────────────────────────────────────────────────────
        if i > 0:
            pl = lows[i-1]
            nh = highs[i+1]
            gap = pl - nh
            if gap >= atr * MIN_FVG_SIZE_ATR:
                mid    = (nh + pl) / 2
                filled = any(highs[j] >= mid for j in range(i+2, n))
                pois.append({
                    "type": "FVG_BEAR", "direction": "bearish",
                    "high": round(pl, 4), "low": round(nh, 4),
                    "midpoint": round(mid, 4), "candle_idx": int(i),
                    "ts": ts, "filled": filled,
                    "strength": round(gap / atr, 2),
                })

        # ── Order Block Demand ────────────────────────────────────────────────
        # Letzte bearishe Kerze direkt vor einem starken Aufwärts-Impuls
        if i >= 2 and i < n - 2:
            is_bear    = closes[i] < opens[i]
            impulse_up = (closes[i+1] - closes[i]) > atr * 1.3 or \
                         (i+2 < n and closes[i+2] - closes[i] > atr * 1.8)
            if is_bear and impulse_up:
                ob_h = max(opens[i], closes[i])
                ob_l = min(opens[i], closes[i])
                hit  = any(lows[j] <= ob_h and highs[j] >= ob_l for j in range(i+2, n))
                pois.append({
                    "type": "OB_DEMAND", "direction": "bullish",
                    "high": round(ob_h, 4), "low": round(ob_l, 4),
                    "midpoint": round((ob_h + ob_l) / 2, 4),
                    "candle_idx": int(i), "ts": ts, "filled": hit,
                    "strength": round((closes[i+1] - closes[i]) / atr, 2),
                })

        # ── Order Block Supply ────────────────────────────────────────────────
        # Letzte bullishe Kerze direkt vor einem starken Abwärts-Impuls
        if i >= 2 and i < n - 2:
            is_bull    = closes[i] > opens[i]
            impulse_dn = (closes[i] - closes[i+1]) > atr * 1.3 or \
                         (i+2 < n and closes[i] - closes[i+2] > atr * 1.8)
            if is_bull and impulse_dn:
                ob_h = max(opens[i], closes[i])
                ob_l = min(opens[i], closes[i])
                hit  = any(lows[j] <= ob_h and highs[j] >= ob_l for j in range(i+2, n))
                pois.append({
                    "type": "OB_SUPPLY", "direction": "bearish",
                    "high": round(ob_h, 4), "low": round(ob_l, 4),
                    "midpoint": round((ob_h + ob_l) / 2, 4),
                    "candle_idx": int(i), "ts": ts, "filled": hit,
                    "strength": round((closes[i] - closes[i+1]) / atr, 2),
                })

    # ── BOS-Retest + EQL/EQH (über calc_smc_zones) ───────────────────────────
    try:
        from sol_analysis_bot import calc_smc_zones
        zones = calc_smc_zones(df)
        now_ts = str(df.index[-1])

        # BOS Bull: gebrochenes Swing-High — Retest von oben
        last_bos = zones.get("last_bos")
        if last_bos and last_bos > 0:
            retested = any(
                abs(lows[j] - last_bos) / max(last_bos, 1) < 0.01
                for j in range(max(0, n-20), n)
            )
            pois.append({
                "type": "BOS_BULL", "direction": "bullish",
                "high": round(last_bos * 1.004, 4),
                "low":  round(last_bos * 0.996, 4),
                "midpoint": round(last_bos, 4),
                "candle_idx": n-1, "ts": now_ts, "filled": retested,
                "strength": 2.0,
            })

        # EQL Zone: Pool von gleichen Tiefs = Liquiditätsziel
        eql = zones.get("eql_level")
        if eql and eql > 0:
            swept = closes[-1] > eql * 1.003
            pois.append({
                "type": "EQL_ZONE", "direction": "bullish",
                "high": round(eql * 1.005, 4),
                "low":  round(eql * 0.995, 4),
                "midpoint": round(eql, 4),
                "candle_idx": n-1, "ts": now_ts, "filled": swept,
                "strength": 1.5,
            })

        # Weak High als EQH (potenzielle Liquiditäts-Zone über dem Markt)
        wh = zones.get("weak_high")
        price_now = closes[-1]
        if wh and wh > price_now * 1.002:
            pois.append({
                "type": "EQH_ZONE", "direction": "bearish",
                "high": round(wh * 1.005, 4),
                "low":  round(wh * 0.995, 4),
                "midpoint": round(wh, 4),
                "candle_idx": n-1, "ts": now_ts, "filled": False,
                "strength": 1.5,
            })

        # Demand Zones (Order Block Cluster aus SMC)
        for dz_lo, dz_hi in zones.get("demand_zones", []):
            touched = any(lows[j] <= dz_hi * 1.002 and highs[j] >= dz_lo * 0.998
                         for j in range(max(0, n-20), n))
            pois.append({
                "type": "OB_DEMAND", "direction": "bullish",
                "high": round(dz_hi, 4), "low": round(dz_lo, 4),
                "midpoint": round((dz_lo + dz_hi) / 2, 4),
                "candle_idx": n-1, "ts": now_ts, "filled": touched,
                "strength": 1.8,
            })
    except Exception:
        pass

    return _dedup(pois)


# ── Lern-Tracking ─────────────────────────────────────────────────────────────

def log_pois(df: pd.DataFrame) -> int:
    """
    Erkennt neue POIs und loggt sie ohne Duplikate.
    Gibt Anzahl neu geloggter POIs zurück.
    """
    existing = _load()
    existing_keys = {
        (e["type"], round(e.get("midpoint", 0) / 0.5) * 0.5)
        for e in existing
    }

    current  = detect_pois(df)
    added    = 0
    now      = datetime.now(timezone.utc).isoformat()
    price    = float(df["close"].iloc[-1])

    for p in current:
        key = (p["type"], round(p["midpoint"] / 0.5) * 0.5)
        if key in existing_keys:
            continue
        existing.append({
            "id":             f"{p['type']}_{p['ts']}",
            "type":           p["type"],
            "direction":      p["direction"],
            "high":           p["high"],
            "low":            p["low"],
            "midpoint":       p["midpoint"],
            "strength":       p.get("strength", 1.0),
            "created_ts":     now,
            "created_price":  round(price, 2),
            "status":         "active",
            "hit_ts":         None,
            "outcome":        None,     # "continue" | "reverse" | "expired"
            "outcome_ts":     None,
            "outcome_price":  None,
            "candles_to_hit": None,
            "_candles_alive": 0,
        })
        existing_keys.add(key)
        added += 1

    if added:
        _save(existing)
    return added


def update_outcomes(df: pd.DataFrame) -> int:
    """
    Aktualisiert den Outcome für alle aktiven POIs basierend auf der letzten Kerze.
    Outcome-Logik:
      'continue' → Schlusskurs setzt sich nach Retest in POI-Richtung durch
      'reverse'  → Schlusskurs bricht durch die Zone entgegen der POI-Richtung
      'expired'  → Zone wurde nie berührt innerhalb MAX_AGE_CANDLES
    """
    pois    = _load()
    updated = 0
    last    = df.iloc[-1]
    price   = float(last["close"])
    high_   = float(last["high"])
    low_    = float(last["low"])
    now_ts  = datetime.now(timezone.utc).isoformat()

    for p in pois:
        if p.get("status") != "active":
            continue

        p["_candles_alive"] = p.get("_candles_alive", 0) + 1

        # Ablaufen
        if p["_candles_alive"] > MAX_AGE_CANDLES:
            p["status"]  = "expired"
            p["outcome"] = "expired"
            updated += 1
            continue

        # Hit: aktuelle Kerze berührt die Zone
        if not (low_ <= p["high"] and high_ >= p["low"]):
            continue

        # Outcome: schließt Preis auf der "richtigen" Seite?
        if p["direction"] == "bullish":
            # Erfolg: Schlusskurs über der Zone (Preis hat gekauft und zog höher)
            success = price > p["high"]
        else:
            # Erfolg: Schlusskurs unter der Zone
            success = price < p["low"]

        p["status"]         = "hit"
        p["hit_ts"]         = now_ts
        p["outcome"]        = "continue" if success else "reverse"
        p["outcome_ts"]     = now_ts
        p["outcome_price"]  = round(price, 2)
        p["candles_to_hit"] = p["_candles_alive"]
        updated += 1

    if updated:
        _save(pois)
    return updated


# ── Statistiken ───────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """
    Berechnet Trefferquoten pro POI-Typ aus allen bisherigen Beobachtungen.
    Rückgabe:
      { "FVG_BULL": { "total":N, "hit":N, "continue":N,
                      "hit_rate":0.78, "continue_rate":0.82,
                      "confidence":0.64 }, ... }
    """
    pois:  list = _load()
    stats: dict = {}

    for p in pois:
        t = p["type"]
        if t not in stats:
            stats[t] = {"total": 0, "hit": 0, "continue": 0, "expired": 0}
        s = stats[t]
        s["total"] += 1
        outcome = p.get("outcome") or p.get("status")
        if outcome == "continue":
            s["hit"]      += 1
            s["continue"] += 1
        elif outcome == "reverse":
            s["hit"]      += 1
        elif outcome == "expired":
            s["expired"]  += 1

    for t, s in stats.items():
        decided           = s["hit"] + s["expired"]
        s["hit_rate"]      = round(s["hit"]      / decided,  3) if decided  > 0 else 0.0
        s["continue_rate"] = round(s["continue"] / s["hit"], 3) if s["hit"] > 0 else 0.0
        # Kombinierte Konfidenz: P(Hit) × P(Continue|Hit)
        s["confidence"]    = round(s["hit_rate"] * s["continue_rate"], 3)

    return stats


def get_active_pois(df: pd.DataFrame) -> list:
    """
    Gibt aktive (nicht abgelaufene/getroffene) POIs zurück,
    angereichert mit gelernten Erfolgsquoten. Max. 25 POIs, sortiert nach
    Entfernung zum aktuellen Preis.
    """
    pois  = _load()
    stats = get_stats()
    price = float(df["close"].iloc[-1]) if df is not None and len(df) else 0.0

    active = []
    for p in pois:
        if p.get("status") != "active":
            continue
        t = p["type"]
        s = stats.get(t, {})
        dist = abs(price - p["midpoint"]) / price * 100 if price else 99.0
        active.append({
            "type":          t,
            "direction":     p["direction"],
            "high":          p["high"],
            "low":           p["low"],
            "midpoint":      p["midpoint"],
            "strength":      p.get("strength", 1.0),
            "hit_rate":      s.get("hit_rate",      0.0),
            "continue_rate": s.get("continue_rate", 0.0),
            "confidence":    s.get("confidence",    0.0),
            "samples":       s.get("total",         0),
            "created_ts":    p.get("created_ts",    ""),
            "distance_pct":  round(dist, 2),
        })

    active.sort(key=lambda x: x["distance_pct"])
    return active[:25]


def get_score_boost(entry_price: float, direction: str) -> float:
    """
    Gibt einen Score-Boost zurück wenn entry_price nahe einem aktiven POI liegt.
    Wird von _score_signal() im paper_trader aufgerufen.
    Boost bis +20: hohe Konfidenz + geringer Abstand.
    """
    pois  = _load()
    stats = get_stats()

    best_boost = 0.0
    for p in pois:
        if p.get("status") != "active":
            continue
        if p["direction"] != direction:
            continue

        # Preis liegt innerhalb der Zone oder sehr nahe (≤1%)
        dist = abs(entry_price - p["midpoint"]) / max(entry_price, 1)
        if dist > 0.015:
            continue

        t  = p["type"]
        s  = stats.get(t, {})
        cr = s.get("continue_rate", 0.0)
        n  = s.get("total", 0)

        if n < 5 or cr < 0.55:
            continue

        # Boost skaliert mit Konfidenz und Nähe
        proximity_factor = 1.0 - dist / 0.015  # 1.0 = exakt in Zone, 0.0 = 1.5% weg
        raw_boost = cr * 20.0 * proximity_factor
        if raw_boost > best_boost:
            best_boost = raw_boost

    return round(best_boost, 2)


if __name__ == "__main__":
    import requests

    # Test: POIs aus echten Kerzendaten erkennen
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "SOLUSDT", "interval": "4h", "limit": 100},
        timeout=10,
    )
    raw = r.json()
    df_test = pd.DataFrame(raw, columns=[
        "t","open","high","low","close","volume",
        "ct","qav","trades","tbbav","tbqav","ignore"
    ])
    df_test[["open","high","low","close","volume"]] = df_test[
        ["open","high","low","close","volume"]
    ].astype(float)
    df_test["t"] = pd.to_datetime(df_test["t"], unit="ms", utc=True)
    df_test.set_index("t", inplace=True)

    pois = detect_pois(df_test)
    print(f"Erkannte POIs: {len(pois)}")
    for p in pois[:10]:
        print(f"  {p['type']:12s} {p['direction']:8s}  {p['low']:.2f}–{p['high']:.2f}"
              f"  mid={p['midpoint']:.2f}  strength={p['strength']:.1f}"
              f"  filled={'✓' if p['filled'] else '○'}")

    added = log_pois(df_test)
    print(f"\nNeu geloggt: {added} POIs")
    print(f"Statistiken: {get_stats()}")
