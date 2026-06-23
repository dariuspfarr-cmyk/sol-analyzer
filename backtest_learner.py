"""
Backtest Learner — extrahiert Mustergewichte aus historischen Backtest-Signalen.

Liest signals.db (source = BACKTEST oder alle), gruppiert nach:
  setup_type × timeframe × bias × zone_position × hour_of_day
Berechnet win_rate, sample_count, avg_rr pro Kombination.
Speichert in backtest_weights.json.

threshold_optimizer.py kombiniert diese Gewichte mit Live-Performance:
  Gewichtung: (live_samples / 2000) × 90% live, Rest aus Backtest
  Minimum live-Gewicht: 70%
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

WEIGHTS_FILE = Path(__file__).parent / "backtest_weights.json"
MAX_LIVE_WEIGHT = 0.90
MIN_LIVE_WEIGHT = 0.70


# ── Scoring-Formel (deterministisch, reproduzierbar) ─────────────────────────
def compute_score(win_rate: float, samples: int, avg_rr: float) -> int:
    """
    Score 0-100 basierend auf Win-Rate, Stichprobengröße und R:R.
    Saturiert bei 200 Samples, max. RR-Bonus bei 1:3.
    """
    base        = win_rate * 60                          # 0-60
    sample_conf = min(samples / 200, 1.0) * 25          # 0-25
    rr_bonus    = min(avg_rr / 3.0, 1.0) * 15           # 0-15
    return int(round(base + sample_conf + rr_bonus))


# ── Lerngewicht (steigt mit wachsenden Live-Daten) ────────────────────────────
def live_weight(live_closed: int) -> float:
    """70% live bei 0 Samples, 90% live bei 2000+ Samples."""
    w = MIN_LIVE_WEIGHT + (live_closed / 2000) * (MAX_LIVE_WEIGHT - MIN_LIVE_WEIGHT)
    return round(min(w, MAX_LIVE_WEIGHT), 4)


# ── Hauptfunktion ─────────────────────────────────────────────────────────────
def run() -> dict:
    """
    Berechnet Backtest-Gewichte und speichert backtest_weights.json.
    Gibt das Gewichte-Dict zurück.
    """
    import signal_logger

    signals = signal_logger.get_all_signals(include_open=False)
    closed  = [s for s in signals if s.get("outcome") in ("WIN", "LOSS", "EXPIRED")]

    if not closed:
        print("  ℹ️  Keine abgeschlossenen Signale für Backtest-Learner.")
        return {}

    # ── Muster-Aggregation ────────────────────────────────────────────────────
    # Schlüssel: "setup_type|timeframe|bias|zone_position|hour_bucket"
    patterns: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "wins": 0, "total_rr": 0.0,
        "setup_type": "", "timeframe": "", "bias": "",
        "zone_position": "", "hour_bucket": 0,
    })
    # Stündliche Aggregation (alle Setups)
    hourly: dict[int, dict] = {h: {"n": 0, "wins": 0} for h in range(24)}
    # Setup-Aggregation
    by_setup: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    # Zeitrahmen
    by_tf: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "dec": 0, "exp": 0})
    # Markt-Kontext (Fear & Greed Buckets + Market Bias)
    by_market_bias: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    by_fg_bucket:   dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    # ADX-Regime-Aggregation: trending / moderate / ranging
    by_adx_bucket:  dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "total_rr": 0.0})
    # ATR-Volatilitäts-Aggregation: high_vol / normal_vol / low_vol
    by_atr_bucket:  dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    # Trigger-Kombinations-Tracking: kommagetrennte Sortierung → win/loss
    by_trigger_combo: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    # MTF-Alignment-Buckets: aligned (≥+1) / neutral (0) / contra (≤−1)
    by_mtf_align: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    # MFE/MAE pro Setup
    mfe_mae_by_setup: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "wins": 0, "total_mfe": 0.0, "total_mae": 0.0, "total_candles": 0,
    })
    # Setup × ADX-Bucket Kreuz-Performance
    setup_adx: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "total_rr": 0.0})

    # Live-Erfahrung wiegt mehr als Backtest-Simulation: Backtest-Signale
    # zählen nur 0.6× — verhindert, dass simulierte 100%-WR-Muster die
    # echten Live-Ergebnisse übertönen.
    BACKTEST_WEIGHT = 0.6

    for s in closed:
        st     = s.get("setup_type", "Unknown")
        tf     = s.get("timeframe",  "4h")
        bias   = s.get("bias",       "neutral")
        zone   = s.get("zone_position", "neutral")
        hour   = int(s.get("time_of_day", 12))
        hb     = (hour // 3) * 3
        outcome  = s.get("outcome", "LOSS")
        src_live = (s.get("source") or "LIVE") != "BACKTEST"
        if outcome == "EXPIRED":
            # EXPIRED = schwaches Negativ: 0.3 für Live, 0.3×0.6 für Backtest
            w   = 0.3 if src_live else round(0.3 * BACKTEST_WEIGHT, 4)
            win = 0.0
        else:
            w   = 1.0 if src_live else BACKTEST_WEIGHT
            win = (1 if outcome == "WIN" else 0) * w
        rr     = float(s.get("reward_pct") or 0) / max(float(s.get("risk_pct") or 1), 0.01)
        mkt    = s.get("market_bias", "neutral") or "neutral"
        fg     = int(s.get("fear_greed") or 50)
        fg_bkt = "extreme_fear" if fg < 25 else "fear" if fg < 45 else "greed" if fg > 75 else "neutral_fg"

        # ADX-Regime aus gespeichertem adx_at_signal
        adx_v   = s.get("adx_at_signal")
        if adx_v is not None:
            adx_bkt = "trending" if float(adx_v) > 28 else "ranging" if float(adx_v) < 18 else "moderate"
        else:
            adx_bkt = None

        # ATR-Volatilität aus atr_pct
        atr_p   = s.get("atr_pct")
        if atr_p is not None:
            atr_bkt = "high_vol" if float(atr_p) > 3.0 else "low_vol" if float(atr_p) < 1.0 else "normal_vol"
        else:
            atr_bkt = None

        key = f"{st}|{tf}|{bias}|{zone}|{hb}"
        p   = patterns[key]
        p["n"]        += w
        p["wins"]     += win
        p["total_rr"] += rr * w
        p["setup_type"]    = st
        p["timeframe"]     = tf
        p["bias"]          = bias
        p["zone_position"] = zone
        p["hour_bucket"]   = hb

        hourly[hour]["n"]    += w
        hourly[hour]["wins"] += win
        by_setup[st]["n"]    += w
        by_setup[st]["wins"] += win
        by_tf[tf]["n"]       += w
        by_tf[tf]["wins"]    += win
        # Roh-Zähler für die Auflösungsquote (TP/SL getroffen vs. abgelaufen)
        if outcome == "EXPIRED":
            by_tf[tf]["exp"] += 1
        elif outcome in ("WIN", "LOSS"):
            by_tf[tf]["dec"] += 1
        by_market_bias[mkt]["n"]    += w
        by_market_bias[mkt]["wins"] += win
        by_fg_bucket[fg_bkt]["n"]   += w
        by_fg_bucket[fg_bkt]["wins"]+= win

        if adx_bkt:
            by_adx_bucket[adx_bkt]["n"]        += w
            by_adx_bucket[adx_bkt]["wins"]     += win
            by_adx_bucket[adx_bkt]["total_rr"] += rr * w
        if atr_bkt:
            by_atr_bucket[atr_bkt]["n"]    += w
            by_atr_bucket[atr_bkt]["wins"] += win

        # Trigger-Kombination (normalisiert: sortiert, kommagetrennt)
        try:
            import json as _j
            raw_triggers = s.get("all_triggers") or "[]"
            triggers_list = _j.loads(raw_triggers) if isinstance(raw_triggers, str) else raw_triggers
            combo_key = ",".join(sorted(str(t).upper() for t in triggers_list)) if triggers_list else "_none"
            by_trigger_combo[combo_key]["n"]    += w
            by_trigger_combo[combo_key]["wins"] += win
        except Exception:
            pass

        # MFE/MAE pro Setup
        mfe = s.get("mfe_pct")
        mae = s.get("mae_pct")
        if mfe is not None and mae is not None:
            mm = mfe_mae_by_setup[st]
            mm["n"]            += 1
            mm["wins"]         += win
            mm["total_mfe"]    += float(mfe)
            mm["total_mae"]    += float(mae)
            mm["total_candles"]+= int(s.get("candles_to_outcome") or 0)

        # Setup × ADX-Bucket Kreuz-Performance
        if adx_bkt:
            sa_key = f"{st}|{adx_bkt}"
            setup_adx[sa_key]["n"]        += w
            setup_adx[sa_key]["wins"]     += win
            setup_adx[sa_key]["total_rr"] += rr * w

        # MTF-Alignment-Bucket (nur Signale mit gespeichertem Alignment)
        mtf_a = s.get("mtf_alignment")
        if mtf_a is not None:
            a_bkt = "aligned" if int(mtf_a) >= 1 else "contra" if int(mtf_a) <= -1 else "neutral_mtf"
            by_mtf_align[a_bkt]["n"]    += w
            by_mtf_align[a_bkt]["wins"] += win

    # ── Scores berechnen ──────────────────────────────────────────────────────
    scored_patterns: dict[str, dict] = {}
    for key, p in patterns.items():
        if p["n"] < 5:           # zu wenig Daten
            continue
        wr     = p["wins"] / p["n"]
        avg_rr = p["total_rr"] / p["n"]
        score  = compute_score(wr, p["n"], avg_rr)
        scored_patterns[key] = {
            "key":          key,
            "setup_type":   p["setup_type"],
            "timeframe":    p["timeframe"],
            "bias":         p["bias"],
            "zone_position":p["zone_position"],
            "hour_bucket":  p["hour_bucket"],
            "win_rate":     round(wr, 4),
            "samples":      round(p["n"], 1),
            "avg_rr":       round(avg_rr, 3),
            "score":        score,
        }

    # ── Stündliche Performance ────────────────────────────────────────────────
    hourly_out = {}
    for h, d in hourly.items():
        if d["n"] > 0:
            hourly_out[str(h)] = {
                "win_rate": round(d["wins"]/d["n"], 4),
                "samples":  round(d["n"], 1),
                "score":    compute_score(d["wins"]/d["n"], d["n"], 1.5),
            }

    # ── Setup-Performance ─────────────────────────────────────────────────────
    setup_out = {}
    for st, d in by_setup.items():
        if d["n"] > 0:
            wr = d["wins"]/d["n"]
            setup_out[st] = {
                "win_rate": round(wr, 4),
                "samples":  round(d["n"], 1),
                "score":    compute_score(wr, d["n"], 1.5),
            }

    # ── Live-Gewicht berechnen ────────────────────────────────────────────────
    live_sigs = [s for s in closed if s.get("source") != "BACKTEST"]
    lw        = live_weight(len(live_sigs))

    # ── Markt-Kontext-Performance ─────────────────────────────────────────────
    market_bias_perf = {
        k: {"win_rate": round(d["wins"]/d["n"], 4), "samples": round(d["n"], 1),
            "score": compute_score(d["wins"]/d["n"], d["n"], 1.5)}
        for k, d in by_market_bias.items() if d["n"] > 0
    }
    fg_bucket_perf = {
        k: {"win_rate": round(d["wins"]/d["n"], 4), "samples": round(d["n"], 1),
            "score": compute_score(d["wins"]/d["n"], d["n"], 1.5)}
        for k, d in by_fg_bucket.items() if d["n"] > 0
    }

    # ── ADX-Regime-Performance ────────────────────────────────────────────────
    adx_bucket_perf = {}
    for bkt, d in by_adx_bucket.items():
        if d["n"] >= 3:
            wr     = d["wins"] / d["n"]
            avg_rr = d["total_rr"] / d["n"]
            adx_bucket_perf[bkt] = {
                "win_rate": round(wr, 4),
                "samples":  round(d["n"], 1),
                "avg_rr":   round(avg_rr, 3),
                "score":    compute_score(wr, d["n"], avg_rr),
            }

    # ── ATR-Volatilität-Performance ───────────────────────────────────────────
    atr_bucket_perf = {
        bkt: {"win_rate": round(d["wins"]/d["n"], 4), "samples": round(d["n"], 1),
              "score": compute_score(d["wins"]/d["n"], d["n"], 1.5)}
        for bkt, d in by_atr_bucket.items() if d["n"] >= 3
    }

    # ── Trigger-Kombinations-Performance ─────────────────────────────────────
    trigger_combo_perf = {}
    for combo, d in by_trigger_combo.items():
        if d["n"] >= 5:
            wr = d["wins"] / d["n"]
            trigger_combo_perf[combo] = {
                "win_rate": round(wr, 4),
                "samples":  round(d["n"], 1),
                "score":    compute_score(wr, d["n"], 1.5),
            }

    # ── MFE/MAE-Analyse pro Setup ─────────────────────────────────────────────
    mfe_mae_analysis = {}
    for st_k, mm in mfe_mae_by_setup.items():
        if mm["n"] >= 3:
            avg_mfe     = mm["total_mfe"]     / mm["n"]
            avg_mae     = mm["total_mae"]      / mm["n"]
            avg_candles = mm["total_candles"]  / mm["n"]
            mfe_mae_ratio = avg_mfe / max(avg_mae, 0.01)
            mfe_mae_analysis[st_k] = {
                "samples":         mm["n"],
                "win_rate":        round(mm["wins"] / mm["n"], 4),
                "avg_mfe_pct":     round(avg_mfe, 3),
                "avg_mae_pct":     round(avg_mae, 3),
                "mfe_mae_ratio":   round(mfe_mae_ratio, 3),
                "avg_candles":     round(avg_candles, 1),
            }

    # ── Setup × ADX-Bucket Kreuz-Performance ─────────────────────────────────
    setup_adx_perf = {}
    for sa_k, d in setup_adx.items():
        if d["n"] >= 3:
            wr     = d["wins"] / d["n"]
            avg_rr = d["total_rr"] / d["n"]
            setup_adx_perf[sa_k] = {
                "win_rate": round(wr, 4),
                "samples":  round(d["n"], 1),
                "avg_rr":   round(avg_rr, 3),
                "score":    compute_score(wr, d["n"], avg_rr),
            }

    # ── MTF-Alignment-Performance ─────────────────────────────────────────────
    mtf_align_perf = {
        bkt: {"win_rate": round(d["wins"]/d["n"], 4), "samples": round(d["n"], 1),
              "score": compute_score(d["wins"]/d["n"], d["n"], 1.5)}
        for bkt, d in by_mtf_align.items() if d["n"] >= 3
    }
    if mtf_align_perf:
        best_mtf = max(mtf_align_perf.items(), key=lambda x: x[1]["win_rate"])
        print(f"  [MTF-Lernen] Bestes Alignment: {best_mtf[0]} "
              f"WR={best_mtf[1]['win_rate']*100:.1f}% N={best_mtf[1]['samples']}")

    # Beste Markt-Bedingung ausgeben
    if market_bias_perf:
        best_mkt = max(market_bias_perf.items(), key=lambda x: x[1]["win_rate"])
        print(f"  [Research-Lernen] Beste Marktlage: {best_mkt[0]} "
              f"WR={best_mkt[1]['win_rate']*100:.1f}% N={best_mkt[1]['samples']}")
    if fg_bucket_perf:
        best_fg = max(fg_bucket_perf.items(), key=lambda x: x[1]["win_rate"])
        print(f"  [Research-Lernen] Bester F&G-Bereich: {best_fg[0]} "
              f"WR={best_fg[1]['win_rate']*100:.1f}% N={best_fg[1]['samples']}")
    if adx_bucket_perf:
        best_adx = max(adx_bucket_perf.items(), key=lambda x: x[1]["win_rate"])
        print(f"  [ADX-Lernen] Bestes Regime: {best_adx[0]} "
              f"WR={best_adx[1]['win_rate']*100:.1f}% N={best_adx[1]['samples']}")
    if trigger_combo_perf:
        best_combo = max(trigger_combo_perf.items(), key=lambda x: x[1]["score"])
        print(f"  [Combo-Lernen] Beste Kombination: {best_combo[0]} "
              f"WR={best_combo[1]['win_rate']*100:.1f}% N={best_combo[1]['samples']}")

    output = {
        "erstellt_am":    datetime.now(timezone.utc).isoformat(),
        "total_samples":  len(closed),
        "live_samples":   len(live_sigs),
        "live_weight":    lw,
        "backtest_weight": round(1.0 - lw, 4),
        "patterns":       scored_patterns,
        "hourly_performance":    hourly_out,
        "setup_performance":     setup_out,
        "timeframe_performance": {
            tf: {"win_rate": round(d["wins"]/d["n"],4), "samples": d["n"],
                 "decided": d["dec"], "expired": d["exp"],
                 "resolution_rate": round(d["dec"]/max(1, d["dec"]+d["exp"]), 4)}
            for tf, d in by_tf.items() if d["n"] > 0
        },
        "market_bias_performance": market_bias_perf,
        "fear_greed_performance":  fg_bucket_perf,
        "adx_bucket_performance":  adx_bucket_perf,
        "atr_bucket_performance":  atr_bucket_perf,
        "trigger_combo_performance": trigger_combo_perf,
        "mfe_mae_analysis":          mfe_mae_analysis,
        "setup_adx_performance":     setup_adx_perf,
        "mtf_alignment_performance": mtf_align_perf,
    }

    WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Bestes Muster ausgeben
    if scored_patterns:
        best = max(scored_patterns.values(), key=lambda x: x["score"])
        print(f"  🏆 Bestes Muster: {best['setup_type']} {best['bias']} "
              f"{best['timeframe']} {best['zone_position']} "
              f"→ Score {best['score']}/100  WR {best['win_rate']*100:.1f}%  "
              f"N={best['samples']}")

    print(f"  💾 backtest_weights.json: {len(scored_patterns)} Muster  "
          f"Live-Gewicht: {lw*100:.0f}%  Backtest-Gewicht: {(1-lw)*100:.0f}%")
    return output


# ── Öffentliche Lookup-API ────────────────────────────────────────────────────
def get_score(setup_type: str, timeframe: str, bias: str,
              zone_position: str, hour: int) -> tuple[int, int]:
    """
    Gibt (score, samples) für ein Setup zurück.
    Sucht zuerst exakten Key, dann Setup-Level-Fallback.
    """
    if not WEIGHTS_FILE.exists():
        return 50, 0

    try:
        with open(WEIGHTS_FILE, encoding="utf-8") as f:
            weights = json.load(f)
    except Exception:
        return 50, 0

    hb  = (hour // 3) * 3
    key = f"{setup_type}|{timeframe}|{bias}|{zone_position}|{hb}"

    if key in weights["patterns"]:
        p = weights["patterns"][key]
        return p["score"], p["samples"]

    # Fallback: Setup-Level
    sp = weights.get("setup_performance", {}).get(setup_type)
    if sp:
        return sp["score"], sp["samples"]

    return 50, 0   # neutrale Default-Werte


def get_weights_meta() -> dict:
    """Metadaten der aktuellen backtest_weights.json."""
    if not WEIGHTS_FILE.exists():
        return {}
    try:
        with open(WEIGHTS_FILE, encoding="utf-8") as f:
            w = json.load(f)
        return {k: v for k, v in w.items() if k != "patterns"}
    except Exception:
        return {}
