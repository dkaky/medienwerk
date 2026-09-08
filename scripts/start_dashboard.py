"""Start one local application on both previously used dashboard addresses."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
URL = "http://localhost:8031"


def existing_dashboard() -> bool:
    try:
        with urllib.request.urlopen(URL + "/api", timeout=2) as response:
            info = json.load(response)
        if info.get("name") != "medienwerk" or info.get("dashboard") != "/":
            raise RuntimeError("Port 8031 ist durch eine andere oder alte Anwendung belegt.")
        return True
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Port 8031 ist bereits belegt; keine zweite Instanz gestartet.") from exc
    except urllib.error.URLError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    if existing_dashboard():
        print("Dashboard laeuft bereits: " + URL)
        if not args.no_browser:
            webbrowser.open(URL)
        return

    import uvicorn

    sockets = []
    stopped = threading.Event()
    try:
        # One process and database for both bookmarks, never two stale apps.
        for port in (8031, 8030):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(listener)
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            listener.setblocking(False)
        server = uvicorn.Server(uvicorn.Config("app.main:app", log_level="warning"))

        def open_when_ready() -> None:
            while not stopped.wait(0.2):
                if server.started:
                    webbrowser.open(URL)
                    return

        if not args.no_browser:
            threading.Thread(target=open_when_ready, daemon=True).start()
        print("medienwerk: " + URL + " (auch Port 8030); Fenster offen lassen.", flush=True)
        server.run(sockets=sockets)
    finally:
        stopped.set()
        for listener in sockets:
            listener.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print("Dashboard konnte nicht starten: " + str(exc), file=sys.stderr)
        sys.exit(1)
