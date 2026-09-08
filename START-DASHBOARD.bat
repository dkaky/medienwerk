@echo off
title medienwerk
cd /d "%~dp0"
echo ============================================
echo  medienwerk startet ...
echo  Der Browser oeffnet sich, sobald das Dashboard bereit ist.
echo  Dieses Fenster OFFEN lassen (Schliessen = Server aus)
echo.
echo  Dashboard und Studio: http://localhost:8031 (auch Port 8030)
echo ============================================
.venv\Scripts\python.exe scripts\start_dashboard.py
if errorlevel 1 pause
