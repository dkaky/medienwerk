@echo off
REM Doppelklick: holt fehlende AliExpress-Belege in das Programm (Windows).
REM Beim ersten Mal oeffnet sich Chrome und du meldest dich einmal an -- danach
REM merkt sich das eigene Profil die Anmeldung.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Keine .venv gefunden. Einmalig einrichten:
  echo   python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  echo   .venv\Scripts\playwright install chromium
  pause
  exit /b 1
)
.venv\Scripts\python.exe scripts\belege_backfill.py %*
echo.
pause
