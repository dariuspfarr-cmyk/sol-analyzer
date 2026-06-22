"""
Strategy Knowledge — zentraler Wissens-Hub für alle Bots.

Vereinheitlicht den Zugriff auf synthetisierte Strategie-Regeln
(strategy_rules.json) und verfolgt deren Live-Wirksamkeit:

  evaluate(setup, bias, zone, hour)   → (score_modifier, matched_signatures)
                                        genutzt von paper_trader UND algo_signal_engine
                                        (→ smart_router, sol_analysis_bot profitieren mit)
  record_feedback(signatures, won)    → schreibt Trade-Outcome pro Regel in
                                        rule_performance.json
  get_rule_performance()              → für strategy_builder (Regel-Selbstverbesserung)

Selbstverbesserung:
  • Jede Regel wird über eine STABILE Signatur identifiziert (Typ + Bedingungen
    + Aktion) — überlebt Regel-Neugenerierungen, bei denen sich IDs ändern.
  • Regeln mit schlechter Live-Bilanz verlieren Einfluss (Effektivitäts-Faktor),
    ab 6 Trades mit < 35% Wirksamkeit werden sie stummgeschaltet.
  • BLOCK-Regeln werden invers bewertet: Trade verloren = Regel hatte recht.
  • strategy_builder liest das Feedback und senkt Konfidenz / verwirft Regeln.
"""

from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

BASE             = Path(__file__).parent
RULES_FILE       = BASE / "strategy_rules.json"
ACTIVE_FILE      = BASE / "active_strategy.json"
PERFORMANCE_FILE = BASE / "rule_performance.json"

# Stummschalt-Schwellen: ab MUTE_MIN_N Trades und Wirksamkeit < MUTE_WR
# wird die Regel ignoriert, bis strategy_builder sie neu bewertet/verwirft.
MUTE_MIN_N = 6
MUTE_WR    = 0.35

# ── Caches (mtime-basiert) ────────────────────────────────────────────────────
_rules_cache: dict = {}
_rules_mtime: float = 0.0
_active_cache: dict = {}
_active_mtime: float = 0.0
_perf_cache: dict = {}
_perf_mtime: float = 0.0


def _load_json_cached(path: Path, cache_name: str) -> dict:
    g = globals()
    try:
        mt = path.stat().st_mtime
        if mt != g[f"_{cache_name}_mtime"]:
            with open(path, encoding="utf-8") as f:
                g[f"_{cache_name}_cache"] = json.load(f)
            g[f"_{cache_name}_mtime"] = mt
    except Exception:
        pass
    return g[f"_{cache_name}_cache"]


def _load_rules_doc() -> dict:
    return _load_json_cached(RULES_FILE, "rules")


def _load_active_profile() -> dict:
    return _load_json_cached(ACTIVE_FILE, "active")


def get_rule_performance() -> dict:
    """Live-Performance aller Regeln: {signature: {fired, trade_wins, trade_losses}}."""
    return dict(_load_json_cached(PERFORMANCE_FILE, "perf"))


# ── Regel-Signatur (stabil über Generationen) ─────────────────────────────────
def rule_signature(rule: dict) -> str:
    """
    Stabile Identität einer Regel, unabhängig von der laufenden ID.
    Basiert auf Typ, Aktion und allen Bedingungen.
    """
    c = rule.get("conditions", {})
    return "|".join([
        rule.get("type", "?"),
        rule.get("action", "?"),
        str(c.get("setup_type", "*")),
        str(c.get("bias", "*")),
        str(c.get("zone_position", "*")),
        f"{c.get('hour_min', '*')}-{c.get('hour_max', '*')}",
    ])


# ── Effektivitäts-Faktor aus Live-Feedback ────────────────────────────────────
def _effectiveness(sig: str, perf: dict) -> float:
    """
    Skaliert den Einfluss einer Regel anhand ihrer Live-Bilanz.
    1.0 = neutral (zu wenig Daten) · 0.0 = stummgeschaltet
    BOOST-Regel wirksam wenn Trades gewinnen; BLOCK-Regel wenn Trades verlieren.
    """
    stats = perf.get(sig)
    if not stats:
        return 1.0
    wins   = int(stats.get("trade_wins", 0))
    losses = int(stats.get("trade_losses", 0))
    n = wins + losses
    if n < 4:
        return 1.0

    is_block = "|BLOCK|" in sig
    # Wirksamkeit: Anteil der Trades, bei denen die Regel "recht hatte"
    eff_wr = (losses / n) if is_block else (wins / n)

    if n >= MUTE_MIN_N and eff_wr < MUTE_WR:
        return 0.0   # Regel hat live versagt → ignorieren

    # WR 0.5 → 1.0 · WR 0.25 → 0.5 · WR 0.7+ → bis 1.4
    return max(0.4, min(1.4, eff_wr * 2.0))


# ── Zentrale Auswertung ───────────────────────────────────────────────────────
def evaluate(setup_type: str, bias: str, zone: str, hour: int,
             profile_id: str | None = None) -> tuple[float, list[str]]:
    """
    Wendet alle passenden Regeln an, gefiltert durch das aktive Profil und
    gewichtet mit Konfidenz × Live-Effektivität.
    Gibt (score_modifier, matched_signatures) zurück.

    profile_id überschreibt das aktive Profil (für die Profil-Bewertung im
    strategy_selector — gleiche Logik, kein Duplikat).
    """
    doc = _load_rules_doc()
    all_rules: list = doc.get("rules", [])
    if not all_rules:
        return 0.0, []

    pid      = profile_id or _load_active_profile().get("profile_id", "balanced")
    prof     = doc.get("profiles", {}).get(pid, {})
    r_types  = prof.get("rule_types")
    s_filter = prof.get("strength_filter")
    c_thresh = float(prof.get("confidence_threshold", 0.0))
    m_scale  = float(prof.get("modifier_scale", 1.0))

    perf  = get_rule_performance()
    total = 0.0
    matched: list[str] = []

    for rule in all_rules:
        if r_types and rule.get("type") not in r_types:
            continue
        if s_filter and rule.get("strength") not in s_filter:
            continue
        conf = float(rule.get("confidence", 0.5))
        if conf < c_thresh:
            continue

        cond = rule.get("conditions", {})
        if "setup_type"    in cond and cond["setup_type"]    != setup_type: continue
        if "bias"          in cond and cond["bias"]          != bias:       continue
        if "zone_position" in cond and cond["zone_position"] != zone:       continue
        if "hour_min" in cond and "hour_max" in cond:
            h0, h1 = cond["hour_min"], cond["hour_max"]
            in_win = (h0 <= hour <= h1) if h0 <= h1 else (hour >= h0 or hour <= h1)
            if not in_win:
                continue

        sig = rule_signature(rule)
        eff = _effectiveness(sig, perf)
        if eff <= 0.0:
            continue   # stummgeschaltete Regel

        total += rule.get("score_modifier", 0) * conf * m_scale * eff
        matched.append(sig)

    return round(total, 2), matched


# ── Live-Feedback nach Trade-Close ────────────────────────────────────────────
def record_feedback(signatures: list[str], won: bool) -> None:
    """
    Schreibt das Trade-Ergebnis allen Regeln gut, die beim Entry gematcht haben.
    Wird vom Paper Trader nach jedem geschlossenen Trade aufgerufen.
    """
    if not signatures:
        return
    perf = get_rule_performance()
    key  = "trade_wins" if won else "trade_losses"
    now  = datetime.now(timezone.utc).isoformat()

    for sig in signatures:
        entry = perf.setdefault(sig, {"fired": 0, "trade_wins": 0, "trade_losses": 0})
        entry["fired"] += 1
        entry[key]     += 1
        entry["last_update"] = now

    try:
        with open(PERFORMANCE_FILE, "w", encoding="utf-8") as f:
            json.dump(perf, f, indent=2, ensure_ascii=False)
        # Cache invalidieren, damit der nächste Read frisch ist
        global _perf_mtime
        _perf_mtime = 0.0
    except Exception:
        pass


# ── Zusammenfassung für Dashboard / Diagnose ──────────────────────────────────
def knowledge_summary() -> dict:
    """Status des Regel-Wissens: Anzahl Regeln, gemutete, beste/schlechteste."""
    doc   = _load_rules_doc()
    rules = doc.get("rules", [])
    perf  = get_rule_performance()

    muted, validated = [], []
    for r in rules:
        sig = rule_signature(r)
        eff = _effectiveness(sig, perf)
        stats = perf.get(sig, {})
        n = stats.get("trade_wins", 0) + stats.get("trade_losses", 0)
        if eff <= 0.0:
            muted.append({"sig": sig, "n": n})
        elif n >= 4:
            validated.append({"sig": sig, "n": n, "effectiveness": round(eff, 2)})

    return {
        "total_rules":     len(rules),
        "tracked_rules":   len([s for s in perf.values() if s.get("fired", 0) > 0]),
        "muted_rules":     muted,
        "validated_rules": sorted(validated, key=lambda x: -x["effectiveness"])[:10],
        "generation":      doc.get("generation", 0),
        "active_profile":  _load_active_profile().get("profile_id", "balanced"),
    }
