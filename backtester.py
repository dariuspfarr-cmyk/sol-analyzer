"""
Backtester — kostenlose Signalgenerierung auf historischen Binance-Daten.

- Kein API-Call außer Binance OHLCV (kostenlos, öffentlich)
- Kein Claude / kein Anthropic — $0.00 Kosten
- Replayed Kerzen chronologisch, wendet Layer-1-Filter an
- Speichert alle Signale mit sofortigem Outcome in signals.db
- Startet automatisch wenn signals.db < 200 Einträge hat
- Läuft jeden Montag 02:00 UTC neu auf frischen Daten
"""

import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

import signal_logger
import config as cfg

BINANCE_BASE = "https://api.binance.com/api/v3"
SYMBOL       = "SOLUSDT"
TIMEFRAMES   = ["4h", "30m"]
CANDLES      = 1000
OUTCOME_WINDOW = 50   # Kerzen für Outcome-Simulation
MIN_WINDOW     = 30   # Mindest-Kerzen für SMC-Zonen

# ── Inkrementelles Lernen ────────────────────────────────────────────────────
LEARN_EVERY_N      = 25   # nach jeweils N Trades Gewichte auf Disk schreiben
SCORE_MIN_SAMPLES  = 5    # erst ab N Samples eines Musters wird gefiltert
SCORE_SKIP_BELOW   = 25   # Signale mit Score < 25 überspringen (schlechtes Muster)
LIVE_WEIGHT_MULT   = 4    # jedes Live-Signal zählt wie N Backtest-Signale (Live ist verlässlicher)
NEW_LIVE_TRIGGER   = 30   # neuer Backtest wenn diese Anzahl neuer Live-Signale seit letztem Lauf


class _IncrementalWeights:
    """
    Hält in-memory Muster-Stats und berechnet Scores während des Backtests.
    Startet mit vorhandenem Live-Wissen (seed_from_live), sodass der Backtester
    von Beginn an von echten Handelsergebnissen profitiert.
    """
    def __init__(self):
        self.patterns:       dict[str, dict] = {}
        self.live_n:         int = 0    # Anzahl geladener Live-Signale
        self.live_skip_keys: set = set()   # Muster die Live klar schlechter als 40% WR

    def seed_from_live(self) -> int:
        """
        Lädt alle abgeschlossenen Live-Signale aus signals.db als Vorwissen.
        Jedes Live-Signal wird LIVE_WEIGHT_MULT-fach gewichtet (verlässlicher als Backtest).
        Live-Muster mit < 35% WR werden als harte Filterbedingung gemerkt.
        """
        try:
            import signal_logger
            signals = signal_logger.get_all_signals(include_open=False)
            live = [s for s in signals
                    if s.get("source") not in ("BACKTEST",)
                    and s.get("outcome") in ("WIN", "LOSS")]
            if not live:
                return 0

            # Temporäre Zählung für dynamische Schwellen
            tmp: dict[str, dict] = {}
            for s in live:
                st   = s.get("setup_type", "Unknown")
                bias = s.get("bias", "neutral")
                tf   = s.get("timeframe", "4h")
                zone = s.get("zone_position", "neutral")
                hour = int(s.get("time_of_day", 12))
                hb   = (hour // 3) * 3
                key  = f"{st}|{tf}|{bias}|{zone}|{hb}"
                if key not in tmp:
                    tmp[key] = {"n": 0, "wins": 0}
                tmp[key]["n"] += 1
                if s["outcome"] == "WIN":
                    tmp[key]["wins"] += 1
                # LIVE_WEIGHT_MULT-fach in die gemeinsamen Gewichte einfügen
                for _ in range(LIVE_WEIGHT_MULT):
                    self.update(st, bias, tf, zone, hour, s["outcome"])

            # Muster mit deutlich schlechter Live-WR merken (werden strenger gefiltert)
            for key, d in tmp.items():
                if d["n"] >= 5 and d["wins"] / d["n"] < 0.35:
                    self.live_skip_keys.add(key)

            self.live_n = len(live)
            print(f"  🔗 {len(live)} Live-Signale als Startwissen geladen "
                  f"(je {LIVE_WEIGHT_MULT}× gewichtet, "
                  f"{len(self.live_skip_keys)} Muster als schwach markiert)")
            return len(live)
        except Exception as e:
            print(f"  ⚠️  Live-Seed fehlgeschlagen: {e}")
            return 0

    def update(self, setup_type: str, bias: str, timeframe: str,
               zone: str, hour: int, outcome: str) -> None:
        hb  = (hour // 3) * 3
        key = f"{setup_type}|{timeframe}|{bias}|{zone}|{hb}"
        if key not in self.patterns:
            self.patterns[key] = {"n": 0, "wins": 0}
        self.patterns[key]["n"] += 1
        if outcome == "WIN":
            self.patterns[key]["wins"] += 1

    def score(self, setup_type: str, bias: str, timeframe: str,
              zone: str, hour: int) -> tuple[int, int, bool]:
        """
        Gibt (score, samples, live_confirmed) zurück.
        live_confirmed=True wenn das Muster durch Live-Daten bestätigt ist.
        Score 0-100.
        """
        from backtest_learner import compute_score
        hb  = (hour // 3) * 3
        key = f"{setup_type}|{timeframe}|{bias}|{zone}|{hb}"

        # Hartes Veto: Live-Daten sagen, dieses Muster verliert klar → überspringen
        if key in self.live_skip_keys:
            return 10, SCORE_MIN_SAMPLES, False

        p = self.patterns.get(key)
        if p and p["n"] >= SCORE_MIN_SAMPLES:
            wr            = p["wins"] / p["n"]
            sc            = compute_score(wr, p["n"], 1.5)
            live_conf     = key in {k for k in self.patterns
                                    if self.live_n > 0}
            return sc, p["n"], live_conf

        # Fallback: Setup-Level (TF + bias, ohne Zone und Stunde)
        total_n, total_w = 0, 0
        for k, v in self.patterns.items():
            if k.startswith(f"{setup_type}|{timeframe}|{bias}|"):
                total_n += v["n"]; total_w += v["wins"]
        if total_n >= SCORE_MIN_SAMPLES:
            return compute_score(total_w / total_n, total_n, 1.5), total_n, False

        return 50, 0, False   # neutral – noch keine Info

    def skip_threshold(self, live_confirmed: bool) -> int:
        """
        Dynamische Skip-Schwelle:
        • Live bestätigt → 20 (lockerer, Muster ist bewährt)
        • Keine Live-Info   → 25 (Standard)
        """
        return 20 if live_confirmed else SCORE_SKIP_BELOW

    def flush_to_disk(self) -> None:
        """Schreibt aktuellen Stand in backtest_weights.json."""
        try:
            import backtest_learner
            backtest_learner.run()
        except Exception:
            pass

_STAMP = Path(__file__).parent / ".last_backtest_run"


# ── Binance-Daten (nur OHLCV, kostenlos) ────────────────────────────────────
def _fetch_ohlcv(symbol: str, interval: str, limit: int = CANDLES) -> pd.DataFrame:
    r = requests.get(
        f"{BINANCE_BASE}/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=20,
    )
    r.raise_for_status()
    raw = r.json()
    df  = pd.DataFrame(raw, columns=[
        "time","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_buy_base","taker_buy_quote","ignore",
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    return df[["time","open","high","low","close","volume"]].copy()


# ── SMC-Zonen (identisch mit calc_smc_zones im Hauptbot) ────────────────────
def _calc_zones(df: pd.DataFrame) -> dict:
    close, high, low, n = df["close"].values, df["high"].values, df["low"].values, len(df)
    pivot_highs, pivot_lows = [], []
    for i in range(5, n - 5):
        if high[i] == max(high[i-5:i+6]):
            pivot_highs.append((i, high[i]))
        if low[i] == min(low[i-5:i+6]):
            pivot_lows.append((i, low[i]))
    recent_highs = sorted(pivot_highs[-6:], key=lambda x: x[1], reverse=True)
    recent_lows  = sorted(pivot_lows[-6:],  key=lambda x: x[1])
    price_now    = close[-1]
    sh, sl       = max(high), min(low)
    eq           = (sh + sl) / 2
    last_bos     = next((v for _, v in reversed(recent_highs) if v < price_now), None)
    if last_bos is None and recent_highs:
        last_bos = recent_highs[-1][1]
    demand_zones = []
    for i in range(n-2, max(n-60, 5), -1):
        if close[i] > close[i-1] * 1.005:
            demand_zones.append((low[i-1], close[i-1]))
            if len(demand_zones) == 3:
                break
    # Auffüllen falls nötig
    while len(demand_zones) < 3:
        off = len(demand_zones) * price_now * 0.015
        demand_zones.append((price_now - off - price_now*0.01, price_now - off))
    eql_cands = [l for _, l in recent_lows
                 if recent_lows and abs(l - recent_lows[0][1]) / max(recent_lows[0][1],1) < 0.01]
    return {
        "price_now":       price_now,
        "swing_high":      sh, "swing_low": sl, "equilibrium": eq,
        "premium_top":     sh, "premium_bottom": sh - (sh - eq) * 0.25,
        "discount_top":    sl + (eq - sl) * 0.25, "discount_bottom": sl,
        "weak_high":       max(high[-20:]),
        "last_bos":        last_bos,
        "demand_zones":    demand_zones,
        "choch_level":     min(low[-30:]) if n >= 30 else min(low),
        "eql_level":       sum(eql_cands)/len(eql_cands) if eql_cands else (recent_lows[0][1] if recent_lows else None),
        "pivot_highs":     pivot_highs, "pivot_lows": pivot_lows, "n": n,
    }


# ── Layer-1-Filter (identisch mit should_run_analysis) ────────────────────
def _layer1(df: pd.DataFrame, zones: dict) -> tuple[bool, str]:
    _close, high, low, vol = (df["close"].values, df["high"].values,
                              df["low"].values,   df["volume"].values)
    n, price  = len(df), zones["price_now"]
    ph, pl    = zones["pivot_highs"], zones["pivot_lows"]
    vol_mult  = float(cfg.get("VOLUME_SPIKE_MULTIPLIER"))
    triggers  = []

    # BOS
    if ph:
        sh = max(h for _, h in ph[-3:])
        if price > sh:
            triggers.append(f"BOS BULLISCH (${price:.2f} > ${sh:.2f})")
    if pl:
        sl = min(l for _, l in pl[-3:])
        if price < sl:
            triggers.append(f"BOS BÄRISCH (${price:.2f} < ${sl:.2f})")

    # CHoCH
    if n >= 10:
        h10, l10 = high[-10:-1], low[-10:-1]
        if all(h10[i] >= h10[i+1] for i in range(3)) and price > max(h10[-3:]):
            triggers.append("CHoCH BULLISCH (Break nach Abwärtsleg)")
        if all(l10[i] <= l10[i+1] for i in range(3)) and price < min(l10[-3:]):
            triggers.append("CHoCH BÄRISCH (Break nach Aufwärtsleg)")

    # Equal Highs / Lows
    TOLERANCE = 0.0015
    if len(ph) >= 2:
        vals = [h for _, h in ph[-5:]]
        if any(abs(vals[i]-vals[j])/vals[i] < TOLERANCE
               for i in range(len(vals)) for j in range(i+1, len(vals))):
            triggers.append(f"EQUAL HIGHS ~${vals[-1]:.2f}")
    if len(pl) >= 2:
        vals = [l for _, l in pl[-5:]]
        if any(abs(vals[i]-vals[j])/vals[i] < TOLERANCE
               for i in range(len(vals)) for j in range(i+1, len(vals))):
            triggers.append(f"EQUAL LOWS ~${vals[-1]:.2f}")

    # Premium / Discount
    if price < zones["discount_top"]:
        triggers.append(f"DISCOUNT ZONE (${price:.2f})")
    elif price > zones["premium_bottom"]:
        triggers.append(f"PREMIUM ZONE (${price:.2f})")

    # Volume Spike
    if n >= 21:
        avg = float(vol[-21:-1].mean())
        if vol[-1] > avg * vol_mult:
            triggers.append(f"VOLUME SPIKE ({vol[-1]:,.0f} > {vol_mult:.1f}× Ø {avg:,.0f})")

    if triggers:
        return True, " | ".join(triggers)
    return False, ""


# ── Outcome-Simulation (Look-Ahead, nur im Backtest erlaubt) ────────────────
def _simulate_outcome(
    df: pd.DataFrame, entry_idx: int,
    entry: float, sl: float, tp: float, bias: str,
) -> tuple[str, float, int]:
    future = df.iloc[entry_idx+1 : entry_idx+1+OUTCOME_WINDOW]
    for steps, (_, row) in enumerate(future.iterrows(), 1):
        if bias == "bullish":
            if row["low"] <= sl:
                return "LOSS", round((sl - entry) / entry * 100, 3), steps
            if row["high"] >= tp:
                return "WIN",  round((tp - entry) / entry * 100, 3), steps
        else:
            if row["high"] >= sl:
                return "LOSS", round((entry - sl) / entry * -100, 3), steps
            if row["low"] <= tp:
                return "WIN",  round((entry - tp) / entry * 100, 3), steps
    return "EXPIRED", 0.0, len(future)


# ── Backtest einer einzelnen Zeitreihe ──────────────────────────────────────
def _run_for_timeframe(df: pd.DataFrame, timeframe: str,
                       weights: "_IncrementalWeights") -> dict:
    results = {"signals": 0, "win": 0, "loss": 0, "expired": 0,
               "by_setup": defaultdict(lambda: {"n":0,"w":0}),
               "duplicate_skip": 0, "score_skip": 0, "learned_updates": 0}

    last_signal_idx: dict[str, int] = {}
    MIN_GAP = 3

    for idx in range(MIN_WINDOW, len(df) - OUTCOME_WINDOW - 1):
        sub   = df.iloc[:idx+1].copy()
        zones = _calc_zones(sub)
        triggered, reason = _layer1(sub, zones)
        if not triggered:
            continue

        _, bias, all_trig = signal_logger._parse_trigger(reason)
        ptype = all_trig[0] if all_trig else "Unknown"
        key   = f"{ptype}_{bias}"
        if last_signal_idx.get(key, -999) >= idx - MIN_GAP:
            results["duplicate_skip"] += 1
            continue
        last_signal_idx[key] = idx

        # ── Inkrementelles Lernen: Signal-Score aus bisherigen Backtest-Trades ──
        candle_ts = df["time"].iloc[idx]
        hour      = candle_ts.hour
        eq        = zones.get("equilibrium", zones["price_now"])
        p_bot     = zones.get("premium_bottom", eq * 1.05)
        d_top     = zones.get("discount_top",   eq * 0.95)
        price     = zones["price_now"]
        zone_pos  = ("premium" if price >= p_bot else
                     "discount" if price <= d_top else "neutral")

        score, samples, live_conf = weights.score(ptype, bias, timeframe, zone_pos, hour)
        threshold = weights.skip_threshold(live_conf)
        if samples >= SCORE_MIN_SAMPLES and score < threshold:
            results["score_skip"] += 1
            continue   # dieses Muster hat sich als schlecht erwiesen → überspringen

        entry, sl, tp, _, _ = signal_logger._derive_sl_tp(zones, bias)
        if sl <= 0 or tp <= 0 or abs(entry - sl) / entry > 0.25:
            continue

        outcome, pnl, candles = _simulate_outcome(df, idx, entry, sl, tp, bias)

        signal_logger.log_backtest_signal(
            zones=zones, df_window=sub, trigger_reason=reason,
            timeframe=timeframe, candle_ts=candle_ts,
            outcome=outcome, pnl_pct=pnl, candles_taken=candles,
        )

        # ── Inkrementelles Lernen: Ergebnis sofort in Gewichte einarbeiten ──────
        weights.update(ptype, bias, timeframe, zone_pos, hour, outcome)
        results["signals"]  += 1
        results["learned_updates"] += 1
        results[outcome.lower()] += 1
        results["by_setup"][ptype]["n"] += 1
        if outcome == "WIN":
            results["by_setup"][ptype]["w"] += 1

        # Alle LEARN_EVERY_N Signale: Gewichte auf Disk schreiben
        if results["signals"] % LEARN_EVERY_N == 0:
            weights.flush_to_disk()
            print(f"    📚 [{timeframe}] {results['signals']} Trades gelernt "
                  f"– Gewichte aktualisiert "
                  f"(Score-Skip bisher: {results['score_skip']})")

    return results


# ── Hauptfunktion ────────────────────────────────────────────────────────────
def run(force: bool = False) -> None:
    """
    Führt den vollständigen Backtest durch.
    Wird automatisch gestartet wenn signals.db < 200 Einträge hat,
    oder wenn force=True übergeben wird.
    """
    counts   = signal_logger.count()
    existing = counts["total"]

    if not force and existing >= 200:
        print(f"  ℹ️  Backtest übersprungen: bereits {existing} Signale in DB.")
        return

    print(f"\n{'═'*58}")
    print(f"  📊  BACKTEST STARTET — {SYMBOL}")
    print(f"  Zeiträume: {', '.join(TIMEFRAMES)}  |  {CANDLES} Kerzen je TF")
    print("  API-Kosten: $0.00 (nur Binance OHLCV, kostenlos)")
    print(f"{'═'*58}")

    all_results: dict[str, dict] = {}

    # Gewichte mit Live-Wissen vorbelegen — der Backtest startet nicht bei Null,
    # sondern kennt bereits welche Muster im echten Handel funktionieren.
    weights = _IncrementalWeights()
    weights.seed_from_live()

    for tf in TIMEFRAMES:
        print(f"\n  → Lade {CANDLES} {tf}-Kerzen für {SYMBOL}…")
        try:
            df = _fetch_ohlcv(SYMBOL, tf)
            print(f"  → {len(df)} Kerzen geladen. Starte Replay (lernt inkrementell)…")
        except Exception as e:
            print(f"  ⚠️  Fehler beim Laden von {tf}: {e}")
            continue

        result = _run_for_timeframe(df, tf, weights)
        all_results[tf] = result

    _STAMP.write_text(datetime.now(timezone.utc).isoformat())
    # Live-Signal-Anzahl zum Zeitpunkt des Backtests speichern (für Auto-Trigger)
    _LIVE_STAMP.write_text(str(_count_live_signals()))
    _print_summary(all_results)

    # Finale Gewichte auf Disk schreiben + vollständige Strategie-Überarbeitung
    print("\n  📚 Backtesting abgeschlossen – starte vollständigen Lernzyklus…")
    try:
        import strategy_evolver
        strategy_evolver.run(force=True)
    except Exception:
        # Fallback: nur backtest_learner
        try:
            import backtest_learner
            backtest_learner.run()
        except Exception as e2:
            print(f"  ⚠️  backtest_learner fehlgeschlagen: {e2}")


def _print_summary(results: dict) -> None:
    print(f"\n{'═'*58}")
    print("  📋  BACKTEST-ZUSAMMENFASSUNG")
    print(f"{'═'*58}")

    # Geschätzte Ersparnis
    haiku_cost = 0.00006
    sonnet_cost = 0.003  # ca. ~800 In + 400 Out Tokens Sonnet

    total_sigs = sum(r["signals"] for r in results.values())
    total_wins = sum(r["win"]     for r in results.values())
    total_loss = sum(r["loss"]    for r in results.values())
    total_exp  = sum(r["expired"] for r in results.values())
    closed     = total_wins + total_loss
    wr         = total_wins / closed * 100 if closed else 0

    print(f"\n  Gesamt: {total_sigs} Signale  |  "
          f"WIN: {total_wins}  LOSS: {total_loss}  EXPIRED: {total_exp}")
    print(f"  Win Rate (abgeschlossen): {wr:.1f}%")

    for tf, r in results.items():
        c = r["win"] + r["loss"]
        w = r["win"] / c * 100 if c else 0
        print(f"\n  [{tf}]  {r['signals']} Signale  WR: {w:.1f}%  "
              f"(dupliziert: {r['duplicate_skip']} übersprungen  "
              f"Score-gefiltert: {r.get('score_skip',0)}  "
              f"Lern-Updates: {r.get('learned_updates',0)})")
        print(f"    {'Setup':<12} {'Signale':>8}  {'Win-Rate':>10}")
        for stype, d in sorted(r["by_setup"].items()):
            swr = d["w"]/d["n"]*100 if d["n"] else 0
            flag = "🟢" if swr > 60 else ("🔴" if swr < 40 else "🟡")
            print(f"    {flag} {stype:<10} {d['n']:>8}  {swr:>9.1f}%")

    estimated_ai_cost = total_sigs * (haiku_cost + sonnet_cost * 0.4)
    print(f"\n  💰 Geschätzte AI-Kosten ohne Backtest: ${estimated_ai_cost:.4f}")
    print("  💰 Tatsächliche Backtest-Kosten:         $0.0000")
    print(f"  💰 Ersparnis durch historisches Lernen:  ${estimated_ai_cost:.4f}")
    print(f"{'═'*58}\n")


_LIVE_STAMP = Path(__file__).parent / ".last_backtest_live_count"


def _count_live_signals() -> int:
    """Zählt abgeschlossene Signale aus Live-Trading (nicht aus Backtest)."""
    try:
        conn = signal_logger._conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM signals "
            "WHERE (source IS NULL OR source NOT IN ('BACKTEST')) "
            "AND outcome IN ('WIN','LOSS')"
        ).fetchone()[0]
        return n
    except Exception:
        return 0


def should_run_now() -> bool:
    """
    True wenn Backtest fällig:
    • < 200 Signale in DB (Initialzustand)
    • Montag ~02:00 UTC (wöchentliche Auffrischung mit frischen Kerzen)
    • ≥ NEW_LIVE_TRIGGER neue Live-Signale seit letztem Backtest
      (neue echte Handelsdaten → Backtest-Gewichte neu einarbeiten)
    """
    counts = signal_logger.count()
    if counts["total"] < 200:
        return True

    now = datetime.now(timezone.utc)

    # Wöchentliche Auffrischung (Montag 02:00 UTC)
    if now.weekday() == 0 and now.hour == 2:
        if _STAMP.exists():
            try:
                last = datetime.fromisoformat(_STAMP.read_text().strip())
                if (now - last).total_seconds() < 20 * 3600:
                    pass   # heute schon gelaufen → trotzdem Live-Trigger prüfen
                else:
                    return True
            except Exception:
                return True

    # Auto-Trigger: genug neue Live-Signale seit letztem Backtest
    current_live = _count_live_signals()
    last_live = 0
    if _LIVE_STAMP.exists():
        try:
            last_live = int(_LIVE_STAMP.read_text().strip())
        except Exception:
            pass
    if current_live - last_live >= NEW_LIVE_TRIGGER:
        print(f"  🔄 Auto-Trigger: {current_live - last_live} neue Live-Signale "
              f"seit letztem Backtest → Backtest-Auffrischung")
        return True

    return False


if __name__ == "__main__":
    run(force=True)
