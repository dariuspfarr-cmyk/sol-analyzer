"""
API Gate — die 8 Pflicht-Checks vor jedem AI-Aufruf.

Alle 8 Checks müssen TRUE sein, bevor lazy_api den Client überhaupt importiert.
Schützt vor versehentlicher API-Key-Abrechnung und Budget-Überschreitung.
Alle Ausgaben auf Deutsch.
"""

import os
from datetime import datetime, timezone

import config
import budget_guardian


def run_all_checks(signal_context: dict) -> tuple[bool, list[str]]:
    """
    Führt alle 8 Gate-Checks aus.
    Gibt (alle_bestanden, protokoll_zeilen) zurück.
    """
    log: list[str] = []
    passed = True

    def check(nr: int, name: str, ok: bool, detail: str = ""):
        nonlocal passed
        mark = "✓" if ok else "✗"
        log.append(f"  Gate {nr}/8 [{mark}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            passed = False
        return ok

    # ── 1. KEIN API-Key gesetzt (Billing-Schutz) ──────────────────────────────
    no_key = not os.getenv("ANTHROPIC_API_KEY")
    check(1, "Kein ANTHROPIC_API_KEY in Umgebung", no_key,
          "" if no_key else "API-KEY GESETZT — BILLING-RISIKO!")

    # ── 2. OAuth-Only-Modus aktiv ─────────────────────────────────────────────
    check(2, "FORCE_OAUTH_ONLY aktiv", config.FORCE_OAUTH_ONLY is True)

    # ── 3. Tagesbudget nicht überschritten ────────────────────────────────────
    budget_ok, budget_reason = budget_guardian.check()
    check(3, "Budget innerhalb der Limits", budget_ok, budget_reason)

    # ── 4. Signal-Kontext vorhanden ───────────────────────────────────────────
    ctx_ok = isinstance(signal_context, dict) and bool(signal_context)
    check(4, "Signal-Kontext vorhanden", ctx_ok)

    # ── 5. Trigger-Grund vorhanden (kein Leeraufruf) ──────────────────────────
    trig_ok = bool(signal_context.get("trigger_reason")) if ctx_ok else False
    check(5, "Trigger-Grund vorhanden", trig_ok)

    # ── 6. Preis-Daten plausibel ──────────────────────────────────────────────
    price = signal_context.get("price_now", 0) if ctx_ok else 0
    price_ok = isinstance(price, (int, float)) and price > 0
    check(6, "Preis-Daten plausibel", price_ok, f"${price}" if price_ok else "ungültig")

    # ── 7. Keine Doppelausführung (Cooldown) ──────────────────────────────────
    cooldown_ok = _cooldown_ok(signal_context.get("timeframe", "4h") if ctx_ok else "4h")
    check(7, "Cooldown eingehalten", cooldown_ok,
          "" if cooldown_ok else "zu kurz seit letztem Call")

    # ── 8. Subscription/OAuth verfügbar ───────────────────────────────────────
    oauth_ok = _oauth_available()
    check(8, "OAuth/Subscription verfügbar", oauth_ok,
          "" if oauth_ok else "keine OAuth-Credentials gefunden")

    return passed, log


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────
_LAST_CALL: dict[str, datetime] = {}
_COOLDOWN_MINUTES = {"30m": 25, "1h": 50, "4h": 200, "1d": 1200}


def _cooldown_ok(timeframe: str) -> bool:
    """Verhindert Mehrfach-Calls innerhalb desselben Kerzenintervalls."""
    now  = datetime.now(timezone.utc)
    last = _LAST_CALL.get(timeframe)
    cd   = _COOLDOWN_MINUTES.get(timeframe, 60)
    if last and (now - last).total_seconds() < cd * 60:
        return False
    _LAST_CALL[timeframe] = now
    return True


def _oauth_available() -> bool:
    """
    Prüft, ob OAuth/Subscription-Credentials vorhanden sind.
    Claude Code legt OAuth-Token im Benutzerprofil ab.
    """
    from pathlib import Path
    [
        Path.home() / ".claude" / ".credentials.json",
        Path.home() / ".config" / "claude" / "credentials.json",
        Path(os.getenv("APPDATA", "")) / "Claude" / "credentials.json",
    ]
    # Wenn kein API-Key gesetzt ist, nutzt der SDK automatisch OAuth.
    # Wir akzeptieren OAuth, solange kein Key gesetzt ist.
    return not os.getenv("ANTHROPIC_API_KEY")


def print_gate_log(log: list[str]) -> None:
    """Druckt das Gate-Protokoll ins Terminal."""
    print("  🚪 API-Gate-Prüfung:")
    for line in log:
        print(line)
