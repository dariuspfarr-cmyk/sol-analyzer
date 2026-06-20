# 🤖 Verbesserungs-Backlog (auto-generiert)

_Zuletzt aktualisiert: 2026-06-20 12:33 UTC · von `improvement_scanner.py`_

> **Für Claude Code:** Dies ist der automatisch erkannte Optimierungs-Backlog des SOL Analyzers. Arbeite die offenen Punkte nach Priorität ab (P1 zuerst). Jeder Punkt hat einen konkreten **Vorschlag**. Nach dem Beheben den Scanner erneut laufen lassen (`python improvement_scanner.py`) — erledigte Punkte verschwinden dann automatisch.

**Offen:** 2  ·  🔴 1  ·  🟠 1  ·  🟡 0  ·  🟢 0

## 🔴 P1 — Kritisch  (1)

### [trading] Win-Rate sinkt (72% → 61%)
- **Problem:** Mittlere Win-Rate fiel von 72.2% auf zuletzt 60.9% (aktuell 58.9%).
- **Vorschlag:** WIN-RATE IST DAS HAUPTZIEL. Regime-Wechsel prüfen (Reversal- vs Trend-Setups), Selektivität erhöhen, verlierende Setups/Regime härter filtern. Ggf. backtester.py nutzen, um optimale Schwellen zu finden.
- **Quelle:** _strategy_evolution.json_  ·  ID `ccb79dfaa6`  ·  erstmals 2026-06-19  ·  19× gesehen

## 🟠 P2 — Hoch  (1)

### [trading] Win-Rate unter Ziel (59% < 60%)
- **Problem:** Aktuelle Win-Rate 58.9% liegt unter dem Zielwert 60%.
- **Vorschlag:** Selektiver werden: nur A+-Setups zulassen (Trend-Folge im Trend, Confluence ≥2 Trigger, hohe Konfidenz). Schwächste Setups/TFs/Bias im aktuellen Regime drosseln.
- **Quelle:** _strategy_evolution.json_  ·  ID `87a45b8065`  ·  erstmals 2026-06-20  ·  2× gesehen

---
_Status manuell setzen: in `improvements.json` `status` auf `done`/`wontfix` ändern. Behobene objektive Funde (Fehler, Lint, verlierende Setups) verschwinden beim nächsten Scan von selbst._
