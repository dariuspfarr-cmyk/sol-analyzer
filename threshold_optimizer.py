"""
Threshold Optimizer — passt Schwellenwerte automatisch basierend auf Performance-Daten an.

Wird jeden Sonntag nach dem Performance-Analyzer ausgeführt.
Ändert config.json innerhalb der sicheren MIN/MAX-Grenzen.
Jede Änderung wird in threshold_changes.log protokolliert.
Maximale Anpassung pro Zyklus: 20% des erlaubten Bereichs (konfigurierbar).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import config as cfg

REPORT_FILE    = Path(__file__).parent / "performance_report.json"
CHANGES_LOG    = Path(__file__).parent / "threshold_changes.log"
EVOLUTION_FILE = Path(__file__).parent / "strategy_evolution.json"

# Win-Rate-Schwellen für Anpassungen
WR_LOW_THRESHOLD  = 45.0   # unter diesem Wert → strenger werden
WR_HIGH_THRESHOLD = 65.0   # über diesem Wert  → lockerer werden
API_EFF_THRESHOLD = 30.0   # API-Effizienz unter diesem Wert → Haiku strenger
VOL_FP_THRESHOLD  = 35.0   # Volumen-Filter Win-Rate unter diesem Wert → Multiplikator erhöhen
WR_DECLINE_FREEZE = 2.0    # fällt die Gesamt-WR um ≥ X Punkte → ALLE Lockerungen
                           # einfrieren (nur noch Verschärfung). Bricht die
                           # Runaway-Schleife "WR>65% → lockern → WR fällt → lockern".


def _wr_is_declining() -> tuple[bool, float, float]:
    """
    True, wenn die Gesamt-Win-Rate fällt — mittlere WR der letzten 3 Lern-Zyklen
    vs. der davor, gemessen an strategy_evolution.json (spiegelt die P1-Logik des
    improvement_scanner). Bei zu wenig Historie: nicht-fallend.
    HAUPTZIEL ist WR-Maximierung über Selektivität: in einer Abwärtsphase darf der
    Optimizer NICHT lockern (= mehr Signale), das verschärft den Rückgang nur.
    Gibt (declining, earlier_avg, recent_avg) zurück.
    """
    try:
        with open(EVOLUTION_FILE, encoding="utf-8") as f:
            ev = json.load(f)
        wrs = [(e.get("metrics_after") or {}).get("win_rate") for e in ev]
        wrs = [w for w in wrs if isinstance(w, (int, float))]
    except Exception:
        return False, 0.0, 0.0
    if len(wrs) < 4:
        return False, 0.0, 0.0
    recent      = wrs[-3:]
    earlier     = wrs[:-3]
    recent_avg  = sum(recent) / len(recent)
    earlier_avg = sum(earlier) / len(earlier)
    return (earlier_avg - recent_avg >= WR_DECLINE_FREEZE, earlier_avg, recent_avg)


def _log(msg: str) -> None:
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{ts}] {msg}"
    CHANGES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CHANGES_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(f"  ⚙️  {line}")


def _confidence_int(level: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(level, 0)


def _confidence_str(val: int) -> str:
    return {0: "low", 1: "medium", 2: "high"}.get(max(0, min(2, int(val))), "medium")


# ── Einzelne Anpassungs-Logik ─────────────────────────────────────────────────
def _adjust_setup_confidence(setup_type: str, win_rate: float,
                              current_cfg: dict,
                              allow_loosen: bool = True) -> tuple[dict, list[str]]:
    """Passt MIN_CONFIDENCE und WEIGHT eines Setup-Typs an."""
    changes = []
    key_conf  = f"{setup_type}_MIN_CONFIDENCE"
    key_weight = f"{setup_type}_WEIGHT"

    if key_conf not in cfg.BOUNDS:
        return current_cfg, changes

    cur_conf  = int(current_cfg.get(key_conf, cfg.BOUNDS[key_conf]["default"]))
    cur_weight = float(current_cfg.get(key_weight, cfg.BOUNDS[key_weight]["default"]))
    max_delta  = cfg.max_delta(key_weight, cur_weight)

    if win_rate < WR_LOW_THRESHOLD:
        # Schlechte Performance → Mindest-Konfidenz erhöhen
        new_conf = min(cur_conf + 1, int(cfg.BOUNDS[key_conf]["max"]))
        if new_conf != cur_conf:
            current_cfg[key_conf] = new_conf
            changes.append(
                f"{setup_type} MIN_CONFIDENCE: {_confidence_str(cur_conf)} → "
                f"{_confidence_str(new_conf)} "
                f"(Win-Rate {win_rate:.1f}% < {WR_LOW_THRESHOLD}%)"
            )
        # Gewicht reduzieren
        new_weight = cfg.clamp(key_weight, cur_weight - max_delta)
        if abs(new_weight - cur_weight) > 0.001:
            current_cfg[key_weight] = round(new_weight, 3)
            changes.append(
                f"{setup_type} WEIGHT: {cur_weight:.3f} → {new_weight:.3f} "
                f"(Performance schwach)"
            )

    elif win_rate > WR_HIGH_THRESHOLD and allow_loosen:
        # Gute Performance → lockerer werden, mehr Signale einfangen
        # (nur wenn die Gesamt-WR NICHT fällt — sonst frieren wir ein)
        new_conf = max(cur_conf - 1, int(cfg.BOUNDS[key_conf]["min"]))
        if new_conf != cur_conf:
            current_cfg[key_conf] = new_conf
            changes.append(
                f"{setup_type} MIN_CONFIDENCE: {_confidence_str(cur_conf)} → "
                f"{_confidence_str(new_conf)} "
                f"(Win-Rate {win_rate:.1f}% > {WR_HIGH_THRESHOLD}%)"
            )
        # Gewicht erhöhen
        new_weight = cfg.clamp(key_weight, cur_weight + max_delta * 0.5)
        if abs(new_weight - cur_weight) > 0.001:
            current_cfg[key_weight] = round(new_weight, 3)
            changes.append(
                f"{setup_type} WEIGHT: {cur_weight:.3f} → {new_weight:.3f} "
                f"(Performance sehr gut)"
            )

    return current_cfg, changes


def _adjust_haiku_strictness(api_efficiency: float,
                              current_cfg: dict,
                              allow_loosen: bool = True) -> tuple[dict, list[str]]:
    """Passt HAIKU_STRICTNESS an basierend auf API-Effizienz."""
    changes  = []
    key      = "HAIKU_STRICTNESS"
    cur_val  = float(current_cfg.get(key, cfg.BOUNDS[key]["default"]))
    max_d    = cfg.max_delta(key, cur_val)

    if api_efficiency < API_EFF_THRESHOLD:
        new_val = cfg.clamp(key, cur_val + max_d)
        if abs(new_val - cur_val) > 0.01:
            current_cfg[key] = round(new_val, 3)
            changes.append(
                f"HAIKU_STRICTNESS: {cur_val:.3f} → {new_val:.3f} "
                f"(API-Effizienz {api_efficiency:.1f}% < {API_EFF_THRESHOLD}%)"
            )

    elif api_efficiency > 60.0 and allow_loosen:
        # Hohe Effizienz → etwas lockerer, um mehr valide Signale durchzulassen
        # (eingefroren, solange die Gesamt-WR fällt)
        new_val = cfg.clamp(key, cur_val - max_d * 0.3)
        if abs(new_val - cur_val) > 0.01:
            current_cfg[key] = round(new_val, 3)
            changes.append(
                f"HAIKU_STRICTNESS: {cur_val:.3f} → {new_val:.3f} "
                f"(API-Effizienz {api_efficiency:.1f}% sehr gut)"
            )

    return current_cfg, changes


def _adjust_volume_multiplier(vol_win_rate: float | None,
                               current_cfg: dict,
                               allow_loosen: bool = True) -> tuple[dict, list[str]]:
    """Passt VOLUME_SPIKE_MULTIPLIER an."""
    changes = []
    if vol_win_rate is None:
        return current_cfg, changes

    key     = "VOLUME_SPIKE_MULTIPLIER"
    cur_val = float(current_cfg.get(key, cfg.BOUNDS[key]["default"]))
    cfg.max_delta(key, cur_val)

    if vol_win_rate < VOL_FP_THRESHOLD:
        # Zu viele False Positives durch Volumen-Spike → Schwelle erhöhen
        new_val = cfg.clamp(key, cur_val + 0.1)  # feste 0.1-Schritte wie spezifiziert
        if abs(new_val - cur_val) > 0.001:
            current_cfg[key] = round(new_val, 2)
            changes.append(
                f"VOLUME_SPIKE_MULTIPLIER: {cur_val:.2f} → {new_val:.2f} "
                f"(Volumen-WR {vol_win_rate:.1f}% zu niedrig, "
                f"zu viele False Positives)"
            )

    elif vol_win_rate > 70.0 and allow_loosen:
        # Sehr guter Volumen-Filter → leicht lockern (eingefroren bei WR-Rückgang)
        new_val = cfg.clamp(key, cur_val - 0.1)
        if abs(new_val - cur_val) > 0.001 and new_val >= cfg.BOUNDS[key]["min"]:
            current_cfg[key] = round(new_val, 2)
            changes.append(
                f"VOLUME_SPIKE_MULTIPLIER: {cur_val:.2f} → {new_val:.2f} "
                f"(Volumen-WR {vol_win_rate:.1f}% sehr gut, mehr einfangen)"
            )

    return current_cfg, changes


# ── Hauptfunktion ─────────────────────────────────────────────────────────────
def _merge_with_backtest(live_report: dict) -> dict:
    """
    Kombiniert Live-Performance-Report mit Backtest-Gewichten.
    Live: 70-90%, Backtest: 10-30% (basierend auf Live-Datenmenge).
    """
    try:
        from backtest_learner import get_weights_meta
        bt_meta = get_weights_meta()
        if not bt_meta:
            return live_report

        lw = bt_meta.get("live_weight", 0.70)
        bw = 1.0 - lw
        bt_setup = bt_meta.get("setup_performance", {})

        merged = dict(live_report)
        merged_by_setup = {}

        for stype, live_d in live_report.get("nach_setup_typ", {}).items():
            bt_d    = bt_setup.get(stype, {})
            live_wr = live_d.get("win_rate_pct", 50.0)
            bt_wr   = bt_d.get("win_rate", 0.5) * 100

            # Gewichtetes Mittel
            combined_wr = live_wr * lw + bt_wr * bw
            merged_d    = dict(live_d)
            merged_d["win_rate_pct"]         = round(combined_wr, 2)
            merged_d["win_rate_source_live"]  = round(live_wr, 2)
            merged_d["win_rate_source_bt"]    = round(bt_wr, 2)
            merged_d["live_weight"]           = lw
            merged_by_setup[stype]            = merged_d

        # Backtest-only setups (kein Live-Data)
        for stype, bt_d in bt_setup.items():
            if stype not in merged_by_setup:
                bt_wr = bt_d.get("win_rate", 0.5) * 100
                merged_by_setup[stype] = {
                    "count": bt_d.get("samples", 0),
                    "win_rate_pct": round(bt_wr * bw + 50.0 * lw, 2),
                    "source": "backtest_only",
                }

        merged["nach_setup_typ"] = merged_by_setup
        merged["gewichtung"]     = {"live": lw, "backtest": bw}
        print(f"  ⚖️  Gewichtung: {lw*100:.0f}% Live / {bw*100:.0f}% Backtest "
              f"({bt_meta.get('total_samples',0)} Backtest-Samples, "
              f"{bt_meta.get('live_samples',0)} Live-Samples)")
        return merged
    except Exception as e:
        print(f"  ⚠️  Backtest-Merge übersprungen: {e}")
        return live_report


def _adjust_detection_params(report: dict,
                              current_cfg: dict,
                              allow_loosen: bool = True) -> tuple[dict, list[str]]:
    """
    Passt Signal-Erkennungsparameter basierend auf Setup-Win-Rates an.
      BOS schlechte WR   → PIVOT_LB erhöhen (signifikantere Swings suchen)
      EQH/EQL gute WR    → EQH_TOLERANCE lockern (mehr Signale einfangen)
      EQH/EQL schlechte  → EQH_TOLERANCE verschärfen
      CHoCH schlechte WR → CHOCH_WINDOW vergrößern (stärkere Trendbestätigung)
    Lockerungen sind eingefroren, solange die Gesamt-WR fällt (allow_loosen).
    """
    changes    = []
    setup_data = report.get("nach_setup_typ", {})

    def _closed(d):  # Signifikanz nur über GESCHLOSSENE Trades
        return d.get("closed", d.get("count", 0))

    # ── PIVOT_LB basierend auf BOS-Performance ───────────────────────────────
    bos = setup_data.get("BOS", {})
    if _closed(bos) >= 15:
        bos_wr = bos.get("win_rate_pct", 50.0)
        key    = "PIVOT_LB"
        cur    = float(current_cfg.get(key, cfg.BOUNDS[key]["default"]))
        if bos_wr < WR_LOW_THRESHOLD:
            new = cfg.clamp(key, cur + 1)
            if new != cur:
                current_cfg[key] = int(new)
                changes.append(
                    f"PIVOT_LB: {cur:.0f} → {new:.0f} "
                    f"(BOS Win-Rate {bos_wr:.1f}% zu niedrig → größeres Pivot-Fenster)"
                )
        elif bos_wr > WR_HIGH_THRESHOLD and allow_loosen:
            new = cfg.clamp(key, cur - 1)
            if new != cur:
                current_cfg[key] = int(new)
                changes.append(
                    f"PIVOT_LB: {cur:.0f} → {new:.0f} "
                    f"(BOS Win-Rate {bos_wr:.1f}% sehr gut → kleineres Fenster, mehr Signale)"
                )

    # ── EQH_TOLERANCE basierend auf EQH- und EQL-Performance ────────────────
    eqh     = setup_data.get("EQH", {})
    eql     = setup_data.get("EQL", {})
    eq_cnt  = _closed(eqh) + _closed(eql)
    if eq_cnt >= 15:
        eq_wr = (
            eqh.get("win_rate_pct", 50.0) * _closed(eqh)
            + eql.get("win_rate_pct", 50.0) * _closed(eql)
        ) / eq_cnt
        key   = "EQH_TOLERANCE"
        cur   = float(current_cfg.get(key, cfg.BOUNDS[key]["default"]))
        max_d = cfg.max_delta(key, cur)
        if eq_wr < WR_LOW_THRESHOLD:
            new = cfg.clamp(key, cur - max_d * 0.5)
            if abs(new - cur) > 0.0001:
                current_cfg[key] = round(new, 4)
                changes.append(
                    f"EQH_TOLERANCE: {cur:.4f} → {new:.4f} "
                    f"(EQ Win-Rate {eq_wr:.1f}% niedrig → strenger)"
                )
        elif eq_wr > WR_HIGH_THRESHOLD and allow_loosen:
            new = cfg.clamp(key, cur + max_d * 0.3)
            if abs(new - cur) > 0.0001:
                current_cfg[key] = round(new, 4)
                changes.append(
                    f"EQH_TOLERANCE: {cur:.4f} → {new:.4f} "
                    f"(EQ Win-Rate {eq_wr:.1f}% sehr gut → lockerer, mehr Signale)"
                )

    # ── CHOCH_WINDOW basierend auf CHoCH-Performance ─────────────────────────
    choch = setup_data.get("CHoCH", {})
    if _closed(choch) >= 15:
        choch_wr = choch.get("win_rate_pct", 50.0)
        key      = "CHOCH_WINDOW"
        cur      = float(current_cfg.get(key, cfg.BOUNDS[key]["default"]))
        if choch_wr < WR_LOW_THRESHOLD:
            new = cfg.clamp(key, cur + 2)
            if new != cur:
                current_cfg[key] = int(new)
                changes.append(
                    f"CHOCH_WINDOW: {cur:.0f} → {new:.0f} "
                    f"(CHoCH Win-Rate {choch_wr:.1f}% zu niedrig → mehr Kerzen für Trendbestätigung)"
                )
        elif choch_wr > WR_HIGH_THRESHOLD and allow_loosen:
            new = cfg.clamp(key, cur - 2)
            if new != cur:
                current_cfg[key] = int(new)
                changes.append(
                    f"CHOCH_WINDOW: {cur:.0f} → {new:.0f} "
                    f"(CHoCH Win-Rate {choch_wr:.1f}% sehr gut → sensitivere Erkennung)"
                )

    return current_cfg, changes


def run(report_path: Path = REPORT_FILE) -> int:
    """
    Liest performance_report.json, kombiniert mit backtest_weights.json,
    optimiert Thresholds und schreibt config.json.
    Gibt die Anzahl vorgenommener Änderungen zurück.
    """
    if not report_path.exists():
        print("  ⚠️  Kein Performance-Report gefunden – Optimizer übersprungen.")
        return 0

    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)

    report = _merge_with_backtest(report)   # Layer: Live + Backtest kombinieren

    current_cfg  = cfg.load()
    all_changes: list[str] = []

    gesamt       = report.get("gesamt", {})
    api_eff      = gesamt.get("api_win_rate_pct", 100.0)
    vol_info     = report.get("volumen_filter", {})
    vol_wr       = vol_info.get("win_rate_pct")

    # ── WR-Trend-Bremse: bei fallender Gesamt-WR ALLE Lockerungen einfrieren ──
    # (HAUPTZIEL = WR via Selektivität; in einer Abwärtsphase nur verschärfen).
    declining, ea, ra = _wr_is_declining()
    allow_loosen = not declining
    if declining:
        _log(f"⏸️  WR fällt ({ea:.1f}% → {ra:.1f}%) → Lockerungen eingefroren "
             f"(nur Verschärfung erlaubt).")

    # ── 1. Pro Setup-Typ: Konfidenz & Gewicht anpassen ───────────────────────
    for stype, stats in report.get("nach_setup_typ", {}).items():
        if stats.get("closed", stats.get("count", 0)) < 15:
            continue   # min. 15 GESCHLOSSENE Samples nötig (stat. belastbar)
        wr = stats.get("win_rate_pct", 50.0)
        current_cfg, changes = _adjust_setup_confidence(
            stype, wr, current_cfg, allow_loosen)
        all_changes.extend(changes)

    # ── 2. Haiku-Striktheit ──────────────────────────────────────────────────
    current_cfg, changes = _adjust_haiku_strictness(api_eff, current_cfg, allow_loosen)
    all_changes.extend(changes)

    # ── 3. Volumen-Multiplikator ─────────────────────────────────────────────
    current_cfg, changes = _adjust_volume_multiplier(vol_wr, current_cfg, allow_loosen)
    all_changes.extend(changes)

    # ── 4. Signal-Erkennungsparameter (PIVOT_LB, EQH_TOL, CHOCH_WIN) ─────────
    current_cfg, changes = _adjust_detection_params(report, current_cfg, allow_loosen)
    all_changes.extend(changes)

    # ── Feststeckende Parameter erkennen ────────────────────────────────────
    _check_stuck_params(current_cfg)

    # ── Speichern und loggen ─────────────────────────────────────────────────
    if all_changes:
        cfg.save(current_cfg)
        _log(f"=== Optimizer-Zyklus ({len(all_changes)} Änderungen) ===")
        for c in all_changes:
            _log(c)

        # Kosteneinsparung abschätzen
        _estimate_savings(report, all_changes)
    else:
        _log("Keine Threshold-Anpassungen notwendig (alle Parameter im grünen Bereich).")

    return len(all_changes)


def _check_stuck_params(current_cfg: dict) -> None:
    """Warnt wenn ein Parameter seit Zyklen am Rand seines Bereichs feststeckt."""
    NEAR_BOUND_PCT = 0.05   # innerhalb 5% der Grenze gilt als 'feststeckend'
    stuck = []
    for key, bounds in cfg.BOUNDS.items():
        val = current_cfg.get(key)
        if val is None:
            continue
        lo, hi = float(bounds["min"]), float(bounds["max"])
        span = hi - lo
        if span <= 0:
            continue
        frac = (float(val) - lo) / span
        if frac <= NEAR_BOUND_PCT:
            stuck.append(f"{key}={val} (am Minimum {lo})")
        elif frac >= 1.0 - NEAR_BOUND_PCT:
            stuck.append(f"{key}={val} (am Maximum {hi})")
    if stuck:
        _log(f"⚠️  Feststeckende Parameter ({len(stuck)}): " + "; ".join(stuck))


def _estimate_savings(report: dict, changes: list[str]) -> None:
    """Schätzt und druckt die monatliche Kosteneinsparung durch die Anpassungen."""
    g            = report.get("gesamt", {})
    total_calls  = g.get("api_calls", 0)
    g.get("api_win_rate_pct", 0)
    costs        = report.get("api_kosten_monat_usd", {})
    monthly_cost = sum(costs.values())

    # Konservative Schätzung: 10% weniger unnötige Calls pro Änderung
    saved_calls_est  = total_calls * 0.10 * len(changes)
    haiku_cost_each  = 0.00006
    savings_est      = saved_calls_est * haiku_cost_each

    print("\n  💡 Optimierungs-Einsparungsschätzung:")
    print(f"     Monatliche API-Kosten bisher:  ${monthly_cost:.4f}")
    print(f"     Geschätzte Ersparnis/Monat:    ${savings_est:.4f}")
    print(f"     ({len(changes)} Anpassungen × ~10% weniger unnötige Calls)\n")
    _log(f"Geschätzte monatliche Ersparnis: ${savings_est:.4f}")
