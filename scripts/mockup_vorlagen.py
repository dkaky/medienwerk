"""Vorlagen bei Dynamic Mockups auflisten - zum Aussuchen der Produktfotos.

Aufruf (aus dem Projektordner):
    .venv\\Scripts\\python.exe -m scripts.mockup_vorlagen            # alle
    .venv\\Scripts\\python.exe -m scripts.mockup_vorlagen hoodie     # Namensfilter

Zeigt je Vorlage die UUID, den Namen, ein Vorschaubild und die Smart Objects mit
ihren UUIDs. Aus diesen Angaben entsteht ``data/mockup_vorlagen.json`` (Aufbau in
``app/studio/mockup_plan.py``). Liest nur, kostet keine Credits.
"""
from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.integrations.dynamic_mockups import DynamicMockupsClient, MockupFehler


async def main(argv: list[str]) -> int:
    name = argv[0] if argv else None
    try:
        client = DynamicMockupsClient(get_settings().dynamic_mockups_api_key)
    except MockupFehler as exc:
        print(f"FEHLER: {exc}")
        return 1
    try:
        vorlagen = await client.vorlagen(name=name)
    except MockupFehler as exc:
        print(f"FEHLER: {exc}")
        return 2
    finally:
        await client.aclose()

    print(f"{len(vorlagen)} Vorlagen" + (f" mit '{name}'" if name else ""))
    for v in vorlagen:
        print(f"\n{v.get('name')}\n  mockup_uuid: {v.get('uuid')}")
        if v.get("thumbnail"):
            print(f"  Vorschau:    {v.get('thumbnail')}")
        for so in v.get("smart_objects") or []:
            print(f"  Ebene '{so.get('name')}': {so.get('uuid')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
