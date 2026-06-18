# SOL Analyzer — Projektkontext für Claude Code

Self-Learning SMC-Trading-Bot für SOL/USDT mit Paper Trader, Web-Dashboards und
automatischem Lern-/Optimierungskreislauf. Python-Backend + eingebettete HTML/JS-
Dashboards. Windows / PowerShell. Python via `.venv`.

## 🤖 Verbesserungs-Backlog — BEI JEDEM START PRÜFEN

**`IMPROVEMENTS.md`** ist ein **automatisch generierter, priorisierter Backlog**
von Verbesserungen/Optimierungen/Bugs des gesamten Tools (erzeugt von
`improvement_scanner.py` aus echten Laufzeit- und Performance-Daten).

**Arbeitsweise für Claude Code:**
1. Zu Beginn relevanter Sessions `IMPROVEMENTS.md` lesen und dem Nutzer die
   offenen **P1/P2**-Punkte kurz nennen + anbieten, sie umzusetzen.
2. Jeder Punkt enthält ein konkretes **„Vorschlag"**-Feld als Umsetzungs-Leitfaden.
3. Nach dem Beheben **`python improvement_scanner.py`** neu laufen lassen —
   objektiv behobene Punkte (Fehler, Lint, verlierende Setups) verschwinden dann
   automatisch aus dem Backlog. Subjektive Punkte ggf. in `improvements.json`
   auf `status: "done"`/`"wontfix"` setzen.
4. Backlog niemals von Hand „leeren" — er ist die Wahrheit aus den Daten.

Der Backlog aktualisiert sich auch selbst: der Server ruft den Scanner nach
jedem Analyse-Lauf auf (`server.py`).

## Wichtige Befehle

```powershell
.\launch.ps1                              # Server + Web-Suite + Bot-API + Paper Trader
.\.venv\Scripts\python.exe improvement_scanner.py   # Backlog manuell aktualisieren
.\.venv\Scripts\python.exe performance_compare.py   # Trade-Performance (vorher/nachher)
.\.venv\Scripts\python.exe -m ruff check .          # Python-Lint (bug-fokussiert)
npm run lint                              # JS/HTML-Lint (ESLint)
```

## Architektur (Kurz)

- **sol_analysis_bot.py** — SMC-Analyse + Chart (`draw_chart`), Layer-1-Prefilter
- **paper_trader.py** — 24/7 Paper-Trading, realistische Ausführung (Slippage,
  Gebühren, Kapitalgrenze, Live-Preis-Überwachung). Indikatoren in `pt_indicators.py`.
- **signal_logger.py** — `signals.db` (SQLite), Outcome-Tracking
- **server.py** — Web-Server (Port 8000), SSE-Live-Updates, Auto-Scheduler
- **Lernkreis** — backtest_learner · learning_engine · threshold_optimizer ·
  strategy_evolver · strategy_builder (Gewichte/Schwellen aus Trade-Outcomes)
- **web_researcher.py / bull_run_detector.py** — Marktkontext
- Dashboards: `index.html`, `trading-suite.html`

## Konventionen & Constraints (WICHTIG)

- **Sicherheit:** Niemals `ANTHROPIC_API_KEY` setzen/committen. Die KI läuft
  ausschließlich über OAuth/Subscription. `config.py`: `FORCE_OAUTH_ONLY = True`,
  Tageslimit $0.08, Monatslimit $1.50. `.env` ist gitignored.
- **Verifizieren** nach Python-Änderungen: betroffene Module importieren + ggf.
  Smoke-Test (`draw_chart`, `get_status`). Linter sind bug-fokussiert konfiguriert
  (`ruff.toml`, `eslint.config.js`) — ein Treffer = echtes Problem.
- **Daten-Dateien** (`*.db`, `state.json`, `trades.*`, Caches) sind gitignored —
  Laufzeitzustand, nicht committen.
