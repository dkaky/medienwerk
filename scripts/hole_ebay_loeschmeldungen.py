"""eBay-Loeschmeldungen von der Supabase-Funktion abholen und verarbeiten - von Hand.

Aufruf (aus dem Projektordner):
    .venv\\Scripts\\python.exe -m scripts.hole_ebay_loeschmeldungen

Laeuft auch automatisch mit, wenn BACKGROUND_JOBS_ENABLED=true gesetzt ist.
Siehe app/services/ebay_loeschmeldungen.py.
"""
from __future__ import annotations

import asyncio

from app.database import SessionLocal, init_db
from app.services.ebay_loeschmeldungen import AbholFehler, hole_und_verarbeite


async def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        zaehler = await hole_und_verarbeite(db)
    except AbholFehler as exc:
        print(f"FEHLER: {exc}")
        return 1
    finally:
        db.close()
    print("eBay-Loeschmeldungen:")
    for name, wert in zaehler.items():
        print(f"  {name:<16} {wert}")
    if zaehler["zurueckgestellt"]:
        print("  (zurueckgestellte Meldungen werden beim naechsten Lauf erneut geprueft)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
