# SOL/USDT Analyzer 🤖📊

Selbstlernender SMC-Analyzer für SOL/USDT (Blofin SOLUSDTPERP).
Binance-Daten → SMC-Analyse → Terminal-Ausgabe. Mit Backtesting, Algo-Only-Modus und KI-Schutz.

---

## ⚠️ Wichtig: Nur OAuth/Subscription — keine API-Abrechnung

Dieses Projekt nutzt **ausschließlich** dein Claude Pro/Max-Abo über OAuth.
Es wird **niemals** ein `ANTHROPIC_API_KEY` verwendet.

- `start.ps1` entfernt den API-Key vor jedem Start automatisch.
- `lazy_api.py` bricht hart ab, falls ein API-Key gesetzt ist.
- `budget_guardian.py` setzt Tages- (\$0.08) und Monatslimits (\$1.50) durch.

---

## Installation (Windows / PowerShell)

1. **Python installieren** von https://python.org
   → Beim Setup unbedingt **"Add Python to PATH"** anhaken.

2. **PowerShell** öffnen und in den Projektordner wechseln:
   ```powershell
   cd "C:\Users\DARIUS OG\sol-analyzer"
   ```

3. **Abhängigkeiten installieren:**
   ```powershell
   pip install -r requirements.txt
   ```

4. **`.env` Datei erstellen** (optional, Vorlage kopieren):
   ```powershell
   Copy-Item .env.example .env
   ```
   Standardwerte (SYMBOL, INTERVAL, CANDLES) funktionieren ohne Anpassung.
   Die Analyse-Ausgabe erfolgt direkt im Terminal; Charts werden im Ordner `charts/` gespeichert.

5. **Starten:**
   ```powershell
   .\start.ps1
   ```

---

## 🚀 Komplett-Start (alles zusammen)

Ein einziger Befehl startet **Web-Suite + Bot-API + Live-Daten** gemeinsam:

```powershell
.\launch.ps1
```

Das startet [server.py](server.py) (lokaler Server auf `http://localhost:8000`) und öffnet
automatisch die Komplett-Suite ([index.html](index.html)) im Browser. Dort siehst du:

- **Live-Statistik** aus `signals.db` (Win-Rate, Signale, Modell-Status, Kosten)
- die **letzten Signale** des Bots als Tabelle
- einen **„Jetzt analysieren"-Button**, der den Bot direkt aus dem Browser ausführt
- alle Web-Tools (Trading Suite, Backtester, Journal) über denselben Server

| Start | Was passiert |
|---|---|
| **Doppelklick auf `Start.bat`** | **Alles** ohne Befehl tippen (empfohlen) |
| `.\launch.ps1` | Dasselbe per PowerShell |
| `.\start.ps1`  | Nur ein einzelner Bot-Durchlauf im Terminal |

### Ohne Befehl starten (Doppelklick)

Einfach **`Start.bat` doppelklicken** — das öffnet Server + Web-Suite automatisch,
kein Tippen nötig. Tipp: Rechtsklick auf `Start.bat` → „Senden an → Desktop
(Verknüpfung erstellen)", dann hast du ein Start-Icon auf dem Desktop.

### Von anderen Geräten starten (Handy, Tablet, zweiter PC)

Der Server ist standardmäßig im **lokalen Netzwerk** erreichbar. Sobald `Start.bat`
auf dem Haupt-PC läuft:

1. Im Server-Fenster steht die Adresse, z. B. `http://192.168.178.172:8000/index.html`
2. Diese Adresse auf dem anderen Gerät (gleiches WLAN) im Browser öffnen — fertig.

- **Firewall:** Beim ersten Start fragt Windows „Zugriff zulassen?" → **Ja / Erlauben**.
- **Nur dieser PC (sicherer):** Server mit `$env:HOST="127.0.0.1"` vor dem Start sperren.
- **Eigenen Port:** `$env:PORT="9000"` vor dem Start setzen.

> Hinweis: Jeder im selben Netzwerk kann dann die Oberfläche öffnen und den
> „Jetzt analysieren"-Button nutzen. Im Heim-WLAN unbedenklich; in offenen
> Netzwerken besser `HOST=127.0.0.1` verwenden.

### Auf einem komplett anderen Computer einrichten

1. Den gesamten Ordner kopieren (USB-Stick, Cloud, `git`).
2. Python installieren (python.org · „Add to PATH").
3. Einmalig: `pip install -r requirements.txt`
4. `Start.bat` doppelklicken.

### API-Endpunkte (für eigene Integrationen)

| Endpoint | Liefert |
|---|---|
| `GET /api/stats` | Aggregierte Statistik (Signale, Modell, Kosten) |
| `GET /api/signals?limit=N` | Die letzten N Signale aus `signals.db` |
| `GET /api/charts` | Liste gespeicherter Chart-PNGs |
| `POST /api/run` | Startet einen Bot-Durchlauf im Hintergrund |
| `GET /api/run/status` | Status + Live-Log des laufenden Durchlaufs |

---

## Sicherheits-Check vor dem ersten Start

```powershell
# 1. Prüfen ob versehentlich ein API-Key gesetzt ist
echo $env:ANTHROPIC_API_KEY

# 2. Falls etwas erscheint → sofort entfernen
Remove-Item Env:ANTHROPIC_API_KEY

# 3. Dauerhaft im PowerShell-Profil verankern
Add-Content $PROFILE "`nRemove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue"
```

Innerhalb von Claude Code prüfen: `/status` muss **OAuth / Subscription** zeigen — nicht API-Key.

---

## Module im Überblick

| Datei | Zweck | Kosten |
|---|---|---|
| `server.py` | Lokaler Server: Web-Suite + JSON-API + Bot-Start | **\$0** |
| `sol_analysis_bot.py` | Haupt-Bot (Binance → SMC → Terminal) | gering |
| `smart_router.py` | Entscheidet Algo-Only vs. KI | — |
| `algo_signal_engine.py` | Reine Algo-Signale (kein API) | **\$0** |
| `backtester.py` | Historischer Backtest (1000 Kerzen) | **\$0** |
| `backtest_learner.py` | Muster-Gewichte → `backtest_weights.json` | **\$0** |
| `signal_logger.py` | SQLite-Datenbank `signals.db` | **\$0** |
| `performance_analyzer.py` | Wöchentliche Auswertung (sonntags) | **\$0** |
| `threshold_optimizer.py` | Auto-Anpassung der Schwellen | **\$0** |
| `local_filter_model.py` | XGBoost-Filter (ersetzt Haiku) | **\$0** |
| `lazy_api.py` | Sicherer KI-Aufruf (OAuth-only) | Abo |
| `api_gate.py` | 8 Schutz-Checks vor jedem AI-Call | — |
| `budget_guardian.py` | Tages-/Monatslimit-Wächter | — |
| `cost_tracker.py` | Kosten-Protokoll `api_costs.jsonl` | — |
| `config.py` | Thresholds mit MIN/MAX | — |
| `learning_dashboard.py` | Terminal-Statistik auf Abruf | — |

---

## Lern-Dashboard anzeigen

```powershell
python learning_dashboard.py
```

Zeigt Win-Rate, Modell-Status, Einsparungen und aktuelle Thresholds.

---

## Wie das Lernen funktioniert

```
Backtest (historisch)  →  backtest_weights.json  →  algo_signal_engine
        ↓
Live-Chart  →  Layer-1-Filter  →  smart_router  →  KI-Pipeline (nur wenn nötig)
        ↓
signals.db  ←  Outcome-Tracker (WIN/LOSS/EXPIRED)
        ↓
threshold_optimizer (jeden Sonntag)  →  Gewichte aktualisieren
        ↓
algo_signal_engine wird klüger
```

- Ab **200 Signalen** trainiert sich ein lokales XGBoost-Modell und ersetzt Haiku (ab 60 % Genauigkeit).
- Je mehr Live-Daten, desto höher das Live-Gewicht (70 % → 90 %).
- Der **Algo-Only-Modus funktioniert auch ohne KI** — rein algorithmisch, $0.
