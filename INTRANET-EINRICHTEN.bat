@echo off
title Intranet einrichten
cd /d "%~dp0"
echo ============================================
echo  Intranet auf einem Server einrichten
echo  Du brauchst: die IP-Adresse und das Root-Passwort deines Servers.
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\intranet\einrichten.ps1" %*
echo.
pause
