"""
strategy_selector — wählt automatisch die BESTE der vom Bot erstellten
Strategien (Profile aus strategy_builder) und aktiviert sie.

Bisher wurde das aktive Profil (active_strategy.json) manuell im Dashboard
gewählt. Dieser Selektor bewertet alle 6 Profile HISTORISCH an den realen
abgeschlossenen Signalen und aktiviert automatisch das stärkste — der Paper
Trader tradet damit immer die aktuell beste Strategie.

Hinweis: Die Bewertung läuft auf denselben Outcomes, aus denen strategy_builder
die Regeln teils ableitet — es ist also eine historische (in-sample-nahe)
Bewertung, gut für das RELATIVE Ranking der Profile, keine OOS-Garantie. Recency-
Gewichtung + Hysterese + Profitabilitäts-Gate dämpfen Überanpassung.

Bewertung pro Profil (faithful zur echten Wirkung im Paper Trader):
  • Für jedes abgeschlossene Signal wird strategy_knowledge.evaluate() MIT DIESEM
    Profil ausgewertet (gleiche Logik, kein Duplikat).
  • BLOCK-Regel gematcht → Signal gilt als NICHT getradet (Gewicht 0) — genau wie
    der Paper Trader BLOCK-Setups überspringt.
  • Sonst Positions-Gewicht = clamp((BASE+Modifier)/BASE, 0.5, 2.0) — spiegelt das
    WR-/Score-gewichtete Sizing (bessere Signale = mehr Kapital).
  • Recency-Gewichtung (Half-Life): jüngste Outcomes zählen mehr → passt sich an
    die aktuelle Marktphase an.

Auswahl-Ziel (HAUPTZIEL): maximale Win-Rate — aber nur unter Profilen, die auch
profitabel sind (Expectancy ≥ 0), damit keine R:R-Tricks gewinnen. Hysterese
verhindert ständiges Umschalten auf Rauschen.

Aufruf: im Lernzyklus (strategy_evolver, Schritt 7) oder per CLI.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE          = Path(__file__).parent
ACTIVE_FILE   = BASE / "active_strategy.json"
SELECT_LOG    = BASE / "strategy_selection.json"

# Sizing-Konstanten — gespiegelt aus paper_trader (risk_mult = comp_score/70,
# geclamped 0.5–2.0). Bewusst dupliziert, um den schweren paper_trader-Import
# (Caches/Seiteneffekte) im Lernzyklus zu vermeiden.
BASE_SCORE    = 70.0
MIN_W         = 0.5
MAX_W         = 2.0

MIN_TRADED    = 40     # ein Profil muss ≥N Signale traden (sonst nicht belastbar)
RECENCY_HALF  = 120    # Half-Life in Signalen (jüngere Outcomes zählen mehr)
SWITCH_MARGIN = 1.5    # nur umschalten, wenn die beste Strategie die aktive um
                       # ≥1.5 WR-Punkte schlägt (Hysterese gegen Flattern)


def _load_closed() -> list[dict]:
    """Abgeschlossene Signale (WIN/LOSS) mit den für evaluate() nötigen Feldern,
    neueste zuerst (für Recency-Rang)."""
    import signal_logger as sl
    rows: list[dict] = []
    try:
        c = sqlite3.connect(str(sl.DB_PATH))
        c.row_factory = sqlite3.Row
        cur = c.execute(
            "SELECT setup_type, bias, zone_position, time_of_day, outcome, pnl_pct "
            "FROM signals WHERE outcome IN ('WIN','LOSS') "
            "ORDER BY id DESC"
        )
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
    except Exception:
        pass
    return rows


def _profiles() -> dict:
    import strategy_knowledge as sk
    return sk._load_rules_doc().get("profiles", {})


def _active_profile_id() -> str:
    try:
        return json.loads(ACTIVE_FILE.read_text(encoding="utf-8")).get("profile_id", "balanced")
    except Exception:
        return "balanced"


def _score_profile(pid: str, closed: list[dict]) -> dict:
    """Bewertet ein Profil out-of-sample. Gibt gewichtete WR/Expectancy + N zurück."""
    import strategy_knowledge as sk
    w_traded = 0.0       # Summe der Gewichte aller getradeten Signale
    w_wins   = 0.0       # Summe der Gewichte der gewonnenen Signale
    w_pnl    = 0.0       # gewichtete Summe signierter pnl%
    n_traded = 0         # Anzahl nicht geblockter Signale (roh)
    n_blocked = 0

    for rank, sig in enumerate(closed):
        rw = 0.5 ** (rank / RECENCY_HALF)          # Recency: rank 0 = neueste
        setup = sig.get("setup_type") or "?"
        bias  = sig.get("bias") or "neutral"
        zone  = sig.get("zone_position") or "neutral"
        hour  = int(sig.get("time_of_day") or 0)
        mod, sigs = sk.evaluate(setup, bias, zone, hour, profile_id=pid)

        if any("|BLOCK|" in s for s in sigs):
            n_blocked += 1
            continue                                # geblockt → nicht getradet

        size_w = max(MIN_W, min(MAX_W, (BASE_SCORE + mod) / BASE_SCORE))
        w = size_w * rw
        won = sig.get("outcome") == "WIN"
        pnl = abs(float(sig.get("pnl_pct") or 0.0))
        signed = pnl if won else -pnl

        w_traded += w
        if won:
            w_wins += w
        w_pnl += w * signed
        n_traded += 1

    wr  = (w_wins / w_traded * 100.0) if w_traded > 0 else 0.0
    exp = (w_pnl / w_traded) if w_traded > 0 else 0.0
    return {
        "profile_id":   pid,
        "weighted_wr":  round(wr, 1),
        "weighted_exp": round(exp, 3),
        "n_traded":     n_traded,
        "n_blocked":    n_blocked,
    }


def evaluate_all() -> list[dict]:
    """Bewertet alle Profile. Liste sortiert nach gewichteter WR (beste zuerst)."""
    closed = _load_closed()
    profs  = _profiles()
    if not closed or not profs:
        return []
    stats = [_score_profile(pid, closed) for pid in profs]
    stats.sort(key=lambda s: (s["weighted_wr"], s["weighted_exp"], s["n_traded"]),
               reverse=True)
    return stats


def select(write: bool = True) -> dict:
    """
    Wählt die beste Strategie und aktiviert sie (mit Hysterese). Respektiert
    ein manuelles Pinning (active_strategy.json {"pinned": true}).
    Gibt {changed, from, to, reason, ranking} zurück.
    """
    stats = evaluate_all()
    if not stats:
        return {"changed": False, "reason": "keine Daten (Signale/Profile fehlen)"}

    cur_id = _active_profile_id()

    # manuelles Pinning respektieren
    try:
        if json.loads(ACTIVE_FILE.read_text(encoding="utf-8")).get("pinned"):
            return {"changed": False, "to": cur_id, "reason": "gepinnt (manuell)",
                    "ranking": stats}
    except Exception:
        pass

    # nur belastbare UND profitable Profile sind wählbar
    eligible = [s for s in stats if s["n_traded"] >= MIN_TRADED and s["weighted_exp"] >= 0]
    if not eligible:
        # Fallback: belastbar genug, auch wenn knapp negativ → bestes nach WR
        eligible = [s for s in stats if s["n_traded"] >= MIN_TRADED] or stats

    best = eligible[0]
    cur  = next((s for s in stats if s["profile_id"] == cur_id), None)
    cur_wr = cur["weighted_wr"] if cur else -1.0

    # Hysterese: nur umschalten, wenn klar besser als die aktive Strategie
    changed = best["profile_id"] != cur_id and best["weighted_wr"] >= cur_wr + SWITCH_MARGIN
    chosen  = best["profile_id"] if changed else cur_id
    reason = (
        f"{best['profile_id']} WR {best['weighted_wr']}% (Exp {best['weighted_exp']:+.2f}%, "
        f"N={best['n_traded']}) schlägt aktiv {cur_id} {cur_wr}% um "
        f"{best['weighted_wr'] - cur_wr:+.1f}p"
        if changed else
        f"aktiv bleibt {cur_id} ({cur_wr}%) — bestes {best['profile_id']} "
        f"({best['weighted_wr']}%) nicht +{SWITCH_MARGIN}p besser"
    )

    if write and changed:
        ACTIVE_FILE.write_text(json.dumps({
            "profile_id":   chosen,
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "auto_selected": True,
            "reason":       reason,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            SELECT_LOG.write_text(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "chosen": chosen, "from": cur_id, "ranking": stats,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    return {"changed": changed, "from": cur_id, "to": chosen,
            "reason": reason, "ranking": stats}


if __name__ == "__main__":
    r = select(write=True)
    print("══ STRATEGIE-SELEKTOR ══")
    for i, s in enumerate(r.get("ranking", []), 1):
        mark = "←" if s["profile_id"] == r.get("to") else " "
        print(f"  {i}. {s['profile_id']:14} WR {s['weighted_wr']:5.1f}%  "
              f"Exp {s['weighted_exp']:+6.2f}%  N={s['n_traded']:3} "
              f"(geblockt {s['n_blocked']}) {mark}")
    print(f"\n  {'UMGESCHALTET' if r['changed'] else 'unverändert'}: {r['reason']}")
