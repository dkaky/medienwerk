@echo off
REM Doppelklick: den Belege-Helfer beim Anmelden automatisch starten (Windows).
REM Wieder entfernen:  AUTOSTART-WINDOWS.bat aus
setlocal
set "PROJEKT=%~dp0.."
set "AUFGABE=POD Shop Belege-Helfer"

if /i "%~1"=="aus" (
  schtasks /delete /tn "%AUFGABE%" /f
  echo Autostart entfernt.
  pause
  exit /b 0
)

if not exist "%PROJEKT%\.venv\Scripts\python.exe" (
  echo Keine .venv gefunden unter %PROJEKT%
  echo Einmalig einrichten:
  echo   python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

REM /rl limited = normale Benutzerrechte. Der Helfer braucht nicht mehr.
schtasks /create /tn "%AUFGABE%" /sc onlogon /rl limited /f ^
  /tr "\"%PROJEKT%\.venv\Scripts\pythonw.exe\" \"%PROJEKT%\scripts\belege_backfill.py\" --dienst"
if errorlevel 1 (
  echo Konnte die Aufgabe nicht anlegen.
  pause
  exit /b 1
)

echo.
echo Der Helfer laeuft ab jetzt bei jeder Anmeldung mit.
echo Er oeffnet dabei ein eigenes Chrome-Fenster - das gehoert dazu und darf
echo minimiert werden. NICHT schliessen, sonst hoert der Knopf nichts mehr.
echo.
echo Jetzt sofort starten? Dann einfach BELEGE-DIENST.bat doppelklicken.
echo Wieder abschalten:  AUTOSTART-WINDOWS.bat aus
pause
