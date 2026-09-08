#!/bin/bash
# Doppelklick: den Belege-Helfer beim Anmelden automatisch starten (macOS).
# Nochmal doppelklicken mit "aus" als Argument entfernt ihn wieder:
#   ./AUTOSTART-MAC.command aus
set -u
PROJEKT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="de.forsetimarketing.belege-helfer"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-ein}" = "aus" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  rm -f "$PLIST"
  echo "Autostart entfernt. Der Helfer laeuft nach dem naechsten Neustart nicht mehr."
  read -r -p "Enter zum Schliessen"; exit 0
fi

if [ ! -x "$PROJEKT/.venv/bin/python" ]; then
  echo "Keine .venv gefunden unter $PROJEKT"
  echo "Einmalig einrichten:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  read -r -p "Enter zum Schliessen"; exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJEKT/logs"
cat > "$PLIST" <<PLISTENDE
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJEKT/.venv/bin/python</string>
    <string>$PROJEKT/scripts/belege_backfill.py</string>
    <string>--dienst</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJEKT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- Nicht sofort neu starten, wenn er scheitert (z.B. Chrome laesst sich nicht
       oeffnen) - sonst dreht sich das im Sekundentakt. -->
  <key>ThrottleInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$PROJEKT/logs/belege-helfer.log</string>
  <key>StandardErrorPath</key><string>$PROJEKT/logs/belege-helfer.log</string>
</dict>
</plist>
PLISTENDE

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
  echo "Autostart eingerichtet."
else
  launchctl load "$PLIST" 2>/dev/null && echo "Autostart eingerichtet (aelteres macOS)." \
    || { echo "Konnte nicht gestartet werden. Datei liegt unter: $PLIST"; \
         read -r -p "Enter zum Schliessen"; exit 1; }
fi

echo
echo "Der Helfer laeuft ab jetzt bei jeder Anmeldung mit."
echo "Er oeffnet dabei ein eigenes Chrome-Fenster - das gehoert dazu und darf"
echo "minimiert werden. NICHT schliessen, sonst hoert der Knopf nichts mehr."
echo
echo "Protokoll: $PROJEKT/logs/belege-helfer.log"
echo "Wieder abschalten:  ./scripts/AUTOSTART-MAC.command aus"
read -r -p "Enter zum Schliessen"
