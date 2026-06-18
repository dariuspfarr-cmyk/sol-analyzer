@echo off
REM ============================================================
REM   SOL Analyzer - Doppelklick-Start
REM   Startet Server + Web-Suite + Bot-API (ueber launch.ps1)
REM ============================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1"
echo.
echo Server beendet. Fenster kann geschlossen werden.
pause
