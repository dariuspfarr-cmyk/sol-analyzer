"""
live_trading — Vorbereitung & Sicherheits-Layer fürs Traden mit ECHTEM GELD.

‼️  SICHERHEIT ZUERST.  Standard = maximal sicher: es wird NIEMALS automatisch
eine echte Order platziert. Um real zu traden, müssen DREI unabhängige Schalter
bewusst umgelegt werden + eine Umgebungs-Bestätigung gesetzt sein + API-Keys
vorhanden + Pre-Flight bestanden + kein Kill-Switch aktiv. Solange irgendeine
Bedingung fehlt, läuft alles als Dry-Run (nur Logging) — wie der Paper Trader,
nur dass dieselben Signale realistisch an die echte Börse gespiegelt würden.

Dieselbe realistische Ausführung wie im Paper Trader (Slippage/Gebühren/SL-TP)
bleibt das Modell — diese Schicht fügt nur die echte Order-Anbindung + harte
Risiko-Begrenzungen hinzu.

Echtes Geld scharf schalten (alle Schritte nötig):
  1. ccxt installieren:            pip install ccxt
  2. Börse + Keys in .env:         SOL_EXCHANGE=blofin
                                   SOL_EXCHANGE_API_KEY=...   (NUR Trade-Recht,
                                   SOL_EXCHANGE_SECRET=...     KEIN Withdrawal!)
                                   SOL_EXCHANGE_PASSWORD=...   (falls nötig)
  3. In dieser Datei:              LIVE_TRADING_ENABLED = True  und  DRY_RUN = False
  4. Umgebungs-Bestätigung:        SOL_LIVE_ARM=I_UNDERSTAND_THE_RISK
  5. Erst auf Testnet / mit Mini-Größe testen!  preflight() muss "ok" sein.
Kill-Switch: Datei „LIVE_KILL" anlegen → sofort keine neuen Orders mehr.
"""
from __future__ import annotations
import os
import json
from datetime import datetime, timezone
from pathlib import Path

BASE          = Path(__file__).parent
AUDIT_LOG     = BASE / "live_orders.log"      # jede Aktion (Dry-Run & echt)
KILL_SWITCH   = BASE / "LIVE_KILL"            # existiert → Halt neuer Orders
DAILY_FILE    = BASE / "live_daily.json"      # Tages-PnL-Tracking (echt)
POS_FILE      = BASE / "live_positions.json"  # offene (echte/Dry-Run) Positionen

# ══ HARTE SICHERHEITS-SCHALTER (Default: maximal sicher) ════════════════════
LIVE_TRADING_ENABLED = False   # Master-Schalter. False ⇒ niemals echte Orders.
DRY_RUN              = True    # True ⇒ Orders nur loggen, NICHT an die Börse.
ARM_ENV              = "SOL_LIVE_ARM"
ARM_VALUE            = "I_UNDERSTAND_THE_RISK"   # genau dieser Wert erforderlich

# ══ RISIKO-LIMITS (echtes Geld) — bewusst klein als Startwerte ══════════════
MAX_POSITION_USD       = 50.0     # max. Einsatz je Trade
MAX_OPEN_POSITIONS     = 3        # max. gleichzeitig offene Live-Positionen
MAX_DAILY_LOSS_USD     = 25.0     # Tages-Verlust-Stop → danach keine neuen Orders
MAX_TOTAL_EXPOSURE_USD = 150.0    # max. Summe aller offenen Notionals
ALLOWED_SYMBOLS        = {"SOLUSDT", "SOL/USDT", "SOL/USDT:USDT"}

EXCHANGE   = os.getenv("SOL_EXCHANGE", "blofin")
_API_KEY   = os.getenv("SOL_EXCHANGE_API_KEY", "")
_API_SECRET= os.getenv("SOL_EXCHANGE_SECRET", "")
_API_PASS  = os.getenv("SOL_EXCHANGE_PASSWORD", "")


class LiveGuardError(Exception):
    """Wird geworfen, wenn eine echte Order gegen eine Schutzbedingung verstößt."""


# ── Audit ────────────────────────────────────────────────────────────────────
def _audit(event: str, data: dict) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── Sicherheits-Status ────────────────────────────────────────────────────────
def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


def keys_present() -> bool:
    return bool(_API_KEY and _API_SECRET)


def is_armed() -> tuple[bool, str]:
    """
    Echtes Trading nur wenn ALLE Bedingungen erfüllt. Gibt (armed, grund) zurück.
    Auch wenn armed=True, platziert place_order im DRY_RUN trotzdem KEINE Order.
    """
    if kill_switch_active():
        return False, "Kill-Switch aktiv (Datei LIVE_KILL existiert)"
    if not LIVE_TRADING_ENABLED:
        return False, "LIVE_TRADING_ENABLED = False"
    if os.getenv(ARM_ENV) != ARM_VALUE:
        return False, f"Umgebungs-Bestätigung {ARM_ENV} fehlt/falsch"
    if not keys_present():
        return False, "API-Keys fehlen (SOL_EXCHANGE_API_KEY/SECRET)"
    if _daily_loss() >= MAX_DAILY_LOSS_USD:
        return False, f"Tages-Verlust-Limit erreicht (${MAX_DAILY_LOSS_USD})"
    return True, "scharf"


# ── Tages-Verlust-Tracking ────────────────────────────────────────────────────
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_loss() -> float:
    try:
        d = json.loads(DAILY_FILE.read_text(encoding="utf-8"))
        if d.get("date") == _today():
            return max(0.0, -float(d.get("pnl", 0.0)))
    except Exception:
        pass
    return 0.0


def record_realized_pnl(pnl_usd: float) -> None:
    """Realisierten PnL eines echten Trades fürs Tages-Limit verbuchen."""
    try:
        d = {}
        if DAILY_FILE.exists():
            d = json.loads(DAILY_FILE.read_text(encoding="utf-8"))
        if d.get("date") != _today():
            d = {"date": _today(), "pnl": 0.0}
        d["pnl"] = round(float(d.get("pnl", 0.0)) + float(pnl_usd), 4)
        DAILY_FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


# ── Offene Live-Positionen verfolgen (echt & Dry-Run) ────────────────────────
# Damit die Exposure-/Anzahl-Limits real greifen UND Positionen geschlossen
# werden können. Im Dry-Run ist das die Generalprobe des kompletten Lebenszyklus.
def get_open_live_positions() -> list:
    try:
        return json.loads(POS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_live_positions(positions: list) -> None:
    try:
        POS_FILE.write_text(json.dumps(positions, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    except Exception:
        pass


def _record_open(order_desc: dict, dry_run: bool, exchange_order_id=None) -> None:
    positions = get_open_live_positions()
    positions.append({**order_desc, "dry_run": dry_run,
                      "exchange_order_id": exchange_order_id,
                      "opened_at": datetime.now(timezone.utc).isoformat()})
    _save_live_positions(positions)


def close_position(signal_id, exit_price: float, reason: str = "") -> dict:
    """
    Schließt die zu signal_id gehörende Live-Position — schließt den Lebenszyklus.
    Echt: reduce-only Market-Order + realisierten PnL fürs Tages-Limit verbuchen.
    Dry-Run: nur simulieren/loggen (kein echtes Geld berührt).
    """
    positions = get_open_live_positions()
    pos = next((p for p in positions if p.get("meta", {}).get("signal_id") == signal_id), None)
    if pos is None:
        return {"closed": False, "reason": "keine Live-Position zu diesem Signal"}

    entry = float(pos.get("entry", 0) or 0)
    size  = float(pos.get("size", 0) or 0)
    side  = pos.get("side")
    pnl   = (exit_price - entry) * size if side == "buy" else (entry - exit_price) * size

    rest = [p for p in positions if p is not pos]
    _save_live_positions(rest)

    if pos.get("dry_run") or DRY_RUN or not is_armed()[0]:
        _audit("dry_run_close", {"signal_id": signal_id, "exit": exit_price,
                                 "pnl": round(pnl, 4), "reason": reason})
        return {"closed": True, "dry_run": True, "pnl": round(pnl, 4)}

    # ── ECHTES Schließen ──
    try:
        ex = _make_exchange()
        opp = "sell" if side == "buy" else "buy"
        ex.create_order(pos["symbol"], "market", opp, size, None, {"reduceOnly": True})
        record_realized_pnl(pnl)
        _audit("live_close", {"signal_id": signal_id, "exit": exit_price,
                              "pnl": round(pnl, 4), "reason": reason})
        return {"closed": True, "dry_run": False, "pnl": round(pnl, 4)}
    except Exception as e:
        _save_live_positions(positions)   # bei Fehler Position behalten
        _audit("live_close_error", {"signal_id": signal_id, "error": str(e)})
        return {"closed": False, "reason": f"Close-Fehler: {e}"}


# ── Risiko-Prüfung jeder Order ────────────────────────────────────────────────
def risk_check(symbol: str, notional_usd: float, open_positions: list) -> tuple[bool, str]:
    if symbol not in ALLOWED_SYMBOLS:
        return False, f"Symbol {symbol} nicht in der Allowlist"
    if notional_usd > MAX_POSITION_USD:
        return False, f"Positionsgröße ${notional_usd:.2f} > Limit ${MAX_POSITION_USD}"
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return False, f"Max. offene Positionen ({MAX_OPEN_POSITIONS}) erreicht"
    exposure = sum(float(p.get("notional", 0)) for p in open_positions) + notional_usd
    if exposure > MAX_TOTAL_EXPOSURE_USD:
        return False, f"Gesamt-Exposure ${exposure:.2f} > Limit ${MAX_TOTAL_EXPOSURE_USD}"
    if _daily_loss() >= MAX_DAILY_LOSS_USD:
        return False, "Tages-Verlust-Limit erreicht"
    return True, "ok"


# ── Pre-Flight: vor dem Scharfschalten alles prüfen ──────────────────────────
def preflight() -> dict:
    """
    Umfassender Bereitschafts-Check. Ändert NICHTS, platziert nichts.
    Gibt einen Report mit ok/Warnungen zurück.
    """
    report: dict = {"checks": {}, "ok": False, "warnings": []}
    c = report["checks"]
    c["live_enabled"]   = LIVE_TRADING_ENABLED
    c["dry_run"]        = DRY_RUN
    c["arm_env_set"]    = os.getenv(ARM_ENV) == ARM_VALUE
    c["keys_present"]   = keys_present()
    c["kill_switch"]    = kill_switch_active()
    c["daily_loss_usd"] = _daily_loss()
    c["limits"] = {
        "MAX_POSITION_USD": MAX_POSITION_USD,
        "MAX_OPEN_POSITIONS": MAX_OPEN_POSITIONS,
        "MAX_DAILY_LOSS_USD": MAX_DAILY_LOSS_USD,
        "MAX_TOTAL_EXPOSURE_USD": MAX_TOTAL_EXPOSURE_USD,
    }
    # ccxt + Börsen-Verbindung (read-only) prüfen
    try:
        import ccxt  # noqa: F401
        c["ccxt"] = True
    except Exception:
        c["ccxt"] = False
        report["warnings"].append("ccxt nicht installiert (pip install ccxt)")
    if c["ccxt"] and keys_present():
        try:
            ex = _make_exchange()
            bal = ex.fetch_balance()
            usdt = (bal.get("USDT") or {}).get("free")
            c["exchange_connected"] = True
            c["balance_usdt"] = usdt
            if not usdt or float(usdt) < MAX_POSITION_USD:
                report["warnings"].append("USDT-Guthaben < MAX_POSITION_USD")
        except Exception as e:
            c["exchange_connected"] = False
            report["warnings"].append(f"Börsen-Verbindung fehlgeschlagen: {e}")
    armed, reason = is_armed()
    c["armed"] = armed
    c["arm_reason"] = reason
    report["ok"] = bool(armed and c.get("ccxt") and c.get("exchange_connected")
                        and not DRY_RUN)
    return report


# ── Börsen-Adapter (ccxt) ─────────────────────────────────────────────────────
def _make_exchange():
    import ccxt
    klass = getattr(ccxt, EXCHANGE)
    cfg = {"apiKey": _API_KEY, "secret": _API_SECRET, "enableRateLimit": True,
           "options": {"defaultType": "swap"}}
    if _API_PASS:
        cfg["password"] = _API_PASS
    return klass(cfg)


# ── Order platzieren (vollständig abgesichert) ───────────────────────────────
def place_order(symbol: str, direction: str, size: float, entry: float,
                sl: float, tp: float, open_positions: list,
                meta: dict | None = None) -> dict:
    """
    Spiegelt einen Trade an die echte Börse — NUR wenn vollständig scharf.
    Sonst Dry-Run (nur Audit-Log). Gibt {placed, dry_run, reason, ...} zurück.
    """
    notional = abs(size * entry)
    side     = "buy" if direction == "long" else "sell"

    ok, why = risk_check(symbol, notional, open_positions)
    if not ok:
        _audit("rejected", {"symbol": symbol, "side": side, "notional": notional,
                            "reason": why})
        return {"placed": False, "dry_run": DRY_RUN, "reason": why}

    armed, reason = is_armed()
    order_desc = {"symbol": symbol, "side": side, "size": round(size, 6),
                  "entry": entry, "sl": sl, "tp": tp, "notional": round(notional, 2),
                  "meta": meta or {}}

    # Dry-Run ODER nicht scharf → nur loggen + Position verfolgen (Generalprobe),
    # aber NICHT an die Börse senden.
    if DRY_RUN or not armed:
        _audit("dry_run_order", {**order_desc, "armed": armed, "arm_reason": reason})
        _record_open(order_desc, dry_run=True)
        return {"placed": False, "dry_run": True, "reason": reason if not armed else "DRY_RUN",
                "order": order_desc}

    # ── ECHTE ORDER (nur erreichbar wenn armed=True UND DRY_RUN=False) ──
    try:
        ex = _make_exchange()
        order = ex.create_order(symbol, "market", side, size)
        # Schutz-Orders (SL/TP) als reduce-only platzieren — börsenabhängig
        try:
            opp = "sell" if side == "buy" else "buy"
            ex.create_order(symbol, "stop", opp, size, None,
                            {"stopPrice": sl, "reduceOnly": True})
            ex.create_order(symbol, "take_profit", opp, size, tp,
                            {"reduceOnly": True})
        except Exception as e:
            _audit("protective_order_failed", {"symbol": symbol, "error": str(e)})
        _audit("live_order_placed", {**order_desc, "exchange_order_id": order.get("id")})
        _record_open(order_desc, dry_run=False, exchange_order_id=order.get("id"))
        return {"placed": True, "dry_run": False, "order": order_desc,
                "exchange_order_id": order.get("id")}
    except Exception as e:
        _audit("live_order_error", {**order_desc, "error": str(e)})
        return {"placed": False, "dry_run": False, "reason": f"Order-Fehler: {e}"}


def mirror_paper_trade(position: dict, open_live_positions: list | None = None) -> dict:
    """
    Spiegelt eine offene Paper-Position an die echte Börse — vollständig durch
    place_order abgesichert (Default: Dry-Run/aus). Die Live-Größe wird EIGENS
    aus MAX_POSITION_USD bestimmt (nicht die Paper-Größe), damit das echte Risiko
    immer ≤ Limit bleibt.
    """
    entry = float(position.get("entry", 0) or 0)
    if entry <= 0:
        return {"placed": False, "reason": "kein Entry-Preis"}
    live_size = MAX_POSITION_USD / entry
    return place_order(
        "SOLUSDT", position.get("direction", "long"), live_size, entry,
        float(position.get("sl", 0) or 0), float(position.get("tp", 0) or 0),
        open_live_positions if open_live_positions is not None else get_open_live_positions(),
        meta={"signal_id": position.get("signal_id"),
              "setup": position.get("setup_type"),
              "tf": position.get("timeframe")},
    )


def status() -> dict:
    """Kompakter Sicherheits-Status für Dashboard/CLI."""
    armed, reason = is_armed()
    return {
        "live_enabled": LIVE_TRADING_ENABLED, "dry_run": DRY_RUN,
        "armed": armed, "reason": reason, "kill_switch": kill_switch_active(),
        "keys_present": keys_present(), "exchange": EXCHANGE,
        "daily_loss_usd": _daily_loss(),
        "limits": {"pos": MAX_POSITION_USD, "open": MAX_OPEN_POSITIONS,
                   "daily_loss": MAX_DAILY_LOSS_USD, "exposure": MAX_TOTAL_EXPOSURE_USD},
    }


if __name__ == "__main__":
    print("══ LIVE-TRADING SICHERHEITS-STATUS ══")
    for k, v in status().items():
        print(f"  {k:14}: {v}")
    print("\n══ PRE-FLIGHT ══")
    pf = preflight()
    for k, v in pf["checks"].items():
        print(f"  {k:18}: {v}")
    if pf["warnings"]:
        print("  Warnungen:")
        for w in pf["warnings"]:
            print(f"    ⚠️  {w}")
    print(f"\n  BEREIT FÜR ECHTES TRADING: {'JA' if pf['ok'] else 'NEIN (sicher im Dry-Run)'}")
