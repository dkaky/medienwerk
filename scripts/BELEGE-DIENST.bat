@echo off
REM Doppelklick: Helfer starten. Wartet auf den Knopf "Belege abrufen" in der
REM Belegablage. Startet von sich aus NICHTS.
REM Fenster offen lassen -- schliessen beendet den Helfer.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Keine .venv gefunden. Einmalig einrichten:
  echo   python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
.venv\Scripts\python.exe scripts\belege_backfill.py --dienst
