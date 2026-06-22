# 🤖 Verbesserungs-Backlog (auto-generiert)

_Zuletzt aktualisiert: 2026-06-22 11:13 UTC · von `improvement_scanner.py`_

> **Für Claude Code:** Dies ist der automatisch erkannte Optimierungs-Backlog des SOL Analyzers. Arbeite die offenen Punkte nach Priorität ab (P1 zuerst). Jeder Punkt hat einen konkreten **Vorschlag**. Nach dem Beheben den Scanner erneut laufen lassen (`python improvement_scanner.py`) — erledigte Punkte verschwinden dann automatisch.

**Offen:** 3  ·  🔴 1  ·  🟠 2  ·  🟡 0  ·  🟢 0

## 🔴 P1 — Kritisch  (1)

### [trading] Win-Rate sinkt (68% → 59%)
- **Problem:** Mittlere Win-Rate fiel von 68.4% auf zuletzt 58.7% (aktuell 58.7%).
- **Vorschlag:** WIN-RATE IST DAS HAUPTZIEL. Regime-Wechsel prüfen (Reversal- vs Trend-Setups), Selektivität erhöhen, verlierende Setups/Regime härter filtern. Ggf. backtester.py nutzen, um optimale Schwellen zu finden.
- **Quelle:** _strategy_evolution.json_  ·  ID `ccb79dfaa6`  ·  erstmals 2026-06-19  ·  26× gesehen

## 🟠 P2 — Hoch  (2)

### [trading] Hohe EXPIRED-Rate (98/636 = 15%)
- **Problem:** 98 von 636 Signalen liefen ab ohne TP/SL zu treffen.
- **Vorschlag:** Hold-Zeiten (tf_profiles.max_hold_hours) oder Entry-Timing prüfen — viele EXPIRED bedeuten, dass Ziele zu ambitioniert oder Einstiege zu früh sind.
- **Quelle:** _performance_report.json_  ·  ID `17ba66e1b0`  ·  erstmals 2026-06-21  ·  4× gesehen

### [trading] Win-Rate unter Ziel (59% < 60%)
- **Problem:** Aktuelle Win-Rate 58.7% liegt unter dem Zielwert 60%.
- **Vorschlag:** Selektiver werden: nur A+-Setups zulassen (Trend-Folge im Trend, Confluence ≥2 Trigger, hohe Konfidenz). Schwächste Setups/TFs/Bias im aktuellen Regime drosseln.
- **Quelle:** _strategy_evolution.json_  ·  ID `87a45b8065`  ·  erstmals 2026-06-20  ·  9× gesehen

---
_Status manuell setzen: in `improvements.json` `status` auf `done`/`wontfix` ändern. Behobene objektive Funde (Fehler, Lint, verlierende Setups) verschwinden beim nächsten Scan von selbst._
