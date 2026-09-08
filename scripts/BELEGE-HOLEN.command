#!/bin/bash
# Doppelklick: holt fehlende AliExpress-Belege in das Programm (macOS).
# Beim ersten Mal oeffnet sich Chrome und du meldest dich einmal an -- danach
# merkt sich das eigene Profil die Anmeldung.
cd "$(dirname "$0")/.." || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Keine .venv gefunden. Einmalig einrichten:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  echo "  .venv/bin/playwright install chromium"
  read -r -p "Enter zum Schliessen"; exit 1
fi
.venv/bin/python scripts/belege_backfill.py "$@"
echo
read -r -p "Fertig. Enter zum Schliessen."
