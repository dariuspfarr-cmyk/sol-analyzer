"""
Strategy Evolver — orchestriert den vollständigen Lernzyklus nach jedem Bot-Lauf.

Wird automatisch aufgerufen:
  • nach jedem automatischen oder manuellen Analyse-Lauf (server.py)
  • nach jedem Backtest-Lauf (backtester.py)

Führt aus:
  1. backtest_learner   → backtest_weights.json aktualisieren
  2. performance_analyzer → performance_report.json aktualisieren
  3. threshold_optimizer  → config.json Thresholds anpassen
  4. local_filter_model   → XGBoost neu trainieren wenn ≥ 200 Signale

Alles wird in strategy_evolution.json protokolliert (max. 200 Einträge).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

EVOLUTION_FILE  = Path(__file__).parent / "strategy_evolution.json"
CONFIG_BACKUP   = Path(__file__).parent / "config_backup.json"
MIN_NEW_SIGNALS = 10   # mindestens N neue abgeschlossene Signale (statistisch belastbar)


def _load() -> list:
    if EVOLUTION_FILE.exists():
        try:
            with open(EVOLUTION_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save(log: list) -> None:
    EVOLUTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(EVOLUTION_FILE, "w", encoding="utf-8") as f:
        json.dump(log[-200:], f, indent=2, ensure_ascii=False)


def _backup_config() -> bool:
    """Sichert config.json vor Optimizer-Lauf. Gibt True zurück wenn erfolgreich."""
    try:
        import config as cfg
        current = cfg.load()
        with open(CONFIG_BACKUP, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        return True
    except Exception as e:
        print(f"  [Evolver] Config-Backup fehlgeschlagen: {e}")
        return False


def _restore_config() -> bool:
    """Stellt config.json aus Backup wieder her. Gibt True zurück wenn erfolgreich."""
    if not CONFIG_BACKUP.exists():
        return False
    try:
        import config as cfg
        with open(CONFIG_BACKUP, encoding="utf-8") as f:
            backup = json.load(f)
        cfg.save(backup)
        print("  [Evolver] ⚠️  Config aus Backup wiederhergestellt (Rollback)")
        return True
    except Exception as e:
        print(f"  [Evolver] Rollback fehlgeschlagen: {e}")
        return False


def _current_metrics() -> dict:
    """Schnappschuss der wichtigsten Metriken für Vorher/Nachher-Vergleich."""
    out = {}
    try:
        import signal_logger
        cnt = signal_logger.count()
        decided = cnt["win"] + cnt["loss"]
        out["win_rate"]    = round(cnt["win"] / decided * 100, 1) if decided else None
        out["total"]       = cnt["total"]
        out["win"]         = cnt["win"]
        out["loss"]        = cnt["loss"]
    except Exception:
        pass
    try:
        import backtest_learner
        m = backtest_learner.get_weights_meta()
        out["patterns"]     = len(m.get("patterns", {})) if "patterns" in m else None
        out["live_weight"]  = m.get("live_weight")
        out["bt_samples"]   = m.get("total_samples")
    except Exception:
        pass
    return out


def run(force: bool = False) -> dict:
    """
    Führt den vollständigen Lernzyklus aus.
    force=True überspringt den MIN_NEW_SIGNALS-Check.
    Gibt einen Report-Dict zurück.
    """
    import signal_logger

    cnt     = signal_logger.count()
    decided = cnt["win"] + cnt["loss"]

    log  = _load()
    last = log[-1].get("decided_after", 0) if log else 0
    new  = decided - last

    if not force and new < MIN_NEW_SIGNALS:
        return {"skipped": True, "new_signals": new, "needed": MIN_NEW_SIGNALS}

    print(f"\n{'═'*58}")
    print(f"  🧠  STRATEGY EVOLVER — {new} neue Signale")
    print(f"{'═'*58}")

    entry = {
        "ts":             datetime.now(timezone.utc).isoformat(),
        "decided_after":  decided,
        "new_signals":    new,
        "forced":         force,
        "actions":        [],
        "metrics_before": _current_metrics(),
        "metrics_after":  {},
    }

    # ── 1. Backtest-Gewichte neu berechnen ────────────────────────────────────
    try:
        import backtest_learner
        weights = backtest_learner.run()
        n_pat   = len(weights.get("patterns", {}))
        entry["actions"].append(f"Gewichte: {n_pat} Muster aktualisiert")
        if weights.get("patterns"):
            best = max(weights["patterns"].values(), key=lambda x: x.get("score", 0))
            entry["best_pattern"] = (
                f"{best['setup_type']} {best['bias']} {best['timeframe']} "
                f"Score={best['score']}/100 "
                f"WR={best['win_rate']*100:.1f}% "
                f"N={best['samples']}"
            )
            print(f"  🏆 Bestes Muster: {entry['best_pattern']}")
    except Exception as e:
        entry["actions"].append(f"Gewichte-Update: Fehler ({e})")

    # ── 2. Performance-Report aktualisieren ───────────────────────────────────
    try:
        import performance_analyzer
        performance_analyzer.run()
        entry["actions"].append("Performance-Report aktualisiert")
    except Exception as e:
        entry["actions"].append(f"Performance-Analyse: Fehler ({e})")

    # ── 3. Thresholds optimieren (mit Backup + Auto-Rollback) ────────────────
    backed_up = _backup_config()
    try:
        import threshold_optimizer
        n_changes = threshold_optimizer.run()
        if n_changes > 0:
            entry["actions"].append(f"Thresholds: {n_changes} Anpassung(en)")
        else:
            entry["actions"].append("Thresholds: keine Änderung nötig")
    except Exception as e:
        entry["actions"].append(f"Threshold-Optimizer: Fehler ({e})")

    # ── 4. Lokales XGBoost-Modell trainieren wenn bereit ─────────────────────
    try:
        import local_filter_model
        result = local_filter_model.train_if_ready()
        if result:
            acc = result.get("accuracy_pct", 0)
            active = result.get("ersetzt_haiku", False)
            entry["actions"].append(
                f"XGBoost trainiert: Accuracy={acc:.1f}% "
                f"({'aktiv' if active else 'noch nicht aktiv – unter Schwelle'})"
            )
            entry["model_accuracy"] = acc
        else:
            import signal_logger as sl
            closed = sl.count()["win"] + sl.count()["loss"]
            entry["actions"].append(
                f"XGBoost: {closed}/200 Signale gesammelt "
                f"({closed/200*100:.0f}%)"
            )
    except Exception as e:
        entry["actions"].append(f"Modell-Training: Fehler ({e})")

    # ── 5. Neue Strategien synthetisieren ────────────────────────────────────
    try:
        import strategy_builder
        sb_result = strategy_builder.run()
        if not sb_result.get("skipped"):
            s = sb_result.get("summary", {})
            entry["actions"].append(
                f"Strategie-Builder Gen {sb_result.get('generation', '?')}: "
                f"{sb_result.get('total_rules', 0)} Regeln "
                f"({s.get('boost_rules', 0)} BOOST · {s.get('block_rules', 0)} BLOCK · "
                f"{s.get('combo_boosts', 0)} Combos)"
            )
        else:
            entry["actions"].append("Strategie-Builder: übersprungen (nicht genug Daten)")
    except Exception as e:
        entry["actions"].append(f"Strategie-Builder: Fehler ({e})")

    # ── Trend-Kontext erfassen (für Regime-/Trendwechsel-Lernen) ─────────────
    try:
        import trend_detector
        tr = trend_detector.current_trend()
        entry["trend"] = {"state": tr["state"], "score": tr["score"],
                          "daily": tr["daily"], "h1": tr["h1"], "phase": tr["phase"]}
        entry["actions"].append(
            f"Trend: {tr['state']} (Daily={tr['daily']} 1H={tr['h1']} ADX={tr['adx']})")
    except Exception:
        pass

    # ── 6. AUTO-KI-SIGNALE optimieren (HAUPT-PRIORITÄT) ──────────────────────
    # Lernt aus den realen Outcomes die profitablen RSI-Zonen + Richtung der
    # Auto-KI-Signale (BREAK/BOUNCE) und schreibt strategy_params.json, das die
    # Live-Signal-Engine anwendet. Alle Lern-Bots oben liefern Gewichte/Thresholds/
    # Regeln; dieser Schritt richtet die SIGNAL-ERZEUGUNG selbst auf Profit aus.
    try:
        import signal_param_optimizer
        spo = signal_param_optimizer.optimize()
        if spo.get("changed"):
            entry["actions"].append(
                "Auto-KI-Signale optimiert: " + " · ".join(spo["changed"]))
            entry["autoki_optimized"] = spo["changed"]
            print(f"  🎯 Auto-KI-Signale verbessert (Long {spo['longs']} / "
                  f"Short {spo['shorts']} Trades): {', '.join(spo['changed'])}")
        else:
            entry["actions"].append(
                f"Auto-KI-Signale: keine Änderung (Long {spo['longs']} / "
                f"Short {spo['shorts']} Trades)")
    except Exception as e:
        entry["actions"].append(f"Auto-KI-Signal-Optimizer: Fehler ({e})")

    # ── Abschluss ─────────────────────────────────────────────────────────────
    entry["metrics_after"] = _current_metrics()

    # Win-Rate-Trend + automatischer Rollback bei starkem Einbruch
    wr_before = entry["metrics_before"].get("win_rate")
    wr_after  = entry["metrics_after"].get("win_rate")
    if wr_before and wr_after:
        delta = round(wr_after - wr_before, 1)
        entry["win_rate_delta"] = delta
        trend = f"+{delta}%" if delta >= 0 else f"{delta}%"
        entry["actions"].append(f"Win-Rate-Trend: {trend} (vorher {wr_before}%, nachher {wr_after}%)")
        # Rollback: wenn Win-Rate um mehr als 5% gesunken und Backup vorhanden
        if delta < -5.0 and backed_up:
            reverted = _restore_config()
            entry["rollback"] = reverted
            entry["actions"].append(
                f"⚠️  Rollback ausgelöst (WR −{abs(delta):.1f}%) — "
                f"{'Config wiederhergestellt' if reverted else 'Rollback fehlgeschlagen'}"
            )
            print(f"  [Evolver] Auto-Rollback: WR-Delta {delta:.1f}% < −5%")

    log.append(entry)
    _save(log)

    print(f"\n  Aktionen ({len(entry['actions'])}):")
    for a in entry["actions"]:
        print(f"    • {a}")
    print(f"{'═'*58}\n")

    return entry


def get_history(n: int = 20) -> list:
    """Gibt die letzten N Evolution-Einträge zurück."""
    return list(reversed(_load()))[:n]


def get_latest() -> dict:
    """Gibt den neuesten Evolution-Eintrag zurück."""
    log = _load()
    return log[-1] if log else {}


if __name__ == "__main__":
    run(force=True)
