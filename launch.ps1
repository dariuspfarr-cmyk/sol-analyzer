# SOL Analyzer - Komplett-Start (Web-Suite + Bot-API)
# Startet den lokalen Server und oeffnet die Suite im Browser.

Set-Location -Path $PSScriptRoot

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  SOL Analyzer - Komplett-Start" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. API-Key entfernen (Abrechnungsschutz)
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
if ($env:ANTHROPIC_API_KEY) {
    Write-Host "[X] API-Key gesetzt - BILLING-RISIKO! Abbruch." -ForegroundColor Red
    Read-Host "Enter zum Schliessen"; exit 1
}
Write-Host "[OK] API-Key sauber" -ForegroundColor Green

# 2. UTF-8 erzwingen + Browser-Auto-Start aktivieren
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:OPEN_BROWSER = "1"

# 3. Python finden (venv bevorzugt)
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $python = $venvPy
    Write-Host "[OK] Python: venv" -ForegroundColor Green
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
    Write-Host "[!] Python: System" -ForegroundColor Yellow
} else {
    Write-Host "[X] Kein Python gefunden - bitte von python.org installieren." -ForegroundColor Red
    Read-Host "Enter zum Schliessen"; exit 1
}

if (-not (Test-Path (Join-Path $PSScriptRoot "server.py"))) {
    Write-Host "[X] server.py fehlt." -ForegroundColor Red
    Read-Host "Enter zum Schliessen"; exit 1
}

$port = if ($env:PORT) { $env:PORT } else { "8000" }

# 4. Alten Server auf diesem Port beenden (Zombie-Schutz)
$stale = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($stale) {
    Write-Host "[..] Port $port belegt - beende alten Server..." -ForegroundColor Yellow
    $stale | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
        try { Stop-Process -Id $_ -Force -ErrorAction Stop } catch {}
    }
    Start-Sleep -Seconds 1
}

# 5. LAN-IP ermitteln (reines PowerShell, kein zweiter Python-Start)
$lan = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -match '^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\.' } |
        Select-Object -First 1 -ExpandProperty IPAddress)
if (-not $lan) { $lan = "<deine-IP>" }

Write-Host ""
Write-Host "------------------------------------------------------------" -ForegroundColor Cyan
Write-Host "  Browser oeffnet sich gleich automatisch." -ForegroundColor White
Write-Host "  Dieser PC:  http://localhost:$port/index.html" -ForegroundColor White
Write-Host "  iPhone/andere (gleiches WLAN):" -ForegroundColor White
Write-Host "              http://${lan}:$port/index.html" -ForegroundColor White
Write-Host "------------------------------------------------------------" -ForegroundColor Cyan
Write-Host "  WICHTIG: Fenster offen lassen! Schliessen = Server aus." -ForegroundColor Yellow
Write-Host "  Beenden mit Strg + C" -ForegroundColor DarkGray
Write-Host ""

# 7. Server im Vordergrund (oeffnet selbst den Browser, blockiert bis Strg+C)
& $python server.py

Write-Host "Server beendet." -ForegroundColor Yellow
Read-Host "Enter zum Schliessen"
