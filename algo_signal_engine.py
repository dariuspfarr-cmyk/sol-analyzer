"""
Algo Signal Engine — vollständig algorithmischer Signalgenerator, $0 AI-Kosten.

Kein Anthropic-API-Key erforderlich.
Verwendet backtest_weights.json für Score-basierte Entscheidungen.
Gibt Algo-Alerts ab Score ≥ 70 im Terminal aus, ohne AI-Beteiligung.
Lernt kontinuierlich aus Outcomes und verbessert backtest_weights.json.
"""

import os
import json
from datetime import datetime, timezone
from pathlib import Path

import signal_logger
import backtest_learner

SYMBOL      = os.getenv("SYMBOL", "SOLUSDT")

SCORE_ALERT_THRESHOLD  = 70    # Score ≥ 70  → Terminal-Alert
SCORE_LOG_THRESHOLD    = 40    # Score 40-70 → nur DB-Log, kein Alert
MIN_SAMPLES_FOR_ALERT  = 10    # Mindest-Stichproben für vertrauenswürdigen Score


# ── Score berechnen ────────────────────────────────────────────────────────────
def score_setup(
    setup_type: str,
    timeframe:  str,
    bias:       str,
    zone_pos:   str,
    hour:       int,
    samples_override: int = 0,
) -> tuple[int, int, float]:
    """
    Berechnet Score (0-100) und gibt (score, samples, win_rate) zurück.
    Liest aus backtest_weights.json.
    """
    score, samples = backtest_learner.get_score(setup_type, timeframe, bias, zone_pos, hour)

    if samples_override > 0:
        samples = samples_override

    # Stündlichen Bonus integrieren — adaptives Mischgewicht nach Stichprobenanzahl
    try:
        weights    = _load_weights()
        hourly     = weights.get("hourly_performance", {})
        h_data     = hourly.get(str(hour), {})
        h_samples  = h_data.get("samples", 0) if h_data else 0
        if h_samples >= 5:
            h_score  = h_data.get("score", 50)
            # Mehr Stunden-Daten → mehr Gewicht (max. 30%)
            h_weight = min(0.30, h_samples / 100)
            score    = int(score * (1 - h_weight) + h_score * h_weight)
    except Exception:
        pass

    # Win-Rate aus Muster zurückholen
    try:
        weights = _load_weights()
        hb  = (hour // 3) * 3
        key = f"{setup_type}|{timeframe}|{bias}|{zone_pos}|{hb}"
        wr  = weights.get("patterns", {}).get(key, {}).get("win_rate", 0.0)
    except Exception:
        wr = 0.0

    # Nicht auf 100 cappen — Überschuss-Score kodiert "sehr hohe Konfidenz"
    # (routing/trading-Logik verwendet separaten Threshold, kein Verlust durch Cap)
    return score, samples, round(wr * 100, 1)


def _load_weights() -> dict:
    p = Path(__file__).parent / "backtest_weights.json"
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ── Setup-Analyse aus Zones ────────────────────────────────────────────────────
def analyze(
    zones:          dict,
    df,                         # pd.DataFrame
    trigger_reason: str,
    timeframe:      str,
) -> tuple[int, int, float, str, str]:
    """
    Analysiert ein getriggertes Setup und gibt zurück:
    (score, samples, win_rate_pct, primary_setup_type, bias)
    """
    setup_type, bias, _ = signal_logger._parse_trigger(trigger_reason)

    price = zones.get("price_now", 0)
    eq    = zones.get("equilibrium", price)
    p_bot = zones.get("premium_bottom", price * 1.05)
    d_top = zones.get("discount_top",   price * 0.95)

    if price >= p_bot:
        zone_pos = "premium"
    elif price <= d_top:
        zone_pos = "discount"
    else:
        zone_pos = "neutral"

    now  = datetime.now(timezone.utc)
    score, samples, wr = score_setup(setup_type, timeframe, bias, zone_pos, now.hour)

    # Synthetisierte Strategie-Regeln einbeziehen (zentrales Wissen) —
    # halbes Gewicht, da der Algo-Score bereits Pattern-Daten enthält.
    # Wirkt damit auch auf smart_router (Routing) und sol_analysis_bot (Alerts).
    try:
        import strategy_knowledge
        rules_mod, _ = strategy_knowledge.evaluate(setup_type, bias, zone_pos, now.hour)
        if rules_mod != 0.0:
            score = max(0, score + int(round(rules_mod * 0.5)))
    except Exception:
        pass

    # Multi-Timeframe-Alignment: HTF-Trends aus dem letzten MTF-Scan.
    # Bestätigende höhere Timeframes erhöhen den Score, widersprechende
    # senken ihn — Contra-HTF-Signale sind die häufigste Fehlsignal-Quelle.
    try:
        import mtf_analyzer
        mtf_mod = mtf_analyzer.alignment_modifier(timeframe, bias)
        if mtf_mod != 0.0:
            score = max(0, score + int(round(mtf_mod)))
    except Exception:
        pass

    return score, samples, wr, setup_type, bias


# ── Algo-Alert ausgeben ───────────────────────────────────────────────────────
def send_alert(
    zones:          dict,
    trigger_reason: str,
    timeframe:      str,
    score:          int,
    samples:        int,
    win_rate_pct:   float,
    setup_type:     str,
    bias:           str,
) -> bool:
    """
    Gibt einen Algo-Signal-Alert im Terminal aus.
    Gibt True zurück wenn ausgegeben.
    Funktioniert OHNE Anthropic-API-Key.
    """
    price = zones["price_now"]
    entry, sl, tp, _, _ = signal_logger._derive_sl_tp(zones, bias)
    bias_icon = "🟢 BULLISH" if bias == "bullish" else "🔴 BEARISH"
    rr_val    = abs(tp - entry) / max(abs(entry - sl), 0.01)

    msg = (
        f"📊 ALGO SIGNAL — {SYMBOL}\n"
        f"────────────────────────\n"
        f"Setup:       {setup_type} + {_zone_label(zones, price)}\n"
        f"Zeitrahmen:  {timeframe}\n"
        f"Bias:        {bias_icon}\n"
        f"Entry:       ${entry:,.2f}\n"
        f"SL:          ${sl:,.2f}\n"
        f"TP:          ${tp:,.2f}\n"
        f"R:R:         1:{rr_val:.1f}\n"
        f"────────────────────────\n"
        f"Score:       {score}/100 "
        f"(basierend auf {samples} historischen Setups)\n"
        f"Win Rate:    {win_rate_pct:.1f}% dieses Setups\n"
        f"KI-Analyse:  NEIN (Algo-Only, $0.00)\n"
        f"────────────────────────\n"
        f"Trigger: {trigger_reason[:120]}"
    )

    print("\n" + msg + "\n")
    print(f"  📊 Algo-Alert ausgegeben: {setup_type} {bias} Score={score}")
    return True


def _zone_label(zones: dict, price: float) -> str:
    if price <= zones.get("discount_top", 0):
        return "Discount Zone"
    if price >= zones.get("premium_bottom", 9e9):
        return "Premium Zone"
    return "Neutrale Zone"


# ── Signal verarbeiten ────────────────────────────────────────────────────────
def process_signal(
    zones:          dict,
    df,
    trigger_reason: str,
    timeframe:      str,
) -> dict:
    """
    Hauptfunktion: analysiert, bewertet und loggt ein Signal.
    Gibt Routing-Entscheidung zurück:
      {"action": "alert"|"log"|"skip", "score": int, "samples": int, ...}
    """
    score, samples, wr, stype, bias = analyze(zones, df, trigger_reason, timeframe)
    now = datetime.now(timezone.utc)

    result = {
        "action":     "skip",
        "score":      score,
        "samples":    samples,
        "win_rate":   wr,
        "setup_type": stype,
        "bias":       bias,
        "timestamp":  now.isoformat(),
    }

    if score < SCORE_LOG_THRESHOLD:
        result["action"] = "skip"
        return result

    if score >= SCORE_ALERT_THRESHOLD and samples >= MIN_SAMPLES_FOR_ALERT:
        result["action"] = "alert"
        alert_sent = send_alert(zones, trigger_reason, timeframe, score, samples, wr, stype, bias)
        result["alert_sent"] = alert_sent

        # In signals.db mit source='ALGO' und algo_score loggen
        sig_id = signal_logger.log_signal(
            zones=zones, df=df, trigger_reason=trigger_reason,
            api_model_used="ALGO", tokens_used=0, cost_usd=0.0,
            timeframe=timeframe,
            atr_pct=zones.get("atr_pct", 0.0),
            ema200_dist_pct=zones.get("ema200_dist_pct", 0.0),
        )
        # Setze algo_score und source nachträglich
        try:
            conn = signal_logger._conn()
            conn.execute("UPDATE signals SET algo_score=?, source=?, routing=? WHERE id=?",
                         (float(score), "ALGO", "algo", sig_id))
            conn.commit()
        except Exception:
            pass
        result["signal_id"] = sig_id

    elif score >= SCORE_LOG_THRESHOLD:
        result["action"] = "log"
        print(f"  📝 Algo-Log (kein Alert): {stype} Score={score} Samples={samples}")
        # Nur DB-Log ohne Alert
        sig_id = signal_logger.log_signal(
            zones=zones, df=df, trigger_reason=trigger_reason,
            api_model_used="ALGO_LOG", tokens_used=0, cost_usd=0.0,
            timeframe=timeframe,
            atr_pct=zones.get("atr_pct", 0.0),
            ema200_dist_pct=zones.get("ema200_dist_pct", 0.0),
        )
        try:
            conn = signal_logger._conn()
            conn.execute("UPDATE signals SET algo_score=?, source=?, routing=? WHERE id=?",
                         (float(score), "ALGO", "algo_log", sig_id))
            conn.commit()
        except Exception:
            pass
        result["signal_id"] = sig_id

    return result
