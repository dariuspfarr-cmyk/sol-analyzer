"""
Lazy API — importiert 'anthropic' NUR im Moment des Aufrufs, niemals oben.

Harte Schutzmechanismen:
  - Wenn ANTHROPIC_API_KEY gesetzt ist → sofortiger Abbruch (Billing-Schutz)
  - Alle 8 Gate-Checks müssen bestehen, bevor der Client importiert wird
  - Budget-Limits werden hart durchgesetzt
  - OAuth/Subscription ist die einzige erlaubte Authentifizierung
  - Client wird nach Gebrauch sofort zerstört

Alle Ausgaben auf Deutsch.
"""

import os

import config
import api_gate
import budget_guardian


def call_api_if_justified(signal_context: dict,
                          system_prompt: str,
                          user_message: str,
                          model: str | None = None,
                          max_tokens: int = 1000) -> dict | None:
    """
    Ruft Claude NUR auf, wenn alle Schutz-Checks bestehen.
    Gibt {"text", "input_tokens", "output_tokens", "model"} zurück
    oder None, wenn der Aufruf blockiert wurde.
    """

    # ── HARTE SPERRE: API-Key darf NIE gesetzt sein ──────────────────────────
    if os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError(
            "ANTHROPIC_API_KEY ist gesetzt — Abbruch zum Schutz des Budgets.\n"
            "Führe in PowerShell aus:  Remove-Item Env:ANTHROPIC_API_KEY"
        )

    if not config.FORCE_OAUTH_ONLY:
        raise EnvironmentError(
            "FORCE_OAUTH_ONLY ist deaktiviert — Abbruch. "
            "Nur OAuth/Subscription ist erlaubt."
        )

    # ── Alle 8 Gate-Checks ───────────────────────────────────────────────────
    passed, log = api_gate.run_all_checks(signal_context)
    api_gate.print_gate_log(log)
    if not passed:
        print("  🛑 API-Gate NICHT bestanden — kein AI-Aufruf.")
        return None

    # ── Budget final prüfen ──────────────────────────────────────────────────
    budget_ok, reason = budget_guardian.check()
    if not budget_ok:
        print(f"  🛑 {reason} — kein AI-Aufruf.")
        return None

    # ── Erst JETZT importieren (lazy) ────────────────────────────────────────
    model = model or config.ANTHROPIC_MODEL_SONNET
    client = None
    try:
        import anthropic   # bewusst lokal, nie oben im Modul
        # Kein api_key übergeben → SDK nutzt automatisch OAuth/Subscription
        client = anthropic.Anthropic()

        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_message}],
        )

        usage = getattr(resp, "usage", None)
        result = {
            "text":          resp.content[0].text,
            "input_tokens":  getattr(usage, "input_tokens", 0)  if usage else 0,
            "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            "cached_tokens": getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
            "model":         model,
        }

        # Kosten protokollieren
        try:
            import cost_tracker
            cost_tracker.log_call(
                model               = model,
                input_tokens        = result["input_tokens"],
                output_tokens       = result["output_tokens"],
                cached_input_tokens = result["cached_tokens"],
            )
        except Exception:
            pass

        return result

    except EnvironmentError:
        raise
    except Exception as e:
        print(f"  ⚠️  AI-Aufruf fehlgeschlagen: {e}")
        return None
    finally:
        client = None   # Client nach Gebrauch zerstören
