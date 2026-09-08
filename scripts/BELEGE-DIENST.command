#!/bin/bash
# Doppelklick: Helfer starten. Wartet auf den Knopf "Belege abrufen" in der
# Belegablage. Startet von sich aus NICHTS.
# Fenster offen lassen -- schliessen beendet den Helfer.
cd "$(dirname "$0")/.." || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Keine .venv gefunden. Einmalig einrichten:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  read -r -p "Enter zum Schliessen"; exit 1
fi
.venv/bin/python scripts/belege_backfill.py --dienst
