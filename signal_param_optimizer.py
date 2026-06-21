"""
signal_param_optimizer — schließt den Lernkreis für die Auto-KI-Signale.

Lernt aus den REALISTISCHEN Paper-Trade-Outcomes der Auto-KI-Signale
(BREAK/BOUNCE) und verbessert deren Erzeugungs-Parameter, damit der Bot diese
Signale selbst profitabler UND präziser macht:

  • profitabler → nur noch in den RSI-Zonen erzeugen, in denen die Signale real
    Geld verdient haben (WR ≥ 50% UND Ø-PnL ≥ 0),
  • präziser    → die RSI-Zonen auf genau diese Gewinn-Spanne verengen.

Schreibt strategy_params.json, das der Browser (AutoSignalEngine) beim Laden
anwendet (_labLoadParams). Wird vom Lernkreis nach Trade-Close aufgerufen.

Konservativ: ändert eine Zone nur mit genug geschlossenen Trades (MIN_SAMPLES)
und nie über die bisher beobachtete Spanne hinaus (kein Raten ohne Daten).
"""
from __future__ import annotations
import sqlite3
import json
import re
import time
from pathlib import Path

DB           = Path(__file__).parent / "signals.db"
PARAMS_FILE  = Path(__file__).parent / "strategy_params.json"
MIN_SAMPLES  = 15         # min. geschlossene Auto-KI-Trades je Richtung
MIN_BUCKET_N = 3          # min. Trades je 5er-RSI-Bucket, um ihm zu trauen
MIN_ZONE_W   = 15         # RSI-Zone nie schmaler als das (sonst keine Signale)
MAX_STEP     = 12         # max. Verschiebung je Grenze pro Lauf → graduell, kein Overfit-Sprung
_RSI_RE      = re.compile(r"RSI=([\d.]+)")

# Defaults (gleich wie im Browser-Scan), falls noch nichts gelernt
_DEFAULTS = {"lRsiMin": 28, "lRsiMax": 52, "sRsiMin": 52, "sRsiMax": 72,
             "rrRatio": 2, "slPct": 6, "pivotLB": 5, "dirMode": "both"}


def _load_autoki_closed() -> list[dict]:
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT trigger_reason, bias, outcome, pnl_pct FROM signals "
        "WHERE routing='autoki' AND outcome IN ('WIN','LOSS')").fetchall()]
    out = []
    for r in rows:
        m = _RSI_RE.search(r.get("trigger_reason") or "")
        if not m:
            continue
        out.append({
            "rsi": float(m.group(1)),
            "dir": "long" if r.get("bias") == "bullish" else "short",
            "win": r.get("outcome") == "WIN",
            "pnl": float(r.get("pnl_pct") or 0.0),
        })
    return out


def _clamp_step(cur: float, target: float) -> float:
    """Bewegt cur höchstens MAX_STEP Richtung target (graduelles Lernen)."""
    if target > cur:
        return min(target, cur + MAX_STEP)
    return max(target, cur - MAX_STEP)


def _best_rsi_zone(samples: list[dict], lo_cur: float, hi_cur: float):
    """
    Trimmt die RSI-Zone an den Rändern, wo die Signale klar VERLIEREN (WR < 45%
    bei genug Trades), behält den profitablen Kern. Bewegt die Grenzen nur
    GRADUELL (MAX_STEP) und hält eine Mindestbreite (MIN_ZONE_W) — robust gegen
    Overfit auf eine Marktphase. Gibt (lo, hi, info) zurück.
    """
    if len(samples) < MIN_SAMPLES:
        return lo_cur, hi_cur, None
    buckets: dict = {}
    for s in samples:
        b = int(s["rsi"] // 5) * 5
        d = buckets.setdefault(b, {"n": 0, "w": 0, "pnl": 0.0})
        d["n"]   += 1
        d["w"]   += 1 if s["win"] else 0
        d["pnl"] += s["pnl"]
    # PROFIT-FOKUS: behalte Buckets mit positiver Erwartung (Ø-PnL ≥ 0) und
    # nicht-katastrophaler WR. Trimmt die Zone auf die real PROFITABLEN RSI-Bereiche.
    keep = sorted(b for b, d in buckets.items()
                  if d["n"] >= MIN_BUCKET_N
                  and d["pnl"] / d["n"] >= 0.0
                  and d["w"] / d["n"] >= 0.40)
    if not keep:
        return lo_cur, hi_cur, {"buckets": buckets, "keep": []}
    tgt_lo, tgt_hi = float(min(keep)), float(max(keep) + 5)
    # Graduell + Mindestbreite
    lo = _clamp_step(lo_cur, tgt_lo)
    hi = _clamp_step(hi_cur, tgt_hi)
    if hi - lo < MIN_ZONE_W:
        mid = (lo + hi) / 2.0
        lo, hi = mid - MIN_ZONE_W / 2.0, mid + MIN_ZONE_W / 2.0
    return lo, hi, {"buckets": buckets, "keep": keep}


def optimize() -> dict:
    """Lernt die Signal-Parameter aus den realistischen Auto-KI-Outcomes."""
    samples = _load_autoki_closed()
    longs   = [s for s in samples if s["dir"] == "long"]
    shorts  = [s for s in samples if s["dir"] == "short"]

    params: dict = {}
    if PARAMS_FILE.exists():
        try:
            params = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
        except Exception:
            params = {}

    changed: list[str] = []

    def setp(k, v):
        v = round(float(v))
        if int(params.get(k, _DEFAULTS.get(k, 0))) != v:
            changed.append(f"{k}: {params.get(k, _DEFAULTS.get(k))} → {v}")
        params[k] = v

    if len(longs) >= MIN_SAMPLES:
        lmin, lmax, _ = _best_rsi_zone(longs, params.get("lRsiMin", _DEFAULTS["lRsiMin"]),
                                       params.get("lRsiMax", _DEFAULTS["lRsiMax"]))
        setp("lRsiMin", lmin); setp("lRsiMax", lmax)
    if len(shorts) >= MIN_SAMPLES:
        smin, smax, _ = _best_rsi_zone(shorts, params.get("sRsiMin", _DEFAULTS["sRsiMin"]),
                                       params.get("sRsiMax", _DEFAULTS["sRsiMax"]))
        setp("sRsiMin", smin); setp("sRsiMax", smax)

    # ── Richtungs-Filter: eine klar unprofitable Richtung abschalten ──────────
    # Nur wenn BEIDE Richtungen genug Daten haben (sonst kein Urteil).
    def _expect(rs):
        return (sum(s["pnl"] for s in rs) / len(rs)) if rs else 0.0
    if len(longs) >= MIN_SAMPLES and len(shorts) >= MIN_SAMPLES:
        l_exp, s_exp = _expect(longs), _expect(shorts)
        if   l_exp >= 0 and s_exp < 0: new_dir = "long"
        elif s_exp >= 0 and l_exp < 0: new_dir = "short"
        else:                          new_dir = "both"
        if params.get("dirMode") != new_dir:
            changed.append(f"dirMode: {params.get('dirMode', 'both')} → {new_dir} "
                           f"(Ø-PnL Long {l_exp:+.2f}% / Short {s_exp:+.2f}%)")
            params["dirMode"] = new_dir

    # Defaults sicherstellen (Browser lädt nur wenn p.lRsiMin existiert)
    for k, v in _DEFAULTS.items():
        params.setdefault(k, v)
    params["learned_from"]    = "autoki_paper_outcomes"
    params["learned_samples"] = {"long": len(longs), "short": len(shorts)}
    params["savedAt"]         = int(time.time() * 1000)

    if changed:
        PARAMS_FILE.write_text(json.dumps(params, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    return {"changed": changed, "longs": len(longs), "shorts": len(shorts),
            "params": params}


if __name__ == "__main__":
    r = optimize()
    print(f"Auto-KI-Signal-Optimierung — Long {r['longs']} / Short {r['shorts']} "
          f"geschlossene Trades  ·  {len(r['changed'])} Änderung(en):")
    for c in r["changed"]:
        print("  ", c)
    if not r["changed"]:
        print("  (keine — zu wenig Daten oder Zonen bereits optimal)")
