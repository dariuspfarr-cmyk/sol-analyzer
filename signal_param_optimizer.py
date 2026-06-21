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
SINGLE_DIR_MIN = 30       # ab so vielen Trades EINER Richtung darf sie allein
                          # (bei klar negativer Bilanz) abgeschaltet werden
BLOCK_MIN    = 20         # min. Trades, um ein Setup/TF als toxisch zu blocken
BLOCK_WR     = 30.0       # WR-Schwelle: darunter (+ negativ) = toxisch → blocken
MIN_BUCKET_N = 3          # min. Trades je 5er-RSI-Bucket, um ihm zu trauen
MIN_ZONE_W   = 15         # RSI-Zone nie schmaler als das (sonst keine Signale)
MAX_STEP     = 12         # max. Verschiebung je Grenze pro Lauf → graduell, kein Overfit-Sprung
_RSI_RE      = re.compile(r"RSI=([\d.]+)")

# Defaults (gleich wie im Browser-Scan), falls noch nichts gelernt
_DEFAULTS = {"lRsiMin": 28, "lRsiMax": 52, "sRsiMin": 52, "sRsiMax": 72,
             "rrRatio": 2, "slPct": 6, "pivotLB": 5, "dirMode": "both"}


# Auto-KI-Setup (DB) → signalType der Browser-Engine
_SETUP_TO_SIGTYPE = {"Zone": "bounce", "BOS": "breakout", "CHoCH": "reversal"}


def _load_autoki_closed() -> list[dict]:
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT trigger_reason, bias, outcome, pnl_pct, setup_type, timeframe "
        "FROM signals WHERE routing='autoki' AND outcome IN ('WIN','LOSS')").fetchall()]
    out = []
    for r in rows:
        m = _RSI_RE.search(r.get("trigger_reason") or "")
        rsi = float(m.group(1)) if m else None
        out.append({
            "rsi": rsi,
            "dir": "long" if r.get("bias") == "bullish" else "short",
            "win": r.get("outcome") == "WIN",
            "pnl": float(r.get("pnl_pct") or 0.0),
            "setup": r.get("setup_type") or "?",
            "tf":    r.get("timeframe") or "?",
        })
    return out


def _block_by(samples: list[dict], key: str):
    """Findet toxische Ausprägungen (≥ BLOCK_MIN Trades, WR < BLOCK_WR, Ø-PnL < 0)."""
    from collections import defaultdict
    agg: dict = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for s in samples:
        d = agg[s[key]]
        d["n"] += 1; d["w"] += 1 if s["win"] else 0; d["pnl"] += s["pnl"]
    blocked = []
    for k, d in agg.items():
        if (d["n"] >= BLOCK_MIN and d["w"] / d["n"] * 100 < BLOCK_WR
                and d["pnl"] / d["n"] < 0):
            blocked.append((k, d["n"], round(d["w"] / d["n"] * 100, 1)))
    return blocked


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
    rsi_ok  = [s for s in samples if s["rsi"] is not None]
    longs   = [s for s in rsi_ok if s["dir"] == "long"]
    shorts  = [s for s in rsi_ok if s["dir"] == "short"]

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

    # ── Richtungs-Filter: klar unprofitable Richtung abschalten ───────────────
    # Greift bei STARKER Einzel-Evidenz (≥ SINGLE_DIR_MIN Trades + klar negativ),
    # auch wenn die Gegenrichtung noch wenig Daten hat — z. B. 82 Shorts @ 20% WR
    # in einem steigenden Markt. Wird jeden Lernzyklus neu bewertet (regime-adaptiv).
    def _stats(rs):
        if not rs:
            return (0.0, 0.0)
        return (sum(s["pnl"] for s in rs) / len(rs),
                sum(1 for s in rs if s["win"]) / len(rs) * 100)
    l_exp, l_wr = _stats(longs)
    s_exp, s_wr = _stats(shorts)
    cur_dir = params.get("dirMode", "both")
    new_dir = cur_dir
    short_bad = len(shorts) >= SINGLE_DIR_MIN and s_exp < 0 and s_wr < 40
    long_bad  = len(longs)  >= SINGLE_DIR_MIN and l_exp < 0 and l_wr < 40
    if short_bad and not long_bad:
        new_dir = "long"
    elif long_bad and not short_bad:
        new_dir = "short"
    elif len(longs) >= MIN_SAMPLES and len(shorts) >= MIN_SAMPLES:
        if   l_exp >= 0 and s_exp < 0: new_dir = "long"
        elif s_exp >= 0 and l_exp < 0: new_dir = "short"
        else:                          new_dir = "both"
    if new_dir != cur_dir:
        changed.append(
            f"dirMode: {cur_dir} → {new_dir} "
            f"(Long {l_wr:.0f}%/{l_exp:+.1f}% · Short {s_wr:.0f}%/{s_exp:+.1f}%)")
        params["dirMode"] = new_dir

    # ── Setup-Blocklist (Punkt 3) ────────────────────────────────────────────
    # Toxischen Setup-Typ (z. B. Zone/Bounce 3.7% WR) abschalten. KEIN Timeframe-
    # Blocking — die Präzision je TF kommt aus der Multi-Timeframe-Konfluenz im
    # Browser (ein Signal muss von den höheren TFs bestätigt sein), nicht durch
    # Wegnehmen eines Charts.
    sig_blocks = sorted({_SETUP_TO_SIGTYPE.get(k, k.lower())
                         for k, n, wr in _block_by(samples, "setup")})
    if sig_blocks != sorted(params.get("blockedSignalTypes", [])):
        info = ", ".join(f"{k} {wr}%" for k, n, wr in _block_by(samples, "setup"))
        changed.append(f"blockedSignalTypes → {sig_blocks} ({info})")
        params["blockedSignalTypes"] = sig_blocks
    # Frühere TF-Blocklist nicht mehr verwenden (4h bleibt erhalten)
    if params.get("blockedTFs"):
        params["blockedTFs"] = []
        changed.append("blockedTFs → [] (kein TF-Blocking; MTF-Konfluenz stattdessen)")

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
