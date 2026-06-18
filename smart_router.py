"""
Smart Router — entscheidet pro Signal ob Algo-Only, KI oder Skip.

Routing-Regeln:
  Score > 75  UND  Samples ≥ 60  →  "algo"  (kostenlos, kein API-Call)
  Score 50-75 ODER Samples < 60  →  "ai"    (Haiku → Sonnet Pipeline)
  Score < 50                     →  "skip"  (vollständig überspringen)

Protokolliert jede Routing-Entscheidung für wöchentlichen Report.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import algo_signal_engine
import config as cfg

ROUTING_LOG = Path(__file__).parent / "routing_log.jsonl"
WEEKLY_REPORT = Path(__file__).parent / "routing_report.json"

# Routing-Schwellen (lesbar aus config.json, hier als Fallback)
_ALGO_SCORE_MIN      = 75
_ALGO_SAMPLES_MIN    = 60
_AI_SCORE_MIN        = 50
_AI_SAMPLES_FALLBACK = 60


# ── Routing-Entscheidung ──────────────────────────────────────────────────────
def route(
    zones:          dict,
    df,
    trigger_reason: str,
    timeframe:      str,
) -> tuple[str, int, int, float]:
    """
    Gibt (decision, score, samples, win_rate_pct) zurück.
    decision: "algo" | "ai" | "skip"
    """
    score, samples, wr, stype, bias = algo_signal_engine.analyze(
        zones, df, trigger_reason, timeframe
    )

    haiku_strict   = float(cfg.get("HAIKU_STRICTNESS"))
    # Haiku-Striktheit adjustiert den AI-Schwellenwert nach oben
    # Buffer von 3 Punkten verhindert Flip-Flop bei Grenzwerten
    HYSTERESIS = 3
    ai_threshold   = int(_AI_SCORE_MIN * haiku_strict)
    algo_threshold = _ALGO_SCORE_MIN

    if score >= algo_threshold + HYSTERESIS and samples >= _ALGO_SAMPLES_MIN:
        decision = "algo"
        reason   = (f"Score {score} ≥ {algo_threshold + HYSTERESIS} UND "
                    f"Samples {samples} ≥ {_ALGO_SAMPLES_MIN}")
    elif score >= algo_threshold and samples >= _ALGO_SAMPLES_MIN:
        # In der Hysterese-Zone: für "algo" bestätigt bleiben wenn letzter Entscheid algo war
        decision = "algo"
        reason   = f"Score {score} in Hysterese-Zone → algo (Samples OK)"
    elif score >= ai_threshold:
        decision = "ai"
        reason   = (f"Score {score} in AI-Bereich [{ai_threshold}…{algo_threshold}]"
                    f" (Haiku-Strict={haiku_strict:.2f})")
    else:
        decision = "skip"
        reason   = f"Score {score} < {ai_threshold} (Schwelle: Haiku-Striktheit {haiku_strict:.2f})"

    _log_decision(decision, score, samples, wr, stype, bias, timeframe, reason)

    print(f"  🔀 Router: {decision.upper()}  Score={score}  "
          f"Samples={samples}  WR={wr:.1f}%  Grund: {reason[:60]}")

    return decision, score, samples, wr


# ── Logging ───────────────────────────────────────────────────────────────────
def _log_decision(
    decision: str, score: int, samples: int, win_rate: float,
    setup_type: str, bias: str, timeframe: str, reason: str,
) -> None:
    entry = {
        "ts":         datetime.now(timezone.utc).isoformat(),
        "decision":   decision,
        "score":      score,
        "samples":    samples,
        "win_rate":   win_rate,
        "setup_type": setup_type,
        "bias":       bias,
        "timeframe":  timeframe,
        "reason":     reason,
    }
    ROUTING_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ROUTING_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Wöchentlicher Routing-Report ──────────────────────────────────────────────
def weekly_report() -> dict:
    """
    Berechnet Routing-Statistiken der letzten Woche und speichert sie.
    Gibt Report-Dict zurück.
    """
    if not ROUTING_LOG.exists():
        return {}

    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    counts: dict[str, int] = defaultdict(int)
    setups: dict[str, dict] = defaultdict(lambda: defaultdict(int))

    with open(ROUTING_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e  = json.loads(line)
                ts = datetime.fromisoformat(e["ts"])
                if ts < cutoff:
                    continue
                d = e["decision"]
                counts[d] += 1
                setups[e["setup_type"]][d] += 1
            except Exception:
                continue

    total = sum(counts.values())
    if total == 0:
        return {}

    # Kostenschätzung
    haiku_cost   = 0.00006
    sonnet_cost  = 0.003
    algo_count   = counts.get("algo", 0)
    ai_count     = counts.get("ai",   0)
    skip_count   = counts.get("skip", 0)

    ai_cost      = ai_count * (haiku_cost + sonnet_cost * 0.4)
    saved_cost   = algo_count * (haiku_cost + sonnet_cost * 0.4)

    report = {
        "erstellt_am":       datetime.now(timezone.utc).isoformat(),
        "zeitraum_tage":     7,
        "gesamt_signale":    total,
        "algo_signale":      algo_count,
        "ai_signale":        ai_count,
        "uebersprungen":     skip_count,
        "algo_quote_pct":    round(algo_count / total * 100, 1),
        "ai_kosten_usd":     round(ai_cost, 5),
        "gespartes_usd":     round(saved_cost, 5),
        "nach_setup":        {k: dict(v) for k, v in setups.items()},
    }

    with open(WEEKLY_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    _print_routing_report(report)
    return report


def _print_routing_report(r: dict) -> None:
    print(f"\n  ── Routing-Report (letzte 7 Tage) ──────────────────")
    print(f"  Signale gesamt:   {r['gesamt_signale']}")
    print(f"  📊 Algo-Only:     {r['algo_signale']} ({r['algo_quote_pct']:.1f}%)")
    print(f"  🤖 KI-Pipeline:   {r['ai_signale']}")
    print(f"  ✗  Übersprungen:  {r['uebersprungen']}")
    print(f"  💰 KI-Kosten:     ${r['ai_kosten_usd']:.5f}")
    print(f"  💰 Ersparnis:     ${r['gespartes_usd']:.5f} (durch Algo-Only)")
    print(f"  ───────────────────────────────────────────────────")


# ── Tages-Zusammenfassung ─────────────────────────────────────────────────────
def daily_summary(symbol: str = "SOLUSDT") -> None:
    """
    Gibt eine tägliche Zusammenfassung im Terminal aus (08:00 MEZ).
    Wird von sol_analysis_bot.py aufgerufen wenn die Zeit stimmt.
    """
    # Kosten dieses Monats
    try:
        import cost_tracker
        monthly = cost_tracker.get_monthly_total()
    except Exception:
        monthly = 0.0

    # Routing-Report (letzte 7 Tage)
    rpt = weekly_report()
    algo_q = rpt.get("algo_quote_pct", 0)
    saved  = rpt.get("gespartes_usd",  0)

    # Bestes Setup der Woche
    weights = algo_signal_engine._load_weights()
    best_setup = "—"
    if weights.get("patterns"):
        best = max(weights["patterns"].values(), key=lambda x: x.get("score", 0))
        best_setup = (f"{best.get('setup_type')} {best.get('bias')} "
                      f"{best.get('timeframe')} — Score {best.get('score')}/100 "
                      f"WR {best.get('win_rate',0)*100:.1f}%")

    # Modell-Status
    try:
        import local_filter_model
        mi       = local_filter_model.get_model_info()
        model_st = (f"✅ Aktiv (Acc. {mi['accuracy_pct']:.1f}%)"
                    if mi and mi.get("ersetzt_haiku") else "⏳ Haiku aktiv")
    except Exception:
        model_st = "—"

    # Signal-Zähler
    import signal_logger as sl
    cnt = sl.count()

    msg = (
        f"🌅 Tages-Zusammenfassung — {symbol}\n"
        f"{datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC\n"
        f"────────────────────────\n"
        f"Signale gesamt:   {cnt['total']}\n"
        f"  📊 Algo-Only:   {rpt.get('algo_signale',0)} ({algo_q:.1f}%)\n"
        f"  🤖 KI:          {rpt.get('ai_signale',0)}\n"
        f"Monatliche Kosten: ${monthly:.4f}\n"
        f"Ersparnis (7T):    ${saved:.4f}\n"
        f"────────────────────────\n"
        f"Bestes Setup:   {best_setup}\n"
        f"Lokales Modell: {model_st}\n"
    )

    print("\n" + msg)
