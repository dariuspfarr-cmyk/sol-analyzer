#!/usr/bin/env python3
"""
catalyst.py — Katalysator-Recherche: liest News/Events + Sentiment-Extreme ein und
macht sie handelbar.

Konsolidiert:
  • Anstehende High-Impact-Events (Wirtschaftskalender) → Risk-Off-Fenster.
  • Sentiment-Extreme aus web_researcher (Fear&Greed, Funding, Long/Short-Crowding)
    → konträre Richtungs-Hinweise.

Liefert snapshot() für Dashboard/Health UND einen kleinen Score-Effekt
(score_adjust) für imminent-event-Risiko — ergänzend zum harten News-Block im
Paper Trader. Bewusst leicht: der harte Block + die gelernte Fear&Greed-Performance
tragen die Hauptlast; dies macht Katalysatoren sichtbar und nutzbar.
"""
import time
from datetime import datetime, timezone

import requests

_CACHE: dict = {"events": [], "ts": 0.0}
_TTL = 1800  # 30 min


def _upcoming_events() -> list[dict]:
    """High-Impact-USD-Events der nächsten 24h aus dem ForexFactory-Wochenfeed."""
    if time.time() - _CACHE["ts"] < _TTL:
        return _CACHE["events"]
    out: list[dict] = []
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=8)
        now = datetime.now(timezone.utc)
        for e in r.json():
            if e.get("impact") != "High" or e.get("country") not in ("USD", "US"):
                continue
            try:
                dt = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            except Exception:
                continue
            hrs = (dt - now).total_seconds() / 3600
            if 0 <= hrs <= 24:
                out.append({"title": e.get("title", "?"),
                            "in_hours": round(hrs, 1),
                            "when": dt.isoformat()})
        out.sort(key=lambda x: x["in_hours"])
    except Exception:
        out = _CACHE["events"]
    _CACHE.update(events=out, ts=time.time())
    return out


def snapshot() -> dict:
    """Konsolidierter Katalysator-Status."""
    events = _upcoming_events()
    imminent = next((e for e in events if e["in_hours"] <= 2), None)

    sentiment: dict = {}
    bias_hints: list[str] = []
    try:
        import web_researcher
        a = web_researcher.get_cached().get("analysis", {})
        fg       = a.get("fear_greed", 50)
        funding  = a.get("funding_pct", 0.0)
        long_pct = a.get("long_pct", 50.0)
        sentiment = {"fear_greed": fg, "funding_pct": funding, "long_pct": long_pct,
                     "phase": a.get("bull_run_phase")}
        # Konträre Katalysator-Hinweise aus Extremen
        if fg <= 20:   bias_hints.append("Extrem-Angst → konträr bullish")
        elif fg >= 80: bias_hints.append("Extrem-Gier → konträr bearish")
        if long_pct >= 65: bias_hints.append(f"{long_pct:.0f}% Long überdehnt → bearish-Risiko")
        elif long_pct <= 35: bias_hints.append(f"{long_pct:.0f}% Long → konträr bullish")
        if abs(funding) >= 0.05: bias_hints.append(f"Funding-Extrem {funding:+.3f}% → Squeeze-Risiko")
        sentiment["insights"] = a.get("insights", [])
    except Exception:
        pass

    risk_off = imminent is not None or any(
        h.startswith("Funding-Extrem") for h in bias_hints)
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "upcoming_events": events[:4],
        "imminent_event": imminent,
        "risk_off": risk_off,
        "sentiment": sentiment,
        "bias_hints": bias_hints,
    }


def score_adjust() -> float:
    """Kleiner Score-Abschlag bei imminenten High-Impact-Events (Risiko-Reduktion,
    ergänzend zum harten News-Block). 0 wenn nichts ansteht."""
    try:
        for e in _upcoming_events():
            if e["in_hours"] <= 2:
                return -12.0   # unmittelbar bevorstehend → Score senken
            if e["in_hours"] <= 6:
                return -5.0
    except Exception:
        pass
    return 0.0


if __name__ == "__main__":
    import json
    print(json.dumps(snapshot(), indent=2, ensure_ascii=False))
