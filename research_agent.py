#!/usr/bin/env python3
"""
research_agent.py — Selbst-forschender, disziplinierter Lern-Agent.

Statt von Hand Regeln zu bauen, FORSCHT der Bot selbst:
  1. Feature-Selbstentdeckung: bildet aus echten geschlossenen Signalen Hypothesen
     ("Feature X → Win-Rate-Edge?") über einen Katalog von Bedingungen
     (Session, Wochentag, Fear&Greed, Markt-Bias, Volumen, RSI, Konfidenz, Zone).
  2. Statistische Validierung: nur Hypothesen mit GENUG Stichprobe UND signifikanter
     Abweichung von der Baseline (Wilson-95%-Intervall schließt die Baseline aus)
     werden übernommen — kein Überanpassen an Rauschen.
  3. Auto-Rollback: übernommene Entdeckungen, deren Edge in den JÜNGSTEN Daten
     verfällt/dreht, werden automatisch wieder zurückgezogen.
  4. Optional LLM-Schicht (OAuth-only, budget-gated): schlägt kreative Feature-Kombis
     vor — die aber durch DENSELBEN Validierungs-Filter müssen.

Übernommene Entdeckungen landen in discovered_rules.json und fließen als Score-
Faktor ins Signal-Scoring (_score_signal). Alles wird in research_log.json
protokolliert — nachvollziehbar, nicht Blackbox.
"""
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
RULES_FILE = BASE / "discovered_rules.json"
LOG_FILE   = BASE / "research_log.json"

MIN_N        = 20     # Mindest-Stichprobe je Feature-Wert (Überanpassungs-Schutz)
MIN_LIFT     = 0.12   # Mind. 12pp Abweichung von der Baseline (praktische Signifikanz)
REVIEW_MIN_N = 15     # Mindest-Stichprobe für ein Rollback-Urteil auf jüngsten Daten
MAX_MOD      = 16.0   # Cap für den Score-Modifier einer Entdeckung


# ── Wilson-Score-Intervall (statistische Signifikanz) ─────────────────────────
def _wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half   = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return centre - half, centre + half


# ── Feature-Katalog: row → kategorialer Wert (oder None) ──────────────────────
def _rsi_of(row) -> float | None:
    try:
        for t in json.loads(row.get("all_triggers") or "[]"):
            if isinstance(t, str) and t.startswith("RSI_"):
                return float(t[4:])
    except Exception:
        pass
    return None


def _bucket(v, edges, labels):
    if v is None:
        return None
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


FEATURES: dict = {
    "session": lambda r: _bucket(r.get("time_of_day"), [3, 8, 13, 16, 21],
                                 ["spaet", "asia", "frueh-eu", "eu", "us", "us-spaet"]),
    "wochentag": lambda r: (["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][int(r["day_of_week"])]
                            if r.get("day_of_week") is not None else None),
    "fear_greed": lambda r: _bucket(r.get("fear_greed"), [25, 45, 55, 75],
                                    ["extrem-angst", "angst", "neutral", "gier", "extrem-gier"]),
    "markt_bias": lambda r: r.get("market_bias") or None,
    "volumen": lambda r: _bucket(r.get("volume_ratio"), [1.0, 1.3, 1.8],
                                 ["unter", "normal", "hoch", "spike"]),
    "rsi": lambda r: _bucket(_rsi_of(r), [30, 45, 55, 70],
                             ["ueberverkauft", "niedrig", "mitte", "hoch", "ueberkauft"]),
    "konfidenz": lambda r: _bucket(r.get("confidence_score"), [0.45, 0.6],
                                   ["niedrig", "mittel", "hoch"]),
    "zone": lambda r: r.get("zone_position") or None,
}


def _matches(rule: dict, row: dict) -> bool:
    """Trifft eine Entdeckung (Einzel-Feature oder Kombi) auf ein Signal zu?"""
    try:
        if rule.get("feature") == "combo":
            for f, v in rule.get("conditions", {}).items():
                fn = FEATURES.get(f)
                if not fn or fn(row) != v:
                    return False
            return bool(rule.get("conditions"))
        fn = FEATURES.get(rule["feature"])
        return bool(fn) and fn(row) == rule["value"]
    except Exception:
        return False


def _closed_signals(limit: int | None = None) -> list[dict]:
    con = sqlite3.connect(BASE / "signals.db", timeout=8)
    con.row_factory = sqlite3.Row
    q = ("SELECT * FROM signals WHERE outcome IN ('WIN','LOSS') ORDER BY id DESC")
    rows = [dict(r) for r in con.execute(q).fetchall()]
    con.close()
    return rows[:limit] if limit else rows


def _wr(rows) -> tuple[int, int]:
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    return wins, len(rows)


# ── 1+2. Entdecken & statistisch validieren ───────────────────────────────────
def discover(rows: list[dict]) -> list[dict]:
    """Findet signifikante Feature→Win-Rate-Edges über den Feature-Katalog."""
    if len(rows) < MIN_N:
        return []
    base_w, base_n = _wr(rows)
    baseline = base_w / base_n if base_n else 0.5

    hyps: list[dict] = []
    for fname, fn in FEATURES.items():
        groups: dict = {}
        for r in rows:
            try:
                val = fn(r)
            except Exception:
                val = None
            if val is None:
                continue
            groups.setdefault(val, []).append(r)
        for val, grp in groups.items():
            w, n = _wr(grp)
            if n < MIN_N:
                continue
            wr = w / n
            lift = wr - baseline
            if abs(lift) < MIN_LIFT:
                continue
            lb, ub = _wilson(w, n)
            # Signifikant nur, wenn das 95%-Intervall die Baseline AUSSCHLIESST.
            significant = (lb > baseline) if lift > 0 else (ub < baseline)
            if not significant:
                continue
            action = "BOOST" if lift > 0 else "AVOID"
            conf   = min(1.0, n / 80.0)
            mod    = round(max(-MAX_MOD, min(MAX_MOD, lift * 50.0 * conf)), 1)
            hyps.append({
                "feature": fname, "value": val, "action": action,
                "win_rate": round(wr, 3), "baseline": round(baseline, 3),
                "lift_pp": round(lift * 100, 1), "samples": n,
                "wilson_lb": round(lb, 3), "wilson_ub": round(ub, 3),
                "score_modifier": mod,
            })
    return sorted(hyps, key=lambda h: abs(h["lift_pp"]), reverse=True)


# ── 3. Auto-Rollback: verfallene Entdeckungen zurückziehen ────────────────────
def _still_valid(rule: dict, recent: list[dict]) -> bool:
    """Hält der Edge auf den JÜNGSTEN Signalen noch (gleiche Richtung, signifikant)?"""
    grp = [r for r in recent if _matches(rule, r)]
    w, n = _wr(grp)
    if n < REVIEW_MIN_N:
        return True   # zu wenig neue Daten → noch nicht verwerfen
    base_w, base_n = _wr(recent)
    baseline = base_w / base_n if base_n else 0.5
    wr = w / n
    # Richtung muss erhalten bleiben (BOOST → weiterhin über Baseline, etc.)
    return (wr >= baseline) if rule["action"] == "BOOST" else (wr <= baseline)


def run(use_llm: bool = True) -> dict:
    """Voller Forschungs-Zyklus: entdecken → validieren → übernehmen → reviewen."""
    rows = _closed_signals()
    discovered = discover(rows)

    # Optional: LLM schlägt zusätzliche Hypothesen vor — aber nur PERIODISCH
    # (max. 1×/Tag), damit das knappe Tagesbudget nicht von jedem Trade-Close
    # aufgebraucht wird. "Forscht periodisch", nicht bei jeder Kerze.
    llm_used = False
    if use_llm and _llm_due():
        try:
            extra = _llm_hypotheses(rows, discovered)
            llm_used = True   # Versuch gezählt (auch wenn 0 valide → Zeitstempel setzt)
            if extra:
                discovered = _merge(discovered, extra)
        except Exception:
            pass

    # Auto-Rollback bestehender Entdeckungen auf den jüngsten 80 Signalen.
    recent = rows[:80]
    prev = _load(RULES_FILE).get("rules", [])
    kept, retired = [], []
    disc_keys = {(d["feature"], d["value"]) for d in discovered}
    for r in prev:
        if (r["feature"], r["value"]) in disc_keys and _still_valid(r, recent):
            kept.append(r)
        else:
            retired.append(r)

    # Neue Entdeckungen ergänzen (Datum behalten/setzen).
    now = datetime.now(timezone.utc).isoformat()
    kept_keys = {(r["feature"], r["value"]) for r in kept}
    added = []
    for d in discovered:
        if (d["feature"], d["value"]) not in kept_keys:
            d = {**d, "adopted_at": now}
            kept.append(d); added.append(d)

    out = {"updated": now, "baseline_wr": round(_wr(rows)[0] / max(1, _wr(rows)[1]), 3),
           "n_signals": len(rows), "llm_used": llm_used, "rules": kept}
    RULES_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    log = _load(LOG_FILE).get("entries", [])
    log.insert(0, {"ts": now, "n_signals": len(rows), "discovered": len(discovered),
                   "adopted_total": len(kept), "neu": len(added),
                   "zurueckgezogen": len(retired), "llm": llm_used,
                   "top": [f"{d['feature']}={d['value']} {d['action']} "
                           f"({d['lift_pp']:+.0f}pp N{d['samples']})" for d in discovered[:5]]})
    LOG_FILE.write_text(json.dumps({"entries": log[:50]}, indent=2, ensure_ascii=False))
    return {"discovered": len(discovered), "adopted": len(kept),
            "added": len(added), "retired": len(retired), "llm_used": llm_used}


# ── Score-Integration: Modifier für ein Signal aus den Entdeckungen ───────────
_cache: dict = {}
_mtime: float = 0.0


def score_modifier(row: dict) -> float:
    """Summe der Score-Modifier aller übernommenen Entdeckungen, die matchen."""
    global _cache, _mtime
    try:
        mt = RULES_FILE.stat().st_mtime
    except OSError:
        return 0.0
    if mt != _mtime:
        _cache = _load(RULES_FILE); _mtime = mt
    total = 0.0
    for r in _cache.get("rules", []):
        if _matches(r, row):
            total += r.get("score_modifier", 0.0)
    return round(total, 1)


# ── Feature-KOMBINATION validieren (für LLM-Hypothesen) ───────────────────────
def _validate_combo(rows: list[dict], conditions: dict, baseline: float) -> dict | None:
    """Prüft eine Feature-Kombination (z. B. {rsi:'ueberverkauft', markt_bias:'bearish'})
    statistisch auf den echten Daten — gleicher Filter wie discover()."""
    grp = []
    for r in rows:
        ok = True
        for f, v in conditions.items():
            fn = FEATURES.get(f)
            try:
                if not fn or fn(r) != v:
                    ok = False; break
            except Exception:
                ok = False; break
        if ok:
            grp.append(r)
    w, n = _wr(grp)
    if n < MIN_N:
        return None
    wr = w / n
    lift = wr - baseline
    if abs(lift) < MIN_LIFT:
        return None
    lb, ub = _wilson(w, n)
    if not ((lb > baseline) if lift > 0 else (ub < baseline)):
        return None
    conf = min(1.0, n / 80.0)
    label = "+".join(f"{f}={v}" for f, v in conditions.items())
    return {"feature": "combo", "value": label, "conditions": conditions,
            "action": "BOOST" if lift > 0 else "AVOID",
            "win_rate": round(wr, 3), "baseline": round(baseline, 3),
            "lift_pp": round(lift * 100, 1), "samples": n,
            "wilson_lb": round(lb, 3), "wilson_ub": round(ub, 3),
            "score_modifier": round(max(-MAX_MOD, min(MAX_MOD, lift * 50.0 * conf)), 1)}


# ── Optionale LLM-Schicht (OAuth-only, budget-gated) ──────────────────────────
def _llm_hypotheses(rows: list[dict], already: list[dict]) -> list[dict]:
    """Claude (OAuth/Subscription) schlägt Feature-KOMBINATIONEN vor, die der Agent
    dann selbst auf den Daten validiert. Nur statistisch belegte werden übernommen.
    No-Op wenn Budget erschöpft oder OAuth nicht verfügbar — der deterministische
    Kern trägt die Hauptlast; die LLM ist die kreative Ergänzung."""
    try:
        import budget_guardian
        ok, _ = budget_guardian.check()
        if not ok:
            return []
    except Exception:
        return []
    base_w, base_n = _wr(rows)
    baseline = base_w / base_n if base_n else 0.5
    feat_desc = {f: "kategorial" for f in FEATURES}
    have = [f"{d['feature']}={d['value']}" for d in already]
    prompt = (
        "Du bist ein quantitativer Trading-Researcher. Aus echten geschlossenen "
        f"SOL-Signalen (Baseline-Win-Rate {baseline*100:.0f}%) sollst du FEATURE-"
        "KOMBINATIONEN vorschlagen, die einen Win-Rate-Edge haben könnten. "
        f"Verfügbare Features: {list(feat_desc)}. Werte z. B.: rsi∈"
        "{ueberverkauft,niedrig,mitte,hoch,ueberkauft}, markt_bias∈{bullish,bearish,"
        "neutral}, session∈{asia,frueh-eu,eu,us,us-spaet,spaet}, fear_greed∈"
        "{extrem-angst,angst,neutral,gier,extrem-gier}, volumen∈{unter,normal,hoch,"
        "spike}, wochentag∈{Mo..So}, zone∈{discount,premium,neutral}. "
        f"Bereits bekannt (nicht wiederholen): {have}. "
        "Antworte NUR mit JSON: [{\"conditions\":{\"feature\":\"wert\",...}}, ...] "
        "— max 3 Kombinationen aus je 2 Features."
    )
    try:
        import anthropic
        client = anthropic.Anthropic()   # KEIN api_key → OAuth/Subscription
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=300,
            messages=[{"role": "user", "content": prompt}])
        import re
        txt = resp.content[0].text
        m = re.search(r"\[.*\]", txt, re.S)
        cand = json.loads(m.group(0)) if m else []
    except Exception:
        return []
    out = []
    for c in cand[:3]:
        cond = c.get("conditions") if isinstance(c, dict) else None
        if not isinstance(cond, dict):
            continue
        cond = {f: v for f, v in cond.items() if f in FEATURES}
        if len(cond) < 2:
            continue
        h = _validate_combo(rows, cond, baseline)
        if h:
            out.append(h)
    return out


def _llm_due() -> bool:
    """True höchstens 1×/Tag (periodische LLM-Forschung statt bei jedem Trade)."""
    try:
        log = _load(LOG_FILE).get("entries", [])
        last = next((e["ts"] for e in log if e.get("llm")), None)
        if not last:
            return True
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
        return age_h >= 20
    except Exception:
        return True


def _merge(a: list[dict], b: list[dict]) -> list[dict]:
    seen = {(h["feature"], h["value"]) for h in a}
    return a + [h for h in b if (h["feature"], h["value"]) not in seen]


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


if __name__ == "__main__":
    r = run()
    print(f"[research_agent] entdeckt={r['discovered']} übernommen={r['adopted']} "
          f"(neu={r['added']}, zurückgezogen={r['retired']}, llm={r['llm_used']})")
    for x in _load(RULES_FILE).get("rules", []):
        print(f"  {x['feature']}={x['value']:14} {x['action']:5} "
              f"{x['lift_pp']:+.0f}pp  N={x['samples']}  mod={x['score_modifier']:+.1f}")
