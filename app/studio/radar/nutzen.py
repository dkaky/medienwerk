"""Der eine Klick: aus einem Vorschlag wird ein Motiv.

Bis hierher kostet nichts ein Bild - Trend-Vorschlaege und Shop-Ideen sind Text.
Erst dieser Aufruf erzeugt, und er laeuft ueber DENSELBEN Weg wie ein von Hand
geschriebenes Motiv (``generation/service.erzeuge``): Rechtefilter, Motivart,
Kostenbremse. Ein Vorschlag bekommt keine Abkuerzung.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.studio.models import MotivIdee


def speichere_bild(bild: Any, bildordner: Path, titel: str) -> Path:
    ordner = Path(bildordner)
    ordner.mkdir(parents=True, exist_ok=True)
    sauber = re.sub(r"[^a-z0-9]+", "-", (titel or "").lower()).strip("-")[:50] or "motiv"
    ziel = ordner / f"{datetime.now():%Y%m%d-%H%M%S}-{sauber}.png"
    bild.save(ziel, "PNG")
    return ziel


def erzeuge_aus_idee(db, idee: MotivIdee, *, anbieter: str, bildordner: Path,
                     breite: int = 1024, hoehe: int = 1024) -> dict[str, Any]:
    """Prompt nehmen (oder entwerfen), Bild erzeugen, Motiv anlegen, Idee als uebernommen markieren."""
    from app.studio import service
    from app.studio.generation import service as gen
    from app.studio.radar import umwandlung

    prompt = idee.eigener_prompt or umwandlung.entwirf_und_merke(db, idee)
    ergebnis = gen.erzeuge(db, prompt=prompt, breite=breite, hoehe=hoehe, anbieter=anbieter)
    pfad = speichere_bild(ergebnis.bild.image, bildordner, idee.thema or "motiv")
    design = service.create_design(db, title=(idee.thema or prompt)[:80],
                                   source=ergebnis.bild.provider,
                                   image_url=f"/studio/bilder/{pfad.name}")
    idee.design_id = design.id
    idee.status = "uebernommen"
    db.commit()
    return {"design": design, "prompt": prompt, "kosten_usd": ergebnis.kosten_usd,
            "rest_budget_usd": ergebnis.rest_budget_usd, "anbieter": ergebnis.bild.provider}
