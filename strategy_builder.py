"""
Strategy Builder — synthetisiert komplett neue Handelsregeln aus Erfahrungsdaten.

Liest:
  backtest_weights.json  → Pattern-Performance (Setup × Bias × Zone × Stunde)
  performance_report.json → Setup/Bias/Timeframe-Statistiken
  trades.json            → Tatsächliche Paper-Trade-Ergebnisse

Schreibt:
  strategy_rules.json    → Neue, aus Daten abgeleitete Handelsregeln

Der Paper Trader wendet diese Regeln beim Scoring an — zusätzlich zu allen
bestehenden Faktoren. Regeln werden nach jedem Lernzyklus neu generiert.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE                  = Path(__file__).parent
WEIGHTS_FILE          = BASE / "backtest_weights.json"
REPORT_FILE           = BASE / "performance_report.json"
TRADES_FILE           = BASE / "trades.json"
RULES_FILE            = BASE / "strategy_rules.json"
ACTIVE_STRATEGY_FILE  = BASE / "active_strategy.json"
BUILD_LOG             = BASE / "strategy_builder.log"

# ── Vordefinierte Strategie-Profile ───────────────────────────────────────────
PROFILES: dict[str, dict] = {
    "balanced": {
        "id":                  "balanced",
        "name":                "Balanced",
        "emoji":               "⚖️",
        "description":         "Alle Regeln, gewichtet nach Konfidenz — ausgewogener Standard",
        "rule_types":          None,       # None = alle Typen
        "strength_filter":     None,       # None = alle Stärken
        "modifier_scale":      1.0,
        "confidence_threshold": 0.0,
        "min_rr_mult":         1.0,
    },
    "conservative": {
        "id":                  "conservative",
        "name":                "Konservativ",
        "emoji":               "🔒",
        "description":         "Nur hochwertige STRONG-Regeln (≥65% Konfidenz) — weniger Trades, höhere Präzision",
        "rule_types":          None,
        "strength_filter":     ["STRONG_BOOST", "STRONG_BLOCK"],
        "modifier_scale":      0.8,
        "confidence_threshold": 0.65,
        "min_rr_mult":         1.2,
    },
    "aggressive": {
        "id":                  "aggressive",
        "name":                "Aggressiv",
        "emoji":               "⚡",
        "description":         "Alle Regeln mit 150% Modifikatoren — maximale Lern-Nutzung, mehr Trades",
        "rule_types":          None,
        "strength_filter":     None,
        "modifier_scale":      1.5,
        "confidence_threshold": 0.0,
        "min_rr_mult":         0.9,
    },
    "backtest_only": {
        "id":                  "backtest_only",
        "name":                "Backtest-Fokus",
        "emoji":               "📊",
        "description":         "Nur historische Muster — ignoriert Live-Erfahrung, rein statistisch",
        "rule_types":          ["PATTERN", "SETUP_PERFORMANCE", "BIAS_PERFORMANCE"],
        "strength_filter":     None,
        "modifier_scale":      1.0,
        "confidence_threshold": 0.0,
        "min_rr_mult":         1.0,
    },
    "live_only": {
        "id":                  "live_only",
        "name":                "Live-Erfahrung",
        "emoji":               "🧪",
        "description":         "Nur tatsächliche Paper-Trade-Resultate — lernt ausschließlich vom echten Trading",
        "rule_types":          ["LIVE_EXPERIENCE"],
        "strength_filter":     None,
        "modifier_scale":      1.2,
        "confidence_threshold": 0.0,
        "min_rr_mult":         1.0,
    },
    "top_setups": {
        "id":                  "top_setups",
        "name":                "Top-Setups",
        "emoji":               "🎯",
        "description":         "Nur die profitabelsten Setup-Typen — filtert schlechte Setups aggressiv heraus",
        "rule_types":          ["SETUP_PERFORMANCE", "LIVE_EXPERIENCE"],
        "strength_filter":     ["STRONG_BOOST", "BOOST"],
        "modifier_scale":      1.1,
        "confidence_threshold": 0.50,
        "min_rr_mult":         1.1,
    },
}

MIN_SAMPLES    = 5      # Mindest-Samples für eine valide Regel
BOOST_WR       = 0.62   # WR ≥ 62% → BOOST
BLOCK_WR       = 0.38   # WR ≤ 38% → BLOCK
STRONG_BOOST   = 0.72   # WR ≥ 72% → starker BOOST
STRONG_BLOCK   = 0.28   # WR ≤ 28% → starker BLOCK


def _load(path: Path):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {} if path.suffix == ".json" else []


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(BUILD_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")
    print(f"  [StratBuilder] {msg}")


def _strength(wr: float) -> str | None:
    if   wr >= STRONG_BOOST: return "STRONG_BOOST"
    elif wr >= BOOST_WR:     return "BOOST"
    elif wr <= STRONG_BLOCK: return "STRONG_BLOCK"
    elif wr <= BLOCK_WR:     return "BLOCK"
    return None


def _modifier(strength: str) -> int:
    return {"STRONG_BOOST": 20, "BOOST": 12, "STRONG_BLOCK": -25, "BLOCK": -15}[strength]


def _confidence(n: int, max_n: int = 100) -> float:
    return round(min(0.95, 0.40 + (n / max_n) * 0.55), 3)


def run() -> dict:
    """
    Analysiert alle verfügbaren Performance-Daten und synthetisiert neue Regeln.
    Gibt das strategy_rules Dict zurück.
    """
    weights = _load(WEIGHTS_FILE)
    report  = _load(REPORT_FILE)
    trades  = _load(TRADES_FILE)
    if not isinstance(trades, list):
        trades = []

    patterns = weights.get("patterns", {}) if isinstance(weights, dict) else {}
    generation = (_load(RULES_FILE).get("generation", 0) if RULES_FILE.exists() else 0) + 1

    rules: list[dict] = []
    rid   = 0

    # ── 1. Pattern-Regeln (Setup × Bias × Zone × Stunde) ─────────────────────
    hour_agg = defaultdict(lambda: {"n": 0, "wins": 0})

    for key, p in patterns.items():
        wr  = p["win_rate"]
        n   = p["samples"]
        avg_rr = p.get("avg_rr", 1.5)
        hb  = int(p["hour_bucket"])

        for h in range(hb, hb + 3):
            hour_agg[h % 24]["n"]    += n
            hour_agg[h % 24]["wins"] += int(round(wr * n))

        if n < MIN_SAMPLES:
            continue
        st = _strength(wr)
        if st is None:
            continue

        rid += 1
        is_boost = "BOOST" in st
        rules.append({
            "id":       f"pat_{rid:03d}",
            "type":     "PATTERN",
            "action":   "BOOST" if is_boost else "BLOCK",
            "strength": st,
            "conditions": {
                "setup_type":    p["setup_type"],
                "bias":          p["bias"],
                "zone_position": p["zone_position"],
                "hour_min":      hb,
                "hour_max":      (hb + 2) % 24,
            },
            "score_modifier": _modifier(st),
            "min_rr":    round(avg_rr * 0.8, 1) if is_boost else None,
            "win_rate":  round(wr, 3),
            "samples":   n,
            "confidence": _confidence(n),
            "evidence":  (
                f"{p['setup_type']} {p['bias']} {p['zone_position']} "
                f"@{hb:02d}h → {wr*100:.1f}% WR N={n}"
            ),
        })

    # ── 2. Setup-Regeln (aus performance_report) ──────────────────────────────
    min_rr_by_setup:  dict[str, float] = {}
    preferred_setups: list[str] = []
    avoided_setups:   list[str] = []

    for st_name, d in report.get("nach_setup_typ", {}).items():
        wr  = d.get("win_rate_pct", 50) / 100
        n   = d.get("count", 0)
        avg_rr = d.get("avg_rr", 1.5)
        if n < MIN_SAMPLES:
            continue
        st = _strength(wr)
        if st is None:
            continue

        is_boost = "BOOST" in st
        rid += 1
        if is_boost:
            preferred_setups.append(st_name)
            min_rr_by_setup[st_name] = round(max(1.5, avg_rr * 0.7), 1)
        else:
            avoided_setups.append(st_name)

        rules.append({
            "id":       f"setup_{rid:03d}",
            "type":     "SETUP_PERFORMANCE",
            "action":   "BOOST" if is_boost else "BLOCK",
            "strength": st,
            "conditions": {"setup_type": st_name},
            "score_modifier": 10 if st == "STRONG_BOOST" else 5 if st == "BOOST" else -18 if st == "STRONG_BLOCK" else -9,
            "min_rr":    min_rr_by_setup.get(st_name),
            "win_rate":  round(wr, 3),
            "samples":   n,
            "confidence": _confidence(n, 50),
            "evidence":  f"{st_name}: {wr*100:.1f}% WR über {n} Signale",
        })

    # ── 3. Bias-Regeln ────────────────────────────────────────────────────────
    for bias_name, d in report.get("nach_bias", {}).items():
        wr = d.get("win_rate_pct", 50) / 100
        n  = d.get("count", 0)
        if n < MIN_SAMPLES or bias_name == "neutral":
            continue
        st = _strength(wr)
        if st is None:
            continue
        rid += 1
        rules.append({
            "id":       f"bias_{rid:03d}",
            "type":     "BIAS_PERFORMANCE",
            "action":   "BOOST" if "BOOST" in st else "BLOCK",
            "strength": st,
            "conditions": {"bias": bias_name},
            "score_modifier": _modifier(st) // 2,
            "min_rr":    None,
            "win_rate":  round(wr, 3),
            "samples":   n,
            "confidence": _confidence(n, 50),
            "evidence":  f"Bias {bias_name}: {wr*100:.1f}% WR N={n}",
        })

    # ── 4. Live-Erfahrungs-Regeln (Paper-Trade-Ergebnisse) ────────────────────
    live_agg = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        if t.get("pnl") is None:
            continue
        setup_t  = t.get("setup_type", "Unknown")
        dirn     = t.get("direction", "long")
        opened   = t.get("opened_at", "")
        try:
            h = datetime.fromisoformat(opened.replace("Z", "+00:00")).hour
        except Exception:
            h = 12
        hb = (h // 3) * 3
        k  = (setup_t, dirn, hb)
        live_agg[k]["n"]    += 1
        live_agg[k]["wins"] += 1 if t["pnl"] > 0 else 0
        live_agg[k]["pnl"]  += t["pnl"]

    for (setup_t, dirn, hb), d in live_agg.items():
        if d["n"] < MIN_SAMPLES:
            continue
        wr    = d["wins"] / d["n"]
        avg_p = d["pnl"] / d["n"]
        st = _strength(wr)
        if st is None:
            continue
        bias_v = "bullish" if dirn == "long" else "bearish"
        rid += 1
        is_boost = "BOOST" in st
        rules.append({
            "id":       f"live_{rid:03d}",
            "type":     "LIVE_EXPERIENCE",
            "action":   "BOOST" if is_boost else "BLOCK",
            "strength": st,
            "conditions": {
                "setup_type": setup_t,
                "bias":       bias_v,
                "hour_min":   hb,
                "hour_max":   (hb + 2) % 24,
            },
            "score_modifier": _modifier(st),
            "min_rr":    None,
            "win_rate":  round(wr, 3),
            "samples":   d["n"],
            "confidence": _confidence(d["n"], 30),
            "evidence":  (
                f"Live: {wr*100:.1f}% WR Ø ${avg_p:+.2f} "
                f"({setup_t} {dirn} {hb:02d}h, N={d['n']})"
            ),
        })

    # ── 5. Stunden-basierte Metaregeln ────────────────────────────────────────
    good_hours: list[int] = []
    bad_hours:  list[int] = []
    for h, d in hour_agg.items():
        if d["n"] < MIN_SAMPLES:
            continue
        wr = d["wins"] / d["n"]
        if wr >= BOOST_WR:
            good_hours.append(h)
        elif wr <= BLOCK_WR:
            bad_hours.append(h)

    # ── 6. LIVE-Regeln priorisieren, dann nach Samples sortieren ─────────────
    rules.sort(key=lambda r: (
        0 if r["type"] == "LIVE_EXPERIENCE" else
        1 if r["type"] == "PATTERN" else 2,
        0 if r["strength"].startswith("STRONG") else 1,
        -r["samples"],
    ))

    # ── 7. Composite-Regeln: Kombination mehrerer starker Signale ─────────────
    # Wenn eine LIVE_EXPERIENCE und eine PATTERN-Regel identische Bedingungen
    # haben und beide BOOST sind → verstärkte Kombo-Regel
    combo_count = 0
    live_keys = {
        (r["conditions"].get("setup_type"), r["conditions"].get("bias"))
        for r in rules if r["type"] == "LIVE_EXPERIENCE" and r["action"] == "BOOST"
    }
    for r in rules:
        if r["type"] == "PATTERN" and r["action"] == "BOOST":
            key = (r["conditions"].get("setup_type"), r["conditions"].get("bias"))
            if key in live_keys:
                r["score_modifier"] = int(r["score_modifier"] * 1.3)
                r["strength"] = "STRONG_BOOST"
                r["evidence"] += " [LIVE+BACKTEST BESTÄTIGT]"
                combo_count += 1

    # ── 7b. Live-Feedback einarbeiten (Selbstverbesserung) ────────────────────
    # rule_performance.json (von strategy_knowledge gepflegt) zeigt, welche
    # Regeln in echten Paper-Trades funktioniert haben. Schlechte Regeln
    # verlieren Konfidenz oder werden ganz verworfen.
    dropped = 0
    try:
        import strategy_knowledge
        perf = strategy_knowledge.get_rule_performance()
        surviving: list[dict] = []
        for r in rules:
            sig    = strategy_knowledge.rule_signature(r)
            stats  = perf.get(sig, {})
            wins   = int(stats.get("trade_wins", 0))
            losses = int(stats.get("trade_losses", 0))
            n      = wins + losses
            if n >= 4:
                # Wirksamkeit: BOOST-Regel = Trades gewonnen, BLOCK = Trades verloren
                eff_wr = (losses / n) if r["action"] == "BLOCK" else (wins / n)
                r["live_validated"] = {"n": n, "effectiveness": round(eff_wr, 3)}
                if n >= 6 and eff_wr < 0.30:
                    dropped += 1
                    continue   # Regel hat live versagt → verwerfen
                # Konfidenz an Live-Bilanz anpassen (0.6×–1.15×)
                r["confidence"] = round(
                    min(0.95, r["confidence"] * max(0.6, min(1.15, eff_wr * 2.0))), 3
                )
                if eff_wr >= 0.65:
                    r["evidence"] += f" [LIVE-VALIDIERT {eff_wr*100:.0f}% N={n}]"
            surviving.append(r)
        rules = surviving
        if dropped:
            _log(f"Selbstverbesserung: {dropped} live-widerlegte Regel(n) verworfen")
    except Exception:
        pass

    # ── 8. Ergebnis schreiben ─────────────────────────────────────────────────
    boost = sum(1 for r in rules if r["action"] == "BOOST")
    block = sum(1 for r in rules if r["action"] == "BLOCK")
    live  = sum(1 for r in rules if r["type"] == "LIVE_EXPERIENCE")

    out = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "generation":      generation,
        "total_rules":     len(rules),
        "rules":           rules,
        "preferred_setups": preferred_setups,
        "avoided_setups":   avoided_setups,
        "good_hours":       sorted(good_hours),
        "bad_hours":        sorted(bad_hours),
        "min_rr_by_setup":  min_rr_by_setup,
        "summary": {
            "boost_rules":   boost,
            "block_rules":   block,
            "live_rules":    live,
            "pattern_rules": sum(1 for r in rules if r["type"] == "PATTERN"),
            "combo_boosts":  combo_count,
            "dropped_by_feedback": dropped,
        },
    }

    # ── 9. Profil-Statistiken berechnen ──────────────────────────────────────
    profile_stats: dict[str, dict] = {}
    for pid, prof in PROFILES.items():
        matching = []
        for r in rules:
            if prof["rule_types"] and r["type"] not in prof["rule_types"]:
                continue
            if prof["strength_filter"] and r["strength"] not in prof["strength_filter"]:
                continue
            if r["confidence"] < prof["confidence_threshold"]:
                continue
            matching.append(r)
        boost_n = sum(1 for r in matching if r["action"] == "BOOST")
        block_n = sum(1 for r in matching if r["action"] == "BLOCK")
        profile_stats[pid] = {
            **prof,
            "matched_rules": len(matching),
            "boost_rules":   boost_n,
            "block_rules":   block_n,
        }

    # Aktives Profil lesen
    active_profile_id = "balanced"
    if ACTIVE_STRATEGY_FILE.exists():
        try:
            active_profile_id = json.loads(
                ACTIVE_STRATEGY_FILE.read_text(encoding="utf-8")
            ).get("profile_id", "balanced")
        except Exception:
            pass

    out["profiles"]          = profile_stats
    out["active_profile_id"] = active_profile_id

    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    _log(
        f"Gen {generation}: {len(rules)} Regeln "
        f"({boost} BOOST · {block} BLOCK · {live} Live · {combo_count} Combos)"
    )
    return out


if __name__ == "__main__":
    result = run()
    print(f"\nFertig: {result['total_rules']} Regeln in strategy_rules.json")
