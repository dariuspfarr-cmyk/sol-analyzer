# ============================================================
#  setup-tools.ps1  —  installiert git + Node.js (LTS) via winget
#  Rechtsklick → "Mit PowerShell ausführen"  (UAC mit "Ja" bestätigen)
# ============================================================

Write-Host "`n=== SOL Analyzer — Tool-Setup ===`n" -ForegroundColor Cyan

function Test-Tool($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# ── Git ──────────────────────────────────────────────────────
if (Test-Tool git) {
    Write-Host "git ist bereits installiert: $(git --version)" -ForegroundColor Green
} else {
    Write-Host "Installiere Git ..." -ForegroundColor Yellow
    winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
}

# ── Node.js LTS ──────────────────────────────────────────────
if (Test-Tool node) {
    Write-Host "node ist bereits installiert: $(node --version)" -ForegroundColor Green
} else {
    Write-Host "Installiere Node.js (LTS) ..." -ForegroundColor Yellow
    winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
}

Write-Host "`n=== Fertig ===" -ForegroundColor Cyan
Write-Host "WICHTIG: VS Code jetzt komplett schliessen und neu oeffnen," -ForegroundColor Yellow
Write-Host "damit git und node im PATH verfuegbar sind.`n" -ForegroundColor Yellow
Read-Host "Mit Enter beenden"
