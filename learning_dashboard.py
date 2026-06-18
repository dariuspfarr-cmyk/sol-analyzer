"""
Learning Dashboard — Terminaltabelle aller Statistiken und Einsparungen.

Aufruf:  python learning_dashboard.py
         oder: import learning_dashboard; learning_dashboard.show()
"""

import json
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent


def _read_json(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _read_costs() -> dict[str, float]:
    log = BASE / "api_costs.jsonl"
    if not log.exists():
        return {}
    from collections import defaultdict
    totals: dict[str, float] = defaultdict(float)
    calls:  dict[str, int]   = defaultdict(int)
    now = datetime.now(timezone.utc)
    with open(log, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e  = json.loads(line)
                ts = datetime.fromisoformat(e["ts"])
                if ts.year == now.year and ts.month == now.month:
                    totals[e["model"]] += e.get("cost_usd", 0.0)
                    calls[e["model"]]  += 1
            except Exception:
                continue
    return {"totals": dict(totals), "calls": dict(calls)}


def _read_changes_log(n: int = 10) -> list[str]:
    log = BASE / "threshold_changes.log"
    if not log.exists():
        return []
    with open(log, encoding="utf-8") as f:
        lines = [l.rstrip() for l in f if l.strip()]
    return lines[-n:]


def show() -> None:
    """Druckt das vollständige Learning Dashboard ins Terminal."""
    perf  = _read_json(BASE / "performance_report.json")
    model = _read_json(BASE / "model_report.json")
    costs = _read_costs()
    import config as cfg

    W = 64
    def bar(label: str, value, width: int = 20, col: str = "─") -> str:
        return f"  {label:<28} {value}"

    def section(title: str) -> str:
        pad = (W - len(title) - 4) // 2
        return f"\n{'═' * pad}  {title}  {'═' * pad}"

    print("\n" + "═" * W)
    print(" " * 18 + "🧠  LEARNING DASHBOARD")
    print(" " * 18 + f"SOL/USDT Analyzer · {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC")
    print("═" * W)

    # ── Signal-Statistik ─────────────────────────────────────────────────────
    print(section("SIGNAL-DATENBANK"))
    try:
        import signal_logger
        cnt = signal_logger.count()
        print(bar("Signale gesamt:",     cnt["total"]))
        print(bar("  WIN:",              cnt["win"]))
        print(bar("  LOSS:",             cnt["loss"]))
        print(bar("  EXPIRED:",          cnt["expired"]))
        print(bar("  Noch offen:",       cnt["open"]))
        if cnt["win"] + cnt["loss"] > 0:
            wr = cnt["win"] / (cnt["win"] + cnt["loss"]) * 100
            print(bar("Win Rate:",           f"{wr:.1f}%"))
    except Exception as e:
        print(f"  ⚠️  Datenbank nicht erreichbar: {e}")

    # ── Performance nach Setup-Typ ────────────────────────────────────────────
    if perf.get("nach_setup_typ"):
        print(section("WIN-RATE NACH SETUP-TYP"))
        print(f"  {'Setup':<12}{'Signale':>8}{'Win-Rate':>10}{'Ø P&L':>9}{'Ø R:R':>8}")
        print("  " + "─" * 47)
        for st, d in sorted(perf["nach_setup_typ"].items()):
            wr   = d.get("win_rate_pct", 0)
            flag = "🔴" if wr < 45 else ("🟢" if wr > 65 else "🟡")
            print(f"  {flag} {st:<10}{d['count']:>7}{wr:>9.1f}%"
                  f"{d.get('avg_pnl_pct', 0):>+8.2f}%"
                  f"{d.get('avg_rr', 0):>8.2f}")

    # ── Modell-Status ────────────────────────────────────────────────────────
    print(section("LOKALES FILTERMODELL"))
    if model:
        acc = model.get("accuracy_pct", 0)
        status_icon = "✅" if model.get("ersetzt_haiku") else "⏳"
        print(bar("Status:", f"{status_icon} {'Aktiv (Haiku ersetzt)' if model.get('ersetzt_haiku') else 'Inaktiv (Haiku aktiv)'}"))
        print(bar("Accuracy:", f"{acc:.1f}%  (Min: {_get_min_accuracy()*100:.0f}%)"))
        print(bar("Trainings-Samples:", model.get("trainings_samples", 0)))
        print(bar("Trainiert am:", (model.get("erstellt_am") or "?")[:16]))
        fi = model.get("feature_importance", {})
        if fi:
            top3 = sorted(fi.items(), key=lambda x: -x[1])[:3]
            print(bar("Top Features:", ", ".join(f"{k} ({v:.3f})" for k,v in top3)))
    else:
        needed = 200
        try:
            import signal_logger
            cnt   = signal_logger.count()
            ready = cnt["win"] + cnt["loss"]
        except Exception:
            ready = 0
        print(f"  Kein Modell vorhanden — {ready}/{needed} abgeschlossene Signale")
        print(f"  ({'%.0f' % (ready/needed*100)}% der benötigten Trainingsdaten)")

    # ── API-Kosten ────────────────────────────────────────────────────────────
    print(section("API-KOSTEN (DIESER MONAT)"))
    if costs.get("totals"):
        total = sum(costs["totals"].values())
        for model_name, cost in sorted(costs["totals"].items()):
            calls_n = costs["calls"].get(model_name, "?")
            short   = model_name.replace("claude-", "").replace("-2025", "")
            print(f"  {short:<35} {calls_n:>4} Calls  ${cost:.5f}")
        print(f"  {'GESAMT':<35}              ${total:.5f}")
    else:
        print("  Noch keine Kosten aufgezeichnet.")

    # ── Ersparnisse ──────────────────────────────────────────────────────────
    print(section("EINSPARUNGEN DURCH SELF-LEARNING"))
    if perf.get("gesamt"):
        g = perf["gesamt"]
        saved_calls   = g.get("api_calls_saved", 0)
        haiku_cost_ea = 0.00006
        saved_usd     = saved_calls * haiku_cost_ea
        print(bar("API-Calls gespart (Monat):", f"{saved_calls}"))
        print(bar("Kostenersparnis Haiku:",     f"${saved_usd:.4f}"))

        # Effizienz
        api_calls = g.get("api_calls", 0)
        api_wr    = g.get("api_win_rate_pct", 0)
        print(bar("API-Calls → echte WINs:", f"{api_wr:.1f}% Effizienz ({api_calls} Calls)"))

    # ── Threshold-Änderungen ─────────────────────────────────────────────────
    print(section("LETZTE THRESHOLD-ÄNDERUNGEN"))
    changes = _read_changes_log(8)
    if changes:
        for line in changes:
            print(f"  {line}")
    else:
        print("  Noch keine automatischen Anpassungen.")

    # ── Aktuelle Thresholds ──────────────────────────────────────────────────
    print(section("AKTUELLE THRESHOLDS"))
    print(cfg.summary())

    print("═" * W + "\n")


def _get_min_accuracy() -> float:
    try:
        import config as cfg
        return float(cfg.get("LOCAL_MODEL_MIN_ACCURACY"))
    except Exception:
        return 0.60


if __name__ == "__main__":
    show()
