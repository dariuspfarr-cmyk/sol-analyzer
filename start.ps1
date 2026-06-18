# ╔══════════════════════════════════════════════════════════╗
# ║   SOL/USDT Analyzer — Sicherer Start (OAuth/Subscription)  ║
# ╚══════════════════════════════════════════════════════════╝

# 1. API-Key zwingend entfernen (Schutz vor versehentlicher Abrechnung)
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

# 2. Sauberen Zustand bestätigen
if ($env:ANTHROPIC_API_KEY) {
    Write-Host "API Key Status: WARNUNG: GESETZT - BILLING-RISIKO!" -ForegroundColor Red
    Write-Host "Abbruch. Bitte API-Key entfernen und erneut starten." -ForegroundColor Red
    exit 1
} else {
    Write-Host "API Key Status: Sauber - Subscription-Modus aktiv" -ForegroundColor Green
}

# 3. In Skript-Verzeichnis wechseln
Set-Location -Path $PSScriptRoot

# 4. Python-Version prüfen
$pyVersion = python --version 2>&1
Write-Host "Python: $pyVersion" -ForegroundColor Cyan

# 5. Analyzer starten
Write-Host "`nStarte Analyzer...`n" -ForegroundColor Cyan
python sol_analysis_bot.py
