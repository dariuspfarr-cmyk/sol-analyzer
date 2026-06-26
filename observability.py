#!/usr/bin/env python3
"""
observability.py — Konsolidierter Bot-Gesundheits-Snapshot.

Beantwortet auf einen Blick: Funktioniert das Lernen? Steigt die Win-Rate? Was
feuert, was wird gemieden, füllen die Pullback-Limits? Aggregiert die ECHTEN
Daten (signals.db, state.json, backtest_weights.json, strategy_rules.json) — keine
Schätzungen. Wird vom /api/health-Endpoint und dem Dashboard-Panel genutzt.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent


def _load(name: str) -> dict:
    try:
        return json.loads((BASE / name).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _wr(rows) -> float | None:
    dec = [r for r in rows if r in ("WIN", "LOSS")]
    if not dec:
        return None
    return round(100.0 * sum(1 for r in dec if r == "WIN") / len(dec), 1)


def snapshot() -> dict:
    out: dict = {"updated": datetime.now(timezone.utc).isoformat()}

    # ── Signale: gesamt + rollierende Win-Rate (7d / 30d) aus echten Outcomes ──
    try:
        con = sqlite3.connect(BASE / "signals.db", timeout=8)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT timestamp, outcome FROM signals WHERE outcome IS NOT NULL"
        ).fetchall()
        con.close()
        now = datetime.now(timezone.utc)

        def _age_days(ts):
            try:
                return (now - datetime.fromisoformat(ts)).total_seconds() / 86400
            except Exception:
                return 9e9

        allout = [r["outcome"] for r in rows]
        wr7  = _wr([r["outcome"] for r in rows if _age_days(r["timestamp"]) <= 7])
        wr30 = _wr([r["outcome"] for r in rows if _age_days(r["timestamp"]) <= 30])
        wr_all = _wr(allout)
        trend = None
        if wr7 is not None and wr30 is not None:
            trend = round(wr7 - wr30, 1)
        out["signals"] = {
            "total":   len(allout),
            "win":     allout.count("WIN"),
            "loss":    allout.count("LOSS"),
            "expired": allout.count("EXPIRED"),
            "wr_all":  wr_all,
            "wr_7d":   wr7,
            "wr_30d":  wr30,
            "wr_trend_7_vs_30": trend,
        }
    except Exception as e:
        out["signals"] = {"error": str(e)}

    # ── "Verbesserung statt Block": füllen & gewinnen die Pullback-Trades? ────
    # Misst die source='improved'-Signale (im trigger_reason markiert): wie viele
    # erzeugt, wie viele gefüllt (getradet), wie das Outcome ist. So sieht man, ob
    # die aggressive Improve-Logik real trägt — oder nur EXPIRED produziert.
    try:
        con = sqlite3.connect(BASE / "signals.db", timeout=8)
        con.row_factory = sqlite3.Row
        imp = con.execute(
            "SELECT paper_traded, outcome FROM signals "
            "WHERE trigger_reason LIKE '%[improved]%'"
        ).fetchall()
        con.close()
        filled = [r for r in imp if r["paper_traded"]]
        out["improvements"] = {
            "created":    len(imp),
            "filled":     len(filled),
            "fill_pct":   round(100.0 * len(filled) / len(imp), 1) if imp else None,
            "win_rate":   _wr([r["outcome"] for r in filled]),
            "expired":    sum(1 for r in imp if r["outcome"] == "EXPIRED"),
        }
    except Exception:
        pass

    # ── Paper Trader (echter Kontostand/Trades) ───────────────────────────────
    st = _load("state.json")
    if st:
        w, l = st.get("wins", 0), st.get("losses", 0)
        out["paper"] = {
            "balance":     round(st.get("balance", 0), 2),
            "pnl":         round(st.get("balance", 0) - 10000.0, 2),
            "trades":      st.get("total_trades", 0),
            "wins":        w,
            "losses":      l,
            "win_rate":    round(100.0 * w / (w + l), 1) if (w + l) else None,
            "open_positions": len(st.get("positions", []) or []),
            "max_drawdown_pct": round(st.get("max_drawdown", 0), 2),
        }

    # ── Was der Bot BEVORZUGT vs. MEIDET (aktive gelernte Regeln) ─────────────
    rules = _load("strategy_rules.json").get("rules", [])
    fav: dict = {}
    avo: dict = {}
    for r in rules:
        c = r.get("conditions", {})
        label = " ".join(str(c[k]) for k in ("setup_type", "bias") if k in c) or str(c)
        item = {"was": label, "wr": r.get("win_rate"), "n": r.get("samples"),
                "mod": r.get("score_modifier", 0)}
        # Pro (Setup×Bias)-Label nur die STÄRKSTE Regel behalten (Pattern-Stunden-
        # Regeln erzeugen sonst Duplikate).
        if r.get("action") == "BOOST":
            if label not in fav or item["mod"] > fav[label]["mod"]:
                fav[label] = item
        elif r.get("action") == "BLOCK":
            if label not in avo or item["mod"] < avo[label]["mod"]:
                avo[label] = item
    out["favored"] = sorted(fav.values(), key=lambda x: x["mod"], reverse=True)[:6]
    out["avoided"] = sorted(avo.values(), key=lambda x: x["mod"])[:6]

    # ── Timeframe-Effizienz: Auflösungsquote (TP/SL vs. abgelaufen) ───────────
    tfp = _load("backtest_weights.json").get("timeframe_performance", {})
    out["timeframes"] = {
        tf: {"win_rate": round(d.get("win_rate", 0) * 100, 1),
             "resolved_pct": round(d.get("resolution_rate", 0) * 100, 1)
                              if d.get("resolution_rate") is not None else None,
             "decided": d.get("decided"), "expired": d.get("expired")}
        for tf, d in sorted(tfp.items())
    }

    # ── Tagesstunden (gelernt) ────────────────────────────────────────────────
    hp = _load("backtest_weights.json").get("hourly_performance", {})
    hours = [(int(k), round(v.get("win_rate", 0) * 100), v.get("samples", 0))
             for k, v in hp.items() if v.get("samples", 0) >= 8]
    hours.sort(key=lambda x: x[1], reverse=True)
    if hours:
        out["best_hours"]  = [{"h": f"{h:02d}-{(h+2)%24:02d}h", "wr": wr} for h, wr, _ in hours[:2]]
        out["worst_hours"] = [{"h": f"{h:02d}-{(h+2)%24:02d}h", "wr": wr} for h, wr, _ in hours[-2:]]

    # ── Markt-Regime (was aktuell erlaubt ist) ────────────────────────────────
    try:
        import bull_run_detector as brd
        out["regime"] = {"phase": brd.get_phase(),
                         "allow_shorts": brd.get_playbook().get("allow_shorts", True)}
    except Exception:
        pass

    # ── Selbst-entdeckte Feature-Edges (research_agent) ───────────────────────
    rj = _load("discovered_rules.json")
    if rj.get("rules"):
        out["discoveries"] = [
            {"was": f"{r['feature']}={r['value']}", "action": r["action"],
             "lift_pp": r.get("lift_pp"), "n": r.get("samples"),
             "mod": r.get("score_modifier")}
            for r in sorted(rj["rules"], key=lambda x: abs(x.get("lift_pp", 0)), reverse=True)[:6]
        ]

    # ── Katalysator (anstehende Events + Sentiment-Extreme) ───────────────────
    try:
        import catalyst
        c = catalyst.snapshot()
        out["catalyst"] = {
            "risk_off": c.get("risk_off"),
            "imminent": (c["imminent_event"]["title"] if c.get("imminent_event") else None),
            "next_events": [f"{e['title']} (in {e['in_hours']}h)" for e in c.get("upcoming_events", [])[:3]],
            "bias_hints": c.get("bias_hints", [])[:3],
        }
    except Exception:
        pass

    # ── XGBoost-Modell-Status ─────────────────────────────────────────────────
    mr = _load("model_report.json")
    if mr:
        out["model"] = {"active": bool(mr.get("ersetzt_haiku")),
                        "accuracy": mr.get("accuracy_pct"),
                        "samples": mr.get("trainings_samples")}
    return out


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=2, ensure_ascii=False))
