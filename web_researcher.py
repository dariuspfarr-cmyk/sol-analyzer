"""
Web Researcher — autonomes Marktresearch-Modul.

Sammelt kostenlos und ohne API-Key Marktdaten:
  • Fear & Greed Index     → alternative.me
  • Funding Rate           → Binance Futures
  • Open Interest          → Binance Futures
  • Long/Short Ratio       → Binance Futures
  • BTC Dominanz           → CoinGecko (global)
  • Solana TVL             → DeFiLlama

Leitet daraus einen Markt-Bias ab (bullish / bearish / neutral)
und gibt Insights, die die Signal-Konfidenz im Bot beeinflussen.

Cache: research_cache.json (TTL = 1h)
Wird automatisch aufgerufen nach jedem Bot-Lauf (server.py).
"""

import json
import requests
from datetime import datetime, timezone
from pathlib import Path

CACHE_FILE = Path(__file__).parent / "research_cache.json"
CACHE_TTL  = 3600  # 1 Stunde

SYMBOL  = "SOLUSDT"
HEADERS = {"User-Agent": "SOLAnalyzer/2.0"}
TIMEOUT = 12


def _get(url: str) -> dict | list:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ── Einzelne Datenquellen ─────────────────────────────────────────────────────

def _fetch_fear_greed() -> dict:
    data = _get("https://api.alternative.me/fng/?limit=7")["data"]
    hist = [{"value": int(d["value"]), "label": d["value_classification"]} for d in data]
    return {
        "current":   hist[0],
        "yesterday": hist[1] if len(hist) > 1 else None,
        "week_avg":  round(sum(d["value"] for d in hist) / len(hist), 1),
        "history":   hist,
    }


def _fetch_funding() -> dict:
    d = _get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL}")
    rate = float(d["lastFundingRate"]) * 100
    return {
        "rate_pct":    round(rate, 4),
        "mark_price":  float(d["markPrice"]),
        "index_price": float(d["indexPrice"]),
    }


def _fetch_open_interest() -> dict:
    d = _get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={SYMBOL}")
    # historische OI-Änderung (1h)
    hist = _get(
        f"https://fapi.binance.com/futures/data/openInterestHist"
        f"?symbol={SYMBOL}&period=1h&limit=2"
    )
    oi_now  = float(d["openInterest"])
    oi_prev = float(hist[0]["sumOpenInterest"]) if hist else oi_now
    change  = (oi_now - oi_prev) / max(oi_prev, 0.01) * 100
    return {
        "oi_tokens":   round(oi_now, 2),
        "change_1h_pct": round(change, 2),
    }


def _fetch_long_short() -> dict:
    data = _get(
        f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
        f"?symbol={SYMBOL}&period=1h&limit=3"
    )
    latest  = data[0] if data else {}
    long_r  = float(latest.get("longAccount", 0.5))
    return {
        "long_pct":  round(long_r * 100, 1),
        "short_pct": round((1 - long_r) * 100, 1),
        "ratio":     round(float(latest.get("longShortRatio", 1.0)), 3),
    }


def _fetch_global_market() -> dict:
    d   = _get("https://api.coingecko.com/api/v3/global")["data"]
    pct = d.get("market_cap_percentage", {})
    return {
        "btc_dominance":         round(float(pct.get("btc", 0)), 1),
        "eth_dominance":         round(float(pct.get("eth", 0)), 1),
        "market_cap_change_24h": round(float(d.get("market_cap_change_percentage_24h_usd", 0)), 2),
        "active_cryptos":        d.get("active_cryptocurrencies", 0),
    }


def _fetch_sol_tvl() -> dict:
    chains = _get("https://api.llama.fi/v2/chains")
    sol = next((c for c in chains if c.get("name", "").lower() == "solana"), None)
    if not sol:
        return {"tvl_usd": 0, "tvl_change_24h": 0}
    return {
        "tvl_usd":        round(float(sol.get("tvl", 0))),
        "tvl_change_24h": round(float(sol.get("change_1d") or 0), 2),
    }


# ── Markt-Analyse ─────────────────────────────────────────────────────────────

def _analyze(sources: dict) -> dict:
    """
    Leitet Markt-Bias (bullish / bearish / neutral) und Risiko-Level aus
    allen Datenquellen ab. Bias-Score: -1.0 (bearish) bis +1.0 (bullish).
    """
    fg       = sources.get("fear_greed",    {}).get("current", {}).get("value", 50)
    funding  = sources.get("funding",       {}).get("rate_pct", 0.0)
    long_pct = sources.get("long_short",    {}).get("long_pct", 50.0)
    btc_dom  = sources.get("global_market", {}).get("btc_dominance", 50.0)
    mc_chg   = sources.get("global_market", {}).get("market_cap_change_24h", 0.0)
    tvl_chg  = sources.get("sol_tvl",       {}).get("tvl_change_24h", 0.0)
    oi_chg   = sources.get("open_interest", {}).get("change_1h_pct", 0.0)

    bias = 0.0

    # Fear & Greed (konträrer Indikator)
    if   fg < 15:  bias += 1.5
    elif fg < 25:  bias += 0.8
    elif fg < 35:  bias += 0.3
    elif fg > 85:  bias -= 1.5
    elif fg > 75:  bias -= 0.8
    elif fg > 65:  bias -= 0.3

    # Funding Rate (negativ = Shorts dominieren = Squeeze-Potential)
    if   funding < -0.05: bias += 1.2
    elif funding < -0.02: bias += 0.5
    elif funding < -0.005: bias += 0.1
    elif funding >  0.10: bias -= 1.2
    elif funding >  0.05: bias -= 0.5
    elif funding >  0.02: bias -= 0.2

    # Long/Short (< 40% Long = überwiegend Short = Bounce-Potential)
    if   long_pct < 35: bias += 0.8
    elif long_pct < 42: bias += 0.3
    elif long_pct > 65: bias -= 0.8
    elif long_pct > 58: bias -= 0.3

    # BTC Dominanz (steigend = Altcoin-schwach)
    if   btc_dom > 58: bias -= 0.4
    elif btc_dom > 52: bias -= 0.1
    elif btc_dom < 42: bias += 0.4
    elif btc_dom < 48: bias += 0.1

    # Gesamtmarkt 24h
    if   mc_chg >  3: bias += 0.4
    elif mc_chg >  1: bias += 0.1
    elif mc_chg < -3: bias -= 0.4
    elif mc_chg < -1: bias -= 0.1

    # SOL TVL
    if   tvl_chg >  5: bias += 0.3
    elif tvl_chg < -5: bias -= 0.3

    # Open Interest Änderung (stark steigend mit Preis = Trend stärker)
    if   oi_chg >  5: bias += 0.2
    elif oi_chg < -5: bias -= 0.2

    # Auf -1..+1 normieren
    bias_norm = max(-1.0, min(1.0, bias / 5.5))

    if   bias_norm >=  0.20: market_bias = "bullish"
    elif bias_norm <= -0.20: market_bias = "bearish"
    else:                    market_bias = "neutral"

    # Risiko-Level
    risk_pts = 0
    if fg > 75 or fg < 25:          risk_pts += 1
    if abs(funding) > 0.05:         risk_pts += 1
    if long_pct > 65 or long_pct < 35: risk_pts += 1
    risk_level = "hoch" if risk_pts >= 2 else "mittel" if risk_pts == 1 else "niedrig"

    # Aktuelle Insights
    insights = []
    if fg < 25:
        insights.append(f"Extrem-Fear ({fg}) → konträr bullishes Signal")
    elif fg > 75:
        insights.append(f"Extrem-Greed ({fg}) → Markt überhitzt, Vorsicht")
    if funding < -0.02:
        insights.append(f"Neg. Funding ({funding:+.3f}%) → Shorts dominant, Squeeze-Potential")
    elif funding > 0.05:
        insights.append(f"Pos. Funding ({funding:+.3f}%) → Longs überdehnt, Liquidationsrisiko")
    if long_pct < 38:
        insights.append(f"Nur {long_pct:.0f}% Long → Markt bearish positioniert")
    elif long_pct > 62:
        insights.append(f"{long_pct:.0f}% Long → Markt bullish überdehnt")
    if tvl_chg < -5:
        insights.append(f"SOL TVL -{abs(tvl_chg):.1f}% → Kapitalabfluss aus Ökosystem")
    elif tvl_chg > 5:
        insights.append(f"SOL TVL +{tvl_chg:.1f}% → Kapitalzufluss ins Ökosystem")
    if btc_dom > 55:
        insights.append(f"BTC Dom. {btc_dom:.0f}% → Altcoins unter Druck")

    return {
        "market_bias":    market_bias,
        "bias_score":     round(bias_norm, 3),
        "risk_level":     risk_level,
        "fear_greed":     fg,
        "funding_pct":    funding,
        "long_pct":       long_pct,
        "btc_dominance":  btc_dom,
        "tvl_change_24h": tvl_chg,
        "oi_change_1h":   oi_chg,
        "insights":       insights,
    }


# ── Öffentliche API ───────────────────────────────────────────────────────────

def run() -> dict:
    """
    Führt alle Research-Abrufe durch, analysiert und cached das Ergebnis.
    Gibt den vollständigen Report zurück.
    """
    print(f"\n  [Research] Marktdaten abrufen…")
    result = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sources":    {},
        "analysis":   {},
        "errors":     [],
    }

    tasks = {
        "fear_greed":    _fetch_fear_greed,
        "funding":       _fetch_funding,
        "open_interest": _fetch_open_interest,
        "long_short":    _fetch_long_short,
        "global_market": _fetch_global_market,
        "sol_tvl":       _fetch_sol_tvl,
    }

    for name, fn in tasks.items():
        try:
            result["sources"][name] = fn()
            print(f"  [Research] {name}: OK")
        except Exception as e:
            result["sources"][name] = {}
            result["errors"].append(f"{name}: {e}")
            print(f"  [Research] {name}: Fehler — {e}")

    result["analysis"] = _analyze(result["sources"])

    try:
        import bull_run_detector as _brd
        _br = _brd.get_cached()
        result["analysis"]["bull_run_phase"]      = _br.get("phase", "unknown")
        result["analysis"]["bull_run_confidence"] = _br.get("confidence", 0.0)
    except Exception:
        result["analysis"]["bull_run_phase"] = "unknown"

    a = result["analysis"]
    print(f"  [Research] Bias={a['market_bias']} ({a['bias_score']:+.2f})"
          f"  FG={a['fear_greed']}  Funding={a['funding_pct']:+.3f}%"
          f"  Risiko={a['risk_level']}")
    for ins in a.get("insights", []):
        print(f"  [Research]   → {ins}")

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return result


def get_cached() -> dict:
    """Gecachtes Ergebnis zurückgeben, oder run() wenn Cache veraltet/fehlt."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            ts  = datetime.fromisoformat(cached["fetched_at"])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < CACHE_TTL:
                return cached
        except Exception:
            pass
    try:
        return run()
    except Exception as e:
        print(f"  [Research] Fehler beim Abruf: {e}")
        return {"fetched_at": datetime.now(timezone.utc).isoformat(),
                "sources": {}, "analysis": {}, "errors": [str(e)]}


def get_market_bias() -> str:
    """Gibt aktuellen Markt-Bias zurück: 'bullish' | 'bearish' | 'neutral'."""
    try:
        return get_cached().get("analysis", {}).get("market_bias", "neutral")
    except Exception:
        return "neutral"


def get_fear_greed() -> int:
    """Gibt aktuellen Fear & Greed Wert zurück (0-100)."""
    try:
        src = get_cached().get("sources", {})
        return src.get("fear_greed", {}).get("current", {}).get("value", 50)
    except Exception:
        return 50


if __name__ == "__main__":
    import json
    result = run()
    print(json.dumps(result["analysis"], indent=2, ensure_ascii=False))
