"""
Performance Analyzer — wöchentliche Auswertung aller gespeicherten Signale.

Wird jeden Sonntag automatisch ausgeführt.
Ergebnis wird in performance_report.json gespeichert und im Terminal ausgegeben.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPORT_FILE = Path(__file__).parent / "performance_report.json"
COST_LOG    = Path(__file__).parent / "api_costs.jsonl"


# ── Interne Hilfsfunktionen ───────────────────────────────────────────────────
def _win_rate(signals: list[dict]) -> float:
    """Win-Rate aus abgeschlossenen Signalen (WIN / (WIN+LOSS))."""
    closed = [s for s in signals if s.get("outcome") in ("WIN", "LOSS")]
    if not closed:
        return 0.0
    wins = sum(1 for s in closed if s["outcome"] == "WIN")
    return round(wins / len(closed) * 100, 2)


def _avg_rr(signals: list[dict]) -> float:
    """Durchschnittliches Reward-Risk-Verhältnis (reward_pct / risk_pct)."""
    valid = [s for s in signals
             if s.get("risk_pct") and s["risk_pct"] > 0 and s.get("reward_pct")]
    if not valid:
        return 0.0
    rrs = [s["reward_pct"] / s["risk_pct"] for s in valid]
    return round(sum(rrs) / len(rrs), 3)


def _avg_pnl(signals: list[dict]) -> float:
    closed = [s for s in signals if s.get("pnl_pct") is not None
              and s.get("outcome") in ("WIN", "LOSS")]
    if not closed:
        return 0.0
    return round(sum(s["pnl_pct"] for s in closed) / len(closed), 3)


def _group_by(signals: list[dict], key: str) -> dict[str, list[dict]]:
    groups: dict[str, list] = defaultdict(list)
    for s in signals:
        val = s.get(key) or "Unbekannt"
        groups[str(val)].append(s)
    return dict(groups)


def _signal_age_days(s: dict) -> float:
    """Alter eines Signals in Tagen (0 = heute)."""
    try:
        ts = datetime.fromisoformat(s.get("created_at") or s.get("timestamp") or "")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400)
    except Exception:
        return 9999.0


def _age_weighted_win_rate(signals: list[dict], half_life_days: float = 30.0) -> float:
    """Win-Rate mit exponentiellem Alters-Decay (neuere Signale gewichtiger)."""
    closed = [s for s in signals if s.get("outcome") in ("WIN", "LOSS")]
    if not closed:
        return 0.0
    now = datetime.now(timezone.utc)
    total_w = 0.0
    win_w   = 0.0
    for s in closed:
        try:
            ts  = datetime.fromisoformat(s.get("created_at") or s.get("timestamp") or "")
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = max(0, (now - ts).total_seconds() / 86400)
        except Exception:
            age_days = half_life_days   # neutrale Gewichtung bei fehlendem Datum
        w = math.exp(-math.log(2) * age_days / half_life_days)
        total_w += w
        if s["outcome"] == "WIN":
            win_w += w
    return round(win_w / total_w * 100, 2) if total_w > 0 else 0.0


def _consecutive_streak(signals: list[dict]) -> dict:
    """Berechnet aktuellen Win/Loss-Streak und maximale Drawdown-Streaks."""
    closed = sorted(
        [s for s in signals if s.get("outcome") in ("WIN", "LOSS")],
        key=lambda x: x.get("created_at") or x.get("timestamp") or "",
    )
    if not closed:
        return {}
    current = 0
    max_loss_streak = 0
    cur_loss = 0
    for s in closed:
        if s["outcome"] == "WIN":
            current = max(0, current) + 1
            cur_loss = 0
        else:
            current = min(0, current) - 1
            cur_loss += 1
            max_loss_streak = max(max_loss_streak, cur_loss)
    return {
        "current_streak":   current,           # positive = wins, negative = losses
        "max_loss_streak":  max_loss_streak,
    }


def _load_costs_this_month() -> dict[str, float]:
    """Liest api_costs.jsonl und summiert Kosten nach Modell für diesen Monat."""
    totals: dict[str, float] = defaultdict(float)
    if not COST_LOG.exists():
        return totals
    now = datetime.now(timezone.utc)
    with open(COST_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e  = json.loads(line)
                ts = datetime.fromisoformat(e["ts"])
                if ts.year == now.year and ts.month == now.month:
                    totals[e["model"]] += e.get("cost_usd", 0.0)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return dict(totals)


# ── Hauptauswertung ───────────────────────────────────────────────────────────
def run() -> dict[str, Any]:
    """
    Liest alle Signale, berechnet Statistiken, speichert report.json
    und gibt eine Deutsche Zusammenfassung ins Terminal aus.
    Gibt das Report-Dict zurück.
    """
    import signal_logger
    signals = signal_logger.get_all_signals(include_open=True)
    closed  = [s for s in signals if s.get("outcome") in ("WIN", "LOSS")]
    ts_now  = datetime.now(timezone.utc).isoformat()

    if not signals:
        print("\n⚠️  Noch keine Signale in der Datenbank – Analyse übersprungen.")
        return {}

    # ── 1. Gesamt-Statistik ──────────────────────────────────────────────────
    recent30 = [s for s in closed
                if _signal_age_days(s) <= 30]
    streak = _consecutive_streak(signals)
    overall = {
        "gesamt_signale":        len(signals),
        "geschlossen":           len(closed),
        "offen":                 sum(1 for s in signals if not s.get("outcome")),
        "abgelaufen":            sum(1 for s in signals if s.get("outcome") == "EXPIRED"),
        "win_rate_pct":          _win_rate(signals),
        "win_rate_pct_gewichtet": _age_weighted_win_rate(signals),
        "win_rate_pct_30d":      _win_rate(recent30),
        "n_30d":                 len(recent30),
        "avg_pnl_pct":           _avg_pnl(closed),
        "avg_pnl_pct_30d":       _avg_pnl(recent30),
        "avg_rr":                _avg_rr(signals),
        "current_streak":        streak.get("current_streak", 0),
        "max_loss_streak":       streak.get("max_loss_streak", 0),
    }

    # API-Signale: echte Win-Rate (nur GESCHLOSSENE) vs. Kosten-Ausbeute (pro Call).
    # Wichtig: die Win-Rate muss über WIN+LOSS gehen — offene/abgelaufene Signale
    # im Nenner verfälschen sie zu einer "Wins-pro-Call"-Ausbeute (≠ Win-Rate).
    def _is_api(s):
        return s.get("api_model_used") and s["api_model_used"] not in ("skipped", "local_model")
    api_calls   = len([s for s in signals if _is_api(s)])
    api_closed  = [s for s in signals if _is_api(s) and s.get("outcome") in ("WIN", "LOSS")]
    wins_from_api = sum(1 for s in api_closed if s["outcome"] == "WIN")
    api_win_rate = round(wins_from_api / len(api_closed) * 100, 2) if api_closed else 0.0
    api_yield    = round(wins_from_api / api_calls * 100, 2) if api_calls else 0.0
    overall["api_calls"]           = api_calls
    overall["api_closed"]          = len(api_closed)
    overall["api_win_rate_pct"]    = api_win_rate
    overall["api_win_yield_pct"]   = api_yield   # Wins pro Call (Kosten-Effizienz)
    overall["local_model_calls"]   = sum(1 for s in signals
                                         if s.get("api_model_used") == "local_model")
    overall["api_calls_saved"]     = overall["local_model_calls"]

    # ── 2. Pro Setup-Typ ─────────────────────────────────────────────────────
    by_setup: dict[str, dict] = {}
    for stype, grp in _group_by(signals, "setup_type").items():
        by_setup[stype] = {
            "count":       len(grp),
            "closed":      sum(1 for s in grp if s.get("outcome") in ("WIN", "LOSS")),
            "win_rate_pct": _win_rate(grp),
            "avg_pnl_pct": _avg_pnl([s for s in grp if s.get("outcome") in ("WIN","LOSS")]),
            "avg_rr":      _avg_rr(grp),
        }

    # ── 3. Pro Timeframe ─────────────────────────────────────────────────────
    by_tf: dict[str, dict] = {}
    for tf, grp in _group_by(signals, "timeframe").items():
        by_tf[tf] = {
            "count":       len(grp),
            "closed":      sum(1 for s in grp if s.get("outcome") in ("WIN", "LOSS")),
            "win_rate_pct": _win_rate(grp),
        }

    # ── 4. Pro Bias ──────────────────────────────────────────────────────────
    by_bias: dict[str, dict] = {}
    for bias, grp in _group_by(signals, "bias").items():
        by_bias[bias] = {
            "count":        len(grp),
            "closed":       sum(1 for s in grp if s.get("outcome") in ("WIN", "LOSS")),
            "win_rate_pct": _win_rate(grp),
            "avg_pnl_pct":  _avg_pnl([s for s in grp if s.get("outcome") in ("WIN","LOSS")]),
        }

    # ── 4b. Pro Setup × Bias (Interaktion — stärkster Win-Rate-Prädiktor) ─────
    # Marginale Setup-/Bias-Raten sind verwechselt (z. B. "BOS schlecht" ist in
    # Wahrheit "bullish-BOS schlecht, bearish-BOS gut"). Die Interaktion trennt das.
    by_setup_bias: dict[str, dict] = {}
    combo_groups: dict[tuple, list] = defaultdict(list)
    for s in signals:
        combo_groups[(s.get("setup_type", "?"), s.get("bias", "?"))].append(s)
    for (st, bias), grp in combo_groups.items():
        cl = [s for s in grp if s.get("outcome") in ("WIN", "LOSS")]
        by_setup_bias[f"{st}|{bias}"] = {
            "setup_type":   st,
            "bias":         bias,
            "count":        len(grp),
            "closed":       len(cl),
            "win_rate_pct": _win_rate(grp),
            "avg_pnl_pct":  _avg_pnl(cl),
            "avg_rr":       _avg_rr(grp),
        }

    # ── 5. Volumen-Filter-Analyse ─────────────────────────────────────────────
    vol_triggered = [s for s in signals if "Volume" in (s.get("all_triggers") or "")]
    vol_win_rate  = _win_rate(vol_triggered) if vol_triggered else None

    # ── 6. Monatliche API-Kosten ─────────────────────────────────────────────
    monthly_costs = _load_costs_this_month()

    # ── Report zusammenbauen ─────────────────────────────────────────────────
    report = {
        "erstellt_am":    ts_now,
        "gesamt":         overall,
        "nach_setup_typ": by_setup,
        "nach_timeframe": by_tf,
        "nach_bias":      by_bias,
        "nach_setup_bias": by_setup_bias,
        "volumen_filter": {
            "signale_mit_volume_spike": len(vol_triggered),
            "win_rate_pct": vol_win_rate,
        },
        "api_kosten_monat_usd": monthly_costs,
    }

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # ── Terminal-Ausgabe ─────────────────────────────────────────────────────
    _print_summary(report)
    return report


def _print_summary(r: dict) -> None:
    g      = r["gesamt"]
    costs  = r.get("api_kosten_monat_usd", {})
    total_cost = sum(costs.values())
    saved  = g.get("api_calls_saved", 0)
    haiku_price_per_call = 0.00006   # ~70 In + 5 Out Tokens Haiku

    print("\n" + "═" * 58)
    print("  📊  WÖCHENTLICHER PERFORMANCE-BERICHT")
    print("═" * 58)
    print(f"  Erstellt:        {r['erstellt_am'][:16]} UTC")
    print(f"  Signale gesamt:  {g['gesamt_signale']}  "
          f"(offen: {g['offen']}, abgelaufen: {g['abgelaufen']})")
    print(f"  Win Rate:        {g['win_rate_pct']:.1f}%  "
          f"(30d: {g.get('win_rate_pct_30d',0):.1f}% N={g.get('n_30d',0)})  "
          f"(gewichtet: {g.get('win_rate_pct_gewichtet',0):.1f}%)")
    print(f"  Ø P&L:           {g['avg_pnl_pct']:+.2f}%  (30d: {g.get('avg_pnl_pct_30d',0):+.2f}%)")
    print(f"  Ø R:R:           {g['avg_rr']:.2f}")
    streak = g.get("current_streak", 0)
    streak_str = (f"🔥 +{streak} Wins" if streak > 0 else f"⛔ {streak} Losses" if streak < 0 else "neutral")
    print(f"  Streak:          {streak_str}  "
          f"(max Loss-Streak: {g.get('max_loss_streak',0)})")
    print(f"  API-Calls:       {g['api_calls']}  →  Win-Rate (geschlossen): "
          f"{g['api_win_rate_pct']:.1f}%  ·  Wins/Call: {g.get('api_win_yield_pct', 0):.1f}%")
    print(f"  Gespartes Haiku: {saved} Calls ≈ ${saved * haiku_price_per_call:.4f}")
    print(f"  Monatliche Kosten: ${total_cost:.4f}")

    print("\n  Nach Setup-Typ:")
    for st, d in sorted(r["nach_setup_typ"].items()):
        flag = "⚠️ " if d["win_rate_pct"] < 45 else ("✅" if d["win_rate_pct"] > 65 else "  ")
        print(f"    {flag} {st:<10}  N={d['count']:<4}  "
              f"WR={d['win_rate_pct']:.1f}%  Ø P&L={d['avg_pnl_pct']:+.2f}%")

    print("\n  Nach Bias:")
    for bias, d in r["nach_bias"].items():
        print(f"    {bias:<10}  N={d['count']:<4}  WR={d['win_rate_pct']:.1f}%")

    vol = r["volumen_filter"]
    if vol["win_rate_pct"] is not None:
        print(f"\n  Volumen-Spike-Filter:  N={vol['signale_mit_volume_spike']}  "
              f"WR={vol['win_rate_pct']:.1f}%")
    print("═" * 58 + "\n")
